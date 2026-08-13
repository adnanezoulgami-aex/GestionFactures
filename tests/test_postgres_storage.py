"""Tests d'intégration du magasin Postgres.

Ces tests exigent une vraie base : ils sont ignorés tant que
`FACTURES_TEST_POSTGRES_URL` n'est pas défini. Pour les exécuter contre le
projet Supabase :

    $env:FACTURES_TEST_POSTGRES_URL = "postgresql://..."
    python -m pytest tests/test_postgres_storage.py -v

Une table temporaire propre à chaque exécution est créée puis supprimée :
aucune donnée applicative n'est touchée.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from factures.postgres_storage import PostgresSentStatusStore

TEST_URL_VARIABLE = "FACTURES_TEST_POSTGRES_URL"

pytestmark = pytest.mark.skipif(
    not os.environ.get(TEST_URL_VARIABLE, "").strip(),
    reason=f"{TEST_URL_VARIABLE} non défini : base Postgres indisponible.",
)


@pytest.fixture
def store() -> Iterator[PostgresSentStatusStore]:
    """Magasin adossé à une table jetable, supprimée à la fin du test."""
    url = os.environ[TEST_URL_VARIABLE]
    table = f"test_facture_envois_{uuid.uuid4().hex[:12]}"
    instance = PostgresSentStatusStore(url, table=table)
    try:
        yield instance
    finally:
        with instance._pool.connection() as connection:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        instance.close()


class TestPostgresSentStatusStore:
    def test_is_declared_persistent(self, store: PostgresSentStatusStore) -> None:
        assert store.is_persistent is True

    def test_description_never_leaks_the_password(
        self, store: PostgresSentStatusStore
    ) -> None:
        secret = os.environ[TEST_URL_VARIABLE].rpartition("@")[0].rpartition(":")[2]
        assert secret
        assert secret not in store.description

    def test_unknown_key_defaults_to_false(
        self, store: PostgresSentStatusStore
    ) -> None:
        assert store.get_many(["inconnue"]) == {"inconnue": False}

    def test_status_round_trip(self, store: PostgresSentStatusStore) -> None:
        store.set_sent("abc", True, client_name="DRAKE", period="2026-07")
        assert store.get_many(["abc"])["abc"] is True

    def test_status_can_be_unset(self, store: PostgresSentStatusStore) -> None:
        store.set_sent("abc", True)
        store.set_sent("abc", False)
        assert store.get_many(["abc"])["abc"] is False

    def test_status_survives_a_reconnection(
        self, store: PostgresSentStatusStore
    ) -> None:
        """C'est tout l'intérêt de Postgres : la donnée survit au processus."""
        store.set_sent("durable", True, client_name="VELVETRAV", period="2026-07")
        table = store._table_name
        with PostgresSentStatusStore(
            os.environ[TEST_URL_VARIABLE], table=table
        ) as reopened:
            assert reopened.get_many(["durable"])["durable"] is True

    def test_bulk_write_and_read(self, store: PostgresSentStatusStore) -> None:
        statuses = {f"k{i}": i % 2 == 0 for i in range(1200)}
        store.set_many(statuses)
        assert store.get_many(statuses) == statuses

    def test_large_key_set_needs_no_chunking(
        self, store: PostgresSentStatusStore
    ) -> None:
        keys = [f"k{i}" for i in range(2500)]
        assert len(store.get_many(keys)) == len(keys)

    def test_duplicate_keys_are_tolerated(
        self, store: PostgresSentStatusStore
    ) -> None:
        store.set_sent("abc", True)
        assert store.get_many(["abc", "abc"]) == {"abc": True}

    def test_empty_inputs(self, store: PostgresSentStatusStore) -> None:
        assert store.get_many([]) == {}
        store.set_many({})  # ne doit pas lever

    def test_metadata_is_updated_on_conflict(
        self, store: PostgresSentStatusStore
    ) -> None:
        store.set_sent("abc", True, client_name="ANCIEN", period="2026-06")
        store.set_sent("abc", True, client_name="NOUVEAU", period="2026-07")
        with store._pool.connection() as connection:
            row = connection.execute(
                f'SELECT client_name, period FROM "{store._table_name}" '
                "WHERE invoice_key = %s",
                ("abc",),
            ).fetchone()
        assert row == ("NOUVEAU", "2026-07")
