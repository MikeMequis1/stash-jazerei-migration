from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.config import Config
from src.migration.executions import load_execution, save_execution
from src.migration.setup import WizardState
from src.migration.wizard import MigrationError, MigrationService
from src.models.execution import ActionExecutionStatus, ExecutionStatus
from src.review_ui import JobRunner, OperationGate
from src.storage import sha256_file

from tests.test_execution import applied_review, make_execution, workspace
from tests.test_preflight import current_snapshot


def ambiguous_execution(tmp_path: Path):
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    record = load_execution(ws.config, execution.execution_id)
    record.actions[0].status = ActionExecutionStatus.WRITE_AMBIGUOUS
    record.actions[0].reason = "http_401_unconfirmed"
    record.actions[0].write_result = {"status_code": 401, "body": ""}
    record.status = ExecutionStatus.INCONCLUSIVE
    save_execution(ws.config, record, reason="test_ambiguous")
    return ws, execution


def _historical_available() -> bool:
    return (
        Path("data/migration/executions")
        / "20261125T224312Z-execution-stage9-review-20261125T201911Z"
    ).is_dir()


def test_real_execution_detail_exposes_actions_and_controls() -> None:
    if not _historical_available():
        pytest.skip("dados históricos removidos pelo reset absoluto")
    config = Config()
    service = MigrationService(config, approvals=config.migration_dir / "approvals.jsonl")
    detail = service.execution_detail("20261125T224312Z-execution-stage9-review-20261125T201911Z")
    assert len(detail["actions"]) == 70
    assert detail["counts"]["verified"] == 18
    assert detail["counts"]["ready"] == 51
    assert detail["counts"]["failed"] == 1
    assert detail["controls"]["can_resume"] is False  # gates desligados
    assert detail["gates"]["allow_writes"] is False
    failure = next(item for item in detail["actions"] if item["source_ref"] == "resident-evil-5")
    assert failure["reason"] == "pre_write_read_failed"
    assert failure["error"] == "BrowserUnavailable"
    assert failure["rating"] is not None
    assert failure["liked"] is not None
    assert detail["never_resend"]
    assert all(item["status"] != "failed" for item in detail["never_resend"])
    assert [item["source_ref"] for item in detail["resume_actions"]] == ["resident-evil-5"]
    assert detail["events_total"] >= detail["confirmations_total"]


def test_previous_ambiguous_execution_is_read_only_and_unresumable() -> None:
    if not _historical_available():
        pytest.skip("dados históricos removidos pelo reset absoluto")
    config = Config()
    service = MigrationService(config, approvals=config.migration_dir / "approvals.jsonl")
    detail = service.execution_detail("20261125T221529Z-execution-stage9-review-20261125T201911Z")
    assert detail["resumable"] is False
    assert detail["controls"]["can_resume"] is False
    assert detail["controls"]["can_resolve"] is True
    assert "gates de escrita desligados" not in detail["controls"]["blocked_reasons"]
    assert detail["ambiguous_actions"][0]["source_ref"] == "pseudoregalia"
    assert detail["ambiguous_actions"][0]["write_result"]["status_code"] == 401
    assert detail["ambiguous_actions"][0]["reason"] == "http_401_unconfirmed"
    assert [item["source_ref"] for item in detail["never_resend"]] == ["pseudoregalia"]


def test_global_listing_includes_both_real_executions() -> None:
    if not _historical_available():
        pytest.skip("dados históricos removidos pelo reset absoluto")
    config = Config()
    service = MigrationService(config, approvals=config.migration_dir / "approvals.jsonl")
    executions = service.list_executions()
    ids = {item["execution_id"] for item in executions}
    assert "20261125T224312Z-execution-stage9-review-20261125T201911Z" in ids
    assert "20261125T221529Z-execution-stage9-review-20261125T201911Z" in ids
    previous = next(
        item
        for item in executions
        if item["execution_id"] == "20261125T221529Z-execution-stage9-review-20261125T201911Z"
    )
    assert previous["has_ambiguous"] is True
    assert previous["has_conflicts"] is False
    assert previous["resumable"] is False


def test_controls_allow_cancel_while_run_is_active(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    record = load_execution(ws.config, execution.execution_id)
    record.status = ExecutionStatus.RUNNING
    record.actions[0].status = ActionExecutionStatus.WRITING
    save_execution(ws.config, record, reason="test_running")
    service = MigrationService(ws.config, approvals=ws.approvals)
    service._run = {
        "running": True,
        "execution_id": execution.execution_id,
        "status": "running",
    }
    detail = service.execution_detail(execution.execution_id)
    assert detail["controls"]["can_cancel"] is True
    assert detail["controls"]["can_reconcile"] is False
    assert detail["has_interrupted"] is False
    with pytest.raises(MigrationError) as error:
        service.reconcile(execution.execution_id)
    assert error.value.as_dict()["code"] == "run_in_progress"


def test_reconcile_interrupted_write_without_browser(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    record = load_execution(ws.config, execution.execution_id)
    record.actions[0].status = ActionExecutionStatus.WRITING
    record.status = ExecutionStatus.RUNNING
    save_execution(ws.config, record, reason="test_writing")
    service = MigrationService(ws.config, approvals=ws.approvals)

    detail = service.reconcile(execution.execution_id)
    assert detail["actions"][0]["status"] == "write_ambiguous"
    assert detail["status"] == "inconclusive"
    assert detail["controls"]["can_reconcile"] is False
    assert detail["controls"]["can_resolve"] is True
    stored = load_execution(ws.config, execution.execution_id)
    assert stored.actions[0].write_result is None


def test_resolve_ambiguity_uses_snapshot_and_never_posts(tmp_path: Path, monkeypatch) -> None:
    ws, execution = ambiguous_execution(tmp_path)
    snapshot = current_snapshot(
        game_id=26570,
        slug="demolition-girl",
        review=applied_review(),
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("src.browser.session.BrowserSession", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr("src.jazerei.state.capture_review_state", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(
        "src.migration.execution.BrowserWriteTransport",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("transporte não deve ser criado")
        ),
    )

    service = MigrationService(ws.config, approvals=ws.approvals)
    detail = service.resolve_ambiguity(execution.execution_id, execution.action_id)
    assert detail["actions"][0]["status"] == "verified"
    assert detail["status"] == "completed"
    assert detail["resolution_snapshot"]["exists"] is True
    stored = load_execution(ws.config, execution.execution_id)
    assert stored.actions[0].write_result == {"status_code": 401, "body": ""}


def test_resolve_absent_review_keeps_ambiguity_and_preserves_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    ws, execution = ambiguous_execution(tmp_path)
    snapshot = current_snapshot(
        game_id=26570,
        slug="demolition-girl",
        review=None,
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("src.browser.session.BrowserSession", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr("src.jazerei.state.capture_review_state", lambda *args, **kwargs: snapshot)
    service = MigrationService(ws.config, approvals=ws.approvals)
    with pytest.raises(MigrationError) as error:
        service.resolve_ambiguity(execution.execution_id, execution.action_id)
    assert error.value.as_dict()["code"] == "resolution_inconclusive"
    stored = load_execution(ws.config, execution.execution_id)
    assert stored.actions[0].status is ActionExecutionStatus.WRITE_AMBIGUOUS
    assert stored.status is ExecutionStatus.INCONCLUSIVE
    runs = [
        path
        for path in (ws.config.jazerei_dir / "runs").iterdir()
        if path.is_dir() and "review-state" in path.name
    ]
    assert runs


def test_resolve_conflicting_review_marks_conflict_without_post(
    tmp_path: Path, monkeypatch
) -> None:
    ws, execution = ambiguous_execution(tmp_path)
    snapshot = current_snapshot(
        game_id=26570,
        slug="demolition-girl",
        review=applied_review(text="texto divergente", rating=9.0),
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("src.browser.session.BrowserSession", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr("src.jazerei.state.capture_review_state", lambda *args, **kwargs: snapshot)
    service = MigrationService(ws.config, approvals=ws.approvals)
    detail = service.resolve_ambiguity(execution.execution_id, execution.action_id)
    assert detail["actions"][0]["status"] == "conflict"
    assert detail["status"] == "completed_with_errors"
    assert detail["controls"]["can_resume"] is False


def test_execution_http_list_and_detail_are_read_only() -> None:
    if not _historical_available():
        pytest.skip("dados históricos removidos pelo reset absoluto")
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen

    from src.review_ui import ReviewService, ReviewUIHandler

    config = Config()
    service = ReviewService(config)
    migration = MigrationService(config, approvals=config.migration_dir / "approvals.jsonl")
    paths = [
        config.migration_dir
        / "executions"
        / "20261125T224312Z-execution-stage9-review-20261125T201911Z"
        / "execution.json",
        config.migration_dir
        / "executions"
        / "20261125T221529Z-execution-stage9-review-20261125T201911Z"
        / "execution.json",
    ]
    before = {path: sha256_file(path) for path in paths}

    class Handler(ReviewUIHandler):
        pass

    Handler.service = service
    Handler.migration_service = migration
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/api/executions", timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert len(body["executions"]) >= 2
        with urlopen(
            f"{base}/api/executions/20261125T224312Z-execution-stage9-review-20261125T201911Z",
            timeout=20,
        ) as response:
            detail = json.loads(response.read().decode("utf-8"))
        assert detail["counts"]["verified"] == 18
        assert len(detail["actions"]) == 70
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert {path: sha256_file(path) for path in paths} == before


def test_http_reconcile_and_resolve_job_never_post(tmp_path: Path, monkeypatch) -> None:
    if not _historical_available():
        pytest.skip("dados históricos removidos pelo reset absoluto")
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen

    from src.review_ui import ReviewUIHandler

    ws, execution = ambiguous_execution(tmp_path)
    state = WizardState(path=tmp_path / "migration" / "wizard-state.json")
    gate = OperationGate()
    snapshot = current_snapshot(
        game_id=26570,
        slug="demolition-girl",
        review=applied_review(),
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("src.browser.session.BrowserSession", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr("src.jazerei.state.capture_review_state", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(
        "src.migration.execution.BrowserWriteTransport",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("transporte não deve ser criado")
        ),
    )
    service = MigrationService(ws.config, approvals=ws.approvals)

    class Handler(ReviewUIHandler):
        pass

    Handler.service = SimpleNamespace(config=ws.config)
    Handler.wizard_state = state
    Handler.migration_service = service
    Handler.job_runner = JobRunner(gate)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = Request(
            f"{base}/api/jobs",
            data=json.dumps(
                {
                    "kind": "resolve_ambiguity",
                    "params": {
                        "execution_id": execution.execution_id,
                        "action_id": execution.action_id,
                    },
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["job"]["kind"] == "resolve_ambiguity"
        deadline = time.monotonic() + 10
        job = None
        while time.monotonic() < deadline:
            with urlopen(f"{base}/api/jobs/current", timeout=10) as response:
                job = json.loads(response.read().decode("utf-8"))["job"]
            if job and job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert job["status"] == "done", job
        stored = load_execution(ws.config, execution.execution_id)
        assert stored.actions[0].status is ActionExecutionStatus.VERIFIED
        assert gate.busy() is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen

    from src.review_ui import ReviewUIHandler

    config = Config()
    service = ReviewServiceForTest(config)
    state = WizardState(path=tmp_path / "wizard-state.json")
    migration = MigrationService(config, approvals=config.migration_dir / "approvals.jsonl")

    class Handler(ReviewUIHandler):
        pass

    Handler.service = service
    Handler.wizard_state = state
    Handler.migration_service = migration
    Handler.job_runner = JobRunner(OperationGate())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = Request(
            f"{base}/api/wizard/select",
            data=json.dumps(
                {
                    "active_execution_id": (
                        "20261125T221529Z-execution-stage9-review-20261125T201911Z"
                    )
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["active_plan_id"] == "stage9-review-20261125T201911Z"
        assert body["active_execution_id"].startswith("20261125T221529Z")
        assert body["active_execution"]["resumable"] is False
        assert state.active_plan_id == "stage9-review-20261125T201911Z"
        stored = migration.execution_detail(state.active_execution_id)
        assert stored["controls"]["can_resume"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def ReviewServiceForTest(config: Config):
    from src.review_ui import ReviewService

    return ReviewService(config)
