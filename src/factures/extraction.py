"""Extraction des factures, par analyse de la mise en page.

Le gabarit place le bloc client à une position fixe : colonne de droite, juste
sous la ligne de date.

Repères mesurés sur le gabarit (page A4 595 x 842 pt) :

===================  ========  ==========================================
Élément              x0        y0
===================  ========  ==========================================
« Facture N° : »      ~24       ~123
Numéro de facture    ~108      ~123  (même rangée que le libellé)
« Tanger le, JJ/MM » ~443      ~123
ICE client            ~24      ~145
Nom du client        ~312      ~151   <- première ligne du bloc de droite
(suite du nom)       ~312      ~168   (interligne ~16.6 pt)
Adresse              ~312      ~185   (saut de ligne ~33.1 pt)
===================  ========  ==========================================

La distinction « suite du nom » / « adresse » repose sur l'écart vertical :
16.6 pt pour un simple retour à la ligne, 33.1 pt lorsqu'une ligne vide sépare
le nom de l'adresse. Le seuil est fixé à mi-chemin.

La mécanique commune de lecture et de découpage vit dans `factures.pdfscan`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

from factures.models import ExtractionReport, Invoice
from factures.pdfscan import (
    DocumentSpan,
    TextLine,
    amount_on_row,
    parse_french_date,
    same_row,
    split_documents,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LayoutProfile:
    """Constantes géométriques du gabarit de facture, en points PDF."""

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

_INVOICE_LABEL_RE = re.compile(r"facture\s*n[°ºo]?", re.IGNORECASE)
_INVOICE_NUMBER_RE = re.compile(r"^(\d{1,7})\s*[-/]\s*(\d{4})$")
_INVOICE_NUMBER_INLINE_RE = re.compile(
    r"facture\s*n[°ºo]?\s*:?\s*(\d{1,7}\s*[-/]\s*\d{4})", re.IGNORECASE
)
_ICE_RE = re.compile(r"\bICE\s*:?\s*([0-9]{9,20})\b", re.IGNORECASE)

_TOTAL_HT_LABELS = ("total ht", "montant ht", "total h.t")
_TOTAL_TVA_LABELS = ("total tva", "montant tva", "t.v.a")
_TOTAL_TTC_LABELS = ("total à payer", "total a payer", "total ttc", "net à payer")

#: ICE du cabinet émetteur, présent dans le pied de page de chaque facture :
#: il ne doit jamais être confondu avec l'ICE du client.
_ISSUER_ICE = "000166897000013"


def _find_client_name(
    lines: list[TextLine], layout: LayoutProfile
) -> tuple[str | None, list[str]]:
    """Nom du client : bloc de droite, première ligne sous la date.

    Le nom peut tenir sur plusieurs lignes consécutives ; l'adresse, séparée par
    une ligne vide, est exclue.
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

    name = re.sub(r"\s+", " ", " ".join(line.text for line in name_lines)).strip()
    if not name:
        return None, warnings
    if len(name_lines) > 3:
        warnings.append(f"Nom du client réparti sur {len(name_lines)} lignes, à vérifier.")
    return name, warnings


def _find_invoice_number(lines: list[TextLine], layout: LayoutProfile) -> str | None:
    """Numéro de facture, lu sur la rangée du libellé « Facture N° »."""
    labels = [
        line
        for line in lines
        if line.y0 < layout.header_max_y and _INVOICE_LABEL_RE.search(line.text)
    ]
    for label in labels:
        # Cas 1 : libellé et numéro sur la même rangée, en deux fragments.
        for candidate in same_row(lines, label, min_x=label.x0 + 1):
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
            parsed = parse_french_date(line.text)
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
        if match.group(1) == _ISSUER_ICE:
            continue
        return match.group(1)
    return None


def _find_total(
    lines: list[TextLine], labels: tuple[str, ...], layout: LayoutProfile
) -> Decimal | None:
    """Montant aligné à droite sur la rangée d'un des libellés de total."""
    for line in lines:
        normalized = line.text.strip().lower()
        if not any(normalized.startswith(label) for label in labels):
            continue
        amount = amount_on_row(lines, line, min_x=layout.amount_column_min_x)
        if amount is not None:
            return amount
    return None


def _starts_invoice(lines: list[TextLine], layout: LayoutProfile) -> bool:
    """Une page démarre une facture si elle porte le libellé « Facture N° »."""
    return any(
        line.y0 < layout.header_max_y and _INVOICE_LABEL_RE.search(line.text)
        for line in lines
    )


def _build_invoice(
    span: DocumentSpan, fallback_period: str | None, layout: LayoutProfile
) -> tuple[Invoice | None, str | None]:
    """Construit une facture depuis les lignes de sa première page."""
    lines = span.lines
    client_name, warnings = _find_client_name(lines, layout)
    if not client_name:
        return None, "Nom du client introuvable sous la date."

    invoice_date = _find_invoice_date(lines, layout)
    invoice_number = _find_invoice_number(lines, layout)
    total_ht = _find_total(lines, _TOTAL_HT_LABELS, layout)
    total_tva = _find_total(lines, _TOTAL_TVA_LABELS, layout)
    total_ttc = _find_total(lines, _TOTAL_TTC_LABELS, layout)

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
        source_name=span.source_name,
        source_digest=span.source_digest,
        first_page=span.first_page,
        last_page=span.last_page,
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
    """
    return split_documents(
        pdf_bytes,
        source_name=source_name,
        starts_new_document=lambda lines: _starts_invoice(lines, layout),
        build_document=lambda span: _build_invoice(span, fallback_period, layout),
        no_document_reason=(
            "Aucune facture détectée : le libellé « Facture N° » est absent."
        ),
    )
