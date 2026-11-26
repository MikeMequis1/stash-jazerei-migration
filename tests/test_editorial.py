from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.migration.editorial import (
    EditorialAcceptanceError,
    append_acceptance,
    load_acceptances,
    pending_editorial_alerts,
)
from src.models.normalized import GameMatch, MatchDecision
from src.models.source import ReviewRecord

SOURCE_HASH = "a" * 64
MATCHES_HASH = "b" * 64


def review() -> ReviewRecord:
    return ReviewRecord(
        game_slug="game",
        game_title="Game: Enhanced Edition",
        source_url="https://stash.games/games/game/reviews/u",
        rating=7,
        text="texto",
    )


def match() -> GameMatch:
    return GameMatch(
        source_slug="game",
        source_title="Game: Enhanced Edition",
        source_year=2020,
        dest_game_id=100,
        dest_slug="game",
        dest_name="Game",
        dest_year=2019,
        confidence=0.2,
        decision=MatchDecision.APPROVED,
    )


def acceptance(tmp_path: Path):
    return append_acceptance(
        tmp_path / "editorial.jsonl",
        plan_id="plan-1",
        plan_sha256="c" * 64,
        manifest_sha256="d" * 64,
        action_id="action-1",
        stable_id="S9-001",
        source_ref="game",
        destination_game_id=100,
        destination_slug="game",
        reason="alerta aceito",
        source_hash=SOURCE_HASH,
        matches_sha256=MATCHES_HASH,
    )


def test_editorial_acceptance_roundtrip_and_pending(tmp_path: Path) -> None:
    event = acceptance(tmp_path)
    loaded = load_acceptances(tmp_path / "editorial.jsonl")
    assert len(loaded) == 1
    assert loaded[0].event_id == event.event_id
    assert loaded[0].event_type == "editorial_acceptance"
    assert loaded[0].decision == "keep_association"
    assert (
        pending_editorial_alerts(
            plan_actions=[{"source_ref": "game"}],
            matches={"game": match()},
            reviews={"game": review()},
            acceptances=loaded,
        )
        == []
    )


def test_tampered_editorial_event_is_rejected(tmp_path: Path) -> None:
    acceptance(tmp_path)
    path = tmp_path / "editorial.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["reason"] = "alterado"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(EditorialAcceptanceError):
        load_acceptances(path)


def test_acceptance_is_not_plan_approval(tmp_path: Path) -> None:
    event = acceptance(tmp_path)
    assert event.event_type == "editorial_acceptance"
    assert not hasattr(event, "approval_scope")
    assert not hasattr(event, "approved_actions")
