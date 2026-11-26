"""Registros de origem (Stash), preservados sem perda semantica."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["stash"] = "stash"
    source_url: str
    raw: dict[str, Any] = Field(default_factory=dict)


class GameRecord(SourceRecord):
    slug: str
    title: str
    cover_url: str | None = None
    year: int | None = None
    status: str | None = None
    date_displayed: str | None = None
    rating: float | None = None
    platform: str | None = None
    category: str | None = None


EvaluationKind = Literal["written_review", "rating_only"]


class ReviewRecord(SourceRecord):
    game_slug: str
    game_title: str | None = None
    cover_url: str | None = None
    text: str = ""
    text_source: Literal["listing", "detail", "none"] = "listing"
    rating: float | None = None
    date: str | None = None
    status: str | None = None
    progress: str | None = None
    likes_count: int | None = None
    comments_count: int | None = None
    game_url: str | None = None
    evaluation_kind: EvaluationKind = "written_review"


class PageFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    url: str
    error: str


class ExportReport(BaseModel):
    """Relatorio de coleta: paginas, contagens, campos ausentes e falhas."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["games", "reviews"]
    started_at: str
    finished_at: str | None = None
    base_url: str
    last_page_detected: int = 0
    pages_requested: int = 0
    pages_fetched: int = 0
    records: int = 0
    unique_records: int = 0
    complete: bool = False
    directed: bool = False
    requested_pages: list[int] = Field(default_factory=list)
    target_slug: str | None = None
    target_found: bool | None = None
    requested_scope_complete: bool = False
    details_checked: int = 0
    text_mismatches: list[str] = Field(default_factory=list)
    missing_fields: dict[str, int] = Field(default_factory=dict)
    duplicates: list[str] = Field(default_factory=list)
    failures: list[PageFailure] = Field(default_factory=list)
