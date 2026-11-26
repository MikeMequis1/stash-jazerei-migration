"""Historico append-only das decisoes de correspondencia por run de avaliacoes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.migration.approval import canonical_hash
from src.storage import utc_now

MATCH_ACTIONS = (
    "approve",
    "reject",
    "manual",
    "no_match",
    "pending",
    "confirm_edition",
    "reopen",
)


class MatchDecisionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2
    event_id: str = "pending"
    at: str = Field(default_factory=utc_now)
    evaluation_run_id: str = ""
    evaluations_sha256: str = ""
    source_run_id: str = ""
    source_ref: str
    evaluation_kind: Literal["written_review", "rating_only"] = "written_review"
    source_hash: str
    action: Literal[
        "approve",
        "reject",
        "manual",
        "no_match",
        "pending",
        "confirm_edition",
        "reopen",
    ]
    previous_decision: str = "manual_review"
    new_decision: str = "manual_review"
    candidate_id: int | None = None
    candidate_slug: str | None = None
    candidate_name: str | None = None
    confidence: float | None = None
    edition_confirmed: bool = False
    operator_id: str = "local"
    destination_user_id: str | None = None
    reason: str = ""
    supersedes: str | None = None
    matches_sha256_before: str = ""
    matches_sha256_after: str = ""


class MatchDecisionStoreError(RuntimeError):
    pass


def compute_event_id(event: MatchDecisionEvent) -> str:
    """Chave idempotente: ignora timestamp, hashes materiais e supersedes."""
    payload = event.model_dump(
        mode="json",
        exclude={"event_id", "at", "matches_sha256_before", "matches_sha256_after", "supersedes"},
    )
    return canonical_hash(payload)


def load_match_events(path: Path | None) -> list[MatchDecisionEvent]:
    if path is None or not path.is_file():
        return []
    events: list[MatchDecisionEvent] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = MatchDecisionEvent.model_validate(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            raise MatchDecisionStoreError(
                f"evento de correspondencia invalido na linha {index}"
            ) from exc
        if event.action not in MATCH_ACTIONS:
            raise MatchDecisionStoreError(f"acao invalida na linha {index}")
        if event.event_id != compute_event_id(event):
            raise MatchDecisionStoreError(f"event_id invalido na linha {index}")
        events.append(event)
    return events


def append_match_event(
    path: Path,
    *,
    source_ref: str,
    evaluation_run_id: str,
    evaluations_sha256: str,
    source_run_id: str,
    evaluation_kind: str,
    source_hash: str,
    action: str,
    previous_decision: str,
    new_decision: str,
    candidate_id: int | None = None,
    candidate_slug: str | None = None,
    candidate_name: str | None = None,
    confidence: float | None = None,
    edition_confirmed: bool = False,
    operator_id: str = "local",
    destination_user_id: str | None = None,
    reason: str = "",
    supersedes: str | None = None,
    matches_sha256_before: str = "",
    matches_sha256_after: str = "",
) -> MatchDecisionEvent:
    if action not in MATCH_ACTIONS:
        raise MatchDecisionStoreError(f"acao invalida: {action}")
    if not source_ref or not source_hash or not evaluation_run_id:
        raise MatchDecisionStoreError(
            "source_ref, source_hash e evaluation_run_id sao obrigatorios"
        )
    event = MatchDecisionEvent(
        source_ref=source_ref,
        evaluation_run_id=evaluation_run_id,
        evaluations_sha256=evaluations_sha256,
        source_run_id=source_run_id,
        evaluation_kind=evaluation_kind,  # type: ignore[arg-type]
        source_hash=source_hash,
        action=action,  # type: ignore[arg-type]
        previous_decision=previous_decision,
        new_decision=new_decision,
        candidate_id=candidate_id,
        candidate_slug=candidate_slug,
        candidate_name=candidate_name,
        confidence=confidence,
        edition_confirmed=edition_confirmed,
        operator_id=operator_id or "local",
        destination_user_id=destination_user_id,
        reason=reason.strip(),
        supersedes=supersedes,
        matches_sha256_before=matches_sha256_before,
        matches_sha256_after=matches_sha256_after,
    )
    event.event_id = compute_event_id(event)
    existing = load_match_events(path)
    if any(item.event_id == event.event_id for item in existing):
        return next(item for item in existing if item.event_id == event.event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return event


def latest_by_slug(events: list[MatchDecisionEvent]) -> dict[str, MatchDecisionEvent]:
    latest: dict[str, MatchDecisionEvent] = {}
    for event in events:
        latest[event.source_ref] = event
    return latest


def edition_confirmation(
    events: list[MatchDecisionEvent], *, source_ref: str, candidate_id: int | None
) -> MatchDecisionEvent | None:
    if candidate_id is None:
        return None
    for event in reversed([item for item in events if item.source_ref == source_ref]):
        if event.edition_confirmed and event.candidate_id == candidate_id:
            return event
    return None


def history_conflict(
    event: MatchDecisionEvent | dict | None,
    *,
    decision: str,
    candidate_id: int | None,
) -> str | None:
    """Compara o ultimo evento do run com o estado materializado."""
    if event is None:
        return None
    if isinstance(event, dict):
        action = event.get("action")
        new_decision = event.get("new_decision")
        event_candidate = event.get("candidate_id")
    else:
        action = event.action
        new_decision = event.new_decision
        event_candidate = event.candidate_id
    if action == "confirm_edition":
        return None
    if new_decision != decision:
        return f"historico={new_decision}; materializado={decision}"
    if event_candidate is not None and candidate_id is not None and event_candidate != candidate_id:
        return f"candidato historico={event_candidate}; materializado={candidate_id}"
    return None


def event_payload(event: MatchDecisionEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")
