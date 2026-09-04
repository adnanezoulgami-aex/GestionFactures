"""Tests de l'extraction des extraits de compte depuis un grand livre."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pymupdf
import pytest

from conftest import AccountSpec, make_ledger_pdf
from factures.ledger import extract_ledger_accounts
from factures.packaging import (
    build_document_pdf,
    document_display_name,
    plan_archive_names,
)


def extract(specs: list[AccountSpec]):
    return extract_ledger_accounts(make_ledger_pdf(specs), source_name="gl.pdf")


class TestAccountHeading:
    def test_reads_the_name_on_the_compte_row(self) -> None:
        report = extract([AccountSpec(client_name="ABM INVEST")])
        assert report.ok
        assert report.documents[0].client_name == "ABM INVEST"

    def test_header_mention_in_the_same_column_is_not_taken_for_a_name(self) -> None:
        """« Présenté en Dh » partage la colonne du nom, à une autre hauteur."""
        account = extract([AccountSpec(client_name="ACULA LOGISTICS")]).documents[0]
        assert account.client_name == "ACULA LOGISTICS"
        assert "Présenté" not in account.client_name

    def test_reads_the_account_code(self) -> None:
        account = extract(
            [AccountSpec(client_name="ABM INVEST", code="0ABMINVE")]
        ).documents[0]
        assert account.account_code == "0ABMINVE"
        assert account.reference == "0ABMINVE"

    def test_missing_code_is_only_a_warning(self) -> None:
        account = extract(
            [AccountSpec(client_name="SANS CODE", code=None)]
        ).documents[0]
        assert account.account_code is None
        assert any("Code du compte" in w for w in account.warnings)

    def test_missing_name_is_reported_not_guessed(self) -> None:
        report = extract([AccountSpec(client_name="")])
        assert not report.documents
        assert "Intitulé du compte introuvable" in report.errors[0].reason


class TestPeriod:
    def test_period_is_read_from_the_header(self) -> None:
        account = extract([AccountSpec(client_name="ABM INVEST")]).documents[0]
        assert account.period_start == date(2025, 10, 1)
        assert account.period_end == date(2026, 9, 30)
        assert account.period_label == "01/10/2025 au 30/09/2026"
        assert account.period == "2025-10/2026-09"

    def test_missing_period_is_only_a_warning(self) -> None:
        account = extract(
            [AccountSpec(client_name="SANS PERIODE", period=None)]
        ).documents[0]
        assert account.period == "inconnue"
        assert account.period_label == "Période inconnue"
        assert any("Période" in w for w in account.warnings)


class TestTotals:
    def test_the_three_columns_are_read(self) -> None:
        account = extract([AccountSpec(client_name="ABM INVEST")]).documents[0]
        assert account.total_debit == Decimal("1200.00")
        assert account.total_credit == Decimal("200.00")
        assert account.balance == Decimal("1000.00")

    def test_unpaid_account_prints_only_debit_and_balance(self) -> None:
        """Le piège du gabarit : sans colonne crédit, lire dans l'ordre ferait
        passer le solde débiteur pour un crédit."""
        account = extract(
            [
                AccountSpec(
                    client_name="ABRATA INVEST MAROC",
                    debit="3 410,00",
                    credit=None,
                    balance_debit="3 410,00",
                )
            ]
        ).documents[0]
        assert account.total_debit == Decimal("3410.00")
        assert account.total_credit is None
        assert account.balance == Decimal("3410.00")

    def test_credit_balance_is_negative(self) -> None:
        account = extract(
            [
                AccountSpec(
                    client_name="DOCTOR FACE",
                    debit="12 920,00",
                    credit="18 920,00",
                    balance_debit=None,
                    balance_credit="6 000,00",
                )
            ]
        ).documents[0]
        assert account.balance == Decimal("-6000.00")

    def test_missing_totals_row_is_only_a_warning(self) -> None:
        account = extract(
            [
                AccountSpec(
                    client_name="SANS TOTAUX",
                    debit=None,
                    credit=None,
                    balance_debit=None,
                )
            ]
        ).documents[0]
        assert account.balance is None
        assert any("Totaux du compte" in w for w in account.warnings)

    def test_inconsistent_totals_raise_a_warning(self) -> None:
        account = extract(
            [
                AccountSpec(
                    client_name="INCOHERENT",
                    debit="1 000,00",
                    credit="200,00",
                    balance_debit="9 999,00",
                )
            ]
        ).documents[0]
        assert any("Incohérence du compte" in w for w in account.warnings)


class TestSplitting:
    def test_each_page_is_one_account(self) -> None:
        report = extract(
            [
                AccountSpec(client_name="CLIENT A", code="0A"),
                AccountSpec(client_name="CLIENT B", code="0B"),
                AccountSpec(client_name="CLIENT C", code="0C"),
            ]
        )
        assert report.ok
        assert [doc.client_name for doc in report.documents] == [
            "CLIENT A",
            "CLIENT B",
            "CLIENT C",
        ]

    def test_continuation_pages_stay_attached(self) -> None:
        report = extract(
            [
                AccountSpec(client_name="COMPTE LONG", extra_pages=2),
                AccountSpec(client_name="COMPTE COURT"),
            ]
        )
        first, second = report.documents
        assert (first.first_page, first.last_page) == (1, 3)
        assert first.page_count == 3
        assert (second.first_page, second.last_page) == (4, 4)

    def test_keys_are_stable_across_reimports(self) -> None:
        payload = make_ledger_pdf([AccountSpec(client_name="CLIENT A")])
        first = extract_ledger_accounts(payload, source_name="gl.pdf").documents[0]
        second = extract_ledger_accounts(payload, source_name="autre.pdf").documents[0]
        assert first.key == second.key

    def test_ledger_and_invoice_keys_cannot_collide(self) -> None:
        """Deux types différents ne doivent jamais partager un statut d'envoi."""
        from conftest import InvoiceSpec, make_pdf
        from factures.extraction import extract_invoices

        ledger = extract([AccountSpec(client_name="X")]).documents[0]
        invoice = extract_invoices(
            make_pdf([InvoiceSpec(client_name_lines=("X",))]), source_name="f.pdf"
        ).documents[0]
        assert ledger.key != invoice.key


class TestRobustness:
    def test_corrupted_file_returns_an_error_instead_of_raising(self) -> None:
        report = extract_ledger_accounts(b"pas un PDF", source_name="ko.pdf")
        assert not report.documents
        assert report.errors

    def test_pdf_without_any_account(self) -> None:
        document = pymupdf.open()
        document.new_page().insert_text((50, 400), "Page de garde")
        payload = document.tobytes()
        document.close()

        report = extract_ledger_accounts(payload, source_name="gl.pdf")
        assert not report.documents
        assert "Aucun compte détecté" in report.errors[0].reason


class TestPackagingIntegration:
    """Les extraits passent par la même chaîne de nommage que les factures."""

    @pytest.fixture
    def lot(self):
        specs = [
            AccountSpec(client_name="ZYNKORA CONSULTING", code="0ZYNKO"),
            AccountSpec(client_name="Hôtel Nord Pinus", code="0HOTEL"),
            AccountSpec(client_name="Hôtel Nord Pinus", code="0HOTEL2"),
        ]
        payload = make_ledger_pdf(specs)
        report = extract_ledger_accounts(payload, source_name="gl.pdf")
        assert report.ok
        return report.documents, {"gl.pdf": payload}

    def test_download_name_is_prefixed(self, lot) -> None:
        """Un PDF isolé doit rester identifiable sans être ouvert."""
        accounts, _ = lot
        assert (
            document_display_name(accounts[0])
            == "Extrait de compte ZYNKORA CONSULTING.pdf"
        )

    def test_single_account_client_sits_at_the_root(self, lot) -> None:
        accounts, _ = lot
        paths = plan_archive_names(accounts)
        assert paths[accounts[0].key] == "Extrait de compte ZYNKORA CONSULTING.pdf"

    def test_repeat_client_gets_a_folder_named_after_them(self, lot) -> None:
        accounts, _ = lot
        paths = plan_archive_names(accounts)
        folder = "Extrait de compte Hôtel Nord Pinus"
        assert paths[accounts[1].key] == f"{folder}/{folder} - 0HOTEL.pdf"
        assert paths[accounts[2].key] == f"{folder}/{folder} - 0HOTEL2.pdf"

    def test_invoices_are_not_prefixed(self) -> None:
        """Le préfixe est propre aux extraits : les factures gardent leur nom."""
        from conftest import InvoiceSpec, make_pdf
        from factures.extraction import extract_invoices

        invoice = extract_invoices(
            make_pdf([InvoiceSpec(client_name_lines=("DRAKE",))]), source_name="f.pdf"
        ).documents[0]
        assert document_display_name(invoice) == "DRAKE.pdf"

    def test_extracted_pdf_contains_only_its_page(self, lot) -> None:
        accounts, sources = lot
        payload = build_document_pdf(accounts[0], sources)
        with pymupdf.open(stream=payload, filetype="pdf") as document:
            assert document.page_count == 1
            assert "ZYNKORA CONSULTING" in document[0].get_text()
