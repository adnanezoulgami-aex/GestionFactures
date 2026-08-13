"""Tests de l'interface Streamlit via `AppTest` (exécution headless de app.py).

Le téléversement de fichiers n'est pas simulable par `AppTest` : les tests
injectent directement l'état de session produit par l'import, puis vérifient le
comportement des filtres, du tableau et des exports.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from conftest import InvoiceSpec, make_pdf
from factures.extraction import extract_invoices
from factures.storage import SentStatusStore

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")

SENT_LABEL = "Envoyé au client"

SPECS = [
    InvoiceSpec(client_name_lines=("DRAKE",), number="1095 - 2026", day="31/07/2026"),
    InvoiceSpec(client_name_lines=("VELVETRAV",), number="1145 - 2026", day="31/07/2026"),
    InvoiceSpec(client_name_lines=("VELVETRAV",), number="1175 - 2026", day="31/07/2026"),
    InvoiceSpec(
        client_name_lines=("Hôtel Nord Pinus Tanger",),
        number="1240 - 2026",
        day="13/08/2026",
    ),
]


def sent_boxes(app: AppTest) -> list:
    """Cases « Envoyé » actuellement affichées, une par ligne du tableau."""
    return [box for box in app.checkbox if box.label == SENT_LABEL]


@pytest.fixture(autouse=True)
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Chaque test dispose de sa propre base et de caches Streamlit vierges."""
    import streamlit as st

    monkeypatch.setenv("FACTURES_DB_PATH", str(tmp_path / "test.db"))
    st.cache_resource.clear()
    st.cache_data.clear()
    yield
    st.cache_resource.clear()
    st.cache_data.clear()


@pytest.fixture
def seeded() -> AppTest:
    """Application chargée avec un lot de 4 factures réparties sur 2 mois."""
    payload = make_pdf(SPECS)
    report = extract_invoices(payload, source_name="lot.pdf", fallback_period="2026-07")
    assert report.ok

    app = AppTest.from_file(APP_PATH, default_timeout=60)
    app.session_state["sources"] = {"lot.pdf": payload}
    app.session_state["report"] = report
    return app.run()


class TestEmptyState:
    def test_start_screen_invites_an_import(self) -> None:
        app = AppTest.from_file(APP_PATH, default_timeout=60).run()
        assert not app.exception
        assert "importez vos PDF" in app.info[0].value

    def test_sidebar_offers_month_year_and_upload(self) -> None:
        app = AppTest.from_file(APP_PATH, default_timeout=60).run()
        assert app.sidebar.selectbox[0].label == "Mois"
        assert app.sidebar.number_input[0].label == "Année"
        assert any(b.label == "Traiter les factures" for b in app.sidebar.button)

    def test_processing_without_files_reports_an_error(self) -> None:
        app = AppTest.from_file(APP_PATH, default_timeout=60).run()
        next(b for b in app.sidebar.button if b.label == "Traiter les factures").click()
        app.run()
        assert "Aucun fichier" in app.sidebar.error[0].value


class TestLoadedState:
    def test_no_exception_is_raised(self, seeded: AppTest) -> None:
        assert not seeded.exception

    def test_metrics_summarise_the_batch(self, seeded: AppTest) -> None:
        values = [metric.value for metric in seeded.metric]
        assert values[0] == "4"  # factures
        assert values[1] == "3"  # clients distincts
        assert values[3] == "0"  # anomalies

    def test_every_invoice_has_a_sent_checkbox(self, seeded: AppTest) -> None:
        boxes = sent_boxes(seeded)
        assert len(boxes) == 4
        assert all(box.value is False for box in boxes)

    def test_every_invoice_has_a_download_button(self, seeded: AppTest) -> None:
        downloads = [
            widget
            for widget in seeded.get("download_button")
            if widget.label == "⬇️ Télécharger"
        ]
        assert len(downloads) == 4


class TestSearchAndFilters:
    def test_search_narrows_the_table(self, seeded: AppTest) -> None:
        seeded.text_input(key="search_query").set_value("velvetrav").run()
        assert len(sent_boxes(seeded)) == 2

    def test_search_ignores_accents_and_case(self, seeded: AppTest) -> None:
        seeded.text_input(key="search_query").set_value("hotel NORD").run()
        assert len(sent_boxes(seeded)) == 1

    def test_search_by_invoice_number(self, seeded: AppTest) -> None:
        seeded.text_input(key="search_query").set_value("1175").run()
        assert len(sent_boxes(seeded)) == 1

    def test_search_terms_are_combined(self, seeded: AppTest) -> None:
        seeded.text_input(key="search_query").set_value("velvetrav 1145").run()
        assert len(sent_boxes(seeded)) == 1

    def test_unmatched_search_warns(self, seeded: AppTest) -> None:
        seeded.text_input(key="search_query").set_value("client inexistant").run()
        assert "Aucune facture" in seeded.warning[0].value

    def test_month_filter_lists_the_periods_present_in_french(
        self, seeded: AppTest
    ) -> None:
        assert seeded.selectbox(key="filter_period").options == [
            "Tous les mois",
            "août 2026",
            "juillet 2026",
        ]

    def test_month_filter_selects_july_only(self, seeded: AppTest) -> None:
        seeded.selectbox(key="filter_period").set_value("2026-07").run()
        assert len(sent_boxes(seeded)) == 3

    def test_month_filter_selects_august_only(self, seeded: AppTest) -> None:
        seeded.selectbox(key="filter_period").set_value("2026-08").run()
        assert len(sent_boxes(seeded)) == 1

    def test_search_and_month_filter_combine(self, seeded: AppTest) -> None:
        seeded.text_input(key="search_query").set_value("velvetrav").run()
        seeded.selectbox(key="filter_period").set_value("2026-08").run()
        assert "Aucune facture" in seeded.warning[0].value


class TestSentStatus:
    def test_checking_a_box_is_persisted(self, seeded: AppTest) -> None:
        target = sent_boxes(seeded)[0]
        target.check().run()

        key = target.key.removeprefix("sent_")
        with SentStatusStore() as store:
            assert store.get_many([key])[key] is True

    def test_unchecking_is_persisted(self, seeded: AppTest) -> None:
        target = sent_boxes(seeded)[0]
        target.check().run()
        sent_boxes(seeded)[0].uncheck().run()

        key = target.key.removeprefix("sent_")
        with SentStatusStore() as store:
            assert store.get_many([key])[key] is False

    def test_status_survives_a_rerun(self, seeded: AppTest) -> None:
        sent_boxes(seeded)[0].check().run()
        seeded.run()
        assert len([box for box in sent_boxes(seeded) if box.value]) == 1

    def test_sent_filter_isolates_sent_invoices(self, seeded: AppTest) -> None:
        sent_boxes(seeded)[0].check().run()
        seeded.selectbox(key="filter_sent").set_value("Envoyées").run()
        assert len(sent_boxes(seeded)) == 1

    def test_sent_filter_isolates_pending_invoices(self, seeded: AppTest) -> None:
        sent_boxes(seeded)[0].check().run()
        seeded.selectbox(key="filter_sent").set_value("Non envoyées").run()
        assert len(sent_boxes(seeded)) == 3


class TestBulkExport:
    def _prepare(self, app: AppTest, period: str) -> AppTest:
        app.selectbox(key="zip_period").set_value(period).run()
        next(b for b in app.button if b.label == "Préparer l'archive ZIP").click()
        return app.run()

    def test_archive_covers_every_month_by_default(self, seeded: AppTest) -> None:
        payload = self._prepare(seeded, "Tous les mois").session_state["zip_payload"]
        assert payload["count"] == 4
        assert payload["name"] == "factures-toutes-periodes.zip"
        assert not payload["failures"]

    def test_archive_can_be_limited_to_one_month(self, seeded: AppTest) -> None:
        payload = self._prepare(seeded, "2026-07").session_state["zip_payload"]
        assert payload["count"] == 3
        assert payload["name"] == "factures-2026-07.zip"

    def test_archive_groups_repeat_clients_in_a_folder(self, seeded: AppTest) -> None:
        data = self._prepare(seeded, "Tous les mois").session_state["zip_payload"]["data"]
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert set(archive.namelist()) == {
                "DRAKE.pdf",
                "Hôtel Nord Pinus Tanger.pdf",
                "VELVETRAV/VELVETRAV - 1145-2026.pdf",
                "VELVETRAV/VELVETRAV - 1175-2026.pdf",
            }

    def test_archive_ignores_the_table_filters(self, seeded: AppTest) -> None:
        """L'export global porte sur tout le lot, pas sur la recherche en cours."""
        seeded.text_input(key="search_query").set_value("drake").run()
        payload = self._prepare(seeded, "Tous les mois").session_state["zip_payload"]
        assert payload["count"] == 4


class TestNameCorrection:
    def test_correcting_a_name_updates_the_export(self, seeded: AppTest) -> None:
        assert seeded.text_input(key="fix_name").label == "Nom du client"
        seeded.text_input(key="fix_name").set_value("DRAKE CORRIGE").run()
        next(b for b in seeded.button if b.label == "Enregistrer la correction").click()
        seeded.run()

        assert "DRAKE CORRIGE" in seeded.session_state["name_overrides"].values()

        data = TestBulkExport()._prepare(seeded, "Tous les mois")
        names = data.session_state["zip_payload"]["data"]
        with zipfile.ZipFile(io.BytesIO(names)) as archive:
            assert "DRAKE CORRIGE.pdf" in archive.namelist()

    def test_an_empty_name_is_refused(self, seeded: AppTest) -> None:
        seeded.text_input(key="fix_name").set_value("   ").run()
        next(b for b in seeded.button if b.label == "Enregistrer la correction").click()
        seeded.run()
        assert any("ne peut pas être vide" in err.value for err in seeded.error)

    def test_corrections_can_be_reverted(self, seeded: AppTest) -> None:
        seeded.text_input(key="fix_name").set_value("AUTRE NOM").run()
        next(b for b in seeded.button if b.label == "Enregistrer la correction").click()
        seeded.run()
        next(
            b for b in seeded.button if b.label == "Annuler toutes les corrections"
        ).click()
        seeded.run()
        assert seeded.session_state["name_overrides"] == {}

    def test_switching_invoice_reloads_the_current_name(self, seeded: AppTest) -> None:
        picker = seeded.selectbox(key="fix_invoice")
        first_name = seeded.text_input(key="fix_name").value
        picker.set_value(picker.options[-1]).run()
        assert seeded.text_input(key="fix_name").value != first_name


class TestStorageStatus:
    def test_sqlite_deployment_is_flagged_as_non_durable(
        self, seeded: AppTest
    ) -> None:
        """L'équipe doit voir que les cases cochées ne survivront pas."""
        warnings = [w.value for w in seeded.sidebar.warning]
        assert any("perdues au prochain redéploiement" in w for w in warnings)

    def test_the_backend_in_use_is_displayed(self, seeded: AppTest) -> None:
        captions = [c.value for c in seeded.sidebar.caption]
        assert any("Suivi des envois : SQLite" in c for c in captions)


class TestBatchScopedState:
    def test_clearing_the_session_also_clears_the_filters(
        self, seeded: AppTest
    ) -> None:
        """« Vider la session » ne doit laisser aucun filtre du lot précédent."""
        seeded.text_input(key="search_query").set_value("velvetrav").run()
        seeded.selectbox(key="filter_period").set_value("2026-07").run()

        next(b for b in seeded.sidebar.button if b.label == "Vider la session").click()
        seeded.run()

        assert not seeded.exception
        assert seeded.session_state["report"].invoices == ()
        assert "importez vos PDF" in seeded.info[0].value
        assert "search_query" not in seeded.session_state

    def test_a_stale_month_filter_survives_a_batch_change(
        self, seeded: AppTest
    ) -> None:
        """Un mois filtré qui disparaît du lot ne doit pas casser l'affichage."""
        seeded.selectbox(key="filter_period").set_value("2026-07").run()

        august = make_pdf(
            [InvoiceSpec(client_name_lines=("AOUT SARL",), day="31/08/2026")]
        )
        seeded.session_state["sources"] = {"aout.pdf": august}
        seeded.session_state["report"] = extract_invoices(
            august, source_name="aout.pdf"
        )
        seeded.run()

        assert not seeded.exception
        # Streamlit retombe sur la première option : tout le lot reste visible.
        assert len(sent_boxes(seeded)) == 1
