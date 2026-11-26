"""Resultado versionado de preflight do destino."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PreflightStatus(StrEnum):
    COMPATIBLE = "compatible"
    CONFLICT = "conflict"
    BLOCKED = "blocked"
    INCONCLUSIVE = "inconclusive"
    INVALID = "invalid"


class ActionStatus(StrEnum):
    COMPATIBLE = "compatible"
    CONFLICT = "conflict"
    UNSUPPORTED = "unsupported"
    INCONCLUSIVE = "inconclusive"
    INVALID = "invalid"


class PreflightIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool
    user_id: str | None = None


class ActionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    kind: str
    source_ref: str
    game_id: int | None = None
    status: ActionStatus
    reason: str
    snapshot_ref: str | None = None
    fields: list[str] = Field(default_factory=list)


class PreflightSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PreflightStatus
    compatible_actions: int = 0
    conflicts: int = 0
    unsupported: int = 0
    inconclusive: int = 0
    invalid: int = 0


class PreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    preflight_id: str
    plan_id: str
    manifest_sha256: str
    approval_id: str | None = None
    approval_status: str
    identity: PreflightIdentity
    checks: dict[str, str] = Field(default_factory=dict)
    actions: list[ActionCheck] = Field(default_factory=list)
    snapshots: list[dict[str, Any]] = Field(default_factory=list)
    result: PreflightSummary
    reasons: list[str] = Field(default_factory=list)
    captured_at: str
    result_sha256: str = ""
