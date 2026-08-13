"""Modèles de données du domaine.

Les objets sont immuables (`frozen=True`) : une facture extraite ne doit jamais
être modifiée en place, seule une copie corrigée peut être produite via
`dataclasses.replace`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

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


def periode_key(day: date) -> str:
    """Clé de période triable : ``2026-07``."""
    return f"{day.year:04d}-{day.month:02d}"


def periode_label(key: str) -> str:
    """Libellé lisible d'une clé de période : ``2026-07`` -> ``juillet 2026``."""
    try:
        year_str, month_str = key.split("-", 1)
        month = int(month_str)
        if not 1 <= month <= 12:
            raise ValueError(month)
        return f"{MOIS_FR[month - 1]} {int(year_str)}"
    except (ValueError, IndexError):
        return key


@dataclass(frozen=True)
class Invoice:
    """Une facture, c'est-à-dire une ou plusieurs pages consécutives d'un PDF."""

    source_name: str
    """Nom du fichier PDF téléversé dont provient la facture."""

    source_digest: str
    """Empreinte SHA-256 du PDF source, garantit la stabilité de `key`."""

    first_page: int
    """Index 1-based de la première page de la facture dans le PDF source."""

    last_page: int
    """Index 1-based de la dernière page (== `first_page` si facture d'une page)."""

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
    """Anomalies non bloquantes détectées pendant l'extraction."""

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1

    @property
    def period(self) -> str:
        """Période de rattachement : issue de la date de facture en priorité."""
        if self.invoice_date is not None:
            return periode_key(self.invoice_date)
        return self.fallback_period or "inconnue"

    @property
    def period_label(self) -> str:
        return periode_label(self.period) if self.period != "inconnue" else "Période inconnue"

    @property
    def key(self) -> str:
        """Identifiant stable d'une facture, indépendant de l'ordre d'import.

        Basé sur l'empreinte du PDF source et la position de la facture : réimporter
        le même fichier retrouve donc les statuts « envoyé » déjà cochés.
        """
        raw = f"{self.source_digest}:{self.first_page}:{self.last_page}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @property
    def search_blob(self) -> str:
        """Concaténation normalisée des champs interrogeables."""
        parts = (
            self.client_name,
            self.invoice_number or "",
            self.ice or "",
            self.source_name,
            self.period_label,
        )
        return " ".join(parts)


@dataclass(frozen=True)
class PageError:
    """Page qui n'a pas pu être rattachée à une facture exploitable."""

    source_name: str
    page: int
    reason: str


@dataclass(frozen=True)
class ExtractionReport:
    """Résultat complet d'une passe d'extraction sur un lot de PDF."""

    invoices: tuple[Invoice, ...] = ()
    errors: tuple[PageError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def merged_with(self, other: ExtractionReport) -> ExtractionReport:
        return ExtractionReport(
            invoices=self.invoices + other.invoices,
            errors=self.errors + other.errors,
        )
