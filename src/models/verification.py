"""Modelos do relatório de verificação pós-execução."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class VerificationCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planned_actions: int = 0
    unmatched: int = 0
    execution_verified: int = 0
    execution_ready: int = 0
    execution_failed: int = 0
    execution_ambiguous: int = 0
    execution_conflicts: int = 0
    execution_cancelled: int = 0
    execution_other: int = 0
    presence_found: int = 0
    presence_absent: int = 0
    presence_duplicate: int = 0
    presence_inconclusive: int = 0
    content_full: int = 0
    content_partial: int = 0
    content_mismatch: int = 0
    content_unverifiable: int = 0
    confirmed: int = 0
    found_not_attributed: int = 0
    divergent: int = 0
    not_executed: int = 0
    insufficient: int = 0


class VerificationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    source_ref: str
    title: str | None = None
    destination_slug: str | None = None
    game_id: int | None = None
    execution_status: str
    presence: Literal["found", "absent", "inconclusive", "duplicate", "divergent"]
    confirmation: Literal[
        "confirmed",
        "found_not_attributed",
        "absent",
        "divergent",
        "ambiguous",
        "conflict",
        "not_executed",
        "inconclusive",
    ]
    content_status: Literal["full", "partial", "mismatch", "unverifiable", "not_compared"]
    planned_rating: float | None = None
    found_rating: float | None = None
    rating_match: bool | None = None
    planned_text: str = ""
    found_text: str | None = None
    text_match: bool | None = None
    planned_liked: bool | None = None
    found_liked: bool | None = None
    liked_status: Literal["match", "mismatch", "unavailable", "not_planned"] = "not_planned"
    game_slug_match: bool | None = None
    user_match: bool | None = None
    prior_presence: Literal["found", "absent", "unavailable", "unverifiable"] = "unavailable"
    causal: str = "insufficient"
    confirmation_blocked_by_plan: bool = False
    review_id: str | None = None
    snapshot_ref: str | None = None
    reasons: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class VerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    verification_id: str
    plan_id: str
    execution_id: str
    snapshot_run_id: str
    snapshot_file: str
    snapshot_manifest_sha256: str
    snapshot_response_hash: str | None = None
    snapshot_complete: bool
    identity_verified: bool
    plan_sha256: str
    manifest_sha256: str
    execution_sha256: str
    approval_id: str | None = None
    preflight_id: str | None = None
    prior_snapshot_run_id: str | None = None
    prior_sources: list[str] = Field(default_factory=list)
    created_at: str
    status: Literal["complete", "inconclusive"]
    capture_status: Literal["complete", "incomplete", "unverifiable", "invalid"] = "unverifiable"
    report_status: Literal["processed", "inconclusive"] = "inconclusive"
    migration_status: Literal[
        "completed", "incomplete", "failed", "inconclusive", "cancelled", "unknown"
    ] = "unknown"
    causal_confirmation_status: Literal["none", "partial", "all"] = "none"
    snapshot_semantic_status: Literal["valid", "incomplete", "unverifiable", "invalid"] = (
        "unverifiable"
    )
    snapshot_semantic_errors: list[str] = Field(default_factory=list)
    plan_blocked: bool = False
    plan_block_reasons: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    counts: VerificationCounts = Field(default_factory=VerificationCounts)
    actions: list[VerificationAction] = Field(default_factory=list)
    plan_verification: dict[str, Any] = Field(default_factory=dict)
    report_sha256: str = ""
