"""Extraction des factures depuis un PDF, par analyse de la mise en page.

Le gabarit des factures place le bloc client à une position fixe : colonne de
droite, juste sous la ligne de date. On s'appuie donc sur les coordonnées des
lignes de texte plutôt que sur des expressions régulières appliquées au texte
brut, ce qui est nettement plus fiable quand un nom de client ressemble à une
ligne d'adresse.

Repères mesurés sur le gabarit (page A4 595 x 842 pt) :

===================  ========  ==========================================
Élément              x0        y0
===================  ========  ==========================================
« Facture N° : »      ~24       ~123
Numéro de facture    ~108      ~123  (même ligne que le libellé)
« Tanger le, JJ/MM » ~443      ~123
ICE client            ~24      ~145
Nom du client        ~312      ~151   <- première ligne du bloc de droite
(suite du nom)       ~312      ~168   (interligne ~16.6 pt)
Adresse              ~312      ~185   (saut de ligne ~33.1 pt)
===================  ========  ==========================================

La distinction « suite du nom » / « adresse » repose sur l'écart vertical :
16.6 pt pour un simple retour à la ligne, 33.1 pt lorsqu'une ligne vide sépare
le nom de l'adresse. Le seuil est fixé à mi-chemin (`NAME_CONTINUATION_MAX_GAP`).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import pairwise

import pymupdf

from factures.models import ExtractionReport, Invoice, PageError

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Profil de mise en page
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LayoutProfile:
    """Constantes géométriques du gabarit de facture, exprimées en points PDF."""

    client_column_min_x: float = 295.0
    client_column_max_x: float = 345.0
    """Fenêtre horizontale du bloc destinataire (nom + adresse)."""

    name_continuation_max_gap: float = 25.0
    """Écart vertical max entre deux lignes appartenant au même nom de client."""

    header_max_y: float = 320.0
    """Limite basse de l'en-tête : au-delà on est dans le tableau de prestations."""

    amount_column_min_x: float = 470.0
    """Les montants sont alignés à droite, bien après le libellé du total."""

    date_column_min_x: float = 380.0
    """La date d'émission est en haut à droite."""


DEFAULT_LAYOUT = LayoutProfile()

# --------------------------------------------------------------------------- #
# Motifs
# --------------------------------------------------------------------------- #

_INVOICE_LABEL_RE = re.compile(r"facture\s*n[°ºo]?", re.IGNORECASE)
_INVOICE_NUMBER_RE = re.compile(r"^(\d{1,7})\s*[-/]\s*(\d{4})$")
_INVOICE_NUMBER_INLINE_RE = re.compile(
    r"facture\s*n[°ºo]?\s*:?\s*(\d{1,7}\s*[-/]\s*\d{4})", re.IGNORECASE
)
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_ICE_RE = re.compile(r"\bICE\s*:?\s*([0-9]{9,20})\b", re.IGNORECASE)
# `\s` couvre deja les espaces insecables U+00A0 et U+202F employes comme
# separateurs de milliers dans le gabarit.
_AMOUNT_RE = re.compile(r"^-?[\d\s.]*\d(?:[.,]\d{1,2})?$")

_TOTAL_HT_LABELS = ("total ht", "montant ht", "total h.t")
_TOTAL_TVA_LABELS = ("total tva", "montant tva", "t.v.a")
_TOTAL_TTC_LABELS = ("total à payer", "total a payer", "total ttc", "net à payer")

# ICE du cabinet émetteur, présent dans le pied de page de chaque facture :
# il ne doit jamais être confondu avec l'ICE du client.
_ISSUER_ICE = "000166897000013"


# --------------------------------------------------------------------------- #
# Primitives de lecture
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TextLine:
    """Une ligne de texte avec sa boîte englobante."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def y_mid(self) -> float:
        return (self.y0 + self.y1) / 2.0

    def contains_y(self, y: float) -> bool:
        return self.y0 <= y <= self.y1


def _read_lines(page: pymupdf.Page) -> list[TextLine]:
    """Retourne les lignes de texte non vides d'une page, triées haut -> bas."""
    lines: list[TextLine] = []
    for block in page.get_text("dict").get("blocks", ()):
        if block.get("type") != 0:  # 0 = bloc texte, 1 = image
            continue
        for line in block.get("lines", ()):
            text = "".join(span.get("text", "") for span in line.get("spans", ())).strip()
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            lines.append(TextLine(x0=x0, y0=y0, x1=x1, y1=y1, text=text))
    lines.sort(key=lambda item: (round(item.y0, 1), item.x0))
    return lines


def _parse_french_amount(raw: str) -> Decimal | None:
    """Convertit ``1 500,00`` / ``1 500.00`` en `Decimal`. `None` si illisible."""
    cleaned = raw.replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_french_date(raw: str) -> date | None:
    match = _DATE_RE.search(raw)
    if match is None:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Extraction champ par champ
# --------------------------------------------------------------------------- #


def _find_client_name(
    lines: list[TextLine], layout: LayoutProfile
) -> tuple[str | None, list[str]]:
    """Nom du client : bloc de droite, première ligne sous la date.

    Renvoie ``(nom, avertissements)``. Le nom peut tenir sur plusieurs lignes
    consécutives ; l'adresse, séparée par une ligne vide, est exclue.
    """
    warnings: list[str] = []
    block = [
        line
        for line in lines
        if layout.client_column_min_x < line.x0 < layout.client_column_max_x
    ]
    if not block:
        return None, warnings

    block.sort(key=lambda line: line.y0)

    # On avance tant que les lignes se suivent à l'interligne simple : dès qu'un
    # saut plus large apparaît, on est passé du nom à l'adresse.
    name_lines = [block[0]]
    for previous, current in pairwise(block):
        if current.y0 - previous.y0 > layout.name_continuation_max_gap:
            break
        name_lines.append(current)

    name = " ".join(line.text for line in name_lines)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return None, warnings
    if len(name_lines) > 3:
        warnings.append(
            f"Nom du client réparti sur {len(name_lines)} lignes, à vérifier."
        )
    return name, warnings


def _find_invoice_number(lines: list[TextLine], layout: LayoutProfile) -> str | None:
    """Numéro de facture, lu sur la ligne du libellé « Facture N° »."""
    labels = [
        line
        for line in lines
        if line.y0 < layout.header_max_y and _INVOICE_LABEL_RE.search(line.text)
    ]
    for label in labels:
        # Cas 1 : libellé et numéro sont sur la même ligne visuelle mais dans
        # deux fragments distincts.
        for candidate in lines:
            if candidate is label or candidate.x0 <= label.x0:
                continue
            if not candidate.contains_y(label.y_mid):
                continue
            match = _INVOICE_NUMBER_RE.match(candidate.text.strip())
            if match:
                return f"{match.group(1)}-{match.group(2)}"
        # Cas 2 : libellé et numéro dans le même fragment.
        inline = _INVOICE_NUMBER_INLINE_RE.search(label.text)
        if inline:
            return re.sub(r"\s*[-/]\s*", "-", inline.group(1).strip())

    # Cas 3 : aucun libellé exploitable, on cherche un motif « NNNN - AAAA ».
    for line in lines:
        if line.y0 >= layout.header_max_y:
            continue
        match = _INVOICE_NUMBER_RE.match(line.text.strip())
        if match:
            return f"{match.group(1)}-{match.group(2)}"
    return None


def _find_invoice_date(lines: list[TextLine], layout: LayoutProfile) -> date | None:
    """Date d'émission : en haut à droite, sinon première date de l'en-tête."""
    header = [line for line in lines if line.y0 < layout.header_max_y]
    right = [line for line in header if line.x0 >= layout.date_column_min_x]
    for group in (right, header):
        for line in sorted(group, key=lambda item: item.y0):
            parsed = _parse_french_date(line.text)
            if parsed is not None:
                return parsed
    return None


def _find_client_ice(lines: list[TextLine], layout: LayoutProfile) -> str | None:
    """ICE du client : dans l'en-tête, à gauche. Exclut l'ICE du cabinet."""
    for line in sorted(lines, key=lambda item: item.y0):
        if line.y0 >= layout.header_max_y:
            continue
        match = _ICE_RE.search(line.text)
        if match is None:
            continue
        value = match.group(1)
        if value == _ISSUER_ICE:
            continue
        return value
    return None


def _find_amount(
    lines: list[TextLine], labels: tuple[str, ...], layout: LayoutProfile
) -> Decimal | None:
    """Montant aligné à droite sur la même ligne visuelle qu'un des libellés."""
    for line in lines:
        normalized = line.text.strip().lower()
        if not any(normalized.startswith(label) for label in labels):
            continue
        candidates = [
            other
            for other in lines
            if other is not line
            and other.x0 >= layout.amount_column_min_x
            and other.contains_y(line.y_mid)
        ]
        for candidate in sorted(candidates, key=lambda item: item.x0):
            text = candidate.text.strip()
            if _AMOUNT_RE.match(text):
                amount = _parse_french_amount(text)
                if amount is not None:
                    return amount
    return None


# --------------------------------------------------------------------------- #
# Découpage du PDF en factures
# --------------------------------------------------------------------------- #


def _starts_new_invoice(lines: list[TextLine], layout: LayoutProfile) -> bool:
    """Une page démarre une facture si elle porte le libellé « Facture N° »."""
    return any(
        line.y0 < layout.header_max_y and _INVOICE_LABEL_RE.search(line.text)
        for line in lines
    )


def _build_invoice(
    *,
    source_name: str,
    source_digest: str,
    first_page: int,
    last_page: int,
    lines: list[TextLine],
    fallback_period: str | None,
    layout: LayoutProfile,
) -> tuple[Invoice | None, str | None]:
    """Construit une facture à partir des lignes de sa première page.

    Renvoie ``(facture, None)`` ou ``(None, motif_d_echec)``.
    """
    client_name, warnings = _find_client_name(lines, layout)
    if not client_name:
        return None, "Nom du client introuvable sous la date."

    invoice_date = _find_invoice_date(lines, layout)
    invoice_number = _find_invoice_number(lines, layout)
    total_ht = _find_amount(lines, _TOTAL_HT_LABELS, layout)
    total_tva = _find_amount(lines, _TOTAL_TVA_LABELS, layout)
    total_ttc = _find_amount(lines, _TOTAL_TTC_LABELS, layout)

    if invoice_date is None:
        warnings.append("Date de facture illisible : période déduite de la saisie.")
    if invoice_number is None:
        warnings.append("Numéro de facture illisible.")
    if total_ttc is None:
        warnings.append("Total à payer illisible.")
    elif (
        total_ht is not None
        and total_tva is not None
        and abs((total_ht + total_tva) - total_ttc) > Decimal("0.02")
    ):
        warnings.append(
            f"Incohérence des totaux : {total_ht} + {total_tva} != {total_ttc}."
        )

    invoice = Invoice(
        source_name=source_name,
        source_digest=source_digest,
        first_page=first_page,
        last_page=last_page,
        client_name=client_name,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        ice=_find_client_ice(lines, layout),
        total_ht=total_ht,
        total_tva=total_tva,
        total_ttc=total_ttc,
        fallback_period=fallback_period,
        warnings=tuple(warnings),
    )
    return invoice, None


def extract_invoices(
    pdf_bytes: bytes,
    *,
    source_name: str,
    fallback_period: str | None = None,
    layout: LayoutProfile = DEFAULT_LAYOUT,
) -> ExtractionReport:
    """Extrait toutes les factures d'un PDF.

    Args:
        pdf_bytes: contenu binaire du PDF.
        source_name: nom du fichier, conservé pour la traçabilité.
        fallback_period: période ``AAAA-MM`` saisie par l'utilisateur, utilisée
            uniquement pour les factures dont la date est illisible.
        layout: profil géométrique du gabarit.

    Returns:
        Un `ExtractionReport` contenant les factures et les pages en échec.
        La fonction ne lève pas d'exception pour une page défaillante : elle
        isole l'incident afin qu'un lot de 160 factures ne soit jamais perdu
        à cause d'une seule page atypique.
    """
    digest = hashlib.sha256(pdf_bytes).hexdigest()

    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # pragma: no cover - dépend du fichier fourni
        logger.exception("Ouverture impossible: %s", source_name)
        return ExtractionReport(
            errors=(PageError(source_name, 0, f"PDF illisible : {exc}"),)
        )

    invoices: list[Invoice] = []
    errors: list[PageError] = []

    with document:
        if document.needs_pass:
            return ExtractionReport(
                errors=(
                    PageError(source_name, 0, "PDF protégé par mot de passe."),
                )
            )

        # 1) Repérer les pages qui ouvrent une facture.
        page_lines: list[list[TextLine]] = []
        starts: list[int] = []
        for index, page in enumerate(document):
            try:
                lines = _read_lines(page)
            except Exception as exc:  # pragma: no cover - page corrompue
                logger.exception("Lecture impossible: %s p.%d", source_name, index + 1)
                page_lines.append([])
                errors.append(
                    PageError(source_name, index + 1, f"Page illisible : {exc}")
                )
                continue
            page_lines.append(lines)
            if _starts_new_invoice(lines, layout):
                starts.append(index)

        if not starts:
            errors.append(
                PageError(
                    source_name,
                    0,
                    "Aucune facture détectée : le libellé « Facture N° » est absent.",
                )
            )
            return ExtractionReport(errors=tuple(errors))

        # 2) Pages précédant la première facture : signalées, jamais rattachées.
        for index in range(starts[0]):
            errors.append(
                PageError(source_name, index + 1, "Page hors facture (ignorée).")
            )

        # 3) Une facture court jusqu'à la page précédant la facture suivante.
        boundaries = [*starts, len(page_lines)]
        for start, next_start in pairwise(boundaries):
            invoice, failure = _build_invoice(
                source_name=source_name,
                source_digest=digest,
                first_page=start + 1,
                last_page=next_start,
                lines=page_lines[start],
                fallback_period=fallback_period,
                layout=layout,
            )
            if invoice is None:
                errors.append(PageError(source_name, start + 1, failure or "Échec."))
            else:
                invoices.append(invoice)

    return ExtractionReport(invoices=tuple(invoices), errors=tuple(errors))
