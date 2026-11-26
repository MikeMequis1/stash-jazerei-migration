"""Modelos de manifesto de plano e aprovação humana."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    UNAPPROVED = "unapproved"
    STALE = "stale"
    INVALID = "invalid"
    BLOCKED = "blocked"


class PlanManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    plan_id: str
    created_at: str
    source: dict[str, Any]
    destination: dict[str, Any]
    matching: dict[str, Any]
    decisions: dict[str, Any]
    plan: dict[str, Any]
    inputs: dict[str, str] = Field(default_factory=dict)
    provenance_complete: bool
    plan_sha256: str
    plan_file_sha256: str
    manifest_sha256: str


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    approval_id: str
    plan_id: str
    manifest_sha256: str
    plan_sha256: str
    approved_at: str
    approved_by: Literal["human"] = "human"
    approved_actions: list[str]
    approval_scope: Literal["exact_plan"] = "exact_plan"
    status: Literal["approved"] = "approved"


class ApprovalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ApprovalStatus
    reasons: list[str] = Field(default_factory=list)
    approval: ApprovalRecord | None = None
