"""Persistance du statut « envoyé au client ».

Le statut est conservé dans une base SQLite locale, indexée par la clé stable de
la facture (empreinte du PDF source + plage de pages). Réimporter le même PDF
retrouve donc les cases déjà cochées.

Si le système de fichiers est en lecture seule — cas d'un hébergement Streamlit
Community Cloud — la base bascule automatiquement en mémoire : l'application
reste utilisable, mais les statuts ne survivent pas au redémarrage. Utiliser
`SentStatusStore.is_persistent` pour en avertir l'utilisateur.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import weakref
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_DB_PATH = "FACTURES_DB_PATH"

#: Racine du projet, déduite de l'emplacement du paquet (`<racine>/src/factures`).
#: Le chemin par défaut est absolu à dessein : relatif, il aurait suivi le
#: dossier d'exécution et la base se serait retrouvée ailleurs à chaque
#: lancement depuis un répertoire différent.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = _PROJECT_ROOT / "data" / "factures.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_status (
    invoice_key TEXT PRIMARY KEY,
    client_name TEXT NOT NULL DEFAULT '',
    period      TEXT NOT NULL DEFAULT '',
    sent        INTEGER NOT NULL DEFAULT 0 CHECK (sent IN (0, 1)),
    updated_at  TEXT NOT NULL
);
"""


def default_db_path() -> Path:
    """Chemin de la base, surchargeable via la variable d'environnement."""
    override = os.environ.get(ENV_DB_PATH, "").strip()
    return Path(override) if override else DEFAULT_DB_PATH


class SentStatusStore:
    """Accès concurrent-safe au statut d'envoi des factures."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._lock = threading.Lock()
        self._path = Path(db_path) if db_path is not None else default_db_path()
        self._is_persistent = True
        self._connection = self._connect()
        with self._connection:
            self._connection.executescript(_SCHEMA)

    # -- cycle de vie ----------------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path, check_same_thread=False)
            connection.execute("PRAGMA journal_mode=WAL")
        except (OSError, sqlite3.Error) as exc:
            logger.warning(
                "Base %s inaccessible (%s) : bascule en mémoire.", self._path, exc
            )
            self._is_persistent = False
            connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        # La connexion doit être fermée même si l'appelant oublie `close()` : le
        # cache de ressources de Streamlit peut évincer un magasin sans prévenir.
        # `finalize` reçoit la méthode de la connexion, jamais celle du magasin,
        # afin de ne pas créer de référence qui empêcherait sa collecte.
        self._finalizer = weakref.finalize(self, connection.close)
        return connection

    def close(self) -> None:
        """Ferme la connexion. Idempotent."""
        with self._lock:
            self._finalizer()

    def __enter__(self) -> SentStatusStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- lecture / écriture ------------------------------------------------ #

    @property
    def is_persistent(self) -> bool:
        """`False` si les statuts sont perdus au redémarrage du serveur."""
        return self._is_persistent

    @property
    def path(self) -> Path:
        return self._path

    @property
    def description(self) -> str:
        """Libellé affichable du stockage."""
        if not self._is_persistent:
            return "SQLite en mémoire (non persistant)"
        return f"SQLite — {self._path}"

    @property
    def durability_warning(self) -> str:
        """SQLite ne survit jamais à un redéploiement : toujours avertir.

        Le fichier vit sur le serveur qui exécute l'application. En local il
        persiste ; sur un hébergement conteneurisé (Streamlit Community Cloud)
        il est recréé vide à chaque redémarrage, sans que l'écriture n'ait
        jamais échoué. L'avertissement est donc inconditionnel.
        """
        if not self._is_persistent:
            return (
                "Stockage en mémoire : les cases « Envoyé » seront perdues dès "
                "l'arrêt de l'application."
            )
        return (
            "Suivi local à ce serveur : les cases « Envoyé » seront perdues au "
            "prochain redéploiement. Configurez Postgres pour un suivi durable "
            "et partagé entre les utilisateurs."
        )

    def get_many(self, keys: Iterable[str]) -> dict[str, bool]:
        """Statut des clés demandées. Une clé inconnue vaut `False`."""
        wanted = list(dict.fromkeys(keys))
        result = dict.fromkeys(wanted, False)
        if not wanted:
            return result

        # Découpage en lots : SQLite limite le nombre de variables liées.
        with self._lock:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = self._connection.execute(
                    f"SELECT invoice_key, sent FROM sent_status "  # noqa: S608
                    f"WHERE invoice_key IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    result[row["invoice_key"]] = bool(row["sent"])
        return result

    def set_sent(
        self,
        invoice_key: str,
        sent: bool,
        *,
        client_name: str = "",
        period: str = "",
    ) -> None:
        """Enregistre (ou met à jour) le statut d'une facture."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO sent_status (invoice_key, client_name, period, sent, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(invoice_key) DO UPDATE SET
                    sent = excluded.sent,
                    client_name = excluded.client_name,
                    period = excluded.period,
                    updated_at = excluded.updated_at
                """,
                (invoice_key, client_name, period, int(sent), now),
            )

    def set_many(self, statuses: Mapping[str, bool]) -> None:
        """Écriture groupée, utilisée par les actions « tout cocher »."""
        if not statuses:
            return
        now = datetime.now(UTC).isoformat(timespec="seconds")
        rows = [(key, int(value), now) for key, value in statuses.items()]
        with self._lock, self._connection:
            self._connection.executemany(
                """
                INSERT INTO sent_status (invoice_key, sent, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(invoice_key) DO UPDATE SET
                    sent = excluded.sent,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
