"""Snapshot versionado do estado de reviews do Já Zerei."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.models.destination import DestinationReview


class SnapshotQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["review-state"] = "review-state"
    game_id: int
    slug: str
    user_id: str
    endpoint: str
    page_size: int


class SnapshotIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool
    user_id: str
    sources: list[str] = Field(default_factory=list)


class SnapshotResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exists: bool
    review: DestinationReview | None = None


class SnapshotPagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    complete: bool
    pages_fetched: int
    records_received: int
    unique_records: int
    duplicates_discarded: int
    termination_reason: str
    page_size: int
    max_pages: int
    content_range_total: int | None = None


class SnapshotCapture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captured_at: str
    response_hash: str
    complete: bool


class ReviewStateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    source: Literal["jazerei"] = "jazerei"
    query: SnapshotQuery
    identity: SnapshotIdentity
    result: SnapshotResult
    pagination: SnapshotPagination
    capture: SnapshotCapture


class AccountSnapshotQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["account-review-state"] = "account-review-state"
    user_id: str
    endpoint: str
    page_size: int


class AccountReviewsSnapshot(BaseModel):
    """Snapshot completo das reviews autenticadas da conta."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    source: Literal["jazerei"] = "jazerei"
    query: AccountSnapshotQuery
    identity: SnapshotIdentity
    reviews: list[DestinationReview]
    pagination: SnapshotPagination
    capture: SnapshotCapture


def canonical_hash(snapshot: ReviewStateSnapshot) -> str:
    """Hash determinístico do conteúdo, sem timestamp."""
    payload = {
        "schema_version": snapshot.schema_version,
        "source": snapshot.source,
        "query": snapshot.query.model_dump(mode="json"),
        "identity": snapshot.identity.model_dump(mode="json"),
        "result": snapshot.result.model_dump(mode="json"),
        "pagination": snapshot.pagination.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_account_hash(snapshot: AccountReviewsSnapshot) -> str:
    """Mesmo payload de `build_account_review_snapshot`, sem timestamp."""
    payload = {
        "query": snapshot.query.model_dump(mode="json"),
        "identity": snapshot.identity.model_dump(mode="json"),
        "reviews": [review.model_dump(mode="json") for review in snapshot.reviews],
        "pagination": snapshot.pagination.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
