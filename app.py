"""Gestion des factures et des extraits de compte — application Streamlit.

Ce module ne contient que la couche de présentation : import des PDF, filtres,
tableau de suivi et téléchargements. Toute la logique métier vit dans le paquet
`factures` (`src/factures/`) et est couverte par les tests.

Deux types de documents sont traités, avec le même parcours : découpage d'un
PDF groupé, renommage au nom du client, recherche, suivi des envois et export.
Ce qui les distingue — le gabarit lu et les colonnes affichées — est rassemblé
dans `DOCUMENT_KINDS`, afin qu'ajouter un troisième type ne demande pas de
toucher au reste de l'interface.

Lancement :

    streamlit run app.py
"""

from __future__ import annotations

import logging
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
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
from factures.ledger import extract_ledger_accounts  # noqa: E402
from factures.models import (  # noqa: E402
    MOIS_FR,
    ExtractionReport,
    PagedDocument,
    periode_label,
)
from factures.naming import sanitize_filename  # noqa: E402
from factures.packaging import (  # noqa: E402
    PackagingError,
    build_document_pdf,
    build_zip_archive,
    document_display_name,
)

logging.basicConfig(level=logging.INFO)

APP_TITLE = "Gestion des documents clients"
PAGE_SIZES = (25, 50, 100, 250)

#: Valeur sentinelle du filtre de période. Son libellé dépend du type traité,
#: d'où une valeur interne distincte de tout affichage.
ALL_PERIODS = "__toutes_periodes__"

#: Widgets dont la valeur mémorisée dépend du lot chargé. Ils sont purgés à
#: chaque import pour que le nouveau lot s'affiche entier : sans cela, une
#: recherche restée en place masquerait silencieusement des documents que
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
# Mise en forme
# --------------------------------------------------------------------------- #


def format_amount(value: Decimal | None) -> str:
    if value is None:
        return "—"
    formatted = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{formatted} DH"


def format_date(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


# --------------------------------------------------------------------------- #
# Types de documents
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Column:
    """Une colonne du tableau de suivi."""

    label: str
    width: float
    value: Callable[[PagedDocument], str]


@dataclass(frozen=True)
class DocumentKind:
    """Tout ce qui distingue un type de document dans l'interface."""

    key: str
    label: str
    """Libellé du sélecteur, par exemple « Factures »."""
    singular: str
    plural: str
    upload_header: str
    upload_help: str
    empty_hint: str
    search_placeholder: str
    all_periods_label: str
    period_filter_label: str
    zip_prefix: str
    columns: tuple[Column, ...]
    amount_label: str
    """Libellé de la troisième carte de synthèse."""
    amount_of: Callable[[PagedDocument], Decimal | None]
    needs_period_input: bool
    """Le type a-t-il besoin d'une période saisie par l'utilisateur ?"""


INVOICE_KIND = DocumentKind(
    key="factures",
    label="Factures",
    singular="facture",
    plural="factures",
    upload_header="Factures",
    upload_help="Un PDF peut contenir plusieurs centaines de factures.",
    empty_hint=(
        "Sélectionnez le mois puis importez vos PDF de factures dans le "
        "panneau de gauche, et cliquez sur **Traiter les documents**."
    ),
    search_placeholder="Nom du client, n° de facture ou ICE…",
    all_periods_label="Tous les mois",
    period_filter_label="Mois",
    zip_prefix="factures",
    columns=(
        Column("Client", 3.4, lambda doc: doc.client_name),
        Column("N° facture", 1.3, lambda doc: doc.reference or "—"),
        Column("Date", 1.2, lambda doc: format_date(doc.invoice_date)),
        Column("Mois", 1.5, lambda doc: doc.period_label),
        Column("Total TTC", 1.4, lambda doc: format_amount(doc.total_ttc)),
    ),
    amount_label="Total TTC",
    amount_of=lambda doc: doc.total_ttc,
    needs_period_input=True,
)

LEDGER_KIND = DocumentKind(
    key="extraits",
    label="Extraits de compte",
    singular="extrait de compte",
    plural="extraits de compte",
    upload_header="Grand livre",
    upload_help="Un PDF de grand livre contenant les comptes les uns à la suite.",
    empty_hint=(
        "Importez votre PDF de grand livre dans le panneau de gauche, "
        "et cliquez sur **Traiter les documents**."
    ),
    search_placeholder="Nom du client ou code de compte…",
    all_periods_label="Toutes les périodes",
    period_filter_label="Période",
    zip_prefix="extraits-de-compte",
    columns=(
        Column("Client", 3.2, lambda doc: doc.client_name),
        Column("Compte", 1.3, lambda doc: doc.reference or "—"),
        Column("Période", 1.9, lambda doc: doc.period_label),
        Column("Débit", 1.3, lambda doc: format_amount(doc.total_debit)),
        Column("Crédit", 1.3, lambda doc: format_amount(doc.total_credit)),
        Column("Solde", 1.4, lambda doc: format_amount(doc.balance)),
    ),
    amount_label="Solde total",
    amount_of=lambda doc: doc.balance,
    needs_period_input=False,
)

DOCUMENT_KINDS: dict[str, DocumentKind] = {
    INVOICE_KIND.key: INVOICE_KIND,
    LEDGER_KIND.key: LEDGER_KIND,
}


def current_kind() -> DocumentKind:
    """Type de document du lot actuellement chargé."""
    return DOCUMENT_KINDS[st.session_state.get("kind", INVOICE_KIND.key)]


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
    documents : seul le suivi des envois est indisponible.
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
            f"enregistrées. Les documents restent téléchargeables.\n\n`{error}`",
            icon="⛔",
        )
        return
    if store is None:
        return
    st.sidebar.caption(f"Suivi des envois : {store.description}")
    warning = store.durability_warning
    if warning:
        st.sidebar.warning(warning, icon="⚠️")


@st.cache_data(show_spinner=False, max_entries=8)
def cached_extract(
    pdf_bytes: bytes, source_name: str, kind_key: str, fallback_period: str
) -> ExtractionReport:
    """Extraction mise en cache : réimporter le même PDF est instantané."""
    if kind_key == LEDGER_KIND.key:
        return extract_ledger_accounts(pdf_bytes, source_name=source_name)
    return extract_invoices(
        pdf_bytes, source_name=source_name, fallback_period=fallback_period
    )


@st.cache_data(show_spinner=False, max_entries=512)
def cached_document_pdf(
    _document: PagedDocument, _sources: dict[str, bytes], cache_key: str
) -> bytes:
    """PDF d'un document, mémorisé pour éviter de le rebâtir à chaque rerun.

    Les paramètres préfixés d'un souligné sont exclus du hachage par Streamlit :
    `cache_key` (la clé stable du document) suffit à identifier le résultat, qui
    ne dépend ni du nom affiché ni des corrections manuelles.
    """
    return build_document_pdf(_document, _sources)


def init_state() -> None:
    st.session_state.setdefault("sources", {})
    st.session_state.setdefault("report", ExtractionReport())
    st.session_state.setdefault("name_overrides", {})
    st.session_state.setdefault("page_index", 0)
    st.session_state.setdefault("zip_payload", None)
    st.session_state.setdefault("kind", INVOICE_KIND.key)


# --------------------------------------------------------------------------- #
# Utilitaires de présentation
# --------------------------------------------------------------------------- #


def normalize(text: str) -> str:
    """Minuscule sans accent, pour une recherche tolérante (« hotel » ≡ « hôtel »)."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def apply_overrides(documents: tuple[PagedDocument, ...]) -> tuple[PagedDocument, ...]:
    """Applique les corrections manuelles de noms de clients."""
    overrides: dict[str, str] = st.session_state["name_overrides"]
    if not overrides:
        return documents
    return tuple(
        replace(document, client_name=overrides[document.key])
        if document.key in overrides
        else document
        for document in documents
    )


def filter_documents(
    documents: tuple[PagedDocument, ...],
    *,
    query: str,
    period: str,
    sent_filter: str,
    statuses: dict[str, bool],
) -> list[PagedDocument]:
    """Applique recherche plein texte, filtre de période et filtre d'envoi."""
    terms = [normalize(term) for term in query.split() if term.strip()]

    def matches(document: PagedDocument) -> bool:
        if period != ALL_PERIODS and document.period != period:
            return False
        if sent_filter == "Envoyés" and not statuses.get(document.key, False):
            return False
        if sent_filter == "Non envoyés" and statuses.get(document.key, False):
            return False
        if terms:
            blob = normalize(document.search_blob)
            if not all(term in blob for term in terms):
                return False
        return True

    return [document for document in documents if matches(document)]


def on_toggle_sent(document: PagedDocument) -> None:
    """Callback de la case « Envoyé » : écrit immédiatement en base."""
    value = bool(st.session_state.get(f"sent_{document.key}", False))
    store, error = resolve_store()
    if store is None:
        st.toast(f"Statut non enregistré : {error}", icon="⚠️")
        return
    try:
        store.set_sent(
            document.key,
            value,
            client_name=document.client_name,
            period=document.period,
        )
    except Exception:  # pragma: no cover - coupure réseau ou disque
        logging.exception("Écriture du statut impossible: %s", document.key)
        st.toast("Statut non enregistré : base de données inaccessible.", icon="⚠️")


# --------------------------------------------------------------------------- #
# Barre latérale : import
# --------------------------------------------------------------------------- #


def render_sidebar() -> None:
    st.sidebar.header("1. Type de document")
    kind_key = st.sidebar.radio(
        "Type de document",
        options=list(DOCUMENT_KINDS),
        format_func=lambda key: DOCUMENT_KINDS[key].label,
        key="kind_choice",
        label_visibility="collapsed",
    )
    kind = DOCUMENT_KINDS[kind_key]

    fallback_period = ""
    if kind.needs_period_input:
        st.sidebar.header("2. Période traitée")
        today = date.today()
        month_name = st.sidebar.selectbox(
            "Mois",
            options=MOIS_FR,
            index=today.month - 1,
            help=(
                "Sert de référence au lot importé. La période réelle de chaque "
                "facture reste celle de sa date d'émission ; ce mois n'est "
                "utilisé que si la date est illisible."
            ),
        )
        year = st.sidebar.number_input(
            "Année", min_value=2000, max_value=2100, value=today.year, step=1
        )
        fallback_period = f"{int(year):04d}-{MOIS_FR.index(month_name) + 1:02d}"
        upload_step = "3"
    else:
        st.sidebar.caption(
            "La période est lue dans l'en-tête du grand livre : rien à saisir."
        )
        upload_step = "2"

    st.sidebar.header(f"{upload_step}. {kind.upload_header}")
    uploads = st.sidebar.file_uploader(
        "Fichiers PDF",
        type=["pdf"],
        accept_multiple_files=True,
        help=kind.upload_help,
    )

    if st.sidebar.button(
        "Traiter les documents", type="primary", use_container_width=True
    ):
        process_uploads(uploads or [], kind, fallback_period)

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


def process_uploads(uploads: list, kind: DocumentKind, fallback_period: str) -> None:
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
        progress.progress(index / len(uploads), text=f"Analyse de « {upload.name} »…")
        report = report.merged_with(
            cached_extract(payload, upload.name, kind.key, fallback_period)
        )

    progress.empty()
    clear_batch_widgets()
    st.session_state["sources"] = sources
    st.session_state["report"] = report
    st.session_state["kind"] = kind.key
    st.session_state["name_overrides"] = {}
    st.session_state["page_index"] = 0
    st.session_state["zip_payload"] = None
    st.rerun()


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def render_diagnostics(
    report: ExtractionReport, documents: Sequence[PagedDocument], kind: DocumentKind
) -> None:
    total = sum(
        (
            amount
            for amount in (kind.amount_of(doc) for doc in documents)
            if amount is not None
        ),
        Decimal(0),
    )
    columns = st.columns(4)
    columns[0].metric(kind.label, len(documents))
    columns[1].metric("Clients", len({doc.client_name.casefold() for doc in documents}))
    columns[2].metric(kind.amount_label, format_amount(total))
    columns[3].metric("Anomalies", len(report.errors))

    warned = [doc for doc in documents if doc.warnings]
    if report.errors:
        with st.expander(
            f"⛔ {len(report.errors)} page(s) non exploitée(s)", expanded=True
        ):
            for error in report.errors:
                st.write(f"**{error.source_name}** — page {error.page} : {error.reason}")
    if warned:
        with st.expander(f"⚠️ {len(warned)} document(s) à vérifier"):
            for document in warned:
                st.write(
                    f"**{document.client_name}** "
                    f"(p.{document.first_page}, {document.source_name}) — "
                    + " ".join(document.warnings)
                )


def render_name_corrections(
    documents: Sequence[PagedDocument], kind: DocumentKind
) -> None:
    """Permet de rectifier un nom de client mal découpé avant export."""
    with st.expander("✏️ Corriger un nom de client"):
        if not documents:
            st.info("Aucun document chargé.")
            return

        # Les options sont les clés des documents, et non les objets : un
        # document renommé devient un nouvel objet, ce qui invaliderait la
        # sélection mémorisée. La clé, elle, ne change jamais.
        by_key = {document.key: document for document in documents}
        options = sorted(
            by_key,
            key=lambda key: (
                normalize(by_key[key].client_name),
                by_key[key].first_page,
            ),
        )

        def describe(key: str) -> str:
            document = by_key[key]
            return (
                f"{document.client_name} — {document.reference or '?'} "
                f"(p.{document.first_page})"
            )

        selected_key = st.selectbox(
            kind.singular.capitalize(),
            options=options,
            format_func=describe,
            key="fix_invoice",
            # Changer de document doit réamorcer le champ avec son nom courant.
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

CHECKBOX_WIDTH = 0.7
DOWNLOAD_WIDTH = 1.5


def column_widths(kind: DocumentKind) -> tuple[float, ...]:
    return (CHECKBOX_WIDTH, *(col.width for col in kind.columns), DOWNLOAD_WIDTH)


def render_table(
    rows: Sequence[PagedDocument], statuses: dict[str, bool], kind: DocumentKind
) -> None:
    widths = column_widths(kind)
    labels = ("Envoyé", *(col.label for col in kind.columns), "PDF")

    header = st.columns(widths)
    for column, label in zip(header, labels, strict=True):
        column.markdown(f"**{label}**")
    st.divider()

    sources = st.session_state["sources"]
    last = len(widths) - 1
    for document in rows:
        columns = st.columns(widths)

        columns[0].checkbox(
            "Envoyé au client",
            value=statuses.get(document.key, False),
            key=f"sent_{document.key}",
            on_change=on_toggle_sent,
            args=(document,),
            label_visibility="collapsed",
        )
        for offset, spec in enumerate(kind.columns, start=1):
            columns[offset].write(spec.value(document))

        try:
            payload = cached_document_pdf(document, sources, document.key)
        except PackagingError as exc:
            columns[last].error("Indisponible", icon="⛔")
            logging.warning("Découpe impossible pour %s: %s", document.key, exc)
            continue

        columns[last].download_button(
            "⬇️ Télécharger",
            data=payload,
            file_name=document_display_name(document),
            mime="application/pdf",
            key=f"dl_{document.key}",
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


def format_period(value: str, kind: DocumentKind) -> str:
    return kind.all_periods_label if value == ALL_PERIODS else periode_label(value)


def render_bulk_download(
    documents: Sequence[PagedDocument], periods: list[str], kind: DocumentKind
) -> None:
    st.divider()
    st.subheader(f"📦 Télécharger tous les {kind.plural}")
    st.caption(
        f"Chaque {kind.singular} est renommé avec le nom du client. Un client "
        f"ayant plusieurs {kind.plural} obtient un dossier à son nom."
    )

    left, right = st.columns([2, 1])
    choice = left.selectbox(
        f"{kind.period_filter_label} à exporter",
        options=[ALL_PERIODS, *periods],
        format_func=lambda value: format_period(value, kind),
        key="zip_period",
    )

    selection = [
        document
        for document in documents
        if choice == ALL_PERIODS or document.period == choice
    ]
    right.metric("Documents sélectionnés", len(selection))

    if st.button(
        "Préparer l'archive ZIP",
        type="primary",
        disabled=not selection,
        use_container_width=True,
    ):
        bar = st.progress(0.0, text="Assemblage de l'archive…")

        def report_progress(done: int, total: int) -> None:
            bar.progress(done / total, text=f"Document {done} / {total}…")

        payload, failures = build_zip_archive(
            selection, st.session_state["sources"], progress=report_progress
        )
        bar.empty()
        label = "toutes-periodes" if choice == ALL_PERIODS else choice
        st.session_state["zip_payload"] = {
            "data": payload,
            "name": f"{kind.zip_prefix}-{sanitize_filename(label)}.zip",
            "count": len(selection) - len(failures),
            "failures": failures,
        }

    payload = st.session_state.get("zip_payload")
    if payload:
        for failure in payload["failures"]:
            st.error(failure)
        st.download_button(
            f"⬇️ Télécharger l'archive ({payload['count']} documents)",
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
    documents = apply_overrides(report.documents)
    # Le type affiché est celui du lot chargé, et non celui sélectionné dans la
    # barre latérale : changer le sélecteur ne doit pas relire le lot précédent
    # avec le mauvais gabarit.
    kind = current_kind()

    if not documents and not report.errors:
        st.info(DOCUMENT_KINDS[st.session_state["kind_choice"]].empty_hint, icon="👈")
        return

    render_diagnostics(report, documents, kind)
    render_name_corrections(documents, kind)

    store, _ = resolve_store()
    try:
        statuses = store.get_many(doc.key for doc in documents) if store else {}
    except Exception:  # pragma: no cover - coupure réseau
        logging.exception("Lecture des statuts impossible")
        statuses = {}
    periods = sorted({doc.period for doc in documents}, reverse=True)

    st.divider()
    st.subheader("🔎 Rechercher")
    search_col, period_col, sent_col, size_col = st.columns([3, 1.6, 1.4, 1])
    query = search_col.text_input(
        "Recherche",
        placeholder=kind.search_placeholder,
        label_visibility="collapsed",
        key="search_query",
    )
    period = period_col.selectbox(
        kind.period_filter_label,
        options=[ALL_PERIODS, *periods],
        format_func=lambda value: format_period(value, kind),
        label_visibility="collapsed",
        key="filter_period",
    )
    sent_filter = sent_col.selectbox(
        "Envoi",
        options=("Tous", "Envoyés", "Non envoyés"),
        label_visibility="collapsed",
        key="filter_sent",
    )
    page_size = size_col.selectbox(
        "Par page", options=PAGE_SIZES, label_visibility="collapsed", key="page_size"
    )

    rows = filter_documents(
        documents,
        query=query,
        period=period,
        sent_filter=sent_filter,
        statuses=statuses,
    )

    sent_count = sum(1 for doc in rows if statuses.get(doc.key, False))
    st.caption(f"{len(rows)} document(s) — {sent_count} envoyé(s).")

    if rows:
        rows.sort(key=lambda doc: (normalize(doc.client_name), doc.first_page))
        start, end = render_pagination(len(rows), page_size)
        render_table(rows[start:end], statuses, kind)
    else:
        st.warning("Aucun document ne correspond à ces critères.")

    render_bulk_download(documents, periods, kind)


if __name__ == "__main__":
    main()
