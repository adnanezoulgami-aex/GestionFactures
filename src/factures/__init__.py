"""Extraction, indexation et export de factures PDF multi-pages.

Le paquet est volontairement découplé de Streamlit : `app.py` ne contient que
la couche de présentation, toute la logique métier est ici et testable.
"""

from __future__ import annotations

from factures.extraction import extract_invoices
from factures.models import ExtractionReport, Invoice, PageError
from factures.naming import sanitize_filename, unique_filename
from factures.packaging import build_invoice_pdf, build_zip_archive

__all__ = [
    "ExtractionReport",
    "Invoice",
    "PageError",
    "build_invoice_pdf",
    "build_zip_archive",
    "extract_invoices",
    "sanitize_filename",
    "unique_filename",
]

__version__ = "1.0.0"
