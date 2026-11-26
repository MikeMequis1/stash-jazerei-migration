"""Modelos de execução controlada e confirmação humana."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExecutionStatus(StrEnum):
    PREPARED = "prepared"
    BLOCKED = "blocked"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    CANCELLED = "cancelled"


class ActionExecutionStatus(StrEnum):
    PREPARED = "prepared"
    BLOCKED = "blocked"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY = "ready"
    WRITING = "writing"
    WRITE_CONFIRMED = "write_confirmed"
    WRITE_AMBIGUOUS = "write_ambiguous"
    VERIFICATION_PENDING = "verification_pending"
    VERIFIED = "verified"
    CONFLICT = "conflict"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    kind: str
    game_id: int | None = None
    source_ref: str
    destination_slug: str | None = None
    planned_before: dict[str, Any] | None = None
    planned_after: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] | None = None
    payload_sha256: str | None = None
    status: ActionExecutionStatus = ActionExecutionStatus.PREPARED
    confirmation_id: str | None = None
    confirmed_at: str | None = None
    before_read: dict[str, Any] | None = None
    write_result: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    reason: str | None = None
    error: str | None = None


class ExecutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_actions: int = 0
    ready: int = 0
    blocked: int = 0
    verified: int = 0
    conflicts: int = 0
    failed: int = 0
    ambiguous: int = 0


class ConfirmationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    confirmation_id: str
    execution_id: str
    plan_id: str
    plan_sha256: str
    manifest_sha256: str
    preflight_id: str
    preflight_manifest_sha256: str
    action_id: str
    game_id: int
    payload_sha256: str
    user_id: str
    confirmed_at: str
    confirmed_by: Literal["human"] = "human"
    status: Literal["confirmed"] = "confirmed"


class ExecutionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: str
    execution_id: str
    action_id: str | None = None
    from_status: str | None = None
    to_status: str
    reason: str | None = None


class ExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    execution_id: str
    plan_id: str
    plan_sha256: str
    manifest_sha256: str
    preflight_id: str
    preflight_manifest_sha256: str
    preflight_sha256: str
    approval_id: str
    user_id: str
    identity_verified: bool
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    status: ExecutionStatus = ExecutionStatus.PREPARED
    actions: list[ExecutionAction] = Field(default_factory=list)
    summary: ExecutionSummary = Field(default_factory=ExecutionSummary)
    errors: list[str] = Field(default_factory=list)
    verification: dict[str, Any] | None = None
