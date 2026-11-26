"""Ratificações humanas em lote de decisões automáticas.

A ratificação não altera `matches.json` nem `liked-decisions.jsonl`: registra,
em arquivo append-only, um conjunto fechado de decisões já existentes que o
usuário revisou e manteve. A elegibilidade do plano é derivada dessa cobertura.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from src.migration.approval import canonical_hash
from src.models.normalized import GameMatch, LikedDecision
from src.storage import utc_now

DIMENSIONS = ("matches", "liked")


class RatificationError(RuntimeError):
    pass


class RatificationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    ratification_id: str
    at: str
    reviewer: Literal["human"] = "human"
    destination_user_id: str | None = None
    plan_id: str
    plan_sha256: str
    manifest_sha256: str
    source_run_id: str
    source_manifest_sha256: str
    destination_run_id: str
    destination_manifest_sha256: str
    dimension: Literal["matches", "liked"]
    review_type: Literal["human_batch_review"] = "human_batch_review"
    result: Literal["confirmed_existing"] = "confirmed_existing"
    items: list[dict[str, Any]]
    items_sha256: str


def compute_ratification_id(event: RatificationEvent) -> str:
    return canonical_hash(event.model_dump(mode="json", exclude={"ratification_id"}))


def load_ratifications(path: Path) -> list[RatificationEvent]:
    if not path.exists():
        return []
    events: list[RatificationEvent] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = RatificationEvent.model_validate(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            raise RatificationError(f"ratificacao invalida na linha {index}") from exc
        if event.items_sha256 != canonical_hash(event.items):
            raise RatificationError(f"items_sha256 invalido na linha {index}")
        if event.ratification_id != compute_ratification_id(event):
            raise RatificationError(f"ratification_id invalido na linha {index}")
        if event.dimension not in DIMENSIONS:
            raise RatificationError(f"dimensao invalida na linha {index}")
        events.append(event)
    return events


def append_ratification(
    path: Path,
    *,
    dimension: Literal["matches", "liked"],
    items: list[dict[str, Any]],
    plan_id: str,
    plan_sha256: str,
    manifest_sha256: str,
    source_run_id: str,
    source_manifest_sha256: str,
    destination_run_id: str,
    destination_manifest_sha256: str,
    destination_user_id: str | None = None,
    at: str | None = None,
) -> RatificationEvent:
    if dimension not in DIMENSIONS:
        raise RatificationError("dimensao invalida")
    ordered = sorted(items, key=canonical_hash)
    event = RatificationEvent(
        ratification_id="pending",
        at=at or utc_now(),
        plan_id=plan_id,
        plan_sha256=plan_sha256,
        manifest_sha256=manifest_sha256,
        source_run_id=source_run_id,
        source_manifest_sha256=source_manifest_sha256,
        destination_run_id=destination_run_id,
        destination_manifest_sha256=destination_manifest_sha256,
        destination_user_id=destination_user_id,
        dimension=dimension,
        items=ordered,
        items_sha256=canonical_hash(ordered),
    )
    event.ratification_id = compute_ratification_id(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return event


def match_item(match: GameMatch, latest_event: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_ref": match.source_slug,
        "game_id": match.dest_game_id,
        "destination_slug": match.dest_slug,
        "decision": match.decision.value,
        "origin_event_id": latest_event.get("event_id"),
        "origin_at": latest_event.get("at"),
        "source_hash": latest_event.get("source_hash"),
    }


def liked_item(decision: LikedDecision) -> dict[str, Any]:
    return {
        "source_ref": decision.source_ref,
        "game_id": decision.game_id,
        "value": decision.value,
        "decision_id": decision.decision_id,
        "decided_at": decision.decided_at,
        "source_hash": decision.source_hash,
    }


def match_origin(latest_event: dict[str, Any] | None) -> str:
    if not latest_event:
        return "unmatched"
    action = latest_event.get("action")
    if action in {"approve", "manual", "confirm_edition"}:
        return "human_individual"
    if action == "auto_approve":
        return "automatic_unreviewed"
    return "unmatched"


def liked_origin(decision: LikedDecision | None) -> str:
    if decision is None:
        return "unmatched"
    return "human_individual" if decision.source == "human" else "automatic_unreviewed"


def match_is_ratified(
    *,
    match: GameMatch,
    latest_event: dict[str, Any] | None,
    ratifications: Iterable[RatificationEvent],
    source_run_id: str,
    source_manifest_sha256: str,
    destination_run_id: str,
    destination_manifest_sha256: str,
) -> RatificationEvent | None:
    if match.decision.value != "approved" or not latest_event:
        return None
    item = match_item(match, latest_event)
    for event in reversed(list(ratifications)):
        if event.dimension != "matches":
            continue
        if event.source_run_id != source_run_id:
            continue
        if event.source_manifest_sha256 != source_manifest_sha256:
            continue
        if event.destination_run_id != destination_run_id:
            continue
        if event.destination_manifest_sha256 != destination_manifest_sha256:
            continue
        if item in event.items:
            return event
    return None


def liked_is_ratified(
    *,
    decision: LikedDecision | None,
    ratifications: Iterable[RatificationEvent],
    source_run_id: str,
    source_manifest_sha256: str,
    destination_run_id: str,
    destination_manifest_sha256: str,
) -> RatificationEvent | None:
    if decision is None:
        return None
    item = liked_item(decision)
    for event in reversed(list(ratifications)):
        if event.dimension != "liked":
            continue
        if event.source_run_id != source_run_id:
            continue
        if event.source_manifest_sha256 != source_manifest_sha256:
            continue
        if event.destination_run_id != destination_run_id:
            continue
        if event.destination_manifest_sha256 != destination_manifest_sha256:
            continue
        if item in event.items:
            return event
    return None


def eligible_matches(
    matches: list[GameMatch],
    history: list[dict[str, Any]],
    ratifications: list[RatificationEvent],
    *,
    source_run_id: str,
    source_manifest_sha256: str,
    destination_run_id: str,
    destination_manifest_sha256: str,
) -> tuple[list[GameMatch], list[dict[str, Any]]]:
    latest_by_slug: dict[str, dict[str, Any]] = {}
    for event in history:
        latest_by_slug[str(event.get("source_slug"))] = event
    eligible: list[GameMatch] = []
    rejected: list[dict[str, Any]] = []
    for match in matches:
        if match.decision.value != "approved":
            continue
        latest = latest_by_slug.get(match.source_slug)
        if match_origin(latest) == "human_individual":
            eligible.append(match)
            continue
        event = match_is_ratified(
            match=match,
            latest_event=latest,
            ratifications=ratifications,
            source_run_id=source_run_id,
            source_manifest_sha256=source_manifest_sha256,
            destination_run_id=destination_run_id,
            destination_manifest_sha256=destination_manifest_sha256,
        )
        if event is not None:
            eligible.append(match)
        else:
            rejected.append(
                {
                    "source_ref": match.source_slug,
                    "reason": "correspondencia automatica sem ratificacao humana",
                    "origin": match_origin(latest),
                }
            )
    return eligible, rejected


def _latest_decisions(decisions: Iterable[LikedDecision]) -> list[LikedDecision]:
    latest: dict[tuple[str, int], LikedDecision] = {}
    for decision in decisions:
        latest[(decision.source_ref, decision.game_id)] = decision
    return list(latest.values())


def eligible_liked(
    decisions: list[LikedDecision],
    match_by_slug: dict[str, GameMatch],
    ratifications: list[RatificationEvent],
    *,
    source_run_id: str,
    source_manifest_sha256: str,
    destination_run_id: str,
    destination_manifest_sha256: str,
) -> tuple[list[LikedDecision], list[dict[str, Any]]]:
    eligible: list[LikedDecision] = []
    rejected: list[dict[str, Any]] = []
    for decision in _latest_decisions(decisions):
        match = match_by_slug.get(decision.source_ref)
        if match is None or match.dest_game_id != decision.game_id:
            continue
        if decision.source == "human":
            eligible.append(decision)
            continue
        event = liked_is_ratified(
            decision=decision,
            ratifications=ratifications,
            source_run_id=source_run_id,
            source_manifest_sha256=source_manifest_sha256,
            destination_run_id=destination_run_id,
            destination_manifest_sha256=destination_manifest_sha256,
        )
        if event is not None:
            eligible.append(decision)
        else:
            rejected.append(
                {
                    "source_ref": decision.source_ref,
                    "reason": "liked automatico sem ratificacao humana",
                    "origin": liked_origin(decision),
                }
            )
    return eligible, rejected


def validation_summary(
    *,
    matches: list[GameMatch],
    history: list[dict[str, Any]],
    decisions: list[LikedDecision],
    ratifications: list[RatificationEvent],
    source_run_id: str,
    source_manifest_sha256: str,
    destination_run_id: str,
    destination_manifest_sha256: str,
) -> dict[str, int]:
    latest_by_slug: dict[str, dict[str, Any]] = {}
    for event in history:
        latest_by_slug[str(event.get("source_slug"))] = event
    match_counts = {"human_individual": 0, "human_batch_review": 0, "automatic_unreviewed": 0}
    for match in matches:
        if match.decision.value != "approved":
            continue
        latest = latest_by_slug.get(match.source_slug)
        if match_origin(latest) == "human_individual":
            match_counts["human_individual"] += 1
        elif match_is_ratified(
            match=match,
            latest_event=latest,
            ratifications=ratifications,
            source_run_id=source_run_id,
            source_manifest_sha256=source_manifest_sha256,
            destination_run_id=destination_run_id,
            destination_manifest_sha256=destination_manifest_sha256,
        ):
            match_counts["human_batch_review"] += 1
        else:
            match_counts["automatic_unreviewed"] += 1
    liked_counts = {"human_individual": 0, "human_batch_review": 0, "automatic_unreviewed": 0}
    approved_by_slug = {
        match.source_slug: match for match in matches if match.decision.value == "approved"
    }
    for decision in _latest_decisions(decisions):
        match = approved_by_slug.get(decision.source_ref)
        if match is None or match.dest_game_id != decision.game_id:
            continue
        if decision.source == "human":
            liked_counts["human_individual"] += 1
        elif liked_is_ratified(
            decision=decision,
            ratifications=ratifications,
            source_run_id=source_run_id,
            source_manifest_sha256=source_manifest_sha256,
            destination_run_id=destination_run_id,
            destination_manifest_sha256=destination_manifest_sha256,
        ):
            liked_counts["human_batch_review"] += 1
        else:
            liked_counts["automatic_unreviewed"] += 1
    return {"matches": match_counts, "liked": liked_counts}
