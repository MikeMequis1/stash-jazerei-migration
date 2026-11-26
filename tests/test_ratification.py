from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.migration.ratification import (
    RatificationError,
    append_ratification,
    eligible_liked,
    eligible_matches,
    liked_item,
    load_ratifications,
    match_item,
    validation_summary,
)
from src.models.normalized import GameMatch, LikedDecision, MatchDecision

SOURCE_RUN = "source"
SOURCE_HASH = "a" * 64
DEST_RUN = "destination"
DEST_HASH = "b" * 64


def match() -> GameMatch:
    return GameMatch(
        source_slug="game",
        source_title="Game",
        dest_game_id=100,
        dest_slug="game",
        dest_name="Game",
        decision=MatchDecision.APPROVED,
    )


def history() -> list[dict]:
    return [
        {
            "event_id": "event-1",
            "at": "2026-01-01T00:00:00+00:00",
            "source_slug": "game",
            "source_hash": "c" * 64,
            "action": "auto_approve",
            "candidate_id": 100,
        }
    ]


def decision(
    *, value: bool = True, source: str = "automatic", decision_id: str = "d1"
) -> LikedDecision:
    return LikedDecision(
        decision_id=decision_id,
        source_ref="game",
        game_id=100,
        source_hash="c" * 64,
        value=value,
        source=source,
        decided_at="2026-01-01T00:00:00+00:00",
    )


def ratify(tmp_path: Path, *, dimension: str, items: list[dict]):
    return append_ratification(
        tmp_path / "ratifications.jsonl",
        dimension=dimension,
        items=items,
        plan_id="plan-1",
        plan_sha256="d" * 64,
        manifest_sha256="e" * 64,
        source_run_id=SOURCE_RUN,
        source_manifest_sha256=SOURCE_HASH,
        destination_run_id=DEST_RUN,
        destination_manifest_sha256=DEST_HASH,
    )


def test_valid_batch_ratification_makes_auto_match_eligible(tmp_path: Path) -> None:
    event = ratify(tmp_path, dimension="matches", items=[match_item(match(), history()[0])])
    eligible, rejected = eligible_matches(
        [match()],
        history(),
        load_ratifications(tmp_path / "ratifications.jsonl"),
        source_run_id=SOURCE_RUN,
        source_manifest_sha256=SOURCE_HASH,
        destination_run_id=DEST_RUN,
        destination_manifest_sha256=DEST_HASH,
    )
    assert len(eligible) == 1
    assert rejected == []
    assert event.ratification_id in (tmp_path / "ratifications.jsonl").read_text(encoding="utf-8")


def test_hash_divergence_invalidates_ratification(tmp_path: Path) -> None:
    ratify(tmp_path, dimension="matches", items=[match_item(match(), history()[0])])
    path = tmp_path / "ratifications.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["items_sha256"] = "0" * 64
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(RatificationError):
        load_ratifications(path)


def test_missing_or_additional_item_is_not_eligible(tmp_path: Path) -> None:
    ratify(tmp_path, dimension="matches", items=[match_item(match(), history()[0])])
    (tmp_path / "ratifications.jsonl").unlink()
    changed = [match_item(match(), history()[0])]
    changed[0]["source_ref"] = "other-game"
    ratify(tmp_path, dimension="matches", items=changed)
    eligible, rejected = eligible_matches(
        [match()],
        history(),
        load_ratifications(tmp_path / "ratifications.jsonl"),
        source_run_id=SOURCE_RUN,
        source_manifest_sha256=SOURCE_HASH,
        destination_run_id=DEST_RUN,
        destination_manifest_sha256=DEST_HASH,
    )
    assert eligible == []
    assert rejected[0]["source_ref"] == "game"


def test_changed_value_or_later_decision_is_not_covered(tmp_path: Path) -> None:
    old = decision(decision_id="old")
    ratify(tmp_path, dimension="liked", items=[liked_item(old)])
    newer = decision(value=False, decision_id="new")
    eligible, rejected = eligible_liked(
        [old, newer],
        {"game": match()},
        load_ratifications(tmp_path / "ratifications.jsonl"),
        source_run_id=SOURCE_RUN,
        source_manifest_sha256=SOURCE_HASH,
        destination_run_id=DEST_RUN,
        destination_manifest_sha256=DEST_HASH,
    )
    assert eligible == []
    assert rejected[0]["source_ref"] == "game"


def test_human_individual_is_preserved_and_batch_is_distinct(tmp_path: Path) -> None:
    human = decision(value=False, source="human", decision_id="human-1")
    eligible, rejected = eligible_liked(
        [human],
        {"game": match()},
        [],
        source_run_id=SOURCE_RUN,
        source_manifest_sha256=SOURCE_HASH,
        destination_run_id=DEST_RUN,
        destination_manifest_sha256=DEST_HASH,
    )
    assert eligible == [human]
    assert rejected == []
    counts = validation_summary(
        matches=[match()],
        history=[],
        decisions=[human],
        ratifications=[],
        source_run_id=SOURCE_RUN,
        source_manifest_sha256=SOURCE_HASH,
        destination_run_id=DEST_RUN,
        destination_manifest_sha256=DEST_HASH,
    )
    assert counts["liked"]["human_individual"] == 1
    assert counts["liked"]["human_batch_review"] == 0


def test_match_and_liked_ratifications_are_separate(tmp_path: Path) -> None:
    ratify(tmp_path, dimension="matches", items=[match_item(match(), history()[0])])
    event = load_ratifications(tmp_path / "ratifications.jsonl")[0]
    eligible_decisions, rejected = eligible_liked(
        [decision()],
        {"game": match()},
        [event],
        source_run_id=SOURCE_RUN,
        source_manifest_sha256=SOURCE_HASH,
        destination_run_id=DEST_RUN,
        destination_manifest_sha256=DEST_HASH,
    )
    assert eligible_decisions == []
    assert rejected[0]["reason"].startswith("liked automatico")
