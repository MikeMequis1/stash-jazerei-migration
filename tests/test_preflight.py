from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from src import cli
from src.config import Config
from src.jazerei.state import PaginationResult, build_review_snapshot
from src.migration.liked import append_decision, review_version_hash
from src.migration.preflight import (
    SnapshotEvidence,
    build_result,
    capture_snapshots_for_actions,  # noqa: F401 - imported for API surface checks
    compare_actions,
    prepare_preflight,
)
from src.migration.preflights import (
    load_preflight,
    verify_preflight,
    write_preflight_run,
)
from src.models.destination import DestinationReview
from src.models.normalized import GameMatch, MatchDecision, MigrationAction
from src.models.preflight import ActionStatus, PreflightStatus, PreflightSummary
from src.models.source import GameRecord, ReviewRecord
from src.storage import read_json, write_json, write_manifest


def workspace(tmp_path: Path, *, approve: bool = True) -> SimpleNamespace:
    config = Config(data_dir=tmp_path, jazerei_user_id="user-1")
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
    write_manifest(source_run, {"schema_version": 1, "run_id": source_run.name})

    snapshot_run = tmp_path / "jazerei" / "runs" / "snapshot-run"
    snapshot_path = snapshot_run / "review-state" / "demolition-girl-26570.json"
    absent = current_snapshot(game_id=26570, slug="demolition-girl", review=None)
    write_json(snapshot_path, absent)
    write_manifest(snapshot_run, {"schema_version": 1, "run_id": snapshot_run.name})

    matches = tmp_path / "migration" / "matches.json"
    write_json(
        matches,
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
    decisions = tmp_path / "migration" / "liked-decisions.jsonl"
    append_decision(
        decisions,
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=review_version_hash(review),
        value=False,
    )
    ws = SimpleNamespace(
        config=config,
        source_run=source_run,
        review=review,
        snapshot_path=snapshot_path,
        matches=matches,
        decisions=decisions,
        approvals=tmp_path / "migration" / "approvals.jsonl",
        plan_id="plan-1",
    )
    args = SimpleNamespace(
        matches=str(matches),
        reviews=str(source_run / "reviews.json"),
        games=str(source_run / "games.json"),
        destination_reviews="",
        destination_snapshot=str(snapshot_path),
        liked_decisions=str(decisions),
        favorite=[],
        playing_now=[],
        watchlist=[],
        plan_id="plan-1",
    )
    assert cli.cmd_plan(config, args) == 0
    if approve:
        assert (
            cli.cmd_migration_approve(
                config,
                SimpleNamespace(plan_id="plan-1", confirm="plan-1", approvals=str(ws.approvals)),
            )
            == 0
        )
    return ws


def current_snapshot(
    *,
    game_id: int,
    slug: str,
    review: DestinationReview | None,
    complete: bool = True,
) -> object:
    pagination = PaginationResult(
        reviews=[review] if review else [],
        pages_fetched=1,
        records_received=1 if review else 0,
        duplicates_discarded=0,
        termination_reason="short_page" if review else "empty_page",
        content_range_total=None,
    )
    snapshot = build_review_snapshot(
        game_id=game_id,
        slug=slug,
        expected_user_id="user-1",
        pagination=pagination,
        page_size=50,
        max_pages=100,
        identity_sources=("firebase_indexeddb", "get_my_profile"),
    )
    if not complete:
        snapshot.capture.complete = False
        snapshot.pagination.complete = False
    return snapshot


def evidence(snapshot: object) -> SnapshotEvidence:
    return SnapshotEvidence(snapshot=snapshot, ref=f"snapshots/test-{snapshot.query.game_id}.json")


def test_preflight_compatible_absent(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    prep = prepare_preflight(ws.config, "plan-1", ws.approvals)
    assert prep.ready
    bundle = prep.bundle
    assert bundle is not None
    snapshots = {
        26570: evidence(current_snapshot(game_id=26570, slug="demolition-girl", review=None))
    }
    checks, summary = compare_actions(bundle.plan.actions, snapshots)
    assert summary.status is PreflightStatus.COMPATIBLE
    assert checks[0].status is ActionStatus.COMPATIBLE
    result = build_result(
        preflight_id="pf-1",
        preparation=prep,
        checks=checks,
        summary=summary,
        snapshots=[],
        identity_verified=True,
        identity_user_id="user-1",
    )
    artifact = write_preflight_run(ws.config, result, snapshots, preflight_id="pf-1")
    assert artifact.result_path.exists()
    assert verify_preflight(ws.config, "pf-1") == []
    saved = load_preflight(ws.config, "pf-1")
    assert saved.result.status is PreflightStatus.COMPATIBLE
    assert saved.snapshots[0]["exists"] is False


def test_preflight_created_after_approval_is_conflict(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    prep = prepare_preflight(ws.config, "plan-1", ws.approvals)
    bundle = prep.bundle
    assert bundle is not None
    current = DestinationReview(
        id="user-1_26570",
        game_id=26570,
        game_slug="demolition-girl",
        user_id="user-1",
        rating=8,
        text="Review criada manualmente",
        liked=True,
    )
    checks, summary = compare_actions(
        bundle.plan.actions,
        {26570: evidence(current_snapshot(game_id=26570, slug="demolition-girl", review=current))},
    )
    assert checks[0].reason == "review_created_after_approval"
    assert summary.status is PreflightStatus.CONFLICT


def test_preflight_modified_and_removed_reviews(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    bundle = prepare_preflight(ws.config, "plan-1", ws.approvals).bundle
    assert bundle is not None
    before = DestinationReview(
        id="user-1_26570",
        game_id=26570,
        game_slug="demolition-girl",
        user_id="user-1",
        rating=2,
        text="Antes",
        liked=False,
    )
    action = bundle.plan.actions[0].model_copy(update={"before": before.model_dump(mode="json")})
    modified = before.model_copy(update={"text": "Depois", "rating": 9, "liked": True})
    checks, summary = compare_actions(
        [action],
        {26570: evidence(current_snapshot(game_id=26570, slug="demolition-girl", review=modified))},
    )
    assert checks[0].reason == "review_modified_after_approval"
    assert set(checks[0].fields) >= {"text", "rating", "liked"}
    assert summary.status is PreflightStatus.CONFLICT

    checks, summary = compare_actions(
        [action],
        {26570: evidence(current_snapshot(game_id=26570, slug="demolition-girl", review=None))},
    )
    assert checks[0].reason == "review_removed_after_approval"
    assert summary.status is PreflightStatus.CONFLICT


def test_preflight_inconclusive_never_means_absent(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    bundle = prepare_preflight(ws.config, "plan-1", ws.approvals).bundle
    assert bundle is not None
    incomplete = current_snapshot(
        game_id=26570, slug="demolition-girl", review=None, complete=False
    )
    checks, summary = compare_actions(bundle.plan.actions, {26570: evidence(incomplete)})
    assert checks[0].status is ActionStatus.INCONCLUSIVE
    assert summary.status is PreflightStatus.INCONCLUSIVE


def test_preflight_unsupported_action_blocks_global(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    bundle = prepare_preflight(ws.config, "plan-1", ws.approvals).bundle
    assert bundle is not None
    unsupported = MigrationAction(
        kind="favorite",
        source_ref="demolition-girl",
        game_id=26570,
    )
    checks, summary = compare_actions(
        [bundle.plan.actions[0], unsupported],
        {26570: evidence(current_snapshot(game_id=26570, slug="demolition-girl", review=None))},
    )
    assert checks[1].status is ActionStatus.UNSUPPORTED
    assert summary.status is PreflightStatus.BLOCKED


def test_preflight_identity_mismatch_precondition(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    wrong = Config(data_dir=ws.config.data_dir, jazerei_user_id="other-user")
    prep = prepare_preflight(wrong, "plan-1", ws.approvals)
    assert not prep.ready
    assert prep.status is PreflightStatus.BLOCKED


def test_preflight_missing_approval_blocks_without_capture(tmp_path: Path) -> None:
    ws = workspace(tmp_path, approve=False)
    prep = prepare_preflight(ws.config, "plan-1", ws.approvals)
    assert not prep.ready
    assert prep.status is PreflightStatus.BLOCKED
    assert any("aprovacao" in reason for reason in prep.reasons)


def test_preflight_storage_preserves_previous_and_detects_tamper(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    prep = prepare_preflight(ws.config, "plan-1", ws.approvals)
    assert prep.ready
    bundle = prep.bundle
    assert bundle is not None
    snapshots = {
        26570: evidence(current_snapshot(game_id=26570, slug="demolition-girl", review=None))
    }
    checks, summary = compare_actions(bundle.plan.actions, snapshots)
    result = build_result(
        preflight_id="pf-1",
        preparation=prep,
        checks=checks,
        summary=summary,
        snapshots=[],
        identity_verified=True,
        identity_user_id="user-1",
    )
    write_preflight_run(ws.config, result, snapshots, preflight_id="pf-1")
    with pytest.raises(FileExistsError):
        write_preflight_run(ws.config, result, snapshots, preflight_id="pf-1")
    result_path = ws.config.migration_dir / "preflights" / "pf-1" / "preflight.json"
    data = read_json(result_path)
    data["result"]["status"] = "conflict"
    write_json(result_path, data, overwrite=True)
    assert verify_preflight(ws.config, "pf-1")


def test_preflight_result_hash_is_deterministic(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    prep = prepare_preflight(ws.config, "plan-1", ws.approvals)
    assert prep.ready
    summary = PreflightSummary(status=PreflightStatus.COMPATIBLE)
    first = build_result(
        preflight_id="pf-a",
        preparation=prep,
        checks=[],
        summary=summary,
        snapshots=[],
        captured_at="2026-01-01T00:00:00+00:00",
    )
    second = build_result(
        preflight_id="pf-a",
        preparation=prep,
        checks=[],
        summary=summary,
        snapshots=[],
        captured_at="2027-01-01T00:00:00+00:00",
    )
    assert first.result_sha256 == second.result_sha256


def test_preflight_cli_blocked_plan_writes_blocked_evidence(tmp_path: Path) -> None:
    ws = workspace(tmp_path, approve=False)
    args = SimpleNamespace(
        plan_id="plan-1",
        preflight_id="pf-cli",
        approvals=str(ws.approvals),
        page_size=50,
        max_pages=100,
        headless=True,
    )
    assert cli.cmd_migration_preflight(ws.config, args) == 2
    result = load_preflight(ws.config, "pf-cli")
    assert result.result.status is PreflightStatus.BLOCKED
    assert result.snapshots == []
    assert verify_preflight(ws.config, "pf-cli") == []
