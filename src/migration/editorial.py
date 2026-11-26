"""Aceitações editoriais append-only.

Diferente de aprovação de correspondência e de aprovação formal do plano:
registra que o usuário examinou um alerta editorial e decidiu manter a
associação existente. Não altera jogos, reviews, liked, matches ou plano.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from src.migration.approval import canonical_hash
from src.models.normalized import GameMatch
from src.models.source import ReviewRecord
from src.storage import utc_now


class EditorialAcceptanceError(RuntimeError):
    pass


class EditorialAcceptance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    event_id: str
    at: str
    reviewer: Literal["human"] = "human"
    destination_user_id: str | None = None
    event_type: Literal["editorial_acceptance"] = "editorial_acceptance"
    plan_id: str
    plan_sha256: str
    manifest_sha256: str
    action_id: str
    stable_id: str
    source_ref: str
    destination_game_id: int
    destination_slug: str
    decision: Literal["keep_association"] = "keep_association"
    reason: str
    source_hash: str
    matches_sha256: str
    data_sha256: str


def _data_payload(event: EditorialAcceptance) -> dict[str, Any]:
    return {
        "plan_id": event.plan_id,
        "plan_sha256": event.plan_sha256,
        "manifest_sha256": event.manifest_sha256,
        "action_id": event.action_id,
        "stable_id": event.stable_id,
        "source_ref": event.source_ref,
        "destination_game_id": event.destination_game_id,
        "destination_slug": event.destination_slug,
        "decision": event.decision,
        "reason": event.reason,
        "source_hash": event.source_hash,
        "matches_sha256": event.matches_sha256,
    }


def compute_event_id(event: EditorialAcceptance) -> str:
    return canonical_hash(event.model_dump(mode="json", exclude={"event_id"}))


def append_acceptance(
    path: Path,
    *,
    plan_id: str,
    plan_sha256: str,
    manifest_sha256: str,
    action_id: str,
    stable_id: str,
    source_ref: str,
    destination_game_id: int,
    destination_slug: str,
    reason: str,
    source_hash: str,
    matches_sha256: str,
    destination_user_id: str | None = None,
) -> EditorialAcceptance:
    event = EditorialAcceptance(
        event_id="pending",
        at=utc_now(),
        plan_id=plan_id,
        plan_sha256=plan_sha256,
        manifest_sha256=manifest_sha256,
        action_id=action_id,
        stable_id=stable_id,
        source_ref=source_ref,
        destination_game_id=destination_game_id,
        destination_slug=destination_slug,
        reason=reason,
        source_hash=source_hash,
        destination_user_id=destination_user_id,
        matches_sha256=matches_sha256,
        data_sha256="pending",
    )
    event.data_sha256 = canonical_hash(_data_payload(event))
    event.event_id = compute_event_id(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return event


def load_acceptances(path: Path) -> list[EditorialAcceptance]:
    if not path.exists():
        return []
    result: list[EditorialAcceptance] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = EditorialAcceptance.model_validate(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            raise EditorialAcceptanceError(f"evento editorial invalido na linha {index}") from exc
        if event.data_sha256 != canonical_hash(_data_payload(event)):
            raise EditorialAcceptanceError(f"data_sha256 invalido na linha {index}")
        if event.event_id != compute_event_id(event):
            raise EditorialAcceptanceError(f"event_id invalido na linha {index}")
        result.append(event)
    return result


def normalized_title(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def editorial_alert(review: ReviewRecord, match: GameMatch) -> str | None:
    """Alerta editorial objetivo: título normalizado ou ano divergente."""
    source_title = review.game_title or match.source_title
    reasons: list[str] = []
    if normalized_title(source_title) != normalized_title(match.dest_name):
        reasons.append(f"titulo: {source_title!r} -> {match.dest_name!r}")
    if match.source_year and match.dest_year and match.source_year != match.dest_year:
        reasons.append(f"ano: {match.source_year} -> {match.dest_year}")
    return "; ".join(reasons) if reasons else None


def pending_editorial_alerts(
    *,
    plan_actions: list[dict[str, Any]],
    matches: dict[str, GameMatch],
    reviews: dict[str, ReviewRecord],
    acceptances: list[EditorialAcceptance],
) -> list[dict[str, Any]]:
    accepted = {(event.source_ref, event.destination_game_id) for event in acceptances}
    pending: list[dict[str, Any]] = []
    for index, action in enumerate(plan_actions, start=1):
        source_ref = action.get("source_ref")
        match = matches.get(str(source_ref))
        review = reviews.get(str(source_ref))
        if match is None or review is None:
            continue
        alert = editorial_alert(review, match)
        if alert is None:
            continue
        key = (match.source_slug, match.dest_game_id)
        if key in accepted:
            continue
        pending.append(
            {
                "stable_id": f"S9-{index:03d}",
                "source_ref": source_ref,
                "destination_game_id": match.dest_game_id,
                "alert": alert,
            }
        )
    return pending
