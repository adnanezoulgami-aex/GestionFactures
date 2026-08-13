"""Tests de la persistance du statut « envoyé au client »."""

from __future__ import annotations

from pathlib import Path

import pytest

from factures.storage import SentStatusStore


@pytest.fixture
def store(tmp_path: Path):
    with SentStatusStore(tmp_path / "test.db") as instance:
        yield instance


class TestSentStatusStore:
    def test_unknown_key_defaults_to_false(self, store: SentStatusStore) -> None:
        assert store.get_many(["inconnue"]) == {"inconnue": False}

    def test_status_round_trip(self, store: SentStatusStore) -> None:
        store.set_sent("abc", True, client_name="DRAKE", period="2026-07")
        assert store.get_many(["abc"])["abc"] is True

    def test_status_can_be_unset(self, store: SentStatusStore) -> None:
        store.set_sent("abc", True)
        store.set_sent("abc", False)
        assert store.get_many(["abc"])["abc"] is False

    def test_status_survives_a_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "persist.db"
        with SentStatusStore(path) as first:
            first.set_sent("abc", True)
        with SentStatusStore(path) as second:
            assert second.get_many(["abc"])["abc"] is True

    def test_bulk_write_and_read(self, store: SentStatusStore) -> None:
        statuses = {f"k{i}": i % 2 == 0 for i in range(1200)}
        store.set_many(statuses)
        assert store.get_many(statuses) == statuses

    def test_get_many_handles_more_keys_than_sqlite_variable_limit(
        self, store: SentStatusStore
    ) -> None:
        keys = [f"k{i}" for i in range(2500)]
        assert len(store.get_many(keys)) == len(keys)

    def test_duplicate_keys_are_tolerated(self, store: SentStatusStore) -> None:
        store.set_sent("abc", True)
        assert store.get_many(["abc", "abc"]) == {"abc": True}

    def test_empty_inputs(self, store: SentStatusStore) -> None:
        assert store.get_many([]) == {}
        store.set_many({})  # ne doit pas lever

    def test_always_warns_that_sqlite_is_not_durable(
        self, store: SentStatusStore
    ) -> None:
        """Une écriture qui aboutit ne prouve pas que la donnée survivra."""
        assert store.is_persistent is True
        assert "redéploiement" in store.durability_warning

    def test_in_memory_fallback_warns_too(self, tmp_path: Path) -> None:
        blocker = tmp_path / "fichier"
        blocker.write_text("pas un dossier", encoding="utf-8")
        store = SentStatusStore(blocker / "sous" / "base.db")
        assert "mémoire" in store.durability_warning
        store.close()

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        store = SentStatusStore(tmp_path / "a" / "b" / "c.db")
        assert store.is_persistent
        assert store.path.exists()
        store.close()

    def test_falls_back_to_memory_when_the_path_is_unusable(
        self, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "fichier"
        blocker.write_text("je ne suis pas un dossier", encoding="utf-8")
        store = SentStatusStore(blocker / "sous" / "base.db")
        assert not store.is_persistent
        store.set_sent("abc", True)  # reste fonctionnel
        assert store.get_many(["abc"])["abc"] is True
        store.close()
