"""Construcao do universo de avaliacoes Stash (escrita ou somente nota).

A distincao e feita pelos dados, nunca pelo status do Stash:

- `written_review`: nota valida e texto nao vazio em `reviews.json`;
- `rating_only`: nota valida em `games.json`, sem texto escrito;
- `no_rating`: sem nota valida; descartado.

O status e preservado apenas como metadado de auditoria.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.models.source import GameRecord, ReviewRecord
from src.storage import utc_now

SPECIAL_CATEGORIES = frozenset(
    {
        "Mod",
        "Bundle",
        "Port",
        "Remaster",
        "Remake",
        "Expanded game",
        "Expansion",
        "Standalone expansion",
        "Episode",
        "DLC",
        "Season",
        "Fork",
    }
)


class EvaluationCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = 0
    written_review: int = 0
    rating_only: int = 0
    no_rating: int = 0
    inconsistencies: int = 0


class EvaluationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "duplicate_game_slug",
        "duplicate_review_slug",
        "review_without_game",
        "review_without_game_rating",
        "invalid_review_rating",
        "rating_divergence",
        "text_without_valid_rating",
    ]
    slug: str | None = None
    message: str
    game_rating: float | None = None
    review_rating: float | None = None


class EvaluationSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    source_run_id: str = ""
    generated_at: str = Field(default_factory=utc_now)
    counts: EvaluationCounts = Field(default_factory=EvaluationCounts)
    evaluations: list[ReviewRecord] = Field(default_factory=list)
    excluded: list[dict[str, Any]] = Field(default_factory=list)
    inconsistencies: list[EvaluationIssue] = Field(default_factory=list)
    special_editions: list[dict[str, Any]] = Field(default_factory=list)
    duplicate_titles: list[dict[str, Any]] = Field(default_factory=list)
    text_divergences: list[dict[str, Any]] = Field(default_factory=list)


def valid_rating(value: float | int | None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0 <= float(value) <= 10


def _written_or_rating_only(game: GameRecord, review: ReviewRecord | None) -> ReviewRecord | None:
    text = (review.text if review else "") or ""
    if text.strip():
        return ReviewRecord.model_validate(
            {
                **(review.model_dump(mode="json") if review else {}),
                "source": "stash",
                "source_url": review.source_url if review else game.source_url,
                "raw": review.raw if review else {},
                "game_slug": game.slug,
                "game_title": review.game_title if review else game.title,
                "cover_url": (review.cover_url if review else None) or game.cover_url,
                "text": text,
                "text_source": review.text_source if review else "none",
                "rating": review.rating if review else game.rating,
                "date": review.date if review else None,
                "status": (review.status if review else None) or game.status,
                "progress": review.progress if review else None,
                "likes_count": review.likes_count if review else None,
                "comments_count": review.comments_count if review else None,
                "game_url": (review.game_url if review else None) or game.source_url,
                "evaluation_kind": "written_review",
            }
        )

    if review is not None:
        return review.model_copy(
            update={
                "text": "",
                "text_source": "none",
                "evaluation_kind": "rating_only",
                "raw": {
                    **review.raw,
                    "evaluation_kind": "rating_only",
                    "origin": "reviews.json",
                },
            }
        )

    return ReviewRecord(
        source="stash",
        source_url=game.source_url,
        raw={
            "evaluation_kind": "rating_only",
            "origin": "games.json",
            "game_status": game.status,
            "category": game.category,
            "date_displayed": game.date_displayed,
        },
        game_slug=game.slug,
        game_title=game.title,
        cover_url=game.cover_url,
        text="",
        text_source="none",
        rating=game.rating,
        date=None,
        status=game.status,
        progress=None,
        likes_count=None,
        comments_count=None,
        game_url=game.source_url,
        evaluation_kind="rating_only",
    )


def build_evaluation_set(
    games: list[GameRecord],
    reviews: list[ReviewRecord],
    *,
    source_run_id: str = "",
) -> EvaluationSet:
    """Cruza jogos e reviews e devolve exatamente as avaliacoes elegiveis."""
    result = EvaluationSet(source_run_id=source_run_id)
    issues: list[EvaluationIssue] = []

    game_counts = Counter(game.slug for game in games)
    review_counts = Counter(review.game_slug for review in reviews)
    for slug, count in sorted(game_counts.items()):
        if count > 1:
            issues.append(
                EvaluationIssue(
                    kind="duplicate_game_slug",
                    slug=slug,
                    message=f"slug de jogo duplicado ({count} registros)",
                )
            )
    for slug, count in sorted(review_counts.items()):
        if count > 1:
            issues.append(
                EvaluationIssue(
                    kind="duplicate_review_slug",
                    slug=slug,
                    message=f"mais de uma review para o mesmo jogo ({count} registros)",
                )
            )

    games_by_slug = {game.slug: game for game in games}
    reviews_by_slug = {review.game_slug: review for review in reviews}

    for slug in sorted(set(reviews_by_slug) - set(games_by_slug)):
        review = reviews_by_slug[slug]
        issues.append(
            EvaluationIssue(
                kind="review_without_game",
                slug=slug,
                message="review sem jogo correspondente em games.json",
                review_rating=review.rating,
            )
        )

    for game in sorted(games, key=lambda item: item.slug):
        if game_counts[game.slug] > 1 or review_counts.get(game.slug, 0) > 1:
            continue
        review = reviews_by_slug.get(game.slug)
        game_rating = game.rating if valid_rating(game.rating) else None

        if not valid_rating(game_rating):
            if review is not None and (review.text or "").strip():
                issues.append(
                    EvaluationIssue(
                        kind="review_without_game_rating",
                        slug=game.slug,
                        message="review escrita sem nota valida no jogo",
                        game_rating=game.rating,
                        review_rating=review.rating,
                    )
                )
            result.excluded.append(
                {
                    "slug": game.slug,
                    "title": game.title,
                    "status": game.status,
                    "category": game.category,
                }
            )
            continue

        if review is not None:
            if not valid_rating(review.rating):
                issues.append(
                    EvaluationIssue(
                        kind="invalid_review_rating",
                        slug=game.slug,
                        message="review com nota invalida",
                        game_rating=game.rating,
                        review_rating=review.rating,
                    )
                )
                if (review.text or "").strip():
                    issues.append(
                        EvaluationIssue(
                            kind="text_without_valid_rating",
                            slug=game.slug,
                            message="texto escrito sem nota valida na review",
                            game_rating=game.rating,
                            review_rating=review.rating,
                        )
                    )
                continue
            if float(review.rating) != float(game.rating):
                issues.append(
                    EvaluationIssue(
                        kind="rating_divergence",
                        slug=game.slug,
                        message="nota da review diverge da nota do jogo",
                        game_rating=game.rating,
                        review_rating=review.rating,
                    )
                )
                continue

        evaluation = _written_or_rating_only(game, review)
        if evaluation is None:
            continue
        result.evaluations.append(evaluation)

    written = sum(item.evaluation_kind == "written_review" for item in result.evaluations)
    rating_only = sum(item.evaluation_kind == "rating_only" for item in result.evaluations)
    result.counts = EvaluationCounts(
        total=len(games),
        written_review=written,
        rating_only=rating_only,
        no_rating=len(result.excluded),
        inconsistencies=len(issues),
    )
    result.inconsistencies = issues

    categories = {game.slug: game.category for game in games}
    for evaluation in result.evaluations:
        category = categories.get(evaluation.game_slug)
        if category:
            result.special_editions.append(
                {
                    "slug": evaluation.game_slug,
                    "title": evaluation.game_title,
                    "category": category,
                    "evaluation_kind": evaluation.evaluation_kind,
                    "rating": evaluation.rating,
                    "text_length": len(evaluation.text),
                    "special": category in SPECIAL_CATEGORIES,
                }
            )

    title_groups: dict[str, list[GameRecord]] = defaultdict(list)
    for game in games:
        title_groups[game.title].append(game)
    evaluation_by_slug = {item.game_slug: item for item in result.evaluations}
    for title, members in sorted(title_groups.items()):
        if len(members) < 2:
            continue
        involved = [
            {
                "slug": member.slug,
                "year": member.year,
                "category": member.category,
                "rating": member.rating,
                "evaluation_kind": evaluation_by_slug[member.slug].evaluation_kind
                if member.slug in evaluation_by_slug
                else None,
            }
            for member in members
        ]
        if any(item["evaluation_kind"] for item in involved):
            result.duplicate_titles.append({"title": title, "records": involved})

    for evaluation in result.evaluations:
        if evaluation.evaluation_kind != "written_review":
            continue
        listing = str((evaluation.raw or {}).get("text_listing") or "").strip()
        detail = str((evaluation.raw or {}).get("text_detail") or "").strip()
        if listing != detail:
            result.text_divergences.append(
                {
                    "slug": evaluation.game_slug,
                    "listing_length": len(listing),
                    "detail_length": len(detail),
                    "used": "detail",
                }
            )

    return result
