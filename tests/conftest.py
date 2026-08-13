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
