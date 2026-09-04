"""Découpe des documents en PDF individuels et assemblage des archives ZIP.

S'applique indifféremment aux factures et aux extraits de compte : seul le
protocole `PagedDocument` est requis.

Règles de nommage demandées :

* un document par client   -> ``NOM DU CLIENT.pdf`` à la racine de l'archive ;
* plusieurs documents      -> dossier ``NOM DU CLIENT/`` contenant
  ``NOM DU CLIENT - <référence>.pdf``.
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence

import pymupdf

from factures.models import PagedDocument
from factures.naming import sanitize_filename, unique_filename

logger = logging.getLogger(__name__)

#: Date fixe pour les entrées ZIP : rend les archives reproductibles.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class PackagingError(RuntimeError):
    """Le PDF source d'un document est absent ou inexploitable."""


def _open_source(invoice: PagedDocument, sources: Mapping[str, bytes]) -> bytes:
    try:
        return sources[invoice.source_name]
    except KeyError as exc:
        raise PackagingError(
            f"PDF source « {invoice.source_name} » indisponible pour le document "
            f"{invoice.reference or invoice.first_page}."
        ) from exc


def build_document_pdf(
    invoice: PagedDocument,
    sources: Mapping[str, bytes],
    *,
    compress: bool = True,
) -> bytes:
    """Extrait les pages d'un document dans un PDF autonome.

    Args:
        invoice: le document à isoler.
        sources: PDF d'origine, indexés par nom de fichier.
        compress: réduit les polices aux seuls glyphes utilisés. Une page
            extraite ré-embarque sinon l'intégralité des polices du document
            d'origine (~460 Ko contre ~140 Ko après réduction). Coûte une
            quarantaine de millisecondes par page.

    Raises:
        PackagingError: si le PDF source manque ou si la plage de pages est
            invalide (fichier remplacé entre l'import et le téléchargement).
    """
    payload = _open_source(invoice, sources)
    try:
        with pymupdf.open(stream=payload, filetype="pdf") as document:
            last = invoice.last_page
            if not 1 <= invoice.first_page <= last <= document.page_count:
                raise PackagingError(
                    f"Pages {invoice.first_page}-{last} hors du document "
                    f"« {invoice.source_name} » ({document.page_count} pages)."
                )
            with pymupdf.open() as extract:
                extract.insert_pdf(
                    document,
                    from_page=invoice.first_page - 1,
                    to_page=last - 1,
                )
                if compress:
                    try:
                        extract.subset_fonts(verbose=False)
                    except Exception:  # pragma: no cover - police exotique
                        # La réduction est une optimisation : son échec ne doit
                        # jamais empêcher la livraison du document.
                        logger.warning(
                            "Réduction des polices impossible pour %s", invoice.key
                        )
                return extract.tobytes(garbage=4, deflate=True, clean=True)
    except PackagingError:
        raise
    except Exception as exc:  # pragma: no cover - PDF corrompu
        logger.exception("Découpe impossible: %s", invoice.key)
        raise PackagingError(f"Découpe impossible : {exc}") from exc


def document_display_name(invoice: PagedDocument) -> str:
    """Nom de fichier proposé au téléchargement unitaire : le nom du client."""
    return f"{sanitize_filename(invoice.client_name)}.pdf"


def plan_archive_names(invoices: Sequence[PagedDocument]) -> dict[str, str]:
    """Calcule le chemin de chaque document dans l'archive.

    Returns:
        Un dictionnaire ``clé de document -> chemin dans le ZIP``. Les chemins
        sont garantis uniques, même si deux clients portent des noms qui
        deviennent identiques après nettoyage.
    """
    # Le regroupement se fait sur le nom *brut* : deux clients distincts dont les
    # noms convergent après nettoyage (« A/B » et « A:B » donnent tous deux
    # « A-B ») ne doivent jamais être fusionnés dans un même dossier. Leurs
    # chemins seront simplement différenciés par `unique_filename`.
    by_client: dict[str, list[PagedDocument]] = defaultdict(list)
    for invoice in invoices:
        by_client[invoice.client_name].append(invoice)

    paths: dict[str, str] = {}
    used_root: list[str] = []

    for raw_client, group in sorted(by_client.items(), key=lambda item: item[0].casefold()):
        client = sanitize_filename(raw_client)
        ordered = sorted(
            group,
            key=lambda inv: (inv.source_name, inv.first_page),
        )

        if len(ordered) == 1:
            name = unique_filename(f"{client}.pdf", used_root)
            used_root.append(name)
            paths[ordered[0].key] = name
            continue

        # Plusieurs documents : un dossier au nom du client.
        folder = unique_filename(client, used_root)
        used_root.append(folder)

        used_inner: list[str] = []
        for invoice in ordered:
            label = invoice.reference or f"p{invoice.first_page}"
            name = unique_filename(
                f"{client} - {sanitize_filename(label)}.pdf", used_inner
            )
            used_inner.append(name)
            paths[invoice.key] = f"{folder}/{name}"

    return paths


def build_zip_archive(
    invoices: Iterable[PagedDocument],
    sources: Mapping[str, bytes],
    *,
    compress: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[bytes, list[str]]:
    """Assemble l'archive ZIP des documents fournis.

    Un document dont la découpe échoue n'interrompt pas l'archive : il est
    signalé dans la liste d'erreurs retournée, afin que l'utilisateur récupère
    tout de même les autres documents.

    Args:
        invoices: documents à inclure.
        sources: PDF d'origine, indexés par nom de fichier.
        compress: voir `build_document_pdf`.
        progress: rappel optionnel ``(traitées, total)``, pour l'interface.

    Returns:
        ``(contenu_zip, erreurs)`` où `erreurs` liste les documents écartés.
    """
    ordered = list(invoices)
    paths = plan_archive_names(ordered)
    failures: list[str] = []
    total = len(ordered)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, invoice in enumerate(
            sorted(ordered, key=lambda inv: paths[inv.key].casefold()), start=1
        ):
            if progress is not None:
                progress(index, total)
            try:
                payload = build_document_pdf(invoice, sources, compress=compress)
            except PackagingError as exc:
                failures.append(f"{invoice.client_name} : {exc}")
                continue
            # `ZipInfo` explicite : horodatage figé et drapeau UTF-8 pour que
            # les noms accentués s'ouvrent correctement sous Windows.
            info = zipfile.ZipInfo(filename=paths[invoice.key], date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)

    return buffer.getvalue(), failures
