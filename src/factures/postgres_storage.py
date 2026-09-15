"""Persistance du statut « envoyé au client » sur Postgres (Supabase).

Contrairement à SQLite, ce stockage survit au redémarrage du serveur : c'est
l'option à retenir dès que l'application est hébergée sur une plateforme au
disque éphémère, Streamlit Community Cloud en particulier.

La chaîne de connexion n'est jamais écrite dans le dépôt : elle provient de
l'environnement ou des secrets Streamlit (voir `factures.backend`).

Connexions
----------
Supabase ferme les connexions inactives et limite leur nombre. Un pool
(`psycopg_pool`) est donc utilisé plutôt qu'une connexion permanente : il
rouvre de lui-même ce qui a été coupé et libère les connexions au repos.
Préférez l'URL du *connection pooler* fournie par Supabase (port 6543).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from psycopg import sql
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

#: Table dédiée à l'application. Aucune table métier existante n'est touchée.
TABLE_NAME = "facture_envois"

#: Délai d'établissement de la connexion. Volontairement bref : l'application
#: doit rester affichable même quand la base dort — un projet Supabase gratuit
#: se met en pause après une semaine sans activité.
CONNECT_TIMEOUT_SECONDS = 5.0

_SCHEMA = sql.SQL(
    """
    CREATE TABLE IF NOT EXISTS {table} (
        invoice_key TEXT PRIMARY KEY,
        client_name TEXT        NOT NULL DEFAULT '',
        period      TEXT        NOT NULL DEFAULT '',
        sent        BOOLEAN     NOT NULL DEFAULT FALSE,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """
)

_SELECT = sql.SQL(
    "SELECT invoice_key, sent FROM {table} WHERE invoice_key = ANY(%s)"
)

_UPSERT_FULL = sql.SQL(
    """
    INSERT INTO {table} (invoice_key, client_name, period, sent, updated_at)
    VALUES (%s, %s, %s, %s, now())
    ON CONFLICT (invoice_key) DO UPDATE SET
        client_name = EXCLUDED.client_name,
        period      = EXCLUDED.period,
        sent        = EXCLUDED.sent,
        updated_at  = now()
    """
)

_UPSERT_STATUS = sql.SQL(
    """
    INSERT INTO {table} (invoice_key, sent, updated_at)
    VALUES (%s, %s, now())
    ON CONFLICT (invoice_key) DO UPDATE SET
        sent       = EXCLUDED.sent,
        updated_at = now()
    """
)


def redact(url: str) -> str:
    """Masque le mot de passe d'une URL, pour un affichage ou un journal sûr.

    >>> redact("postgresql://user:motdepasse@db.example.com:5432/postgres")
    'postgresql://user:***@db.example.com:5432/postgres'
    """
    scheme, separator, rest = url.partition("://")
    if not separator or "@" not in rest:
        return url
    credentials, _, host = rest.rpartition("@")
    user, has_password, _ = credentials.partition(":")
    if not has_password:
        return url
    return f"{scheme}://{user}:***@{host}"


class PostgresSentStatusStore:
    """Magasin de statuts adossé à Postgres.

    Implémente `factures.backend.SentStatusBackend`.
    """

    def __init__(
        self,
        url: str,
        *,
        table: str = TABLE_NAME,
        min_size: int = 0,
        max_size: int = 4,
        timeout: float = CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        if not url or not url.strip():
            raise ValueError("Chaîne de connexion Postgres vide.")

        self._url = url.strip()
        self._table = sql.Identifier(table)
        self._table_name = table
        # `open=False` puis `open()` explicite : la connexion échoue ici, à la
        # construction, plutôt qu'au premier clic de l'utilisateur. Le délai est
        # court à dessein : une base injoignable doit se signaler vite, sans
        # retarder l'affichage au point de faire échouer le contrôle de santé
        # de l'hébergeur.
        self._pool = ConnectionPool(
            self._url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            kwargs={"row_factory": tuple_row},
            open=False,
        )
        self._pool.open(wait=True, timeout=timeout)
        self._ensure_schema()

    # -- cycle de vie ------------------------------------------------------ #

    def _ensure_schema(self) -> None:
        with self._pool.connection() as connection:
            connection.execute(_SCHEMA.format(table=self._table))

    def close(self) -> None:
        """Ferme le pool. Idempotent."""
        self._pool.close()

    def __enter__(self) -> PostgresSentStatusStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- métadonnées ------------------------------------------------------- #

    @property
    def is_persistent(self) -> bool:
        return True

    @property
    def durability_warning(self) -> None:
        """Rien à signaler : la base survit aux redéploiements."""
        return None

    @property
    def description(self) -> str:
        """Libellé affichable : l'hôte, jamais le mot de passe."""
        host = redact(self._url).rpartition("@")[2] or "Postgres"
        return f"Postgres — {host} (table {self._table_name})"

    # -- lecture / écriture ------------------------------------------------ #

    def get_many(self, keys: Iterable[str]) -> dict[str, bool]:
        wanted = list(dict.fromkeys(keys))
        result = dict.fromkeys(wanted, False)
        if not wanted:
            return result

        # `= ANY(%s)` prend la liste entière en un seul paramètre : contrairement
        # à SQLite, aucun découpage en lots n'est nécessaire.
        with self._pool.connection() as connection:
            rows = connection.execute(
                _SELECT.format(table=self._table), (wanted,)
            ).fetchall()
        for invoice_key, sent in rows:
            result[invoice_key] = bool(sent)
        return result

    def set_sent(
        self,
        invoice_key: str,
        sent: bool,
        *,
        client_name: str = "",
        period: str = "",
    ) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                _UPSERT_FULL.format(table=self._table),
                (invoice_key, client_name, period, bool(sent)),
            )

    def set_many(self, statuses: Mapping[str, bool]) -> None:
        if not statuses:
            return
        rows = [(key, bool(value)) for key, value in statuses.items()]
        with self._pool.connection() as connection, connection.cursor() as cursor:
            cursor.executemany(_UPSERT_STATUS.format(table=self._table), rows)
