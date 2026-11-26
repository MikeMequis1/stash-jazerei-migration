"""Correspondencia entre jogos do Stash e candidatos do catalogo.

Nunca aprova automaticamente: o melhor resultado vira `candidate` ou
`manual_review`, e a aprovacao humana e sempre necessaria.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.migration.normalize import (
    edition_tokens,
    normalize_slug,
    normalize_title,
    title_tokens,
)
from src.models.normalized import GameMatch, MatchDecision
from src.models.source import GameRecord

DEFAULT_THRESHOLD = 0.80
DEFAULT_MARGIN = 0.15


def score_candidate(source: GameRecord, candidate: Any) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    source_slug = normalize_slug(source.slug)
    candidate_slug = normalize_slug(candidate.slug or "")
    if source_slug and source_slug == candidate_slug:
        score += 0.45
        reasons.append("slug identico")

    source_title = normalize_title(source.title)
    candidate_title = normalize_title(candidate.name or "")
    if source_title and source_title == candidate_title:
        score += 0.40
        reasons.append("titulo normalizado identico")
    elif source_title and candidate_title:
        source_tokens = title_tokens(source_title)
        candidate_tokens = title_tokens(candidate_title)
        overlap = source_tokens & candidate_tokens
        ratio = len(overlap) / max(len(source_tokens | candidate_tokens), 1)
        if ratio >= 0.75:
            score += 0.25
            reasons.append(f"titulos com forte sobreposicao ({ratio:.2f})")
        elif ratio >= 0.5:
            score += 0.12
            reasons.append(f"titulos parcialmente semelhantes ({ratio:.2f})")

    if source.year and candidate.release_year:
        if source.year == candidate.release_year:
            score += 0.05
            reasons.append("ano identico")
        elif abs(source.year - candidate.release_year) <= 1:
            score += 0.02
            reasons.append("ano proximo")

    source_developer = str(source.raw.get("developer") or "").strip().lower()
    candidate_developer = str(candidate.developer or "").strip().lower()
    if source_developer and candidate_developer and source_developer == candidate_developer:
        score += 0.03
        reasons.append("desenvolvedora identica")

    source_platform = str(source.platform or "").strip().lower()
    candidate_platforms = {str(item).lower() for item in candidate.platforms}
    if source_platform and source_platform in candidate_platforms:
        score += 0.02
        reasons.append("plataforma em comum")

    source_editions = edition_tokens(source.title)
    candidate_editions = edition_tokens(candidate.name or "")
    if source_editions != candidate_editions:
        score -= 0.15
        reasons.append("risco de versao/remake/port")
    elif source_editions:
        reasons.append("edicao coincide")

    return max(0.0, min(1.0, round(score, 4))), reasons


def match_game(
    source: GameRecord,
    candidates: list[Any],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    margin: float = DEFAULT_MARGIN,
) -> GameMatch:
    scored = sorted(
        ((score_candidate(source, candidate), candidate) for candidate in candidates),
        key=lambda entry: entry[0][0],
        reverse=True,
    )
    alternatives = [
        {
            "game_id": candidate.id,
            "slug": candidate.slug,
            "name": candidate.name,
            "release_year": candidate.release_year,
            "confidence": score,
        }
        for (score, _reasons), candidate in scored[:5]
    ]

    if not scored:
        return GameMatch(
            source_slug=source.slug,
            source_title=source.title,
            source_year=source.year,
            alternatives=[],
            decision=MatchDecision.MANUAL_REVIEW,
            reasons=["nenhum candidato retornado pelo catalogo"],
        )

    (best_score, best_reasons), best = scored[0]
    runner_up = scored[1][0][0] if len(scored) > 1 else 0.0
    ambiguous = best_score - runner_up < margin
    reasonably_confident = best_score >= threshold and not ambiguous

    reasons = list(best_reasons)
    if ambiguous:
        reasons.append(f"ambiguidade: margem {best_score - runner_up:.2f} < {margin:.2f}")
    if best_score < threshold:
        reasons.append(f"confianca {best_score:.2f} < limiar {threshold:.2f}")

    return GameMatch(
        source_slug=source.slug,
        source_title=source.title,
        source_year=source.year,
        dest_game_id=best.id,
        dest_slug=best.slug,
        dest_name=best.name,
        dest_year=best.release_year,
        confidence=best_score,
        reasons=reasons,
        decision=MatchDecision.CANDIDATE if reasonably_confident else MatchDecision.MANUAL_REVIEW,
        alternatives=alternatives,
    )


def match_all(
    records: list[GameRecord],
    search: Callable[[str], list[Any]],
    *,
    limit: int = 8,
) -> list[GameMatch]:
    matches: list[GameMatch] = []
    for record in records:
        candidates = search(record.title)[:limit]
        matches.append(match_game(record, candidates))
    return matches
