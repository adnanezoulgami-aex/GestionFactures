"""Contrat commun aux magasins de statuts « envoyé au client ».

Deux implémentations existent :

* `factures.storage.SentStatusStore` — SQLite, pour le développement local ;
* `factures.postgres_storage.PostgresSentStatusStore` — Postgres / Supabase,
  pour un hébergement où le disque est éphémère.

`create_store()` choisit la bonne selon la configuration, afin que `app.py`
n'ait jamais à connaître le backend utilisé.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Chaîne de connexion Postgres. Renseignée par l'utilisateur, jamais versionnée.
#:
#: Ce nom est propre à l'application, et c'est délibéré : accepter un nom
#: générique comme ``SUPABASE_DB_URL`` reviendrait à se brancher sur la première
#: base Supabase configurée sur le poste — potentiellement une base de
#: production sans rapport avec cette application. Le nom doit être choisi
#: explicitement pour elle.
ENV_POSTGRES_URL = "FACTURES_POSTGRES_URL"


@runtime_checkable
class SentStatusBackend(Protocol):
    """Ce dont l'application a besoin pour suivre les envois."""

    @property
    def is_persistent(self) -> bool:
        """`False` si les statuts sont perdus au redémarrage du serveur."""

    @property
    def description(self) -> str:
        """Libellé affichable du stockage, sans aucun secret."""

    @property
    def durability_warning(self) -> str | None:
        """Message à afficher si les statuts risquent d'être perdus, sinon `None`.

        `is_persistent` ne dit que si l'écriture aboutit. Une base SQLite dans
        un conteneur s'écrit très bien puis disparaît au redéploiement : ce
        message est le seul moyen d'en avertir l'utilisateur.
        """

    def get_many(self, keys: Iterable[str]) -> dict[str, bool]:
        """Statut des clés demandées. Une clé inconnue vaut `False`."""

    def set_sent(
        self,
        invoice_key: str,
        sent: bool,
        *,
        client_name: str = "",
        period: str = "",
    ) -> None:
        """Enregistre (ou met à jour) le statut d'une facture."""

    def set_many(self, statuses: Mapping[str, bool]) -> None:
        """Écriture groupée."""

    def close(self) -> None:
        """Libère les ressources. Idempotent."""


def configured_postgres_url() -> str | None:
    """Chaîne de connexion Postgres si elle est configurée, sinon `None`.

    Cherche d'abord dans l'environnement, puis dans les secrets Streamlit
    (`.streamlit/secrets.toml` en local, interface Secrets sur Streamlit Cloud).
    La valeur n'est ni journalisée ni affichée.
    """
    value = os.environ.get(ENV_POSTGRES_URL, "").strip()
    if value:
        return value

    # Tout l'accès aux secrets est protégé : Streamlit peut être absent (tests
    # du domaine) et `st.secrets` lève dès la première lecture quand aucun
    # fichier `secrets.toml` n'existe — le cas normal en développement local.
    try:
        import streamlit as st

        block = st.secrets.get("postgres")
        if isinstance(block, Mapping):
            value = str(block.get("url", "")).strip()
            if value:
                return value
    except Exception:
        logger.debug("Secrets Streamlit indisponibles.", exc_info=True)
    return None


def create_store() -> SentStatusBackend:
    """Instancie le magasin adapté à la configuration.

    Postgres est utilisé dès qu'une chaîne de connexion est disponible ; sinon
    l'application retombe sur SQLite. Si Postgres est configuré mais injoignable,
    l'erreur n'est pas masquée par un basculement silencieux vers un stockage
    local qui donnerait l'illusion d'une persistance partagée : elle est
    propagée pour que l'interface l'affiche.
    """
    url = configured_postgres_url()
    if url:
        from factures.postgres_storage import PostgresSentStatusStore

        return PostgresSentStatusStore(url)

    from factures.storage import SentStatusStore

    return SentStatusStore()
