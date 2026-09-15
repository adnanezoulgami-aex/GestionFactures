"""Lecture géométrique des PDF et découpage en documents.

Les deux types de documents traités — factures et extraits de compte — se
présentent de la même façon : un PDF groupé où chaque document commence par un
repère imprimé à une position fixe. Ce module porte cette mécanique commune ;
les modules `extraction` et `ledger` n'y ajoutent que la lecture des champs
propres à leur gabarit.

L'analyse s'appuie sur les coordonnées des lignes de texte plutôt que sur le
texte brut : un nom de société ressemble à une ligne d'adresse, mais il n'est
jamais imprimé au même endroit.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TypeVar

import pymupdf

from factures.models import ExtractionReport, PagedDocument, PageError

logger = logging.getLogger(__name__)

T = TypeVar("T")

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")

#: Un montant : chiffres, séparateurs de milliers, deux décimales au plus.
#: `\s` couvre les espaces insécables U+00A0 et U+202F du gabarit.
AMOUNT_RE = re.compile(r"^-?[\d\s.]*\d(?:[.,]\d{1,2})?$")


def consecutive_pairs(items: Sequence[T]) -> Iterator[tuple[T, T]]:
    """Paires d'éléments successifs : ``[a, b, c]`` -> ``(a, b), (b, c)``.

    Équivaut à `itertools.pairwise`, absent avant Python 3.10. L'application
    doit rester exécutable sur les versions de Python proposées par les
    hébergeurs, qui ne sont pas toujours les plus récentes.
    """
    return zip(items, items[1:])


@dataclass(frozen=True)
class TextLine:
    """Une ligne de texte avec sa boîte englobante."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def y_mid(self) -> float:
        return (self.y0 + self.y1) / 2.0

    def contains_y(self, y: float) -> bool:
        return self.y0 <= y <= self.y1


@dataclass(frozen=True)
class DocumentSpan:
    """Les pages d'un document et le texte de sa première page."""

    source_name: str
    source_digest: str
    first_page: int
    """Index 1-based dans le PDF source."""
    last_page: int
    lines: list[TextLine]
    """Lignes de la première page, d'où sont lus les champs."""


#: Construit un document depuis son étendue, ou explique pourquoi c'est impossible.
DocumentBuilder = Callable[[DocumentSpan], "tuple[PagedDocument | None, str | None]"]


def read_lines(page: pymupdf.Page) -> list[TextLine]:
    """Lignes de texte non vides d'une page, triées de haut en bas."""
    lines: list[TextLine] = []
    for block in page.get_text("dict").get("blocks", ()):
        if block.get("type") != 0:  # 0 = bloc texte, 1 = image
            continue
        for line in block.get("lines", ()):
            text = "".join(span.get("text", "") for span in line.get("spans", ())).strip()
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            lines.append(TextLine(x0=x0, y0=y0, x1=x1, y1=y1, text=text))
    lines.sort(key=lambda item: (round(item.y0, 1), item.x0))
    return lines


def same_row(lines: list[TextLine], anchor: TextLine, *, min_x: float = 0.0) -> list[TextLine]:
    """Lignes situées sur la même rangée visuelle que `anchor`, à sa droite.

    Indispensable quand une colonne porte plusieurs informations à des hauteurs
    différentes : seule la rangée du repère est pertinente.
    """
    middle = anchor.y_mid
    return sorted(
        (
            line
            for line in lines
            if line is not anchor and line.x0 >= min_x and line.contains_y(middle)
        ),
        key=lambda line: line.x0,
    )


def parse_french_amount(raw: str) -> Decimal | None:
    """Convertit ``1 500,00`` ou ``1 500.00`` en `Decimal`. `None` si illisible."""
    cleaned = re.sub(r"\s", "", raw)
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_french_date(raw: str) -> date | None:
    """Première date ``JJ/MM/AAAA`` trouvée dans le texte, ou `None`."""
    match = DATE_RE.search(raw)
    if match is None:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def amount_on_row(
    lines: list[TextLine], anchor: TextLine, *, min_x: float
) -> Decimal | None:
    """Premier montant lisible aligné à droite sur la rangée de `anchor`."""
    for candidate in same_row(lines, anchor, min_x=min_x):
        text = candidate.text.strip()
        if AMOUNT_RE.match(text):
            amount = parse_french_amount(text)
            if amount is not None:
                return amount
    return None


def split_documents(
    pdf_bytes: bytes,
    *,
    source_name: str,
    starts_new_document: Callable[[list[TextLine]], bool],
    build_document: DocumentBuilder,
    no_document_reason: str,
) -> ExtractionReport:
    """Découpe un PDF en documents et les construit.

    Un document commence sur une page reconnue par `starts_new_document` et
    court jusqu'à la page précédant le document suivant : une pièce s'étendant
    sur plusieurs pages reste donc entière.

    Aucune exception n'est levée pour une page défaillante : l'incident est
    isolé dans le rapport, afin qu'un lot de 150 documents ne soit jamais perdu
    à cause d'une seule page atypique.
    """
    digest = hashlib.sha256(pdf_bytes).hexdigest()

    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # pragma: no cover - dépend du fichier fourni
        logger.exception("Ouverture impossible: %s", source_name)
        return ExtractionReport(
            errors=(PageError(source_name, 0, f"PDF illisible : {exc}"),)
        )

    documents: list[PagedDocument] = []
    errors: list[PageError] = []

    with document:
        if document.needs_pass:
            return ExtractionReport(
                errors=(PageError(source_name, 0, "PDF protégé par mot de passe."),)
            )

        # 1) Lire chaque page et repérer celles qui ouvrent un document.
        page_lines: list[list[TextLine]] = []
        starts: list[int] = []
        for index, page in enumerate(document):
            try:
                lines = read_lines(page)
            except Exception as exc:  # pragma: no cover - page corrompue
                logger.exception("Lecture impossible: %s p.%d", source_name, index + 1)
                page_lines.append([])
                errors.append(PageError(source_name, index + 1, f"Page illisible : {exc}"))
                continue
            page_lines.append(lines)
            if starts_new_document(lines):
                starts.append(index)

        if not starts:
            errors.append(PageError(source_name, 0, no_document_reason))
            return ExtractionReport(errors=tuple(errors))

        # 2) Pages précédant le premier document : signalées, jamais rattachées.
        for index in range(starts[0]):
            errors.append(
                PageError(source_name, index + 1, "Page hors document (ignorée).")
            )

        # 3) Chaque document court jusqu'au début du suivant.
        boundaries = [*starts, len(page_lines)]
        for start, next_start in consecutive_pairs(boundaries):
            span = DocumentSpan(
                source_name=source_name,
                source_digest=digest,
                first_page=start + 1,
                last_page=next_start,
                lines=page_lines[start],
            )
            built, failure = build_document(span)
            if built is None:
                errors.append(PageError(source_name, start + 1, failure or "Échec."))
            else:
                documents.append(built)

    return ExtractionReport(documents=tuple(documents), errors=tuple(errors))
