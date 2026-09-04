"""Extraction des extraits de compte depuis un grand livre.

Le grand livre imprime les comptes les uns après les autres. Chaque compte
s'ouvre sur une rangée repérable, à position fixe :

===========================  =======  ======  =====================
Élément                      x0       y0      Exemple
===========================  =======  ======  =====================
Repère « COMPTE »             ~23      ~99    COMPTE
Intitulé du compte           ~253      ~99    ABM INVEST
Code du compte auxiliaire    ~493      ~99    0ABMINVE
===========================  =======  ======  =====================

L'intitulé est lu sur **la rangée du repère**, et non simplement dans sa
colonne : celle-ci porte aussi la mention « Présenté en Dh » de l'en-tête, qui
serait sinon prise pour un nom de client.

Le compte se termine par une rangée « Totaux du compte <code> » portant le
débit, le crédit et le solde de clôture.

La mécanique commune de lecture et de découpage vit dans `factures.pdfscan`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from factures.models import ExtractionReport, LedgerAccount
from factures.pdfscan import (
    DATE_RE,
    DocumentSpan,
    TextLine,
    parse_french_amount,
    same_row,
    split_documents,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LedgerLayout:
    """Constantes géométriques du gabarit de grand livre, en points PDF."""

    marker_max_x: float = 60.0
    """Le repère « COMPTE » est collé à la marge gauche."""

    marker_max_y: float = 200.0
    """Il figure dans l'en-tête, jamais dans le corps du tableau."""

    name_min_x: float = 200.0
    name_max_x: float = 400.0
    """Fenêtre horizontale de l'intitulé du compte."""

    code_min_x: float = 450.0
    """Le code du compte est aligné à droite."""

    totals_amount_min_x: float = 300.0
    """Abscisse à partir de laquelle commencent les colonnes chiffrées."""

    # Les colonnes de montants sont alignées à droite : c'est le bord droit qui
    # les identifie de façon fiable, la largeur du nombre variant avec sa valeur.
    debit_right_x: float = 367.0
    credit_right_x: float = 427.0
    balance_debit_right_x: float = 507.0
    balance_credit_right_x: float = 567.0
    column_tolerance: float = 6.0
    """Demi-largeur d'acceptation autour du bord droit d'une colonne."""


DEFAULT_LEDGER_LAYOUT = LedgerLayout()

_MARKER_RE = re.compile(r"^compte$", re.IGNORECASE)
_TOTALS_RE = re.compile(r"totaux\s+du\s+compte\s*(\S+)?", re.IGNORECASE)
_PERIOD_RE = re.compile(
    r"p[ée]riode\s+du\s+(\d{1,2}/\d{1,2}/\d{4})\s+au\s+(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)


def _find_marker(lines: list[TextLine], layout: LedgerLayout) -> TextLine | None:
    """Rangée d'ouverture d'un compte."""
    for line in lines:
        if (
            line.x0 < layout.marker_max_x
            and line.y0 < layout.marker_max_y
            and _MARKER_RE.match(line.text.strip())
        ):
            return line
    return None


def _read_heading(
    lines: list[TextLine], marker: TextLine, layout: LedgerLayout
) -> tuple[str | None, str | None]:
    """Intitulé et code du compte, lus sur la rangée du repère."""
    row = same_row(lines, marker, min_x=layout.name_min_x)
    name = next(
        (line.text.strip() for line in row if line.x0 < layout.name_max_x), None
    )
    code = next(
        (line.text.strip() for line in row if line.x0 >= layout.code_min_x), None
    )
    return (name or None), (code or None)


def _read_period(lines: list[TextLine]) -> tuple[date | None, date | None]:
    """Période couverte par le grand livre, imprimée en tête de chaque page."""
    for line in lines:
        match = _PERIOD_RE.search(line.text)
        if match is None:
            continue
        bounds: list[date | None] = []
        for raw in match.groups():
            found = DATE_RE.search(raw)
            if found is None:
                bounds.append(None)
                continue
            day, month, year = (int(group) for group in found.groups())
            try:
                bounds.append(date(year, month, day))
            except ValueError:
                bounds.append(None)
        return bounds[0], bounds[1]
    return None, None


def _read_totals(
    lines: list[TextLine], layout: LedgerLayout
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Débit, crédit et solde de la rangée « Totaux du compte ».

    Chaque montant est rattaché à sa colonne par son **bord droit**, jamais par
    son rang : le gabarit n'imprime que les colonnes non nulles, si bien qu'un
    compte jamais réglé n'affiche que deux montants — son débit et son solde.
    Les lire dans l'ordre ferait passer ce solde pour un crédit.

    Le solde est signé : positif s'il est débiteur (le client doit), négatif
    s'il est créditeur.
    """
    for line in lines:
        if _TOTALS_RE.search(line.text) is None:
            continue

        columns: dict[str, Decimal] = {}
        for candidate in same_row(lines, line, min_x=layout.totals_amount_min_x):
            parsed = parse_french_amount(candidate.text.strip())
            if parsed is None:
                continue
            for name, edge in (
                ("debit", layout.debit_right_x),
                ("credit", layout.credit_right_x),
                ("balance_debit", layout.balance_debit_right_x),
                ("balance_credit", layout.balance_credit_right_x),
            ):
                if abs(candidate.x1 - edge) <= layout.column_tolerance:
                    columns.setdefault(name, parsed)
                    break

        balance = columns.get("balance_debit")
        if balance is None and "balance_credit" in columns:
            balance = -columns["balance_credit"]
        return columns.get("debit"), columns.get("credit"), balance
    return None, None, None


def _starts_account(lines: list[TextLine], layout: LedgerLayout) -> bool:
    return _find_marker(lines, layout) is not None


def _build_account(
    span: DocumentSpan, layout: LedgerLayout
) -> tuple[LedgerAccount | None, str | None]:
    """Construit un extrait de compte depuis les lignes de sa première page."""
    lines = span.lines
    marker = _find_marker(lines, layout)
    if marker is None:  # pragma: no cover - garanti par `_starts_account`
        return None, "Repère « COMPTE » introuvable."

    client_name, account_code = _read_heading(lines, marker, layout)
    if not client_name:
        return None, "Intitulé du compte introuvable sur la ligne « COMPTE »."

    warnings: list[str] = []
    if not account_code:
        warnings.append("Code du compte illisible.")

    period_start, period_end = _read_period(lines)
    if period_start is None or period_end is None:
        warnings.append("Période du grand livre illisible.")

    debit, credit, balance = _read_totals(lines, layout)
    if balance is None:
        warnings.append("Ligne « Totaux du compte » illisible.")
    elif debit is not None:
        # Une colonne vide vaut zéro : le gabarit ne l'imprime pas.
        expected = debit - (credit or Decimal(0))
        if abs(expected - balance) > Decimal("0.02"):
            warnings.append(
                f"Incohérence du compte : débit {debit} - crédit "
                f"{credit or 0} != solde {balance}."
            )

    account = LedgerAccount(
        source_name=span.source_name,
        source_digest=span.source_digest,
        first_page=span.first_page,
        last_page=span.last_page,
        client_name=client_name,
        account_code=account_code,
        period_start=period_start,
        period_end=period_end,
        total_debit=debit,
        total_credit=credit,
        balance=balance,
        warnings=tuple(warnings),
    )
    return account, None


def extract_ledger_accounts(
    pdf_bytes: bytes,
    *,
    source_name: str,
    layout: LedgerLayout = DEFAULT_LEDGER_LAYOUT,
) -> ExtractionReport:
    """Extrait tous les extraits de compte d'un grand livre.

    Args:
        pdf_bytes: contenu binaire du PDF.
        source_name: nom du fichier, conservé pour la traçabilité.
        layout: profil géométrique du gabarit.

    Returns:
        Un `ExtractionReport` contenant les comptes et les pages en échec.
    """
    return split_documents(
        pdf_bytes,
        source_name=source_name,
        starts_new_document=lambda lines: _starts_account(lines, layout),
        build_document=lambda span: _build_account(span, layout),
        no_document_reason=(
            "Aucun compte détecté : le repère « COMPTE » est absent."
        ),
    )
