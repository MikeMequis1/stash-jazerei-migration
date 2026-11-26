from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from src.config import Config
from src.migration.liked import (
    DecisionError,
    DecisionExistsError,
    append_decision,
    load_decisions,
    resolve_liked,
    review_version_hash,
)
from src.migration.planner import build_plan
from src.migration.validator import validate_plan
from src.models.destination import DestinationReview
from src.models.normalized import (
    GameMatch,
    LikedDecision,
    LikedStatus,
    MatchDecision,
)
from src.models.source import ReviewRecord


def review(text: str = "Bom jogo", rating: float = 7) -> ReviewRecord:
    return ReviewRecord(
        game_slug="demolition-girl",
        game_title="Demolition Girl",
        text=text,
        rating=rating,
        source_url="https://stash.games/games/demolition-girl/reviews/MikeMequis1",
    )


def match() -> GameMatch:
    return GameMatch(
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


def test_source_hash_is_stable_and_changes_with_content() -> None:
    first = review_version_hash(review())
    second = review_version_hash(review())
    assert first == second
    assert review_version_hash(review(text="Outro texto")) != first
    assert review_version_hash(review(rating=8)) != first


def test_unresolved_liked_is_not_inferred_from_rating_or_text(tmp_path: Path) -> None:
    plan = build_plan(
        [match()],
        [review(text="Excelente, recomendo muito", rating=10)],
    )
    action = plan.actions[0]
    assert action.liked.status is LikedStatus.UNRESOLVED
    assert action.liked.value is None
    assert action.after["liked"] is None
    codes = {issue.code for issue in validate_plan(plan)}
    assert "liked_unresolved" in codes


def test_decisions_true_and_false_are_explicit(tmp_path: Path) -> None:
    decisions = tmp_path / "liked.jsonl"
    source = review()
    source_hash = review_version_hash(source)
    append_decision(
        decisions,
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=source_hash,
        value=True,
    )
    resolved = resolve_liked(
        load_decisions(decisions),
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=source_hash,
    )
    assert resolved.status is LikedStatus.DECIDED
    assert resolved.value is True

    append_decision(
        decisions,
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=source_hash,
        value=False,
        replace=True,
    )
    decisions_list = load_decisions(decisions)
    assert len(decisions_list) == 2
    assert decisions_list[0].value is True
    assert decisions_list[1].value is False
    assert decisions_list[1].supersedes == decisions_list[0].decision_id
    resolved = resolve_liked(
        decisions_list,
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=source_hash,
    )
    assert resolved.value is False


def test_decision_cannot_be_silently_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "liked.jsonl"
    source_hash = review_version_hash(review())
    append_decision(
        path,
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=source_hash,
        value=True,
    )
    with pytest.raises(DecisionExistsError):
        append_decision(
            path,
            source_ref="demolition-girl",
            game_id=26570,
            source_hash=source_hash,
            value=False,
        )


def test_stale_decision_and_wrong_game_are_unresolved(tmp_path: Path) -> None:
    path = tmp_path / "liked.jsonl"
    old = review(text="texto antigo")
    new = review(text="texto novo")
    append_decision(
        path,
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=review_version_hash(old),
        value=True,
    )
    state = resolve_liked(
        load_decisions(path),
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=review_version_hash(new),
    )
    assert state.status is LikedStatus.STALE
    assert state.value is None

    other_game = resolve_liked(
        load_decisions(path),
        source_ref="demolition-girl",
        game_id=974,
        source_hash=review_version_hash(old),
    )
    assert other_game.status is LikedStatus.UNRESOLVED


def test_invalid_decision_inputs(tmp_path: Path) -> None:
    path = tmp_path / "liked.jsonl"
    with pytest.raises(DecisionError):
        append_decision(
            path,
            source_ref="demolition-girl",
            game_id=26570,
            source_hash="not-a-hash",
            value=True,
        )
    with pytest.raises(DecisionError):
        append_decision(
            path,
            source_ref="demolition-girl",
            game_id=0,
            source_hash="a" * 64,
            value=True,
        )
    from argparse import ArgumentTypeError

    from src.cli import _parse_bool

    with pytest.raises(ArgumentTypeError):
        _parse_bool("maybe")


def test_planner_uses_decided_liked_and_preserves_destination() -> None:
    source = review()
    source_hash = review_version_hash(source)
    decision = LikedDecision(
        decision_id="d1",
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=source_hash,
        value=False,
        decided_at="2026-01-01T00:00:00+00:00",
    )
    plan = build_plan([match()], [source], liked_decisions=[decision])
    action = plan.actions[0]
    assert action.liked.status is LikedStatus.DECIDED
    assert action.liked.value is False
    assert action.after["liked"] is False

    existing = DestinationReview(game_id=26570, liked=True, rating=9, text="antiga")
    plan_with_existing = build_plan(
        [match()], [source], destination_reviews=[existing], liked_decisions=[decision]
    )
    assert plan_with_existing.actions == []
    assert plan_with_existing.skipped[0]["before"]["liked"] is True


def test_liked_does_not_confuse_engagement_counts(tmp_path: Path) -> None:
    source = review(rating=10, text="gostei")
    source.likes_count = 999
    source.comments_count = 888
    decision = LikedDecision(
        decision_id="d1",
        source_ref="demolition-girl",
        game_id=26570,
        source_hash=review_version_hash(source),
        value=False,
        decided_at="2026-01-01T00:00:00+00:00",
    )
    plan = build_plan([match()], [source], liked_decisions=[decision])
    action = plan.actions[0]
    assert action.after["liked"] is False
    assert "likes_count" not in action.after
    assert "comments_count" not in action.after


def test_cli_decide_show_and_no_silent_overwrite(tmp_path: Path) -> None:
    from src import cli

    review_file = tmp_path / "reviews.json"
    from src.storage import write_json

    write_json(review_file, [review()])
    decisions = tmp_path / "decisions.jsonl"
    decide_args = SimpleNamespace(
        source_ref="demolition-girl",
        game_id=26570,
        value=False,
        review_file=str(review_file),
        decisions=str(decisions),
        replace=False,
    )
    assert cli.cmd_liked_decide(Config(), decide_args) == 0
    assert len(load_decisions(decisions)) == 1
    assert (
        cli.cmd_liked_show(
            Config(),
            SimpleNamespace(
                source_ref="demolition-girl",
                game_id=26570,
                review_file=str(review_file),
                decisions=str(decisions),
            ),
        )
        == 0
    )
    with pytest.raises(DecisionExistsError):
        cli.cmd_liked_decide(Config(), decide_args)
