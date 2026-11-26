from __future__ import annotations

from pathlib import Path

import pytest
from src.config import Config, load_config
from src.migration.eligibility import evaluate_eligibility
from src.migration.liked import append_decision, append_deferral, load_decisions, resolve_liked
from src.migration.match_decisions import (
    append_match_event,
    edition_confirmation,
    history_conflict,
    latest_by_slug,
    load_match_events,
)
from src.models.normalized import (
    GameMatch,
    LikedState,
    LikedStatus,
    MatchDecision,
)
from src.models.source import GameRecord, ReviewRecord
from src.review_ui import ReviewPaths, ReviewService, ReviewUIError
from src.storage import read_json, write_json, write_manifest


def event_kwargs() -> dict:
    return {
        "source_ref": "one",
        "evaluation_run_id": "run",
        "evaluations_sha256": "a" * 64,
        "source_run_id": "source",
        "evaluation_kind": "rating_only",
        "source_hash": "b" * 64,
        "action": "approve",
        "previous_decision": "candidate",
        "new_decision": "approved",
        "candidate_id": 10,
        "candidate_slug": "one",
        "candidate_name": "One",
        "confidence": 0.9,
        "reason": "teste",
    }


def test_match_event_is_append_only_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "match-decisions.jsonl"
    first = append_match_event(path, **event_kwargs())
    second = append_match_event(path, **event_kwargs())
    assert first.event_id == second.event_id
    assert len(load_match_events(path)) == 1
    assert latest_by_slug(load_match_events(path))["one"].new_decision == "approved"
    assert history_conflict(first, decision="manual_review", candidate_id=10)


def test_edition_confirmation_is_bound_to_candidate(tmp_path: Path) -> None:
    path = tmp_path / "match-decisions.jsonl"
    confirmed = append_match_event(
        path,
        **{
            **event_kwargs(),
            "action": "confirm_edition",
            "new_decision": "approved",
            "edition_confirmed": True,
            "reason": "confirmado",
        },
    )
    events = load_match_events(path)
    assert edition_confirmation(events, source_ref="one", candidate_id=10) is not None
    assert edition_confirmation(events, source_ref="one", candidate_id=11) is None
    assert confirmed.edition_confirmed is True


def test_liked_source_only_deferral_and_reconfirmation(tmp_path: Path) -> None:
    path = tmp_path / "liked.jsonl"
    source_hash = "c" * 64
    decision = append_decision(
        path,
        source_ref="one",
        game_id=None,
        source_hash=source_hash,
        value=True,
        event_type="decision",
        evaluation_kind="rating_only",
        evaluation_run_id="run",
        evaluations_sha256="d" * 64,
        operator_id="local",
    )
    assert decision.binding_status == "pending_identity"
    state = resolve_liked(
        load_decisions(path), source_ref="one", game_id=None, source_hash=source_hash
    )
    assert state.status is LikedStatus.DECIDED
    assert state.value is True
    assert state.binding_status == "pending_identity"

    deferred = append_deferral(path, source_ref="one", source_hash=source_hash)
    assert deferred is not None
    state = resolve_liked(
        load_decisions(path), source_ref="one", game_id=None, source_hash=source_hash
    )
    assert state.status is LikedStatus.UNRESOLVED
    assert len(load_decisions(path)) == 2

    reconfirmed = append_decision(
        path,
        source_ref="one",
        game_id=10,
        source_hash=source_hash,
        value=False,
        replace=True,
        event_type="reconfirmation",
    )
    assert reconfirmed.supersedes == deferred.decision_id
    state = resolve_liked(
        load_decisions(path), source_ref="one", game_id=10, source_hash=source_hash
    )
    assert state.status is LikedStatus.DECIDED
    assert state.value is False


def test_automatic_liked_requires_human_reconfirmation(tmp_path: Path) -> None:
    path = tmp_path / "liked.jsonl"
    source_hash = "e" * 64
    append_decision(
        path,
        source_ref="one",
        game_id=10,
        source_hash=source_hash,
        value=True,
        source="automatic",
    )
    state = resolve_liked(
        load_decisions(path), source_ref="one", game_id=10, source_hash=source_hash
    )
    assert state.status is LikedStatus.REQUIRES_RECONFIRMATION
    assert state.requires_reconfirmation is True
    append_decision(
        path,
        source_ref="one",
        game_id=10,
        source_hash=source_hash,
        value=True,
        replace=True,
        event_type="reconfirmation",
    )
    state = resolve_liked(
        load_decisions(path), source_ref="one", game_id=10, source_hash=source_hash
    )
    assert state.status is LikedStatus.DECIDED


def _match() -> GameMatch:
    return GameMatch(
        source_slug="one",
        source_title="One",
        dest_game_id=10,
        dest_slug="one",
        dest_name="One",
        confidence=0.95,
        decision=MatchDecision.APPROVED,
        reasons=["slug identico", "titulo normalizado identico", "ano identico"],
    )


def test_eligibility_blocks_missing_match_liked_destination_and_edition() -> None:
    result = evaluate_eligibility(
        match=_match(),
        liked=LikedState(status=LikedStatus.UNRESOLVED),
        special_edition=False,
        duplicate_title=False,
        edition_confirmed=False,
        destination_present=False,
        history_conflict=False,
        evaluation_current=True,
        source_hash_current=True,
    )
    assert result.eligible is False
    assert {reason.code for reason in result.reasons} == {"liked_unresolved"}

    result = evaluate_eligibility(
        match=_match(),
        liked=LikedState(status=LikedStatus.DECIDED, value=False),
        special_edition=True,
        duplicate_title=False,
        edition_confirmed=False,
        destination_present=False,
        history_conflict=False,
        evaluation_current=True,
        source_hash_current=True,
    )
    assert {reason.code for reason in result.reasons} == {"edition_unconfirmed"}

    result = evaluate_eligibility(
        match=_match(),
        liked=LikedState(status=LikedStatus.DECIDED, value=False),
        special_edition=False,
        duplicate_title=False,
        edition_confirmed=False,
        destination_present=True,
        history_conflict=False,
        evaluation_current=True,
        source_hash_current=True,
    )
    assert "destination_present" in {reason.code for reason in result.reasons}


def test_batch_eligibility_is_strict() -> None:
    match = GameMatch(
        source_slug="one",
        source_title="One",
        dest_game_id=10,
        dest_slug="one",
        dest_name="One",
        confidence=0.9,
        decision=MatchDecision.CANDIDATE,
        reasons=["slug identico", "titulo normalizado identico", "ano identico"],
    )
    result = evaluate_eligibility(
        match=match,
        liked=None,
        special_edition=False,
        duplicate_title=False,
        edition_confirmed=False,
        destination_present=False,
        history_conflict=False,
        evaluation_current=True,
        source_hash_current=True,
    )
    assert result.eligible is False
    assert result.batch_eligible is True

    match.decision = MatchDecision.MANUAL_REVIEW
    result = evaluate_eligibility(
        match=match,
        liked=None,
        special_edition=False,
        duplicate_title=False,
        edition_confirmed=False,
        destination_present=False,
        history_conflict=False,
        evaluation_current=True,
        source_hash_current=True,
    )
    assert result.batch_eligible is False


def _workspace(tmp_path: Path) -> ReviewPaths:
    source = tmp_path / "stash" / "run"
    destination = tmp_path / "jazerei" / "run"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    review = ReviewRecord(
        game_slug="one",
        game_title="One",
        text="texto",
        rating=8.0,
        source_url="https://stash.games/games/one/reviews/u",
        raw={"text_listing": "texto", "text_detail": "texto"},
    )
    game = GameRecord(
        slug="one", title="One", source_url="https://stash.games/games/one", rating=8.0
    )
    write_json(source / "reviews.json", [review])
    write_json(source / "games.json", [game])
    write_manifest(source, {"schema_version": 1, "run_id": "source"})
    write_json(destination / "reviews.json", [])
    write_manifest(destination, {"schema_version": 1, "run_id": "destination"})
    matches = tmp_path / "migration" / "matches.json"
    write_json(
        matches,
        [
            GameMatch(
                source_slug="one",
                source_title="One",
                dest_game_id=10,
                dest_slug="one",
                dest_name="One",
                confidence=0.95,
                decision=MatchDecision.APPROVED,
                reasons=["slug identico", "titulo normalizado identico", "ano identico"],
            )
        ],
    )
    return ReviewPaths(
        source_run=source,
        destination_run=destination,
        matches=matches,
        liked=tmp_path / "migration" / "liked.jsonl",
        history=tmp_path / "migration" / "history.jsonl",
        plans=tmp_path / "migration" / "plans",
        categories=tmp_path / "migration" / "categories.json",
        overrides=tmp_path / "migration" / "overrides.jsonl",
    )


def test_service_liked_source_only_and_deferral(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    service = ReviewService(Config(data_dir=tmp_path), paths)
    service.decide_liked({"slug": "one", "value": True})
    state = service.state()
    item = state["items"][0]
    assert item["liked"]["status"] == "decided"
    assert item["liked"]["game_id"] == 10
    service.decide_liked({"slug": "one", "value": None})
    state = service.state()
    assert state["items"][0]["liked"]["status"] == "unresolved"
    assert len(load_decisions(paths.liked)) == 2


def test_service_batch_rejects_non_batch_item(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    service = ReviewService(Config(data_dir=tmp_path), paths)
    result = service.decide_match_batch({"items": [{"slug": "one", "candidate_id": 10}]})
    assert result["failed"] == 1
    assert result["approved"] == 0


def test_service_batch_approves_explicit_candidate(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    matches = read_json(paths.matches)
    matches[0]["decision"] = "candidate"
    write_json(paths.matches, matches, overwrite=True)
    service = ReviewService(Config(data_dir=tmp_path), paths)
    assert service.state()["items"][0]["batch_eligible"] is True
    result = service.decide_match_batch({"items": [{"slug": "one", "candidate_id": 10}]})
    assert result["approved"] == 1
    assert result["failed"] == 0
    assert service.state()["items"][0]["review_status"] == "approved"


def test_special_edition_requires_explicit_confirmation(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    write_json(paths.categories, {"source_run_id": "test", "categories": {"one": "Mod"}})
    matches = read_json(paths.matches)
    matches[0]["decision"] = "candidate"
    write_json(paths.matches, matches, overwrite=True)
    service = ReviewService(Config(data_dir=tmp_path), paths)
    with pytest.raises(ReviewUIError):
        service.decide_match({"slug": "one", "action": "approve", "candidate_id": 10})
    service.decide_match(
        {
            "slug": "one",
            "action": "approve",
            "candidate_id": 10,
            "edition_confirmed": True,
            "reason": "remaster correto",
        }
    )
    item = service.state()["items"][0]
    assert item["match_decision"] == "approved"
    assert item["edition_confirmed"] is True


def test_rebuild_requires_explicit_confirmation(tmp_path: Path) -> None:
    paths = _workspace(tmp_path)
    service = ReviewService(Config(data_dir=tmp_path), paths)
    with pytest.raises(ReviewUIError):
        service.rebuild_evaluation_matches(confirm="wrong")


def test_operator_has_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIGRATION_OPERATOR", raising=False)
    assert load_config(env_file=None).operator_id == "local"
    monkeypatch.setenv("MIGRATION_OPERATOR", "revisor-teste")
    assert load_config(env_file=None).operator_id == "revisor-teste"
