"""Tests de la sélection du backend et du masquage des secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from factures.backend import (
    ENV_POSTGRES_URL,
    ENV_SUPABASE_URL,
    SentStatusBackend,
    configured_postgres_url,
    create_store,
)
from factures.postgres_storage import PostgresSentStatusStore, redact
from factures.storage import SentStatusStore


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch):
    """Aucune variable héritée du poste ne doit influencer les tests."""
    monkeypatch.delenv(ENV_POSTGRES_URL, raising=False)
    monkeypatch.delenv(ENV_SUPABASE_URL, raising=False)


class TestConfiguredUrl:
    def test_absent_by_default(self) -> None:
        assert configured_postgres_url() is None

    def test_read_from_the_dedicated_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_POSTGRES_URL, "postgresql://a:b@h:6543/postgres")
        assert configured_postgres_url() == "postgresql://a:b@h:6543/postgres"

    def test_supabase_variable_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_SUPABASE_URL, "postgresql://a:b@h:5432/postgres")
        assert configured_postgres_url() == "postgresql://a:b@h:5432/postgres"

    def test_dedicated_variable_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_POSTGRES_URL, "postgresql://prioritaire@h/postgres")
        monkeypatch.setenv(ENV_SUPABASE_URL, "postgresql://secondaire@h/postgres")
        assert "prioritaire" in configured_postgres_url()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_values_are_ignored(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ) -> None:
        monkeypatch.setenv(ENV_POSTGRES_URL, blank)
        assert configured_postgres_url() is None


class TestStoreSelection:
    def test_sqlite_when_nothing_is_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTURES_DB_PATH", str(tmp_path / "local.db"))
        store = create_store()
        assert isinstance(store, SentStatusStore)
        store.close()

    def test_sqlite_store_satisfies_the_contract(self, tmp_path: Path) -> None:
        with SentStatusStore(tmp_path / "c.db") as store:
            assert isinstance(store, SentStatusBackend)

    def test_unreachable_postgres_raises_instead_of_falling_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un basculement muet sur SQLite ferait croire à une persistance partagée."""
        monkeypatch.setenv(
            ENV_POSTGRES_URL,
            "postgresql://user:pwd@127.0.0.1:1/postgres?connect_timeout=1",
        )
        with pytest.raises(Exception) as failure:
            create_store()
        assert not isinstance(failure.value, SentStatusStore)


class TestPostgresConstruction:
    """Vérifications ne nécessitant aucune base joignable."""

    @pytest.mark.parametrize("url", ["", "   "])
    def test_empty_url_is_rejected(self, url: str) -> None:
        with pytest.raises(ValueError, match="vide"):
            PostgresSentStatusStore(url)


class TestRedact:
    def test_password_is_masked(self) -> None:
        assert redact("postgresql://postgres:s3cr3t@db.example.com:5432/postgres") == (
            "postgresql://postgres:***@db.example.com:5432/postgres"
        )

    def test_supabase_pooler_url(self) -> None:
        masked = redact(
            "postgresql://postgres.abcdef:MonMotDePasse"
            "@aws-0-eu-west-3.pooler.supabase.com:6543/postgres"
        )
        assert "MonMotDePasse" not in masked
        assert masked.endswith("@aws-0-eu-west-3.pooler.supabase.com:6543/postgres")

    def test_password_containing_an_at_sign(self) -> None:
        masked = redact("postgresql://user:pa@ss@db.example.com:5432/postgres")
        assert "pa@ss" not in masked
        assert masked == "postgresql://user:***@db.example.com:5432/postgres"

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://db.example.com:5432/postgres",
            "postgresql://user@db.example.com:5432/postgres",
            "pas-une-url",
            "",
        ],
    )
    def test_urls_without_a_password_are_unchanged(self, url: str) -> None:
        assert redact(url) == url
