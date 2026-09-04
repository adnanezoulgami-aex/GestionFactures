"""Tests de la découpe en PDF individuels et de la structure de l'archive ZIP."""

from __future__ import annotations

import io
import zipfile

import pymupdf
import pytest

from conftest import InvoiceSpec, make_pdf
from factures.extraction import extract_invoices
from factures.packaging import (
    PackagingError,
    build_document_pdf,
    build_zip_archive,
    document_display_name,
    plan_archive_names,
)


@pytest.fixture
def lot():
    """Un lot représentatif : clients uniques, doublons et triplons."""
    specs = [
        InvoiceSpec(client_name_lines=("DRAKE",), number="1095 - 2026"),
        InvoiceSpec(client_name_lines=("VELVETRAV",), number="1145 - 2026"),
        InvoiceSpec(client_name_lines=("VELVETRAV",), number="1175 - 2026"),
        InvoiceSpec(client_name_lines=("VELVETRAV",), number="1185 - 2026"),
        InvoiceSpec(client_name_lines=("Hôtel Nord Pinus Tanger",), number="1104 - 2026"),
        InvoiceSpec(client_name_lines=("Hôtel Nord Pinus Tanger",), number="1160 - 2026"),
    ]
    payload = make_pdf(specs)
    report = extract_invoices(payload, source_name="lot.pdf")
    assert report.ok
    return report.documents, {"lot.pdf": payload}


class TestSinglePdf:
    def test_extracted_pdf_contains_only_its_pages(self, lot) -> None:
        invoices, sources = lot
        payload = build_document_pdf(invoices[2], sources)
        with pymupdf.open(stream=payload, filetype="pdf") as document:
            assert document.page_count == 1
            assert "1175 - 2026" in document[0].get_text()

    def test_multi_page_invoice_keeps_all_its_pages(self) -> None:
        payload = make_pdf([InvoiceSpec(client_name_lines=("LONG",), extra_pages=2)])
        invoice = extract_invoices(payload, source_name="lot.pdf").documents[0]
        result = build_document_pdf(invoice, {"lot.pdf": payload})
        with pymupdf.open(stream=result, filetype="pdf") as document:
            assert document.page_count == 3

    def test_download_name_is_the_client_name(self, lot) -> None:
        invoices, _ = lot
        assert document_display_name(invoices[0]) == "DRAKE.pdf"

    def test_missing_source_raises_a_clear_error(self, lot) -> None:
        invoices, _ = lot
        with pytest.raises(PackagingError, match="indisponible"):
            build_document_pdf(invoices[0], {})

    def test_page_range_outside_the_document_is_rejected(self, lot) -> None:
        invoices, _ = lot
        shrunk = make_pdf([InvoiceSpec(client_name_lines=("SEUL",))])
        with pytest.raises(PackagingError, match="hors du document"):
            build_document_pdf(invoices[5], {"lot.pdf": shrunk})


class TestArchiveLayout:
    def test_single_invoice_client_sits_at_the_root(self, lot) -> None:
        invoices, _ = lot
        paths = plan_archive_names(invoices)
        assert paths[invoices[0].key] == "DRAKE.pdf"

    def test_repeat_client_gets_a_folder_named_after_them(self, lot) -> None:
        invoices, _ = lot
        paths = plan_archive_names(invoices)
        for invoice in invoices[1:4]:
            assert paths[invoice.key].startswith("VELVETRAV/")
        assert paths[invoices[1].key] == "VELVETRAV/VELVETRAV - 1145-2026.pdf"

    def test_accented_client_folder(self, lot) -> None:
        invoices, _ = lot
        paths = plan_archive_names(invoices)
        assert paths[invoices[4].key].startswith("Hôtel Nord Pinus Tanger/")

    def test_all_paths_are_unique(self, lot) -> None:
        invoices, _ = lot
        paths = plan_archive_names(invoices)
        lowered = [path.casefold() for path in paths.values()]
        assert len(set(lowered)) == len(lowered)

    def test_clients_colliding_after_sanitising_stay_distinct(self) -> None:
        payload = make_pdf(
            [
                InvoiceSpec(client_name_lines=("A/B",), number="1 - 2026"),
                InvoiceSpec(client_name_lines=("A:B",), number="2 - 2026"),
            ]
        )
        invoices = extract_invoices(payload, source_name="lot.pdf").documents
        paths = plan_archive_names(invoices)
        assert set(paths.values()) == {"A-B.pdf", "A-B (2).pdf"}

    def test_invoice_without_number_uses_its_page(self) -> None:
        payload = make_pdf(
            [
                InvoiceSpec(client_name_lines=("SANS NUM",), number=None),
                InvoiceSpec(client_name_lines=("SANS NUM",), number=None),
            ]
        )
        invoices = extract_invoices(payload, source_name="lot.pdf").documents
        paths = set(plan_archive_names(invoices).values())
        assert paths == {"SANS NUM/SANS NUM - p1.pdf", "SANS NUM/SANS NUM - p2.pdf"}


class TestZipArchive:
    def test_archive_contains_every_invoice(self, lot) -> None:
        invoices, sources = lot
        payload, failures = build_zip_archive(invoices, sources)
        assert not failures
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert len(archive.namelist()) == len(invoices)

    def test_archive_structure(self, lot) -> None:
        invoices, sources = lot
        payload, _ = build_zip_archive(invoices, sources)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert set(archive.namelist()) == {
                "DRAKE.pdf",
                "Hôtel Nord Pinus Tanger/Hôtel Nord Pinus Tanger - 1104-2026.pdf",
                "Hôtel Nord Pinus Tanger/Hôtel Nord Pinus Tanger - 1160-2026.pdf",
                "VELVETRAV/VELVETRAV - 1145-2026.pdf",
                "VELVETRAV/VELVETRAV - 1175-2026.pdf",
                "VELVETRAV/VELVETRAV - 1185-2026.pdf",
            }

    def test_entries_are_valid_pdfs(self, lot) -> None:
        invoices, sources = lot
        payload, _ = build_zip_archive(invoices, sources)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in archive.namelist():
                content = archive.read(name)
                assert content.startswith(b"%PDF")
                with pymupdf.open(stream=content, filetype="pdf") as document:
                    assert document.page_count >= 1

    def test_archive_passes_its_own_integrity_check(self, lot) -> None:
        invoices, sources = lot
        payload, _ = build_zip_archive(invoices, sources)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert archive.testzip() is None

    def test_a_broken_invoice_does_not_lose_the_others(self, lot) -> None:
        invoices, sources = lot
        broken = invoices[0].__class__(
            source_name="absent.pdf",
            source_digest="0" * 64,
            first_page=1,
            last_page=1,
            client_name="FANTOME",
        )
        payload, failures = build_zip_archive([*invoices, broken], sources)
        assert len(failures) == 1
        assert "FANTOME" in failures[0]
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert len(archive.namelist()) == len(invoices)

    def test_empty_selection_produces_a_valid_empty_archive(self) -> None:
        payload, failures = build_zip_archive([], {})
        assert not failures
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert archive.namelist() == []

    def test_progress_callback_reports_every_invoice(self, lot) -> None:
        invoices, sources = lot
        seen: list[tuple[int, int]] = []
        build_zip_archive(invoices, sources, progress=lambda d, t: seen.append((d, t)))
        assert seen == [(index, len(invoices)) for index in range(1, len(invoices) + 1)]

    def test_compression_preserves_the_content(self, lot) -> None:
        """La réduction des polices ne doit rien altérer du contenu.

        Le gain de taille n'est pas vérifiable ici : les PDF de test utilisent
        les polices standard PDF, qui ne sont pas embarquées et n'offrent donc
        rien à réduire. Sur les factures réelles, dont les polices sont
        embarquées, la page passe d'environ 460 Ko à 140 Ko.
        """
        invoices, sources = lot
        compressed = build_document_pdf(invoices[0], sources, compress=True)
        raw = build_document_pdf(invoices[0], sources, compress=False)
        for payload in (compressed, raw):
            with pymupdf.open(stream=payload, filetype="pdf") as document:
                assert document.page_count == 1
                assert "DRAKE" in document[0].get_text()
                assert "1095 - 2026" in document[0].get_text()

    def test_archives_are_reproducible(self, lot) -> None:
        invoices, sources = lot
        first, _ = build_zip_archive(invoices, sources)
        second, _ = build_zip_archive(invoices, sources)
        with zipfile.ZipFile(io.BytesIO(first)) as a, zipfile.ZipFile(io.BytesIO(second)) as b:
            assert a.namelist() == b.namelist()
