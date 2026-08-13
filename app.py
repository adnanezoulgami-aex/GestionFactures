"""Gestion des factures — application Streamlit.

Ce module ne contient que la couche de présentation : import des PDF, filtres,
tableau de suivi et téléchargements. Toute la logique métier vit dans le paquet
`factures` (`src/factures/`) et est couverte par les tests.

Lancement :

    streamlit run app.py
"""

from __future__ import annotations

import logging
import sys
import unicodedata
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import streamlit as st

# Rend le paquet importable sans installation préalable (utile sur Streamlit Cloud).
_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from factures.backend import SentStatusBackend, create_store  # noqa: E402
from factures.extraction import extract_invoices  # noqa: E402
from factures.models import (  # noqa: E402
    MOIS_FR,
    ExtractionReport,
    Invoice,
    periode_label,
)
from factures.naming import sanitize_filename  # noqa: E402
from factures.packaging import (  # noqa: E402
    PackagingError,
    build_invoice_pdf,
    build_zip_archive,
    invoice_display_name,
)

logging.basicConfig(level=logging.INFO)

APP_TITLE = "Gestion des factures"
PAGE_SIZES = (25, 50, 100, 250)
ALL_PERIODS = "Tous les mois"

#: Widgets dont la valeur mémorisée dépend du lot chargé. Ils sont purgés à
#: chaque import pour que le nouveau lot s'affiche entier : sans cela, une
#: recherche restée en place masquerait silencieusement des factures que
#: l'utilisateur vient d'importer.
BATCH_SCOPED_KEYS = (
    "search_query",
    "filter_period",
    "filter_sent",
    "zip_period",
    "fix_invoice",
    "fix_name",
)

st.set_page_config(page_title=APP_TITLE, page_icon="🧾", layout="wide")


# --------------------------------------------------------------------------- #
# Ressources partagées
# --------------------------------------------------------------------------- #


@st.cache_resource(show_spinner=False)
def get_store() -> SentStatusBackend:
    """Base de suivi des envois, partagée par toutes les sessions du serveur.

    Postgres si une chaîne de connexion est configurée, SQLite sinon. Une
    défaillance n'est pas masquée : elle est renvoyée pour que l'interface
    l'affiche, plutôt que de laisser croire que les statuts sont enregistrés.
    """
    return create_store()


def resolve_store() -> tuple[SentStatusBackend | None, str | None]:
    """Magasin de statuts, ou le message d'erreur à afficher.

    Une base injoignable ne doit pas empêcher d'extraire et de télécharger les
    factures : seul le suivi des envois est indisponible.
    """
    try:
        return get_store(), None
    except Exception as exc:
        logging.exception("Connexion au stockage impossible")
        return None, str(exc)


def render_storage_status(store: SentStatusBackend | None, error: str | None) -> None:
    """Informe sur la durabilité du suivi, sans jamais dévoiler de secret."""
    if error is not None:
        st.sidebar.error(
            "Base de suivi injoignable : les cases « Envoyé » ne seront pas "
            f"enregistrées. Les factures restent téléchargeables.\n\n`{error}`",
            icon="⛔",
        )
        return
    if store is None:
        return
    if store.is_persistent:
        st.sidebar.caption(f"Suivi des envois : {store.description}")
    else:
        st.sidebar.warning(
            "Stockage non persistant : les cases « Envoyé » seront perdues au "
            "redémarrage du serveur.",
            icon="⚠️",
        )


@st.cache_data(show_spinner=False, max_entries=8)
def cached_extract(
    pdf_bytes: bytes, source_name: str, fallback_period: str
) -> ExtractionReport:
    """Extraction mise en cache : réimporter le même PDF est instantané."""
    return extract_invoices(
        pdf_bytes, source_name=source_name, fallback_period=fallback_period
    )


@st.cache_data(show_spinner=False, max_entries=512)
def cached_invoice_pdf(
    _invoice: Invoice, _sources: dict[str, bytes], cache_key: str
) -> bytes:
    """PDF d'une facture, mémorisé pour éviter de le rebâtir à chaque rerun.

    Les paramètres préfixés d'un souligné sont exclus du hachage par Streamlit :
    `cache_key` (la clé stable de la facture) suffit à identifier le résultat,
    qui ne dépend ni du nom affiché ni des corrections manuelles.
    """
    return build_invoice_pdf(_invoice, _sources)


def init_state() -> None:
    st.session_state.setdefault("sources", {})
    st.session_state.setdefault("report", ExtractionReport())
    st.session_state.setdefault("name_overrides", {})
    st.session_state.setdefault("page_index", 0)
    st.session_state.setdefault("zip_payload", None)


# --------------------------------------------------------------------------- #
# Utilitaires de présentation
# --------------------------------------------------------------------------- #


def normalize(text: str) -> str:
    """Minuscule sans accent, pour une recherche tolérante (« hotel » ≡ « hôtel »)."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def format_amount(value: Decimal | None) -> str:
    if value is None:
        return "—"
    formatted = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{formatted} DH"


def format_date(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


def apply_overrides(invoices: tuple[Invoice, ...]) -> tuple[Invoice, ...]:
    """Applique les corrections manuelles de noms de clients."""
    overrides: dict[str, str] = st.session_state["name_overrides"]
    if not overrides:
        return invoices
    return tuple(
        replace(invoice, client_name=overrides[invoice.key])
        if invoice.key in overrides
        else invoice
        for invoice in invoices
    )


def filter_invoices(
    invoices: tuple[Invoice, ...],
    *,
    query: str,
    period: str,
    sent_filter: str,
    statuses: dict[str, bool],
) -> list[Invoice]:
    """Applique recherche plein texte, filtre mensuel et filtre d'envoi."""
    terms = [normalize(term) for term in query.split() if term.strip()]

    def matches(invoice: Invoice) -> bool:
        if period != ALL_PERIODS and invoice.period != period:
            return False
        if sent_filter == "Envoyées" and not statuses.get(invoice.key, False):
            return False
        if sent_filter == "Non envoyées" and statuses.get(invoice.key, False):
            return False
        if terms:
            blob = normalize(invoice.search_blob)
            if not all(term in blob for term in terms):
                return False
        return True

    return [invoice for invoice in invoices if matches(invoice)]


def on_toggle_sent(invoice: Invoice) -> None:
    """Callback de la case « Envoyé » : écrit immédiatement en base."""
    value = bool(st.session_state.get(f"sent_{invoice.key}", False))
    store, error = resolve_store()
    if store is None:
        st.toast(f"Statut non enregistré : {error}", icon="⚠️")
        return
    try:
        store.set_sent(
            invoice.key,
            value,
            client_name=invoice.client_name,
            period=invoice.period,
        )
    except Exception:  # pragma: no cover - coupure réseau ou disque
        logging.exception("Écriture du statut impossible: %s", invoice.key)
        st.toast("Statut non enregistré : base de données inaccessible.", icon="⚠️")


# --------------------------------------------------------------------------- #
# Barre latérale : import
# --------------------------------------------------------------------------- #


def render_sidebar() -> None:
    st.sidebar.header("1. Période traitée")

    today = date.today()
    month_name = st.sidebar.selectbox(
        "Mois",
        options=MOIS_FR,
        index=today.month - 1,
        help=(
            "Sert de référence au lot importé. La période réelle de chaque facture "
            "reste celle de sa date d'émission ; ce mois n'est utilisé que si la "
            "date est illisible."
        ),
    )
    year = st.sidebar.number_input(
        "Année", min_value=2000, max_value=2100, value=today.year, step=1
    )
    fallback_period = f"{int(year):04d}-{MOIS_FR.index(month_name) + 1:02d}"

    st.sidebar.header("2. Factures")
    uploads = st.sidebar.file_uploader(
        "Fichiers PDF",
        type=["pdf"],
        accept_multiple_files=True,
        help="Un PDF peut contenir plusieurs centaines de factures.",
    )

    if st.sidebar.button(
        "Traiter les factures", type="primary", use_container_width=True
    ):
        process_uploads(uploads or [], fallback_period)

    if st.session_state["sources"]:
        st.sidebar.divider()
        st.sidebar.caption("Fichiers chargés")
        for name in st.session_state["sources"]:
            st.sidebar.write(f"• {name}")
        if st.sidebar.button("Vider la session", use_container_width=True):
            reset_session()

    st.sidebar.divider()
    render_storage_status(*resolve_store())


def clear_batch_widgets() -> None:
    """Oublie les valeurs de widgets liées au lot précédent."""
    stale = [key for key in st.session_state if key.startswith("sent_")]
    for key in (*stale, *BATCH_SCOPED_KEYS):
        st.session_state.pop(key, None)


def reset_session() -> None:
    clear_batch_widgets()
    st.session_state["sources"] = {}
    st.session_state["report"] = ExtractionReport()
    st.session_state["name_overrides"] = {}
    st.session_state["page_index"] = 0
    st.session_state["zip_payload"] = None
    st.rerun()


def process_uploads(uploads: list, fallback_period: str) -> None:
    if not uploads:
        st.sidebar.error("Aucun fichier sélectionné.")
        return

    sources: dict[str, bytes] = {}
    report = ExtractionReport()
    progress = st.sidebar.progress(0.0, text="Lecture des PDF…")

    for index, upload in enumerate(uploads, start=1):
        payload = upload.getvalue()
        if not payload:
            st.sidebar.error(f"« {upload.name} » est vide.")
            continue
        sources[upload.name] = payload
        progress.progress(
            index / len(uploads), text=f"Analyse de « {upload.name} »…"
        )
        report = report.merged_with(
            cached_extract(payload, upload.name, fallback_period)
        )

    progress.empty()
    clear_batch_widgets()
    st.session_state["sources"] = sources
    st.session_state["report"] = report
    st.session_state["name_overrides"] = {}
    st.session_state["page_index"] = 0
    st.session_state["zip_payload"] = None
    st.rerun()


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def render_diagnostics(report: ExtractionReport, invoices: tuple[Invoice, ...]) -> None:
    total_ttc = sum(
        (inv.total_ttc for inv in invoices if inv.total_ttc is not None), Decimal(0)
    )
    columns = st.columns(4)
    columns[0].metric("Factures", len(invoices))
    columns[1].metric("Clients", len({inv.client_name.casefold() for inv in invoices}))
    columns[2].metric("Total TTC", format_amount(total_ttc))
    columns[3].metric("Anomalies", len(report.errors))

    warned = [inv for inv in invoices if inv.warnings]
    if report.errors:
        with st.expander(f"⛔ {len(report.errors)} page(s) non exploitée(s)", expanded=True):
            for error in report.errors:
                st.write(f"**{error.source_name}** — page {error.page} : {error.reason}")
    if warned:
        with st.expander(f"⚠️ {len(warned)} facture(s) à vérifier"):
            for invoice in warned:
                st.write(
                    f"**{invoice.client_name}** "
                    f"(p.{invoice.first_page}, {invoice.source_name}) — "
                    + " ".join(invoice.warnings)
                )


def render_name_corrections(invoices: tuple[Invoice, ...]) -> None:
    """Permet de rectifier un nom de client mal découpé avant export."""
    with st.expander("✏️ Corriger un nom de client"):
        if not invoices:
            st.info("Aucune facture chargée.")
            return

        # Les options sont les clés de facture, et non les objets : une facture
        # renommée devient un nouvel objet, ce qui invaliderait la sélection
        # mémorisée. La clé, elle, ne change jamais.
        by_key = {invoice.key: invoice for invoice in invoices}
        options = sorted(
            by_key,
            key=lambda key: (
                normalize(by_key[key].client_name),
                by_key[key].first_page,
            ),
        )

        def describe(key: str) -> str:
            invoice = by_key[key]
            return (
                f"{invoice.client_name} — n° {invoice.invoice_number or '?'} "
                f"(p.{invoice.first_page})"
            )

        selected_key = st.selectbox(
            "Facture",
            options=options,
            format_func=describe,
            key="fix_invoice",
            # Changer de facture doit réamorcer le champ avec son nom courant.
            on_change=lambda: st.session_state.pop("fix_name", None),
        )
        selected = by_key[selected_key]
        new_name = st.text_input(
            "Nom du client", value=selected.client_name, key="fix_name"
        )

        left, right = st.columns(2)
        if left.button("Enregistrer la correction", type="primary"):
            cleaned = new_name.strip()
            if not cleaned:
                st.error("Le nom du client ne peut pas être vide.")
            else:
                st.session_state["name_overrides"][selected.key] = cleaned
                st.session_state["zip_payload"] = None
                st.session_state.pop("fix_name", None)
                st.rerun()
        if right.button("Annuler toutes les corrections"):
            st.session_state["name_overrides"] = {}
            st.session_state["zip_payload"] = None
            st.session_state.pop("fix_name", None)
            st.rerun()


# --------------------------------------------------------------------------- #
# Tableau de suivi
# --------------------------------------------------------------------------- #

_COLUMN_WIDTHS = (0.7, 3.4, 1.3, 1.2, 1.5, 1.4, 1.5)


def render_table(rows: list[Invoice], statuses: dict[str, bool]) -> None:
    header = st.columns(_COLUMN_WIDTHS)
    for column, label in zip(
        header,
        ("Envoyé", "Client", "N° facture", "Date", "Mois", "Total TTC", "PDF"),
        strict=True,
    ):
        column.markdown(f"**{label}**")
    st.divider()

    sources = st.session_state["sources"]
    for invoice in rows:
        columns = st.columns(_COLUMN_WIDTHS)

        columns[0].checkbox(
            "Envoyé au client",
            value=statuses.get(invoice.key, False),
            key=f"sent_{invoice.key}",
            on_change=on_toggle_sent,
            args=(invoice,),
            label_visibility="collapsed",
        )
        columns[1].write(invoice.client_name)
        columns[2].write(invoice.invoice_number or "—")
        columns[3].write(format_date(invoice.invoice_date))
        columns[4].write(invoice.period_label)
        columns[5].write(format_amount(invoice.total_ttc))

        try:
            payload = cached_invoice_pdf(invoice, sources, invoice.key)
        except PackagingError as exc:
            columns[6].error("Indisponible", icon="⛔")
            logging.warning("Découpe impossible pour %s: %s", invoice.key, exc)
            continue

        columns[6].download_button(
            "⬇️ Télécharger",
            data=payload,
            file_name=invoice_display_name(invoice),
            mime="application/pdf",
            key=f"dl_{invoice.key}",
            use_container_width=True,
        )


def render_pagination(total: int, page_size: int) -> tuple[int, int]:
    """Affiche le sélecteur de page et retourne la tranche à afficher."""
    page_count = max(1, -(-total // page_size))
    current = min(st.session_state["page_index"], page_count - 1)

    if page_count > 1:
        left, middle, right = st.columns([1, 2, 1])
        if left.button("◀ Précédent", disabled=current == 0, use_container_width=True):
            st.session_state["page_index"] = current - 1
            st.rerun()
        middle.markdown(
            f"<div style='text-align:center'>Page {current + 1} / {page_count}</div>",
            unsafe_allow_html=True,
        )
        if right.button(
            "Suivant ▶", disabled=current >= page_count - 1, use_container_width=True
        ):
            st.session_state["page_index"] = current + 1
            st.rerun()

    st.session_state["page_index"] = current
    start = current * page_size
    return start, min(start + page_size, total)


# --------------------------------------------------------------------------- #
# Export global
# --------------------------------------------------------------------------- #


def render_bulk_download(invoices: tuple[Invoice, ...], periods: list[str]) -> None:
    st.divider()
    st.subheader("📦 Télécharger toutes les factures")
    st.caption(
        "Chaque facture est renommée avec le nom du client. Un client ayant "
        "plusieurs factures obtient un dossier à son nom."
    )

    left, right = st.columns([2, 1])
    choice = left.selectbox(
        "Mois à exporter",
        options=[ALL_PERIODS, *periods],
        format_func=lambda value: value
        if value == ALL_PERIODS
        else periode_label(value),
        key="zip_period",
    )

    selection = [
        invoice
        for invoice in invoices
        if choice == ALL_PERIODS or invoice.period == choice
    ]
    right.metric("Factures sélectionnées", len(selection))

    if st.button(
        "Préparer l'archive ZIP",
        type="primary",
        disabled=not selection,
        use_container_width=True,
    ):
        bar = st.progress(0.0, text="Assemblage de l'archive…")

        def report_progress(done: int, total: int) -> None:
            bar.progress(done / total, text=f"Facture {done} / {total}…")

        payload, failures = build_zip_archive(
            selection, st.session_state["sources"], progress=report_progress
        )
        bar.empty()
        label = "toutes-periodes" if choice == ALL_PERIODS else choice
        st.session_state["zip_payload"] = {
            "data": payload,
            "name": f"factures-{sanitize_filename(label)}.zip",
            "count": len(selection) - len(failures),
            "failures": failures,
        }

    payload = st.session_state.get("zip_payload")
    if payload:
        for failure in payload["failures"]:
            st.error(failure)
        st.download_button(
            f"⬇️ Télécharger l'archive ({payload['count']} factures)",
            data=payload["data"],
            file_name=payload["name"],
            mime="application/zip",
            type="primary",
            use_container_width=True,
        )


# --------------------------------------------------------------------------- #
# Point d'entrée
# --------------------------------------------------------------------------- #


def main() -> None:
    init_state()
    st.title(f"🧾 {APP_TITLE}")

    render_sidebar()

    report: ExtractionReport = st.session_state["report"]
    invoices = apply_overrides(report.invoices)

    if not invoices and not report.errors:
        st.info(
            "Sélectionnez le mois puis importez vos PDF de factures dans le "
            "panneau de gauche, et cliquez sur **Traiter les factures**.",
            icon="👈",
        )
        return

    render_diagnostics(report, invoices)
    render_name_corrections(invoices)

    store, _ = resolve_store()
    try:
        statuses = (
            store.get_many(invoice.key for invoice in invoices) if store else {}
        )
    except Exception:  # pragma: no cover - coupure réseau
        logging.exception("Lecture des statuts impossible")
        statuses = {}
    periods = sorted({invoice.period for invoice in invoices}, reverse=True)

    st.divider()
    st.subheader("🔎 Rechercher")
    search_col, period_col, sent_col, size_col = st.columns([3, 1.6, 1.4, 1])
    query = search_col.text_input(
        "Recherche",
        placeholder="Nom du client, n° de facture ou ICE…",
        label_visibility="collapsed",
        key="search_query",
    )
    period = period_col.selectbox(
        "Mois",
        options=[ALL_PERIODS, *periods],
        format_func=lambda value: value
        if value == ALL_PERIODS
        else periode_label(value),
        label_visibility="collapsed",
        key="filter_period",
    )
    sent_filter = sent_col.selectbox(
        "Envoi",
        options=("Toutes", "Envoyées", "Non envoyées"),
        label_visibility="collapsed",
        key="filter_sent",
    )
    page_size = size_col.selectbox(
        "Par page", options=PAGE_SIZES, label_visibility="collapsed", key="page_size"
    )

    rows = filter_invoices(
        invoices,
        query=query,
        period=period,
        sent_filter=sent_filter,
        statuses=statuses,
    )

    sent_count = sum(1 for invoice in rows if statuses.get(invoice.key, False))
    st.caption(f"{len(rows)} facture(s) — {sent_count} envoyée(s).")

    if rows:
        rows.sort(key=lambda inv: (normalize(inv.client_name), inv.first_page))
        start, end = render_pagination(len(rows), page_size)
        render_table(rows[start:end], statuses)
    else:
        st.warning("Aucune facture ne correspond à ces critères.")

    render_bulk_download(invoices, periods)


if __name__ == "__main__":
    main()
