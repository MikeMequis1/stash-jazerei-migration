from __future__ import annotations

from pathlib import Path

from src.migration.evaluation_matching import match_evaluations
from src.migration.evaluations import build_evaluation_set
from src.migration.execution import build_review_payload
from src.migration.validator import validate_plan
from src.models.destination import DestinationGame
from src.models.normalized import (
    GameMatch,
    LikedState,
    LikedStatus,
    MatchDecision,
    MigrationAction,
    MigrationPlan,
)
from src.models.source import GameRecord
from src.transport import HttpError


def game(slug: str, title: str, rating: float = 8.0) -> GameRecord:
    return GameRecord(
        slug=slug,
        title=title,
        source_url=f"https://stash.games/games/{slug}",
        year=2020,
        rating=rating,
        status="beaten",
    )


def candidate(game_id: int, name: str) -> DestinationGame:
    return DestinationGame(id=game_id, slug=name.lower(), name=name, release_year=2020)


def evaluations(games: list[GameRecord]):
    return build_evaluation_set(games, []).evaluations


def test_match_never_approves_and_marks_candidate() -> None:
    games = [game("one", "One")]
    result = match_evaluations(
        evaluations(games),
        games,
        lambda title: [candidate(10, title)],
        retry_delay=0,
    )
    assert len(result.matches) == 1
    assert result.matches[0].decision is MatchDecision.CANDIDATE
    assert result.matches[0].decision is not MatchDecision.APPROVED
    assert result.matches[0].lookup_status == "candidate"
    assert result.report.counts["candidate"] == 1


def test_no_candidates_is_not_an_error() -> None:
    games = [game("one", "One")]
    result = match_evaluations(evaluations(games), games, lambda title: [], retry_delay=0)
    assert result.matches[0].lookup_status == "no_candidate"
    assert result.matches[0].decision is MatchDecision.MANUAL_REVIEW
    assert result.report.counts["no_candidate"] == 1


def test_http_error_is_inconclusive_and_not_approved() -> None:
    games = [game("one", "One")]

    def search(title: str):
        raise HttpError("HTTP 503", status_code=503)

    result = match_evaluations(evaluations(games), games, search, max_attempts=1, retry_delay=0)
    assert result.matches[0].lookup_status == "inconclusive"
    assert result.matches[0].decision is MatchDecision.MANUAL_REVIEW
    assert result.report.counts["inconclusive"] == 1
    assert "consulta inconclusiva" in result.matches[0].reasons[0]


def test_refresh_error_preserves_historical_candidates() -> None:
    games = [game("one", "One")]
    historical = GameMatch(
        source_slug="one",
        source_title="One",
        source_year=2020,
        dest_game_id=10,
        dest_slug="one",
        dest_name="One",
        confidence=0.9,
        decision=MatchDecision.CANDIDATE,
        alternatives=[{"game_id": 10, "name": "One"}],
    )

    def search(title: str):
        raise HttpError("HTTP 503", status_code=503)

    result = match_evaluations(
        evaluations(games),
        games,
        search,
        existing_matches=[historical],
        refresh_slugs={"one"},
        max_attempts=1,
        retry_delay=0,
    )
    assert result.matches[0].dest_game_id == 10
    assert result.matches[0].alternatives == [{"game_id": 10, "name": "One"}]
    assert result.matches[0].lookup_status == "inconclusive"


def test_cache_avoids_redundant_queries(tmp_path: Path) -> None:
    games = [game("one", "Same Title"), game("two", "Same Title")]
    calls: list[str] = []

    def search(title: str):
        calls.append(title)
        return [candidate(10, title)]

    cache = tmp_path / "cache.jsonl"
    result = match_evaluations(evaluations(games), games, search, cache_path=cache, retry_delay=0)
    assert len(calls) == 1
    assert result.report.counts["queries"] == 1
    assert result.report.counts["cache_hits"] == 1
    assert len(result.matches) == 2


def test_empty_text_payload_is_valid_and_liked_unresolved_blocks_plan() -> None:
    games = [game("one", "One")]
    evaluation = evaluations(games)[0]
    assert evaluation.text == ""
    action = MigrationAction(
        kind="review",
        source_ref=evaluation.game_slug,
        source_payload=evaluation.model_dump(mode="json"),
        game_id=10,
        destination_slug="one",
        after={
            "game_id": 10,
            "rating": 8.0,
            "text": "",
            "liked": False,
        },
        liked=LikedState(status=LikedStatus.DECIDED, value=False),
    )
    payload, _ = build_review_payload(action)
    assert payload["text"] == ""
    plan = MigrationPlan(actions=[action], approved=True)
    issues = validate_plan(plan)
    assert not [issue for issue in issues if issue.code == "liked_unresolved"]
    action.liked = LikedState(status=LikedStatus.UNRESOLVED)
    action.after["liked"] = None  # type: ignore[index]
    issues = validate_plan(plan)
    assert any(issue.code == "liked_unresolved" for issue in issues)
