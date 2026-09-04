"""Modèles de données du domaine.

Deux types de documents sont traités, et partagent la même mécanique : un PDF
groupé est découpé en documents, chacun renommé au nom de son client.

* `Invoice` — une facture émise par le cabinet ;
* `LedgerAccount` — un extrait de compte issu du grand livre.

Le protocole `PagedDocument` décrit ce dont le découpage, le nommage et
l'interface ont besoin. Tout le code en aval s'appuie sur lui, et non sur l'un
ou l'autre type concret.

Les objets sont immuables (`frozen=True`) : un document extrait ne doit jamais
être modifié en place, seule une copie corrigée peut être produite via
`dataclasses.replace`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

MOIS_FR: tuple[str, ...] = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)

UNKNOWN_PERIOD = "inconnue"


def periode_key(day: date) -> str:
    """Clé de période triable : ``2026-07``."""
    return f"{day.year:04d}-{day.month:02d}"


def periode_label(key: str) -> str:
    """Libellé lisible d'une clé de période : ``2026-07`` -> ``juillet 2026``.

    Les clés qui ne suivent pas ce format — la période d'un grand livre, par
    exemple — sont renvoyées telles quelles.
    """
    try:
        year_str, month_str = key.split("-", 1)
        month = int(month_str)
        if not 1 <= month <= 12:
            raise ValueError(month)
        return f"{MOIS_FR[month - 1]} {int(year_str)}"
    except (ValueError, IndexError):
        return key


def _document_key(kind: str, digest: str, first_page: int, last_page: int) -> str:
    """Identifiant stable d'un document, indépendant de l'ordre d'import.

    Basé sur le type, l'empreinte du PDF source et la position du document :
    réimporter le même fichier retrouve donc les statuts déjà cochés, et deux
    documents de types différents ne peuvent pas entrer en collision.
    """
    raw = f"{kind}:{digest}:{first_page}:{last_page}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@runtime_checkable
class PagedDocument(Protocol):
    """Ce dont le découpage, le nommage et l'interface ont besoin."""

    source_name: str
    """Nom du fichier PDF téléversé dont provient le document."""

    source_digest: str
    """Empreinte SHA-256 du PDF source."""

    first_page: int
    """Index 1-based de la première page du document dans le PDF source."""

    last_page: int
    """Index 1-based de la dernière page."""

    client_name: str
    """Nom du client. Sert de nom de fichier. Jamais vide."""

    warnings: tuple[str, ...]
    """Anomalies non bloquantes détectées pendant l'extraction."""

    @property
    def key(self) -> str:
        """Identifiant stable, support du suivi des envois."""

    @property
    def reference(self) -> str | None:
        """Référence distinguant deux documents d'un même client."""

    @property
    def period(self) -> str:
        """Clé de période, support du filtre par période."""

    @property
    def period_label(self) -> str:
        """Libellé affichable de la période."""

    @property
    def search_blob(self) -> str:
        """Champs interrogeables, concaténés."""


@dataclass(frozen=True)
class Invoice:
    """Une facture, c'est-à-dire une ou plusieurs pages consécutives d'un PDF."""

    source_name: str
    source_digest: str
    first_page: int
    last_page: int

    client_name: str
    """Nom du client tel qu'imprimé sous la date. Jamais vide."""

    invoice_number: str | None = None
    invoice_date: date | None = None
    ice: str | None = None
    total_ht: Decimal | None = None
    total_tva: Decimal | None = None
    total_ttc: Decimal | None = None

    fallback_period: str | None = None
    """Période saisie par l'utilisateur, utilisée si `invoice_date` est absente."""

    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1

    @property
    def reference(self) -> str | None:
        return self.invoice_number

    @property
    def period(self) -> str:
        """Période de rattachement : issue de la date de facture en priorité."""
        if self.invoice_date is not None:
            return periode_key(self.invoice_date)
        return self.fallback_period or UNKNOWN_PERIOD

    @property
    def period_label(self) -> str:
        if self.period == UNKNOWN_PERIOD:
            return "Période inconnue"
        return periode_label(self.period)

    @property
    def key(self) -> str:
        return _document_key(
            "facture", self.source_digest, self.first_page, self.last_page
        )

    @property
    def search_blob(self) -> str:
        parts = (
            self.client_name,
            self.invoice_number or "",
            self.ice or "",
            self.source_name,
            self.period_label,
        )
        return " ".join(parts)


@dataclass(frozen=True)
class LedgerAccount:
    """Un extrait de compte du grand livre : le compte d'un client."""

    source_name: str
    source_digest: str
    first_page: int
    last_page: int

    client_name: str
    """Intitulé du compte, imprimé sur la ligne « COMPTE ». Jamais vide."""

    account_code: str | None = None
    """Code du compte auxiliaire, par exemple ``0ABMINVE``."""

    period_start: date | None = None
    period_end: date | None = None

    total_debit: Decimal | None = None
    total_credit: Decimal | None = None
    balance: Decimal | None = None
    """Solde de clôture, tel qu'imprimé sur la ligne des totaux du compte."""

    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1

    @property
    def reference(self) -> str | None:
        return self.account_code

    @property
    def period(self) -> str:
        """Un grand livre couvre un exercice entier, pas un mois.

        La clé encadre donc deux dates : ``2025-10/2026-09``. Le filtre de
        l'interface continue de fonctionner, en proposant un exercice par
        fichier importé au lieu d'un mois.
        """
        if self.period_start is None or self.period_end is None:
            return UNKNOWN_PERIOD
        return f"{periode_key(self.period_start)}/{periode_key(self.period_end)}"

    @property
    def period_label(self) -> str:
        if self.period_start is None or self.period_end is None:
            return "Période inconnue"
        debut = self.period_start.strftime("%d/%m/%Y")
        fin = self.period_end.strftime("%d/%m/%Y")
        return f"{debut} au {fin}"

    @property
    def key(self) -> str:
        return _document_key(
            "extrait", self.source_digest, self.first_page, self.last_page
        )

    @property
    def search_blob(self) -> str:
        parts = (
            self.client_name,
            self.account_code or "",
            self.source_name,
            self.period_label,
        )
        return " ".join(parts)


@dataclass(frozen=True)
class PageError:
    """Page qui n'a pas pu être rattachée à un document exploitable."""

    source_name: str
    page: int
    reason: str


@dataclass(frozen=True)
class ExtractionReport:
    """Résultat complet d'une passe d'extraction sur un lot de PDF."""

    documents: tuple[PagedDocument, ...] = ()
    errors: tuple[PageError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def merged_with(self, other: ExtractionReport) -> ExtractionReport:
        return ExtractionReport(
            documents=self.documents + other.documents,
            errors=self.errors + other.errors,
        )
