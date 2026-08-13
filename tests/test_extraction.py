"""Tests de l'extraction : identification du client, des totaux et découpage."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from conftest import FOOTER_ICE, InvoiceSpec, make_pdf
from factures.extraction import extract_invoices


def extract(specs: list[InvoiceSpec], **kwargs):
    return extract_invoices(make_pdf(specs), source_name="lot.pdf", **kwargs)


class TestClientName:
    def test_reads_the_line_below_the_date(self, single_invoice_pdf: bytes) -> None:
        report = extract_invoices(single_invoice_pdf, source_name="lot.pdf")
        assert report.ok
        assert report.invoices[0].client_name == "ACME LOGISTICS"

    def test_joins_a_name_wrapped_on_two_lines(self) -> None:
        report = extract(
            [InvoiceSpec(client_name_lines=("OMKA BUILDING (ELHAMSS", "BUILDING COMPANY)"))]
        )
        assert report.invoices[0].client_name == "OMKA BUILDING (ELHAMSS BUILDING COMPANY)"

    def test_stops_before_the_address(self) -> None:
        report = extract(
            [
                InvoiceSpec(
                    client_name_lines=("GRAND TANGER IMMO",),
                    address_lines=("15 RES. AL TAJML RUE ABI DARDAE", "TANGER"),
                )
            ]
        )
        assert report.invoices[0].client_name == "GRAND TANGER IMMO"

    def test_missing_name_is_reported_not_guessed(self) -> None:
        report = extract([InvoiceSpec(client_name_lines=(), address_lines=())])
        assert not report.invoices
        assert "Nom du client introuvable" in report.errors[0].reason


class TestHeaderFields:
    def test_number_date_and_ice(self, single_invoice_pdf: bytes) -> None:
        invoice = extract_invoices(single_invoice_pdf, source_name="lot.pdf").invoices[0]
        assert invoice.invoice_number == "1075-2026"
        assert invoice.invoice_date == date(2026, 7, 31)
        assert invoice.ice == "003070399000083"

    def test_issuer_ice_from_the_footer_is_ignored(self) -> None:
        invoice = extract([InvoiceSpec(client_name_lines=("SANS ICE",), ice=None)]).invoices[0]
        assert invoice.ice is None, f"l'ICE du cabinet {FOOTER_ICE} a été retenu"

    def test_period_uses_the_invoice_date(self, single_invoice_pdf: bytes) -> None:
        invoice = extract_invoices(
            single_invoice_pdf, source_name="lot.pdf", fallback_period="2026-01"
        ).invoices[0]
        assert invoice.period == "2026-07"
        assert invoice.period_label == "juillet 2026"

    def test_period_falls_back_when_the_date_is_unreadable(self) -> None:
        report = extract(
            [InvoiceSpec(client_name_lines=("SANS DATE",), day=None)],
            fallback_period="2026-07",
        )
        invoice = report.invoices[0]
        assert invoice.invoice_date is None
        assert invoice.period == "2026-07"
        assert any("Date de facture illisible" in w for w in invoice.warnings)


class TestAmounts:
    def test_totals_are_parsed(self, single_invoice_pdf: bytes) -> None:
        invoice = extract_invoices(single_invoice_pdf, source_name="lot.pdf").invoices[0]
        assert invoice.total_ht == Decimal("1500.00")
        assert invoice.total_tva == Decimal("300.00")
        assert invoice.total_ttc == Decimal("1800.00")

    def test_quantity_column_is_not_mistaken_for_an_amount(
        self, single_invoice_pdf: bytes
    ) -> None:
        invoice = extract_invoices(single_invoice_pdf, source_name="lot.pdf").invoices[0]
        assert invoice.total_ht != Decimal("1.00")

    def test_inconsistent_totals_raise_a_warning(self) -> None:
        invoice = extract(
            [
                InvoiceSpec(
                    client_name_lines=("FAUX TOTAL",),
                    total_ht="1 000,00",
                    total_tva="200,00",
                    total_ttc="9 999,00",
                )
            ]
        ).invoices[0]
        assert any("Incohérence des totaux" in w for w in invoice.warnings)


class TestSplitting:
    def test_each_page_is_one_invoice(self) -> None:
        report = extract(
            [
                InvoiceSpec(client_name_lines=("CLIENT A",), number="1001 - 2026"),
                InvoiceSpec(client_name_lines=("CLIENT B",), number="1002 - 2026"),
                InvoiceSpec(client_name_lines=("CLIENT C",), number="1003 - 2026"),
            ]
        )
        assert report.ok
        assert [inv.client_name for inv in report.invoices] == [
            "CLIENT A",
            "CLIENT B",
            "CLIENT C",
        ]
        assert [inv.first_page for inv in report.invoices] == [1, 2, 3]

    def test_continuation_pages_stay_attached_to_their_invoice(self) -> None:
        report = extract(
            [
                InvoiceSpec(client_name_lines=("CLIENT LONG",), extra_pages=2),
                InvoiceSpec(client_name_lines=("CLIENT COURT",)),
            ]
        )
        first, second = report.invoices
        assert (first.first_page, first.last_page) == (1, 3)
        assert first.page_count == 3
        assert (second.first_page, second.last_page) == (4, 4)

    def test_keys_are_stable_across_reimports(self) -> None:
        specs = [InvoiceSpec(client_name_lines=("CLIENT A",))]
        payload = make_pdf(specs)
        first = extract_invoices(payload, source_name="lot.pdf").invoices[0]
        second = extract_invoices(payload, source_name="autre-nom.pdf").invoices[0]
        assert first.key == second.key


class TestRobustness:
    def test_corrupted_file_returns_an_error_instead_of_raising(self) -> None:
        report = extract_invoices(b"ceci n'est pas un PDF", source_name="ko.pdf")
        assert not report.invoices
        assert report.errors

    def test_pdf_without_any_invoice_label(self) -> None:
        import pymupdf

        document = pymupdf.open()
        document.new_page().insert_text((50, 50), "Note de service")
        payload = document.tobytes()
        document.close()

        report = extract_invoices(payload, source_name="note.pdf")
        assert not report.invoices
        assert "Aucune facture détectée" in report.errors[0].reason
