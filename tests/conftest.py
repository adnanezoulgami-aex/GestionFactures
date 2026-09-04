"""Fixtures partagées : génération de PDF de test reproduisant le gabarit.

Aucune facture réelle n'est versionnée dans le dépôt : les tests fabriquent
leurs propres PDF avec les mêmes coordonnées que le gabarit de production.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Coordonnées relevées sur le gabarit réel (voir factures/extraction.py).
LABEL_X = 23.6
NUMBER_X = 107.6
DATE_X = 443.2
ICE_X = 24.4
CLIENT_X = 311.6
TOTAL_LABEL_X = 365.6
TOTAL_VALUE_X = 523.1

HEADER_BASELINE = 137.0
ICE_BASELINE = 157.0
CLIENT_BASELINE = 164.0
NAME_LINE_GAP = 16.6  # simple retour à la ligne : même nom
ADDRESS_GAP = 33.1  # ligne vide : début de l'adresse

FOOTER_ICE = "000166897000013"


@dataclass
class InvoiceSpec:
    """Description d'une facture à générer dans un PDF de test."""

    client_name_lines: tuple[str, ...]
    number: str | None = "1075 - 2026"
    day: str | None = "31/07/2026"
    ice: str | None = "003070399000083"
    total_ht: str | None = "1 500,00"
    total_tva: str | None = "300,00"
    total_ttc: str | None = "1 800,00"
    address_lines: tuple[str, ...] = ("12 rue de Test", "Tanger")
    extra_pages: int = 0
    """Pages de continuation, sans libellé « Facture N° »."""

    warnings: list[str] = field(default_factory=list)


def _draw_invoice(page: pymupdf.Page, spec: InvoiceSpec) -> None:
    page.insert_text((LABEL_X, HEADER_BASELINE), "Facture N° : ", fontsize=15)
    if spec.number:
        page.insert_text((NUMBER_X, HEADER_BASELINE), spec.number, fontsize=15)
    if spec.day:
        page.insert_text((DATE_X, HEADER_BASELINE), f"Tanger le, {spec.day}", fontsize=14)
    if spec.ice:
        page.insert_text((ICE_X, ICE_BASELINE), f"ICE : {spec.ice}", fontsize=14)

    baseline = CLIENT_BASELINE
    for line in spec.client_name_lines:
        page.insert_text((CLIENT_X, baseline), line, fontsize=14)
        baseline += NAME_LINE_GAP

    baseline += ADDRESS_GAP - NAME_LINE_GAP
    for line in spec.address_lines:
        page.insert_text((CLIENT_X, baseline), line, fontsize=14)
        baseline += NAME_LINE_GAP

    page.insert_text((LABEL_X, 337.0), "Désignation", fontsize=13)
    page.insert_text((TOTAL_LABEL_X, 337.0), "Qté", fontsize=13)
    # Quantité : proche du libellé mais hors de la colonne des montants.
    page.insert_text((370.7, 358.0), "1,00", fontsize=13)

    totals = (
        ("Total HT", spec.total_ht, 604.0),
        ("Total TVA", spec.total_tva, 623.0),
        ("Total à payer", spec.total_ttc, 647.0),
    )
    for label, value, y in totals:
        page.insert_text((TOTAL_LABEL_X, y), label, fontsize=13)
        if value is not None:
            page.insert_text((TOTAL_VALUE_X, y), value, fontsize=13)

    # Pied de page : contient l'ICE du cabinet, qui ne doit jamais être retenu.
    page.insert_text((126.1, 810.0), f"RC TANGER 49067 -ICE {FOOTER_ICE}", fontsize=8)


def make_pdf(specs: list[InvoiceSpec]) -> bytes:
    """Génère un PDF contenant les factures décrites."""
    document = pymupdf.open()
    for spec in specs:
        _draw_invoice(document.new_page(width=595.32, height=841.92), spec)
        for _ in range(spec.extra_pages):
            page = document.new_page(width=595.32, height=841.92)
            page.insert_text((LABEL_X, 300.0), "Suite du détail des prestations", fontsize=12)
    payload = document.tobytes()
    document.close()
    return payload


@pytest.fixture
def single_invoice_pdf() -> bytes:
    return make_pdf([InvoiceSpec(client_name_lines=("ACME LOGISTICS",))])


# --------------------------------------------------------------------------- #
# Grand livre : gabarit des extraits de compte
# --------------------------------------------------------------------------- #

# Coordonnées relevées sur le grand livre réel (voir factures/ledger.py).
LEDGER_MARKER_X = 23.0
LEDGER_NAME_X = 253.0
LEDGER_CODE_X = 493.0
LEDGER_HEADING_BASELINE = 108.0
LEDGER_PERIOD_BASELINE = 70.0
LEDGER_TOTALS_LABEL_X = 163.0
LEDGER_TOTALS_BASELINE = 310.0

# Bords droits des colonnes de montants, alignées à droite.
LEDGER_DEBIT_RIGHT = 367.0
LEDGER_CREDIT_RIGHT = 427.0
LEDGER_BALANCE_DEBIT_RIGHT = 507.0
LEDGER_BALANCE_CREDIT_RIGHT = 567.0


@dataclass
class AccountSpec:
    """Description d'un compte à générer dans un grand livre de test."""

    client_name: str
    code: str | None = "0TEST"
    debit: str | None = "1 200,00"
    credit: str | None = "200,00"
    balance_debit: str | None = "1 000,00"
    balance_credit: str | None = None
    period: str | None = "Période du  01/10/2025  au  30/09/2026"
    with_marker: bool = True
    extra_pages: int = 0


def _right_aligned(page: pymupdf.Page, right_x: float, baseline: float,
                   text: str, size: float = 8.5) -> None:
    """Écrit `text` de sorte que son bord droit tombe sur `right_x`."""
    width = pymupdf.get_text_length(text, fontname="helv", fontsize=size)
    page.insert_text((right_x - width, baseline), text, fontsize=size)


def _draw_account(page: pymupdf.Page, spec: AccountSpec) -> None:
    if spec.period:
        page.insert_text((LEDGER_MARKER_X, LEDGER_PERIOD_BASELINE), spec.period, fontsize=9)
    # La colonne du nom porte aussi cette mention d'en-tête, à une autre hauteur :
    # l'extraction ne doit jamais la confondre avec un nom de client.
    page.insert_text((LEDGER_NAME_X, LEDGER_PERIOD_BASELINE), "Présenté en Dh", fontsize=9)

    if spec.with_marker:
        page.insert_text((LEDGER_MARKER_X, LEDGER_HEADING_BASELINE), "COMPTE", fontsize=9)
    if spec.client_name:
        page.insert_text((LEDGER_NAME_X, LEDGER_HEADING_BASELINE), spec.client_name, fontsize=9)
    if spec.code:
        page.insert_text((LEDGER_CODE_X, LEDGER_HEADING_BASELINE), spec.code, fontsize=9)

    page.insert_text((LEDGER_TOTALS_LABEL_X, LEDGER_TOTALS_BASELINE),
                     f"Totaux du compte {spec.code or ''}".strip(), fontsize=8.5)
    for value, right in (
        (spec.debit, LEDGER_DEBIT_RIGHT),
        (spec.credit, LEDGER_CREDIT_RIGHT),
        (spec.balance_debit, LEDGER_BALANCE_DEBIT_RIGHT),
        (spec.balance_credit, LEDGER_BALANCE_CREDIT_RIGHT),
    ):
        if value is not None:
            _right_aligned(page, right, LEDGER_TOTALS_BASELINE, value)


def make_ledger_pdf(specs: list[AccountSpec]) -> bytes:
    """Génère un grand livre contenant les comptes décrits."""
    document = pymupdf.open()
    for spec in specs:
        _draw_account(document.new_page(width=595.0, height=842.0), spec)
        for _ in range(spec.extra_pages):
            page = document.new_page(width=595.0, height=842.0)
            page.insert_text((LEDGER_MARKER_X, 300.0), "Suite des écritures", fontsize=8)
    payload = document.tobytes()
    document.close()
    return payload
