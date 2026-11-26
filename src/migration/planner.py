"""Montagem do plano de migracao.

O plano e uma proposta: nenhuma acao nasce aprovada. Aprovacao e explicita,
por acao ou por grupo, e conflitos exigem aprovacao separada.
"""

from __future__ import annotations

from src.migration.liked import resolve_liked, review_version_hash
from src.migration.normalize import constrain_rating
from src.models.destination import DestinationReview, WatchlistEntry
from src.models.normalized import (
    GameMatch,
    LikedDecision,
    LikedStatus,
    MigrationAction,
    MigrationPlan,
)
from src.models.source import GameRecord, ReviewRecord

MAX_FAVORITES = 5
MAX_PLAYING_NOW = 10


def build_plan(
    matches: list[GameMatch],
    reviews: list[ReviewRecord],
    *,
    games: list[GameRecord] | None = None,
    destination_reviews: list[DestinationReview] | None = None,
    destination_watchlist: list[WatchlistEntry] | None = None,
    liked_decisions: list[LikedDecision] | None = None,
    watchlist_slugs: list[str] | None = None,
    favorite_slugs: list[str] | None = None,
    playing_now_slugs: list[str] | None = None,
) -> MigrationPlan:
    plan = MigrationPlan()
    approved_matches = [match for match in matches if match.is_approved]
    by_source_slug = {match.source_slug: match for match in approved_matches}
    game_by_slug = {game.slug: game for game in games or []}
    review_map, duplicate_reviews = _review_index(reviews)
    for source_slug in duplicate_reviews:
        plan.skipped.append(
            {"kind": "review", "source": source_slug, "reason": "mais de uma review no Stash"}
        )
    dest_reviews = destination_reviews or []
    dest_review_by_game = {review.game_id: review for review in dest_reviews}
    dest_review_by_slug = {review.game_slug: review for review in dest_reviews if review.game_slug}
    dest_watchlist_by_game = {entry.game_id: entry for entry in (destination_watchlist or [])}

    seen_review_games: set[int] = set()
    for source_slug, record in review_map.items():
        match = by_source_slug.get(source_slug)
        if match is None or match.dest_game_id is None:
            plan.unmatched.append(source_slug)
            continue
        if match.dest_game_id in seen_review_games:
            plan.skipped.append(
                {"kind": "review", "source": source_slug, "reason": "review duplicada para o jogo"}
            )
            continue
        seen_review_games.add(match.dest_game_id)

        before = dest_review_by_game.get(match.dest_game_id) or dest_review_by_slug.get(
            match.dest_slug or ""
        )
        if before is not None:
            plan.skipped.append(
                {
                    "kind": "review",
                    "source": source_slug,
                    "reason": "review existente no destino; preservada sem atualizacao",
                    "destination_review_id": before.id,
                    "before": before.model_dump(mode="json"),
                }
            )
            continue

        source_hash = review_version_hash(record)
        liked_state = resolve_liked(
            liked_decisions or [],
            source_ref=source_slug,
            game_id=match.dest_game_id,
            source_hash=source_hash,
        )
        plan.actions.append(
            MigrationAction(
                kind="review",
                source_ref=source_slug,
                source_payload=record.model_dump(mode="json"),
                game_id=match.dest_game_id,
                destination_slug=match.dest_slug,
                before=None,
                after={
                    "game_id": match.dest_game_id,
                    "rating": constrain_rating(record.rating),
                    "text": record.text,
                    "liked": liked_state.value
                    if liked_state.status is LikedStatus.DECIDED
                    else None,
                },
                liked=liked_state,
            )
        )

    for source_slug in watchlist_slugs or []:
        match = by_source_slug.get(source_slug)
        if match is None or match.dest_game_id is None:
            plan.unmatched.append(source_slug)
            continue
        game = game_by_slug.get(source_slug)
        before = dest_watchlist_by_game.get(match.dest_game_id)
        plan.actions.append(
            MigrationAction(
                kind="watchlist",
                source_ref=source_slug,
                source_payload=game.model_dump(mode="json") if game else {},
                game_id=match.dest_game_id,
                destination_slug=match.dest_slug,
                before=before.model_dump(mode="json") if before else None,
                after={
                    "user_id": None,
                    "game_id": str(match.dest_game_id),
                    "game_slug": match.dest_slug,
                    "game_title": match.dest_name,
                    "cover_url": game.cover_url if game else None,
                    "release_year": match.dest_year,
                    "primary_platform": game.platform if game else None,
                },
            )
        )

    plan.actions.extend(
        _profile_actions(
            kind="favorite",
            slugs=favorite_slugs or [],
            limit=MAX_FAVORITES,
            by_source_slug=by_source_slug,
            game_by_slug=game_by_slug,
            plan=plan,
        )
    )
    plan.actions.extend(
        _profile_actions(
            kind="playing_now",
            slugs=playing_now_slugs or [],
            limit=MAX_PLAYING_NOW,
            by_source_slug=by_source_slug,
            game_by_slug=game_by_slug,
            plan=plan,
        )
    )

    return plan


def _review_index(reviews: list[ReviewRecord]) -> tuple[dict[str, ReviewRecord], list[str]]:
    """Primeira review de cada jogo; jogos com duplicatas sao reportados."""
    index: dict[str, ReviewRecord] = {}
    duplicates: list[str] = []
    for review in reviews:
        if review.game_slug in index:
            duplicates.append(review.game_slug)
            continue
        index[review.game_slug] = review
    return index, sorted(set(duplicates))


def _profile_actions(
    *,
    kind: str,
    slugs: list[str],
    limit: int,
    by_source_slug: dict[str, GameMatch],
    game_by_slug: dict[str, GameRecord],
    plan: MigrationPlan,
) -> list[MigrationAction]:
    actions: list[MigrationAction] = []
    seen: set[int] = set()
    for source_slug in slugs:
        match = by_source_slug.get(source_slug)
        if match is None or match.dest_game_id is None:
            plan.unmatched.append(source_slug)
            continue
        if len(actions) >= limit:
            plan.skipped.append(
                {
                    "kind": kind,
                    "source": source_slug,
                    "reason": f"excede o limite visual de {limit}; selecao manual necessaria",
                }
            )
            continue
        if match.dest_game_id in seen:
            plan.skipped.append(
                {"kind": kind, "source": source_slug, "reason": "duplicado na selecao"}
            )
            continue
        seen.add(match.dest_game_id)

        game = game_by_slug.get(source_slug)
        payload = {
            "id": match.dest_game_id,
            "slug": match.dest_slug,
            "title": match.dest_name,
            "coverUrl": game.cover_url if game else None,
        }
        if kind == "playing_now":
            payload["platform"] = game.platform if game else None

        actions.append(
            MigrationAction(
                kind=kind,  # type: ignore[arg-type]
                source_ref=source_slug,
                source_payload=game.model_dump(mode="json") if game else {},
                game_id=match.dest_game_id,
                destination_slug=match.dest_slug,
                after=payload,
            )
        )
    return actions


def approve_plan(
    plan: MigrationPlan,
    *,
    kinds: set[str] | None = None,
    include_conflicts: bool = False,
) -> MigrationPlan:
    """Aprova explicitamente acoes; conflitos exigem include_conflicts=True."""
    for action in plan.actions:
        if kinds is not None and action.kind not in kinds:
            continue
        if action.before is not None and not include_conflicts:
            continue
        action.approved = True
    plan.approved = all(action.approved for action in plan.actions)
    return plan
