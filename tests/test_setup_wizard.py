from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from src.config import Config
from src.jazerei.snapshots import write_account_snapshot_run
from src.jazerei.state import PaginationResult, build_account_review_snapshot
from src.migration.setup import WizardState
from src.migration.wizard import MigrationService
from src.review_ui import (
    JobBusy,
    JobRunner,
    OperationGate,
    ReviewService,
    build_wizard_payload,
)
from src.storage import read_json, sha256_file, write_json, write_manifest

from tests.test_review_ui import workspace


def review_paths_with_snapshot(tmp_path: Path):
    paths = workspace(tmp_path)
    snapshot = build_account_review_snapshot(
        expected_user_id="user-1",
        pagination=PaginationResult(
            reviews=[],
            pages_fetched=1,
            records_received=0,
            duplicates_discarded=0,
            termination_reason="empty_page",
            content_range_total=0,
        ),
        page_size=50,
        max_pages=100,
    )
    artifact = write_account_snapshot_run(
        Config(data_dir=tmp_path), snapshot, run_id="snapshot-empty"
    )
    return replace(paths, account_snapshot=artifact.snapshot_path)


def real_config() -> Config:
    return Config()


def _historical_data_available() -> bool:
    return (
        real_config().migration_dir / "plans" / "stage9-review-20261125T201911Z" / "plan.json"
    ).is_file()


def test_prepare_interface_does_not_apply_policy_or_write(tmp_path: Path) -> None:
    from src.review_ui import prepare_interface

    config = real_config()
    watched = [
        config.migration_dir / "matches.json",
        config.migration_dir / "liked-decisions.jsonl",
        config.migration_dir / "stage9" / "match-decisions.jsonl",
    ]
    before = {path: sha256_file(path) for path in watched if path.exists()}
    state_path = config.migration_dir / "wizard-state.json"
    state_before = state_path.read_text(encoding="utf-8") if state_path.exists() else None

    prepare_interface(config)

    after = {path: sha256_file(path) for path in watched if path.exists()}
    assert after == before
    if state_before is None:
        assert state_path.exists() is False
    else:
        assert state_path.read_text(encoding="utf-8") == state_before


def test_wizard_state_roundtrip_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "migration" / "wizard-state.json"
    state = WizardState(path=path)
    state.active_plan_id = "plan-1"
    state.source_run_id = "source-1"
    state.identity = {"jazerei": {"logged_in": True, "user_id": "user-1"}}
    state.save()

    loaded = WizardState.load(Config(data_dir=tmp_path))
    assert loaded.active_plan_id == "plan-1"
    assert loaded.source_run_id == "source-1"
    assert loaded.identity["jazerei"]["logged_in"] is True
    assert not list(path.parent.glob("*.tmp"))


def test_job_runner_cancels_cooperatively_and_releases_gate() -> None:
    gate = OperationGate()
    runner = JobRunner(gate)
    started = threading.Event()

    def operation(*, progress, should_cancel):
        started.set()
        while not should_cancel():
            time.sleep(0.01)
        return {"ok": True}

    runner.start("test", "Teste", operation)
    assert started.wait(2)
    assert gate.busy()
    with pytest.raises(JobBusy):
        runner.start("other", "Outro", operation)

    runner.cancel()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = runner.current()
        if job and job["status"] == "cancelled":
            break
        time.sleep(0.02)
    assert runner.current()["status"] == "cancelled"
    assert gate.busy() is False


def test_state_uses_active_account_snapshot_for_destination(tmp_path: Path) -> None:
    paths = review_paths_with_snapshot(tmp_path)
    service = ReviewService(Config(data_dir=tmp_path), paths)
    state = service.state()
    assert state["summary"]["already_exists"] == 0
    existing = next(item for item in state["items"] if item["slug"] == "existing")
    assert existing["initial_classification"] == "strong_candidate"

    legacy = ReviewService(
        Config(data_dir=tmp_path / "other"),
        replace(paths, account_snapshot=None),
    ).state()
    assert legacy["summary"]["already_exists"] == 1


def test_policy_preview_does_not_write_artifacts(tmp_path: Path) -> None:
    paths = workspace(tmp_path)
    service = ReviewService(Config(data_dir=tmp_path), paths)
    before = {
        "matches": sha256_file(paths.matches),
        "liked": paths.liked.exists(),
        "history": paths.history.exists(),
    }
    preview = service.apply_automatic_policy(dry_run=True)
    assert preview["auto_approved"] == 1
    assert sha256_file(paths.matches) == before["matches"]
    assert paths.liked.exists() is before["liked"]
    assert paths.history.exists() is before["history"]

    applied = service.apply_automatic_policy()
    assert applied["auto_approved"] == 1
    assert sha256_file(paths.matches) != before["matches"]
    assert paths.liked.exists()


def test_incremental_matches_preserve_existing_decisions(tmp_path: Path) -> None:
    paths = workspace(tmp_path)
    service = ReviewService(Config(data_dir=tmp_path), paths)
    service.decide_match({"slug": "strong", "action": "approve", "candidate_id": 100})
    before = next(item for item in read_json(paths.matches) if item["source_slug"] == "strong")

    reviews = read_json(paths.source_run / "reviews.json")
    reviews.append(
        {
            "game_slug": "new-game",
            "game_title": "New Game",
            "text": "nova review",
            "rating": 8.0,
            "date": "2025-01-01T00:00:00Z",
            "status": "beaten",
            "source_url": "https://stash.games/games/new-game/reviews/MikeMequis1",
            "raw": {"text_listing": "nova review", "text_detail": "nova review"},
        }
    )
    write_json(paths.source_run / "reviews.json", reviews, overwrite=True)
    games = read_json(paths.source_run / "games.json")
    games.append(
        {
            "slug": "new-game",
            "title": "New Game",
            "year": 2025,
            "rating": 8.0,
            "source_url": "https://stash.games/games/new-game",
        }
    )
    write_json(paths.source_run / "games.json", games, overwrite=True)
    write_manifest(paths.source_run, {"schema_version": 1, "run_id": "source"}, overwrite=True)

    result = service.ensure_match_entries(with_candidates=False)
    assert result["added"] == 1
    after = read_json(paths.matches)
    preserved = next(item for item in after if item["source_slug"] == "strong")
    assert preserved["decision"] == before["decision"] == "approved"
    assert preserved["dest_game_id"] == before["dest_game_id"] == 100
    assert any(item["source_slug"] == "new-game" for item in after)


def test_wizard_payload_is_read_only_and_recovers_executions(tmp_path: Path) -> None:
    if not _historical_data_available():
        pytest.skip("dados históricos removidos pelo reset absoluto")
    config = real_config()
    service = ReviewService(config)
    state = WizardState(path=tmp_path / "wizard-state.json")
    migration = MigrationService(config, approvals=config.migration_dir / "approvals.jsonl")
    watched = [
        config.migration_dir / "matches.json",
        config.migration_dir / "liked-decisions.jsonl",
        config.migration_dir / "approvals.jsonl",
        config.migration_dir / "stage9" / "match-decisions.jsonl",
        config.migration_dir / "stage9" / "ratifications.jsonl",
        config.migration_dir / "plans" / "stage9-review-20261125T201911Z" / "plan.json",
        config.migration_dir / "plans" / "stage9-review-20261125T201911Z" / "manifest.json",
        config.migration_dir
        / "executions"
        / "20261125T224312Z-execution-stage9-review-20261125T201911Z"
        / "execution.json",
    ]
    before = {path: sha256_file(path) for path in watched if path.exists()}

    payload = build_wizard_payload(
        config=config,
        service=service,
        migration=migration,
        state=state,
        jobs=JobRunner(OperationGate()),
    )

    assert payload["active_plan_id"] is None
    assert payload["active_execution_id"] is None
    assert payload["active_execution"] is None
    previous = next(
        item
        for item in payload["all_executions"]
        if item["execution_id"] == "20261125T221529Z-execution-stage9-review-20261125T201911Z"
    )
    assert previous["resumable"] is False
    assert previous["blocking"][0]["source_ref"] == "pseudoregalia"
    assert previous["blocking"][0]["status"] == "write_ambiguous"
    latest = next(
        item
        for item in payload["all_executions"]
        if item["execution_id"] == "20261125T224312Z-execution-stage9-review-20261125T201911Z"
    )
    assert latest["resumable"] is True
    assert [step["id"] for step in payload["steps"]] == ["prepare", "review", "result"]
    assert payload["steps"][0]["status"] == "blocked"
    assert payload["prepare"]["safety"]["blocked"] is True

    after = {path: sha256_file(path) for path in watched if path.exists()}
    assert after == before
    assert state.path.exists() is False


def test_wizard_select_refuses_input_change_while_execution_is_resumable(tmp_path: Path) -> None:
    if not _historical_data_available():
        pytest.skip("dados históricos removidos pelo reset absoluto")
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from src.review_ui import ReviewUIHandler

    config = real_config()
    service = ReviewService(config)
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
            data=json.dumps({"source_run_id": "stage9-full-stash-browser-20261125"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=10)
        assert error.value.code == 400
        body = json.loads(error.value.read().decode("utf-8"))
        assert body["code"] == "inputs_locked"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_job_endpoint_records_identity_and_releases_gate(tmp_path: Path, monkeypatch) -> None:
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen

    import src.review_ui as review_ui

    paths = workspace(tmp_path)
    service = ReviewService(Config(data_dir=tmp_path), paths)
    state = WizardState(path=tmp_path / "migration" / "wizard-state.json")
    gate = OperationGate()

    monkeypatch.setattr(
        review_ui,
        "open_stash",
        lambda config, *, progress=None, should_cancel=None: {
            "site": "stash",
            "logged_in": True,
            "user": "MikeMequis1",
            "label": "@MikeMequis1",
        },
    )

    class Handler(review_ui.ReviewUIHandler):
        pass

    Handler.service = service
    Handler.wizard_state = state
    Handler.job_runner = JobRunner(gate)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = Request(
            f"{base}/api/jobs",
            data=json.dumps({"kind": "stash_access"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["job"]["kind"] == "stash_access"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with urlopen(f"{base}/api/jobs/current", timeout=5) as response:
                job = json.loads(response.read().decode("utf-8"))["job"]
            if job and job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert job["status"] == "done"
        loaded = WizardState.load(Config(data_dir=tmp_path))
        assert loaded.identity["stash"]["logged_in"] is True
        assert gate.busy() is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_match_decision_with_snapshot_does_not_read_legacy_destination(tmp_path: Path) -> None:
    paths = review_paths_with_snapshot(tmp_path)
    paths = replace(paths, destination_run=tmp_path / "jazerei" / "missing")
    service = ReviewService(Config(data_dir=tmp_path), paths)

    state = service.decide_match({"slug": "strong", "action": "approve", "candidate_id": 100})

    strong = next(item for item in state["items"] if item["slug"] == "strong")
    assert strong["review_status"] == "approved"
    assert strong["selected_candidate_id"] == 100


def test_generated_plan_does_not_reference_legacy_destination(tmp_path: Path) -> None:
    paths = review_paths_with_snapshot(tmp_path)
    paths = replace(paths, destination_run=tmp_path / "jazerei" / "missing")
    service = ReviewService(Config(data_dir=tmp_path), paths)

    result = service.generate_plan()

    manifest = read_json(Path(result["path"]) / "manifest.json")
    assert manifest["inputs"]["destination_reviews"] == ""
    assert manifest["destination"]["snapshot_run_id"] == "snapshot-empty"


def test_generated_plan_becomes_active(tmp_path: Path) -> None:
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen

    from src.review_ui import ReviewUIHandler

    paths = workspace(tmp_path)
    service = ReviewService(Config(data_dir=tmp_path), paths)
    state = WizardState(path=tmp_path / "migration" / "wizard-state.json")

    class Handler(ReviewUIHandler):
        pass

    Handler.service = service
    Handler.wizard_state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = Request(
            f"{base}/api/plan",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["activated"] is True
        assert state.active_plan_id == body["plan_id"]
        assert (paths.plans / body["plan_id"] / "plan.json").is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
