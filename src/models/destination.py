"""Formatos lidos do destino (Ja Zerei). Somente dados, sem credenciais."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DestinationGame(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    slug: str | None = None
    name: str
    cover: str | None = None
    release_year: int | None = None
    developer: str | None = None
    platforms: list[str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)


class DestinationReview(BaseModel):
    """Review do destino; campos desconhecidos sao preservados via extra."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    game_id: int
    game_slug: str | None = None
    game_name: str | None = None
    game_cover: str | None = None
    game_year: int | None = None
    developer: str | None = None
    user_id: str | None = None
    username: str | None = None
    rating: float | None = None
    text: str = ""
    likes_count: int | None = None
    comments_count: int | None = None
    created_at: str | None = None
    moderation_status: str | None = None
    short_id: str | None = None
    genres: list[Any] = Field(default_factory=list)
    liked: bool | None = None
    platforms: list[str] | None = None


class WatchlistEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str | None = None
    game_id: int
    game_slug: str | None = None
    game_title: str | None = None
    cover_url: str | None = None
    release_year: int | None = None
    primary_platform: str | None = None


class ProfileFavorite(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    slug: str | None = None
    title: str | None = None
    cover_url: str | None = None


class PlayingNowEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    slug: str | None = None
    title: str | None = None
    cover_url: str | None = None
    platform: str | None = None


class DestinationProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str | None = None
    username: str | None = None
    favorite_games: list[ProfileFavorite] = Field(default_factory=list)
    playing_now: list[PlayingNowEntry] = Field(default_factory=list)
