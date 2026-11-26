"""Testes do transporte real do gateway, sempre com sessao simulada.

Nenhum teste abre navegador, usa rede ou envia POST real.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.config import Config
from src.migration.execution import (
    REVIEW_UPSERT_URL,
    BrowserWriteTransport,
    ExecutionError,
    PayloadError,
    WriteBlockedError,
    WriteResult,
    WriteTimeout,
    execute_action,
)
from src.models.destination import DestinationReview

from tests.test_execution import (
    FakeTransport,
    applied_review,
    confirm,
    make_execution,
    run_action,
)
from tests.test_preflight import current_snapshot, workspace


class FakeResponse:
    def __init__(self, status: int, body: object = None) -> None:
        self.status = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("sem corpo JSON")
        return self._body


class FakeRequest:
    def __init__(self, *, response: object | None = None) -> None:
        self.response = response or FakeResponse(200, {"ok": True})
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeSession:
    def __init__(self, *, page_url: str = "https://jazerei.com/perfil", request=None) -> None:
        self.page = SimpleNamespace(url=page_url)
        self.context = SimpleNamespace(request=request or FakeRequest())


def payload() -> dict:
    return {"game_id": 26570, "rating": 2.0, "text": "texto", "liked": False}


def enabled_config(**changes) -> Config:
    base = {
        "allow_writes": True,
        "enable_real_write_transport": True,
        "min_interval": 0,
    }
    base.update(changes)
    return Config(**base)


def test_transport_requires_both_flags() -> None:
    with pytest.raises(WriteBlockedError):
        BrowserWriteTransport(Config(), FakeSession())
    with pytest.raises(WriteBlockedError):
        BrowserWriteTransport(Config(allow_writes=True), FakeSession())
    with pytest.raises(WriteBlockedError):
        BrowserWriteTransport(Config(enable_real_write_transport=True), FakeSession())


def test_transport_uses_fixed_endpoint_method_and_no_redirect() -> None:
    request = FakeRequest()
    transport = BrowserWriteTransport(enabled_config(), FakeSession(request=request))
    result = transport.post_review(payload())
    assert result == WriteResult(200, {"ok": True})
    assert len(request.calls) == 1
    url, kwargs = request.calls[0]
    assert url == REVIEW_UPSERT_URL
    assert kwargs["data"] == payload()
    assert kwargs["max_redirects"] == 0
    assert kwargs["headers"] == {"Content-Type": "application/json"}
    assert "timeout" in kwargs
    assert not hasattr(transport, "request")
    assert not hasattr(transport, "write_json")


def test_transport_sends_only_authorization_header() -> None:
    request = FakeRequest()
    transport = BrowserWriteTransport(
        enabled_config(),
        FakeSession(request=request),
        auth_headers={
            "Authorization": "Bearer test-token",
            "apikey": "ignored",
            "Range": "0-1",
        },
    )
    transport.post_review(payload())
    _url, kwargs = request.calls[0]
    assert kwargs["headers"] == {
        "Content-Type": "application/json",
        "authorization": "Bearer test-token",
    }


def test_transport_rejects_non_jazerei_session_domain() -> None:
    request = FakeRequest()
    transport = BrowserWriteTransport(
        enabled_config(), FakeSession(page_url="https://evil.example/", request=request)
    )
    with pytest.raises(WriteBlockedError):
        transport.post_review(payload())
    assert request.calls == []


@pytest.mark.parametrize(
    "bad_payload",
    [
        {**payload(), "game_id": True},
        {**payload(), "game_id": 0},
        {**payload(), "rating": 11},
        {**payload(), "rating": True},
        {**payload(), "text": 1},
        {**payload(), "liked": None},
        {**payload(), "liked": 0},
        {**payload(), "extra": 1},
        {"game_id": 26570, "rating": 2.0, "text": "x"},
    ],
)
def test_transport_rejects_invalid_payload_before_network(bad_payload: dict) -> None:
    request = FakeRequest()
    transport = BrowserWriteTransport(enabled_config(), FakeSession(request=request))
    with pytest.raises(PayloadError):
        transport.post_review(bad_payload)
    assert request.calls == []


def test_transport_timeout_is_ambiguous_and_not_retried() -> None:
    class TimeoutErrorAfterSend(Exception):
        pass

    request = FakeRequest(response=TimeoutErrorAfterSend("timeout"))
    transport = BrowserWriteTransport(enabled_config(), FakeSession(request=request))
    with pytest.raises(WriteTimeout):
        transport.post_review(payload())
    assert len(request.calls) == 1


def test_transport_generic_failure_is_not_retried() -> None:
    request = FakeRequest(response=ConnectionResetError("reset"))
    transport = BrowserWriteTransport(enabled_config(), FakeSession(request=request))
    with pytest.raises(ExecutionError):
        transport.post_review(payload())
    assert len(request.calls) == 1


@pytest.mark.parametrize("status", [400, 422, 500, 503, 302])
def test_transport_returns_status_without_following_or_retrying(status: int) -> None:
    request = FakeRequest(response=FakeResponse(status, {"error": "x"}))
    transport = BrowserWriteTransport(enabled_config(), FakeSession(request=request))
    result = transport.post_review(payload())
    assert result.status_code == status
    assert len(request.calls) == 1


def test_execute_action_accepts_only_concrete_gateway_transport() -> None:
    class RealLike:
        simulated = False

        def post_review(self, payload):
            raise AssertionError("nao deveria ser chamado")

    with pytest.raises(WriteBlockedError):
        # Objeto real-like sem ser o transporte concreto do gateway.
        execute_action(
            enabled_config(data_dir="data"),
            execution_id="missing",
            action_id="missing",
            transport=RealLike(),
            read_state=lambda game_id: None,
            approvals_path=None,
        )


def test_real_transport_with_fake_session_verifies_end_to_end(tmp_path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    config = enabled_config(data_dir=ws.config.data_dir, jazerei_user_id="user-1")
    request = FakeRequest()
    transport = BrowserWriteTransport(config, FakeSession(request=request))
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        current_snapshot(
            game_id=26570,
            slug="demolition-girl",
            review=DestinationReview(
                id="user-1_26570",
                game_id=26570,
                game_slug="demolition-girl",
                user_id="user-1",
                rating=2.0,
                text="Texto original",
                liked=False,
            ),
        ),
    ]
    result = execute_action(
        config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        transport=transport,
        read_state=lambda game_id: states.pop(0),
        approvals_path=ws.approvals,
    )
    assert len(request.calls) == 1
    assert result.status.value == "completed"
    assert result.actions[0].status.value == "verified"
    assert request.calls[0][0] == REVIEW_UPSERT_URL


def test_real_transport_simulated_2xx_with_missing_review_is_ambiguous(tmp_path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    config = enabled_config(data_dir=ws.config.data_dir, jazerei_user_id="user-1")
    request = FakeRequest()
    transport = BrowserWriteTransport(config, FakeSession(request=request))
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
    ]
    result = execute_action(
        config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        transport=transport,
        read_state=lambda game_id: states.pop(0),
        approvals_path=ws.approvals,
    )
    assert len(request.calls) == 1
    assert result.actions[0].status.value == "write_ambiguous"
    assert result.actions[0].reason == "review_not_found_after_write"
    assert result.status.value == "inconclusive"


def test_real_transport_simulated_2xx_with_divergence_is_conflict(tmp_path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    config = enabled_config(data_dir=ws.config.data_dir, jazerei_user_id="user-1")
    request = FakeRequest()
    transport = BrowserWriteTransport(config, FakeSession(request=request))
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        current_snapshot(game_id=26570, slug="demolition-girl", review=applied_review(rating=9.0)),
    ]
    result = execute_action(
        config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        transport=transport,
        read_state=lambda game_id: states.pop(0),
        approvals_path=ws.approvals,
    )
    assert len(request.calls) == 1
    assert result.actions[0].status.value == "conflict"
    assert result.actions[0].reason == "post_write_verification_mismatch"


def test_real_transport_simulated_inconclusive_verification_is_ambiguous(tmp_path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    config = enabled_config(data_dir=ws.config.data_dir, jazerei_user_id="user-1")
    request = FakeRequest()
    transport = BrowserWriteTransport(config, FakeSession(request=request))
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        current_snapshot(
            game_id=26570, slug="demolition-girl", review=applied_review(), complete=False
        ),
    ]
    result = execute_action(
        config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        transport=transport,
        read_state=lambda game_id: states.pop(0),
        approvals_path=ws.approvals,
    )
    assert len(request.calls) == 1
    assert result.actions[0].status.value == "write_ambiguous"
    assert result.status.value == "inconclusive"


def test_simulated_transport_still_works(tmp_path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    result = run_action(
        ws,
        execution,
        transport=FakeTransport(),
        states=[
            current_snapshot(game_id=26570, slug="demolition-girl", review=None),
            current_snapshot(game_id=26570, slug="demolition-girl", review=applied_review()),
        ],
    )
    assert result.actions[0].status.value == "verified"
