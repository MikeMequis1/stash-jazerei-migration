from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from src.config import Config
from src.jazerei.snapshots import write_account_snapshot_run
from src.jazerei.state import PaginationResult, build_account_review_snapshot
from src.migration.orchestrator import PrepareError, PrepareOrchestrator, PrepareStore
from src.migration.setup import WizardState
from src.migration.wizard import MigrationService
from src.review_ui import JobRunner, OperationGate, ReviewService, build_wizard_payload

from tests.test_review_ui import workspace


class FakeOperations:
    """Operações de navegador substituídas; registra chamadas."""

    def __init__(self, *, fail_collect: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_collect = fail_collect

    def open_stash(self, config, *, progress=None, should_cancel=None):
        self.calls.append("open_stash")
        return {"logged_in": True}

    def collect_stash(self, config, *, progress=None, should_cancel=None):
        self.calls.append("collect_stash")
        if self.fail_collect:
            raise RuntimeError("coleta falhou")
        return {"run_id": "novo-run", "outcome": "complete", "manifest": "manifest.json"}

    def verify_jazerei(self, config, *, progress=None, should_cancel=None):
        self.calls.append("verify_jazerei")
        return {"logged_in": True, "user_id": "user-1"}

    def login_jazerei(self, config, *, progress=None, should_cancel=None):
        self.calls.append("login_jazerei")
        return {"logged_in": True, "user_id": "user-1"}

    def snapshot_destination(self, config, *, progress=None, should_cancel=None):
        self.calls.append("snapshot_destination")
        return {
            "run_id": "novo-snapshot",
            "complete": True,
            "pagination_complete": True,
        }

    def ensure_matches(self, service, *, progress=None, should_cancel=None):
        self.calls.append("ensure_matches")
        return {"added": 0}


def make_workspace(tmp_path: Path):
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
    run_dir = tmp_path / "stash" / "runs" / "source"
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    paths.source_run.rename(run_dir)
    paths = replace(
        paths,
        source_run=run_dir,
        account_snapshot=artifact.snapshot_path,
        ratifications=tmp_path / "migration" / "ratifications.jsonl",
    )
    config = Config(data_dir=tmp_path)
    service = ReviewService(config, paths)
    state = WizardState(path=tmp_path / "migration" / "wizard-state.json")
    state.identity = {
        "jazerei": {
            "logged_in": True,
            "user_id": "user-1",
            "checked_at": "2026-11-26T00:00:00+00:00",
            "label": "user…-1",
        }
    }
    migration = MigrationService(config, approvals=config.migration_dir / "approvals.jsonl")
    return config, service, state, migration, paths


def test_prepare_pauses_for_login_and_continues(tmp_path: Path) -> None:
    config, service, state, migration, _ = make_workspace(tmp_path)
    state.identity = {}
    ops = FakeOperations()

    def not_logged(config, *, progress=None, should_cancel=None):
        ops.calls.append("verify_jazerei")
        return {"logged_in": False}

    ops.verify_jazerei = not_logged
    orchestrator = PrepareOrchestrator(
        config, service, state, migration, operations=ops, store=PrepareStore(config)
    )

    result = orchestrator.run(apply_policy=True)

    assert result["status"] == "done"
    assert ops.calls[:2] == ["verify_jazerei", "login_jazerei"]
    assert state.identity["jazerei"]["logged_in"] is True
    assert PrepareStore(config).latest()["status"] == "done"


def test_scan_reuses_valid_artifacts_and_flags_policy(tmp_path: Path) -> None:
    config, service, state, migration, _ = make_workspace(tmp_path)
    ops = FakeOperations()
    orchestrator = PrepareOrchestrator(
        config, service, state, migration, operations=ops, store=PrepareStore(config)
    )

    scan = orchestrator.scan()
    statuses = {step["id"]: step["status"] for step in scan["steps"]}
    assert statuses["source"] == "done"
    assert statuses["destination"] == "done"
    assert statuses["matches"] == "done"
    assert statuses["policy"] == "needs_confirmation"
    assert statuses["plan"] == "pending"
    assert scan["needs_policy_confirmation"] is True
    assert ops.calls == []


def test_policy_confirmation_pauses_without_writing(tmp_path: Path) -> None:
    config, service, state, migration, paths = make_workspace(tmp_path)
    ops = FakeOperations()
    orchestrator = PrepareOrchestrator(
        config, service, state, migration, operations=ops, store=PrepareStore(config)
    )

    result = orchestrator.run(apply_policy=False)

    assert result["status"] == "waiting_confirmation"
    assert result["pause"] == "policy"
    assert service.state()["summary"]["approved"] == 0
    assert not list((paths.plans).glob("*")) if paths.plans.exists() else True
    latest = PrepareStore(config).latest()
    assert latest["status"] == "waiting_confirmation"
    assert ops.calls == []


def test_confirmed_policy_generates_and_validates_plan(tmp_path: Path) -> None:
    config, service, state, migration, paths = make_workspace(tmp_path)
    ops = FakeOperations()
    orchestrator = PrepareOrchestrator(
        config, service, state, migration, operations=ops, store=PrepareStore(config)
    )

    result = orchestrator.run(apply_policy=True)

    assert result["status"] == "done"
    assert result["plan_id"] == state.active_plan_id
    assert service.state()["summary"]["approved"] >= 1
    assert (
        next(item for item in service.state()["items"] if item["slug"] == "strong")["review_status"]
        == "approved"
    )
    assert (paths.plans / result["plan_id"] / "plan.json").is_file()
    assert migration.status(result["plan_id"])["provenance_complete"] is True
    latest = PrepareStore(config).latest()
    assert latest["status"] == "done"
    assert ops.calls == []


def test_second_run_reuses_valid_plan(tmp_path: Path) -> None:
    config, service, state, migration, paths = make_workspace(tmp_path)
    ops = FakeOperations()
    orchestrator = PrepareOrchestrator(
        config, service, state, migration, operations=ops, store=PrepareStore(config)
    )
    first = orchestrator.run(apply_policy=True)
    before = sorted(path.name for path in paths.plans.iterdir() if path.is_dir())

    second = orchestrator.run(apply_policy=True)
    after = sorted(path.name for path in paths.plans.iterdir() if path.is_dir())

    assert second["plan_id"] == first["plan_id"]
    assert after == before
    assert ops.calls == []


def test_human_decisions_survive_confirmed_policy(tmp_path: Path) -> None:
    config, service, state, migration, paths = make_workspace(tmp_path)
    service.decide_match({"slug": "strong", "action": "approve", "candidate_id": 100})
    service.decide_liked({"slug": "strong", "value": False})
    before_match = next(
        item
        for item in json.loads(paths.matches.read_text(encoding="utf-8"))
        if item["source_slug"] == "strong"
    )
    before_liked = paths.liked.read_text(encoding="utf-8")
    ops = FakeOperations()
    orchestrator = PrepareOrchestrator(
        config, service, state, migration, operations=ops, store=PrepareStore(config)
    )

    orchestrator.run(apply_policy=True)

    after_match = next(
        item
        for item in json.loads(paths.matches.read_text(encoding="utf-8"))
        if item["source_slug"] == "strong"
    )
    assert after_match["decision"] == before_match["decision"] == "approved"
    assert after_match["dest_game_id"] == before_match["dest_game_id"] == 100
    assert paths.liked.read_text(encoding="utf-8").startswith(before_liked)


def test_batch_ratification_regenerates_plan_without_changing_decisions(tmp_path: Path) -> None:
    config, service, state, migration, paths = make_workspace(tmp_path)
    orchestrator = PrepareOrchestrator(
        config,
        service,
        state,
        migration,
        operations=FakeOperations(),
        store=PrepareStore(config),
    )
    first = orchestrator.run(apply_policy=True)
    assert first["actions"] == 0
    matches_before = paths.matches.read_bytes()
    liked_before = paths.liked.read_bytes()

    result = service.ratify_automatic(first["plan_id"])

    assert result["ratified_matches"] >= 1
    assert result["ratified_liked"] >= 1
    assert result["plan_id"] and result["plan_id"] != first["plan_id"]
    assert result["actions"] >= 1
    assert paths.matches.read_bytes() == matches_before
    assert paths.liked.read_bytes() == liked_before
    lines = paths.ratifications.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert migration.status(result["plan_id"])["actions"] >= 1


def test_prior_write_ambiguous_blocks_preparation(tmp_path: Path) -> None:
    config, service, state, _, _ = make_workspace(tmp_path)

    class AmbiguousMigration:
        def list_executions(self, plan_id=None):
            return [{"execution_id": "old", "summary": {"ambiguous": 1}}]

        def execution_detail(self, execution_id):
            return {
                "execution_id": execution_id,
                "actions": [
                    {
                        "action_id": "a1",
                        "source_ref": "pseudoregalia",
                        "game_id": 123,
                        "status": "write_ambiguous",
                    }
                ],
            }

        def status(self, plan_id):
            return {"exists": False}

    ops = FakeOperations()
    orchestrator = PrepareOrchestrator(
        config,
        service,
        state,
        AmbiguousMigration(),
        operations=ops,
        store=PrepareStore(config),
    )

    scan = orchestrator.scan()
    assert scan["safety"]["blocked"] is True
    with pytest.raises(PrepareError) as error:
        orchestrator.run(apply_policy=True)
    assert error.value.code == "prior_write_ambiguous"
    assert PrepareStore(config).latest()["status"] == "failed"
    assert ops.calls == []


def test_collection_failure_keeps_previous_state_and_stops(tmp_path: Path) -> None:
    config, service, state, migration, paths = make_workspace(tmp_path)
    missing = replace(paths, source_run=tmp_path / "stash" / "missing")
    service = ReviewService(config, missing)
    ops = FakeOperations(fail_collect=True)
    orchestrator = PrepareOrchestrator(
        config, service, state, migration, operations=ops, store=PrepareStore(config)
    )

    with pytest.raises(RuntimeError, match="coleta falhou"):
        orchestrator.run(apply_policy=True)

    latest = PrepareStore(config).latest()
    assert latest["status"] == "failed"
    assert latest["result"] is None
    assert not list(paths.plans.glob("*")) if paths.plans.exists() else True
    assert ops.calls == ["open_stash", "collect_stash"]


def test_invalid_plan_is_not_published_as_ready(tmp_path: Path) -> None:
    config, service, state, _, _ = make_workspace(tmp_path)

    class InvalidMigration:
        def list_executions(self, plan_id=None):
            return []

        def execution_detail(self, execution_id):
            return {}

        def status(self, plan_id):
            return {
                "plan_id": plan_id,
                "exists": True,
                "invalid": ["hash divergente"],
                "stale": [],
                "provenance_complete": True,
                "actions": 1,
                "unmatched": 0,
            }

    ops = FakeOperations()
    orchestrator = PrepareOrchestrator(
        config,
        service,
        state,
        InvalidMigration(),
        operations=ops,
        store=PrepareStore(config),
    )

    with pytest.raises(PrepareError) as error:
        orchestrator.run(apply_policy=True)

    assert error.value.code == "plan_invalid"
    assert PrepareStore(config).latest()["status"] == "failed"


def test_startup_payload_does_not_generate_plan_or_select_history(tmp_path: Path) -> None:
    config, service, state, migration, paths = make_workspace(tmp_path)
    before = sorted(path.name for path in paths.plans.iterdir()) if paths.plans.exists() else []

    payload = build_wizard_payload(
        config=config,
        service=service,
        migration=migration,
        state=state,
        jobs=JobRunner(OperationGate()),
    )

    after = sorted(path.name for path in paths.plans.iterdir()) if paths.plans.exists() else []
    assert payload["active_plan_id"] is None
    assert payload["active_execution_id"] is None
    assert after == before
    assert payload["prepare"]["needs_policy_confirmation"] is True


def test_gates_remain_off_and_no_write_path_is_used(tmp_path: Path) -> None:
    config, service, state, migration, _ = make_workspace(tmp_path)
    assert config.allow_writes is False
    assert config.enable_real_write_transport is False

    ops = FakeOperations()
    orchestrator = PrepareOrchestrator(
        config, service, state, migration, operations=ops, store=PrepareStore(config)
    )
    orchestrator.run(apply_policy=True)

    assert "execute" not in ops.calls
    assert all("write" not in call for call in ops.calls)
