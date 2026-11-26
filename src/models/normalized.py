"""Tipos normalizados de correspondencia e plano de migracao."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MatchDecision(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    MANUAL_REVIEW = "manual_review"


class GameMatch(BaseModel):
    """Correspondencia explicita entre jogo do Stash e jogo do Ja Zerei."""

    model_config = ConfigDict(extra="forbid")

    source_slug: str
    source_title: str
    source_year: int | None = None
    dest_game_id: int | None = None
    dest_slug: str | None = None
    dest_name: str | None = None
    dest_year: int | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    decision: MatchDecision = MatchDecision.MANUAL_REVIEW
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    lookup_status: (
        Literal["candidate", "manual_review", "no_candidate", "inconclusive", "error", "not_run"]
        | None
    ) = None

    @property
    def is_approved(self) -> bool:
        return self.decision is MatchDecision.APPROVED and self.dest_game_id is not None


ActionKind = Literal["review", "watchlist", "favorite", "playing_now"]


class LikedStatus(StrEnum):
    UNRESOLVED = "unresolved"
    DECIDED = "decided"
    STALE = "stale"
    REQUIRES_RECONFIRMATION = "requires_reconfirmation"


class LikedState(BaseModel):
    """Estado interno de recomendacao. Nunca implica um valor no destino."""

    model_config = ConfigDict(extra="forbid")

    value: bool | None = None
    status: LikedStatus = LikedStatus.UNRESOLVED
    source: str = "human_decision_required"
    source_ref: str | None = None
    game_id: int | None = None
    source_hash: str | None = None
    decision_id: str | None = None
    decided_at: str | None = None
    event_type: str | None = None
    binding_status: Literal["bound", "pending_identity", "stale", "none"] = "none"
    requires_reconfirmation: bool = False


class LikedDecision(BaseModel):
    """Decisao versionada, vinculada ao conteudo da review de origem."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    decision_id: str
    source_ref: str
    game_id: int | None = None
    source_hash: str
    value: bool | None
    source: Literal["human", "automatic"] = "human"
    decided_at: str
    supersedes: str | None = None
    event_type: Literal["decision", "reconfirmation", "deferral", "binding"] = "decision"
    binding_status: Literal["bound", "pending_identity", "stale", "none"] = "bound"
    evaluation_kind: Literal["written_review", "rating_only"] | None = None
    evaluation_run_id: str | None = None
    evaluations_sha256: str | None = None
    operator_id: str | None = None
    destination_user_id: str | None = None
    previous_value: bool | None = None
    previous_status: str | None = None
    reason: str = ""


class MigrationAction(BaseModel):
    """Acao proposta. Nada e executado sem approved=True no plano e na acao."""

    model_config = ConfigDict(extra="forbid")

    kind: ActionKind
    source_ref: str
    source_payload: dict[str, Any] = Field(default_factory=dict)
    game_id: int | None = None
    destination_slug: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    liked: LikedState = Field(default_factory=LikedState)
    approved: bool = False
    executed: bool = False
    verified: bool = False
    error: str | None = None


class MigrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_version: int = 1
    plan_id: str | None = None
    source: str = "stash"
    destination: str = "jazerei"
    approved: bool = False
    actions: list[MigrationAction] = Field(default_factory=list)
    unmatched: list[str] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def approved_actions(self) -> list[MigrationAction]:
        return [action for action in self.actions if action.approved]
