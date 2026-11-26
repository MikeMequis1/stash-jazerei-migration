from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from src import cli
from src.config import Config
from src.jazerei.state import PaginationResult, build_review_snapshot
from src.migration.approval import (
    BundleError,
    action_hash,
    canonical_hash,
    canonical_json,
    load_approvals,
    load_bundle,
    verify_bundle,
)
from src.migration.liked import append_decision, review_version_hash
from src.models.normalized import GameMatch, MatchDecision, MigrationPlan
from src.models.source import GameRecord, ReviewRecord
from src.storage import read_json, write_json, write_manifest


def setup_workspace(tmp_path: Path) -> SimpleNamespace:
    config = Config(data_dir=tmp_path)
    source_run = tmp_path / "stash" / "runs" / "source-run"
    source_run.mkdir(parents=True)
    game = GameRecord(
        slug="demolition-girl",
        title="Demolition Girl",
        source_url="https://stash.games/games/demolition-girl",
        year=2004,
        rating=2.0,
    )
    review = ReviewRecord(
        game_slug="demolition-girl",
        game_title="Demolition Girl",
        text="Texto original",
        rating=2.0,
        source_url="https://stash.games/games/demolition-girl/reviews/MikeMequis1",
    )
    write_json(source_run / "games.json", [game])
    write_json(source_run / "reviews.json", [review])
    write_manifest(
        source_run, {"schema_version": 1, "run_id": source_run.name, "outcome": "directed"}
    )

    snapshot_run = tmp_path / "jazerei" / "runs" / "snapshot-run"
    snapshot_path = snapshot_run / "review-state" / "demolition-girl-26570.json"
    snapshot = build_review_snapshot(
        game_id=26570,
        slug="demolition-girl",
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
        identity_sources=("firebase_indexeddb", "get_my_profile"),
    )
    write_json(snapshot_path, snapshot)
    write_manifest(
        snapshot_run,
        {"schema_version": 1, "run_id": snapshot_run.name, "outcome": "absent"},
    )

    matches_path = tmp_path / "migration" / "matches.json"
    write_json(
        matches_path,
        [
            GameMatch(
                source_slug="demolition-girl",
                source_title="Demolition Girl",
                source_year=2004,
                dest_game_id=26570,
                dest_slug="demolition-girl",
                dest_name="Demolition Girl",
                dest_year=2004,
                confidence=0.9,
                decision=MatchDecision.APPROVED,
            )
        ],
    )
    decisions_path = tmp_path / "migration" / "liked-decisions.jsonl"
    append_decision(
        decisions_path,
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=review_version_hash(review),
        value=False,
    )
    return SimpleNamespace(
        config=config,
        source_run=source_run,
        game=game,
        review=review,
        snapshot_run=snapshot_run,
        snapshot_path=snapshot_path,
        matches_path=matches_path,
        decisions_path=decisions_path,
        plan_id="plan-1",
        approvals=tmp_path / "migration" / "approvals.jsonl",
    )


def plan_args(ws: SimpleNamespace, *, plan_id: str = "plan-1") -> SimpleNamespace:
    return SimpleNamespace(
        matches=str(ws.matches_path),
        reviews=str(ws.source_run / "reviews.json"),
        games=str(ws.source_run / "games.json"),
        destination_reviews="",
        destination_snapshot=str(ws.snapshot_path),
        liked_decisions=str(ws.decisions_path),
        favorite=[],
        playing_now=[],
        watchlist=[],
        plan_id=plan_id,
    )


def test_plan_manifest_and_approval_roundtrip(tmp_path: Path) -> None:
    ws = setup_workspace(tmp_path)
    assert cli.cmd_plan(ws.config, plan_args(ws)) == 0
    plan_dir = ws.config.migration_dir / "plans" / "plan-1"
    assert (plan_dir / "plan.json").exists()
    assert (plan_dir / "manifest.json").exists()
    manifest = read_json(plan_dir / "manifest.json")
    assert manifest["provenance_complete"] is True
    assert manifest["plan"]["action_count"] == 1
    assert verify_bundle(ws.config, load_bundle(ws.config, "plan-1")).invalid == []
    assert verify_bundle(ws.config, load_bundle(ws.config, "plan-1")).stale == []

    status = cli.cmd_migration_approval_status(
        ws.config, SimpleNamespace(plan_id="plan-1", approvals=str(ws.approvals))
    )
    assert status == 1  # unapproved
    assert (
        cli.cmd_migration_approve(
            ws.config,
            SimpleNamespace(plan_id="plan-1", confirm="wrong", approvals=str(ws.approvals)),
        )
        == 1
    )
    assert not ws.approvals.exists()

    assert (
        cli.cmd_migration_approve(
            ws.config,
            SimpleNamespace(plan_id="plan-1", confirm="plan-1", approvals=str(ws.approvals)),
        )
        == 0
    )
    assert len(load_approvals(ws.approvals)) == 1
    assert (
        cli.cmd_migration_approval_status(
            ws.config, SimpleNamespace(plan_id="plan-1", approvals=str(ws.approvals))
        )
        == 0
    )
    approval = load_approvals(ws.approvals)[0]
    assert approval.approved_actions == [manifest["plan"]["actions"][0]["action_id"]]


def test_source_change_makes_approval_stale(tmp_path: Path) -> None:
    ws = setup_workspace(tmp_path)
    assert cli.cmd_plan(ws.config, plan_args(ws)) == 0
    assert (
        cli.cmd_migration_approve(
            ws.config,
            SimpleNamespace(plan_id="plan-1", confirm="plan-1", approvals=str(ws.approvals)),
        )
        == 0
    )

    changed = ReviewRecord.model_validate(read_json(ws.source_run / "reviews.json")[0])
    changed.text = "Texto alterado"
    write_json(ws.source_run / "reviews.json", [changed], overwrite=True)
    # Manifest deliberately not regenerated: source file hash changed.
    check = cli.cmd_migration_approval_status(
        ws.config, SimpleNamespace(plan_id="plan-1", approvals=str(ws.approvals))
    )
    assert check == 2


def test_plan_change_invalidates_approval(tmp_path: Path) -> None:
    ws = setup_workspace(tmp_path)
    assert cli.cmd_plan(ws.config, plan_args(ws)) == 0
    assert (
        cli.cmd_migration_approve(
            ws.config,
            SimpleNamespace(plan_id="plan-1", confirm="plan-1", approvals=str(ws.approvals)),
        )
        == 0
    )
    plan_path = ws.config.migration_dir / "plans" / "plan-1" / "plan.json"
    plan = read_json(plan_path)
    plan["actions"][0]["after"]["rating"] = 9
    write_json(plan_path, plan, overwrite=True)
    assert (
        cli.cmd_migration_approval_status(
            ws.config, SimpleNamespace(plan_id="plan-1", approvals=str(ws.approvals))
        )
        == 2
    )


def test_unresolved_plan_cannot_be_approved(tmp_path: Path) -> None:
    ws = setup_workspace(tmp_path)
    ws.decisions_path.unlink()
    assert cli.cmd_plan(ws.config, plan_args(ws)) == 0
    assert (
        cli.cmd_migration_approve(
            ws.config,
            SimpleNamespace(plan_id="plan-1", confirm="plan-1", approvals=str(ws.approvals)),
        )
        == 2
    )
    assert not ws.approvals.exists()


def test_action_and_plan_hashes_are_stable_and_change_with_content() -> None:
    from src.migration.approval import plan_actions_payload
    from src.migration.planner import build_plan

    source = ReviewRecord(
        game_slug="demolition-girl",
        game_title="Demolition Girl",
        text="Texto",
        rating=2,
        source_url="https://stash.games/games/demolition-girl/reviews/MikeMequis1",
    )
    match = GameMatch(
        source_slug="demolition-girl",
        source_title="Demolition Girl",
        dest_game_id=26570,
        dest_slug="demolition-girl",
        dest_name="Demolition Girl",
        confidence=0.9,
        decision=MatchDecision.APPROVED,
    )
    plan = build_plan([match], [source])
    first = plan_actions_payload(plan)[0]
    plan.actions[0].approved = True
    plan.actions[0].executed = True
    assert plan_actions_payload(plan)[0] == first
    plan.actions[0].after["rating"] = 9
    assert plan_actions_payload(plan)[0] != first
    assert action_hash(plan.actions[0]) == plan_actions_payload(plan)[0]["action_id"]
    assert canonical_hash({"b": 1, "a": 2}) == canonical_hash({"a": 2, "b": 1})
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_new_plan_after_source_change_requires_new_approval(tmp_path: Path) -> None:
    ws = setup_workspace(tmp_path)
    assert cli.cmd_plan(ws.config, plan_args(ws)) == 0
    assert (
        cli.cmd_migration_approve(
            ws.config,
            SimpleNamespace(plan_id="plan-1", confirm="plan-1", approvals=str(ws.approvals)),
        )
        == 0
    )
    old_approval = load_approvals(ws.approvals)[0]

    changed = ReviewRecord.model_validate(read_json(ws.source_run / "reviews.json")[0])
    changed.text = "Texto alterado para nova versao"
    write_json(ws.source_run / "reviews.json", [changed], overwrite=True)
    write_manifest(
        ws.source_run,
        {"schema_version": 1, "run_id": ws.source_run.name, "outcome": "directed"},
        overwrite=True,
    )
    append_decision(
        ws.decisions_path,
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=review_version_hash(changed),
        value=True,
    )
    assert cli.cmd_plan(ws.config, plan_args(ws, plan_id="plan-2")) == 0
    assert (
        cli.cmd_migration_approve(
            ws.config,
            SimpleNamespace(plan_id="plan-2", confirm="plan-2", approvals=str(ws.approvals)),
        )
        == 0
    )
    approvals = load_approvals(ws.approvals)
    assert len(approvals) == 2
    assert approvals[0] == old_approval
    assert (
        cli.cmd_migration_approval_status(
            ws.config, SimpleNamespace(plan_id="plan-1", approvals=str(ws.approvals))
        )
        == 2
    )
    assert (
        cli.cmd_migration_approval_status(
            ws.config, SimpleNamespace(plan_id="plan-2", approvals=str(ws.approvals))
        )
        == 0
    )


def test_manifest_tamper_and_old_plan_is_not_approved(tmp_path: Path) -> None:
    ws = setup_workspace(tmp_path)
    assert cli.cmd_plan(ws.config, plan_args(ws)) == 0
    manifest_path = ws.config.migration_dir / "plans" / "plan-1" / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["plan_sha256"] = "0" * 64
    write_json(manifest_path, manifest, overwrite=True)
    assert (
        cli.cmd_migration_approval_status(
            ws.config, SimpleNamespace(plan_id="plan-1", approvals=str(ws.approvals))
        )
        == 2
    )

    legacy = MigrationPlan()
    assert not legacy.approved
    # Legacy plan has no bundle/manifest and therefore cannot be approved.
    with pytest.raises(BundleError):
        load_bundle(ws.config, "legacy-plan")
