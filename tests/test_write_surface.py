"""Testes arquiteturais da fronteira de escrita da Etapa 8.

Objetivo: provar que nao existe helper mutavel fora do gateway, que a
configuracao `allow_writes=True` nao reativa capacidades e que o transporte
HTTP restante e somente leitura (GET/HEAD + POSTs de consulta allowlisted).
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from src import cli
from src.config import Config
from src.jazerei.auth import Auth
from src.jazerei.catalog import search_games
from src.jazerei.client import JazereiClient
from src.jazerei.profile import get_profile
from src.jazerei.watchlist import list_watchlist
from src.transport import HttpClient, ReadOnlyViolation

LEGACY_HELPERS = {
    "src.jazerei.reviews": ("upsert_review",),
    "src.jazerei.profile": ("update_profile", "build_partial_update"),
    "src.jazerei.watchlist": ("add_to_watchlist", "remove_from_watchlist"),
}


class ExplodingHttp:
    def __getattr__(self, name: str):
        raise AssertionError(f"transporte nao deveria ser alcancado: {name}")


def readonly_client() -> JazereiClient:
    return JazereiClient(
        Config(allow_writes=False),
        auth=Auth(api_key="key", user_id="user-1", id_token="token"),
        http=ExplodingHttp(),
    )


def test_legacy_write_helpers_are_absent() -> None:
    for module_name, names in LEGACY_HELPERS.items():
        module = importlib.import_module(module_name)
        for name in names:
            assert not hasattr(module, name), f"{module_name}.{name} ainda existe"


def test_client_exposes_no_write_api() -> None:
    client = readonly_client()
    for name in ("write_json", "require_writes", "post_json", "public_post_json"):
        assert not hasattr(client, name)
    assert hasattr(client, "post_read_json")
    assert hasattr(client, "public_post_read_json")


def test_allow_writes_true_does_not_reactivate_legacy_helpers() -> None:
    client = JazereiClient(
        Config(allow_writes=True),
        auth=Auth(api_key="key", user_id="user-1", id_token="token"),
        http=ExplodingHttp(),
    )
    assert not hasattr(client, "write_json")
    for module_name, names in LEGACY_HELPERS.items():
        module = importlib.import_module(module_name)
        for name in names:
            assert not hasattr(module, name)


def test_http_client_has_no_generic_mutation_api() -> None:
    http = HttpClient(Config(min_interval=0))
    try:
        for name in ("request", "post_json", "post", "patch", "put", "delete"):
            assert not hasattr(http, name)
        assert hasattr(http, "get_json")
        assert hasattr(http, "get_text")
        assert hasattr(http, "post_read_json")
        with pytest.raises(ReadOnlyViolation):
            http._request("POST", "https://jazerei.com/api/reviews/upsert")
        with pytest.raises(ReadOnlyViolation):
            http._request("DELETE", "https://jazerei.com/rest/v1/reviews")
    finally:
        http.close()


def test_transport_rejects_mutation_endpoints() -> None:
    # allow_writes=True nao altera a allowlist do transporte de leitura.
    http = HttpClient(Config(min_interval=0, allow_writes=True))
    try:
        for url in (
            "https://jazerei.com/api/reviews/upsert",
            "https://jazerei.com/rest/v1/user_watchlist_games",
            "https://jazerei.com/rest/v1/rpc/update_my_profile",
            "https://jazerei.com/api/favorite",
            "https://jazerei.com/api/playing",
        ):
            with pytest.raises(ReadOnlyViolation):
                http.post_read_json(url, {"game_id": 26570})
    finally:
        http.close()


def test_allow_writes_true_client_cannot_reach_mutation_endpoint() -> None:
    client = JazereiClient(
        Config(allow_writes=True, min_interval=0),
        auth=Auth(api_key="key", user_id="user-1", id_token="token"),
        http=HttpClient(Config(min_interval=0, allow_writes=True)),
    )
    with pytest.raises(ReadOnlyViolation):
        client.post_read_json("/api/reviews/upsert", {"game_id": 26570})
    client.close()


def test_allowlisted_read_post_still_works_without_network() -> None:
    http = HttpClient(Config(min_interval=0))

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"results": [{"id": 5169}]}

    calls: list[tuple[str, dict]] = []

    def fake_post(url: str, json: dict, **kwargs):
        calls.append((url, json))
        return FakeResponse()

    http._session.post = fake_post  # type: ignore[method-assign]
    try:
        assert http.post_read_json(
            "https://jazerei.com/api/search/games", {"q": "Sonic", "limit": 3}
        ) == {"results": [{"id": 5169}]}
        assert calls == [("https://jazerei.com/api/search/games", {"q": "Sonic", "limit": 3})]
    finally:
        http.close()


def test_read_clients_profile_watchlist_and_catalog() -> None:
    class FakeReadClient:
        auth = Auth(api_key="key", user_id="user-1", id_token="token")

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def post_read_json(self, path: str, payload: dict):
            self.calls.append((path, payload))
            if path.endswith("get_my_profile"):
                return {"id": "user-1", "favorite_games": [], "playing_now": []}
            if path.endswith("get_profile_watchlist"):
                return {"items": [{"game_id": 26570, "game_slug": "demolition-girl"}]}
            return []

        def public_post_read_json(self, path: str, payload: dict):
            self.calls.append((path, payload))
            return [{"id": 5169, "name": "Sonic Unleashed", "release_year": 2008}]

    client = FakeReadClient()
    profile, raw = get_profile(client)  # type: ignore[arg-type]
    assert profile.id == "user-1"
    assert raw["id"] == "user-1"
    entries = list_watchlist(client)  # type: ignore[arg-type]
    assert entries[0].game_id == 26570
    games = search_games(client, "Sonic", limit=3)  # type: ignore[arg-type]
    assert games[0].id == 5169
    assert [path for path, _ in client.calls] == [
        "/rest/v1/rpc/get_my_profile",
        "/rest/v1/rpc/get_profile_watchlist",
        "/api/search/games",
    ]


def test_http_retry_policy_excludes_mutations() -> None:
    http = HttpClient(Config(min_interval=0))
    try:
        for adapter in http._session.adapters.values():
            allowed = adapter.max_retries.allowed_methods
            assert "POST" not in allowed
            assert "PATCH" not in allowed
            assert "PUT" not in allowed
            assert "DELETE" not in allowed
            assert "GET" in allowed
    finally:
        http.close()


def test_no_mutation_endpoint_literals_outside_policy() -> None:
    markers = (
        "/api/reviews/upsert",
        "update_my_profile",
        "user_watchlist_games",
        "/api/favorite",
        "/api/playing",
    )
    for path in Path("src").rglob("*.py"):
        if path.as_posix() in {"src/browser/policy.py", "src/migration/execution.py"}:
            continue  # allowlist de bloqueio e transporte do gateway
        source = path.read_text(encoding="utf-8")
        for marker in markers:
            assert marker not in source, f"endpoint mutavel referenciado em {path}"


def test_no_write_call_attributes_in_jazerei_modules() -> None:
    forbidden = {"write_json", "require_writes", "post_json", "public_post_json"}
    for path in Path("src/jazerei").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                raise AssertionError(f"{path}: chamada/atributo mutavel {node.attr}")


def test_cli_exposes_no_ungated_write_command() -> None:
    parser = cli.build_parser()
    for command in ("import", "import-all", "migrate", "write", "upload", "publish"):
        with pytest.raises(SystemExit):
            parser.parse_args([command])
    with pytest.raises(SystemExit):
        parser.parse_args(["migration", "execute", "--execution-id", "x"])


def test_execution_run_is_gated_by_two_flags_and_exact_confirmation() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "migration",
            "execution-run",
            "--execution-id",
            "exec-1",
            "--action-id",
            "action-1",
            "--confirm",
            "exec-1",
        ]
    )
    # Flags falsas: retorna antes de abrir navegador/ler execucao.
    assert cli.cmd_execution_run(Config(), args) == 2
    only_allow = Config(allow_writes=True)
    assert cli.cmd_execution_run(only_allow, args) == 2
    only_transport = Config(enable_real_write_transport=True)
    assert cli.cmd_execution_run(only_transport, args) == 2
    both = Config(allow_writes=True, enable_real_write_transport=True)
    wrong = SimpleNamespace(**{**vars(args), "confirm": "other"})
    assert cli.cmd_execution_run(both, wrong) == 2


def test_cli_source_does_not_wire_write_helpers() -> None:
    source = inspect.getsource(cli)
    for name in (
        "upsert_review",
        "update_profile",
        "add_to_watchlist",
        "remove_from_watchlist",
        "importer.execute",
    ):
        assert name not in source


def test_importer_has_no_execute_layer() -> None:
    module = importlib.import_module("src.migration.importer")
    assert not hasattr(module, "execute")
    assert not hasattr(module, "ImportRefused")
