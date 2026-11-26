from __future__ import annotations

import json
from pathlib import Path

import pytest
from src import cli
from src.config import Config, load_config
from src.jazerei import identity as identity_module
from src.jazerei.identity import (
    SessionIdentity,
    discover_session_user_id,
    identity_source,
    load_session_identity,
    resolve_expected_user_id,
    save_session_identity,
    stored_user_id,
)
from src.migration.wizard import MigrationService


class FakePage:
    def __init__(self, local_value: str | None = None) -> None:
        self.local_value = local_value

    def evaluate(self, _script: str):
        return self.local_value


class FakeSession:
    def __init__(self, local_value: str | None = None) -> None:
        self.page = FakePage(local_value)

    def goto(self, _url: str) -> None:
        return None

    def wait(self, _milliseconds: int) -> None:
        return None


def test_identity_roundtrip_contains_no_credentials(tmp_path: Path) -> None:
    path = save_session_identity(
        tmp_path,
        SessionIdentity("user-123", "firebase_indexeddb"),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"schema_version", "user_id", "source", "captured_at", "note"}
    assert payload["user_id"] == "user-123"
    assert "token" not in path.read_text(encoding="utf-8").lower()
    assert stored_user_id(tmp_path) == "user-123"


def test_invalid_identity_is_ignored(tmp_path: Path) -> None:
    (tmp_path / identity_module.IDENTITY_FILENAME).write_text("{not-json", encoding="utf-8")
    assert load_session_identity(tmp_path) is None
    (tmp_path / identity_module.IDENTITY_FILENAME).write_text(
        json.dumps({"user_id": "bad user id"}), encoding="utf-8"
    )
    assert load_session_identity(tmp_path) is None


def test_env_user_id_prevails(tmp_path: Path) -> None:
    save_session_identity(tmp_path, SessionIdentity("file-user", "firebase_indexeddb"))
    assert resolve_expected_user_id(configured="env-user", browser_dir=tmp_path) == "env-user"
    assert identity_source(configured="env-user", browser_dir=tmp_path) == "env"
    assert identity_source(configured="", browser_dir=tmp_path) == "session_file"


def test_load_config_falls_back_to_session_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_session_identity(
        tmp_path / "browser", SessionIdentity("file-user", "firebase_local_storage")
    )
    monkeypatch.setenv("JAZEREI_USER_ID", "")
    monkeypatch.setenv("MIGRATION_DATA_DIR", str(tmp_path))
    config = load_config(env_file=None)
    assert config.jazerei_user_id == "file-user"


def test_load_config_env_overrides_session_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_session_identity(tmp_path / "browser", SessionIdentity("file-user", "firebase_indexeddb"))
    monkeypatch.setenv("JAZEREI_USER_ID", "env-user")
    monkeypatch.setenv("MIGRATION_DATA_DIR", str(tmp_path))
    config = load_config(env_file=None)
    assert config.jazerei_user_id == "env-user"


def test_discover_prefers_indexed_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.jazerei.state.read_session_user_id", lambda _page: "indexed-user")
    result = discover_session_user_id(FakeSession("local-user"))
    assert result == SessionIdentity("indexed-user", "firebase_indexeddb")


def test_discover_falls_back_to_local_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.jazerei.state.read_session_user_id", lambda _page: None)
    result = discover_session_user_id(FakeSession("local-user"))
    assert result == SessionIdentity("local-user", "firebase_local_storage")


def test_discover_falls_back_to_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.jazerei.state.read_session_user_id", lambda _page: None)
    monkeypatch.setattr("src.jazerei.state.read_profile_user_id", lambda _session: "profile-user")
    result = discover_session_user_id(FakeSession(None))
    assert result == SessionIdentity("profile-user", "get_my_profile")


def test_migration_service_refreshes_identity_after_ui_start(tmp_path: Path) -> None:
    save_session_identity(tmp_path / "browser", SessionIdentity("late-user", "firebase_indexeddb"))
    service = MigrationService(Config(data_dir=tmp_path), approvals=tmp_path / "approvals.jsonl")
    assert service._effective_config().jazerei_user_id == "late-user"


def test_migration_service_keeps_environment_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_session_identity(tmp_path / "browser", SessionIdentity("file-user", "firebase_indexeddb"))
    monkeypatch.setenv("JAZEREI_USER_ID", "env-user")
    monkeypatch.setenv("MIGRATION_DATA_DIR", str(tmp_path))
    config = load_config(env_file=None)
    service = MigrationService(config, approvals=tmp_path / "approvals.jsonl")
    assert service._effective_config().jazerei_user_id == "env-user"


def test_login_helper_persists_discovered_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "src.cli.discover_session_user_id",
        lambda _session: SessionIdentity("login-user", "firebase_indexeddb"),
    )
    config = Config(data_dir=tmp_path)
    identity = cli._persist_session_identity(config, object())
    assert identity == SessionIdentity("login-user", "firebase_indexeddb")
    assert stored_user_id(config.browser_dir) == "login-user"
