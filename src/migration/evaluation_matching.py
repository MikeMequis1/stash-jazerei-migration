"""Matching somente leitura das avaliacoes, com estados de consulta explicitos.

Nunca aprova correspondencias: o melhor resultado continua sendo `candidate`
ou `manual_review`. Falha de consulta nao vira ausencia de jogo.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.cancellation import OperationCancelled
from src.migration.matcher import match_game
from src.models.destination import DestinationGame
from src.models.normalized import GameMatch, MatchDecision
from src.models.source import GameRecord, ReviewRecord
from src.storage import utc_now
from src.transport import HttpError

MATCH_STATUSES = (
    "candidate",
    "manual_review",
    "no_candidate",
    "inconclusive",
    "error",
)


class EvaluationMatchReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    generated_at: str = Field(default_factory=utc_now)
    counts: dict[str, int] = Field(default_factory=dict)
    by_kind: dict[str, dict[str, int]] = Field(default_factory=dict)
    items: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EvaluationMatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matches: list[GameMatch]
    report: EvaluationMatchReport


def _cache_key(title: str) -> str:
    return " ".join(title.casefold().split())


def load_catalog_cache(path: Path | None) -> dict[str, list[DestinationGame]]:
    if path is None or not path.is_file():
        return {}
    cache: dict[str, list[DestinationGame]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = __import__("json").loads(line)
            key = str(payload["key"])
            candidates = [
                DestinationGame.model_validate(item) for item in payload.get("candidates", [])
            ]
        except Exception:  # noqa: BLE001 - cache corrompido nao derruba o matching
            continue
        cache[key] = candidates
    return cache


def append_catalog_cache(
    path: Path | None, *, key: str, query: str, candidates: list[DestinationGame]
) -> None:
    if path is None:
        return
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "key": key,
        "query": query,
        "candidates": [item.model_dump(mode="json") for item in candidates],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _status_for(match: GameMatch, candidates: list[DestinationGame]) -> str:
    if not candidates:
        return "no_candidate"
    if match.decision is MatchDecision.CANDIDATE:
        return "candidate"
    return "manual_review"


def _error_status(exc: Exception) -> str:
    if isinstance(exc, HttpError):
        return "inconclusive"
    return "error"


def _existing_lookup_status(match: GameMatch) -> str:
    if match.lookup_status:
        return match.lookup_status
    if match.dest_game_id is None:
        return "no_candidate"
    if match.decision is MatchDecision.CANDIDATE:
        return "candidate"
    if match.decision is MatchDecision.APPROVED:
        return "candidate"
    return "manual_review"


def match_evaluations(
    evaluations: list[ReviewRecord],
    games: list[GameRecord],
    search: Callable[[str], list[DestinationGame]],
    *,
    existing_matches: list[GameMatch] | None = None,
    refresh_slugs: set[str] | None = None,
    cache_path: Path | None = None,
    limit: int = 8,
    max_attempts: int = 3,
    retry_delay: float = 2.0,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> EvaluationMatchResult:
    """Executa matching sem aprovar e sem confundir erro com ausencia."""
    existing_by_slug = {match.source_slug: match for match in existing_matches or []}
    refresh_slugs = refresh_slugs or set()
    games_by_slug = {game.slug: game for game in games}
    cache = load_catalog_cache(cache_path)
    matches: list[GameMatch] = []
    items: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    by_kind: dict[str, Counter[str]] = {}

    for index, evaluation in enumerate(evaluations, start=1):
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("matching de avaliacoes cancelado")
        slug = evaluation.game_slug
        kind = evaluation.evaluation_kind
        counts["total"] += 1
        by_kind.setdefault(kind, Counter())["total"] += 1
        historical = existing_by_slug.get(slug)

        if historical is not None and slug not in refresh_slugs:
            status = _existing_lookup_status(historical)
            matches.append(historical)
            counts["reused_historical"] += 1
            by_kind[kind]["reused_historical"] += 1
            counts[status] += 1
            by_kind[kind][status] += 1
            items.append(
                {
                    "slug": slug,
                    "evaluation_kind": kind,
                    "status": "reused_historical",
                    "lookup_status": status,
                    "candidate_count": len(historical.alternatives),
                    "confidence": historical.confidence,
                    "decision": historical.decision.value,
                }
            )
            if on_progress is not None:
                on_progress({"current": index, "total": len(evaluations), "slug": slug})
            continue

        game = games_by_slug.get(slug)
        if game is None:
            game = GameRecord(
                slug=slug,
                title=evaluation.game_title or slug,
                source_url=evaluation.source_url,
                year=None,
            )

        key = _cache_key(evaluation.game_title or game.title)
        candidates: list[DestinationGame] | None = cache.get(key)
        source = "cache" if candidates is not None else "query"
        error: str | None = None
        if candidates is None:
            for attempt in range(1, max_attempts + 1):
                try:
                    candidates = search(game.title)[:limit]
                    append_catalog_cache(
                        cache_path, key=key, query=game.title, candidates=candidates
                    )
                    cache[key] = candidates
                    break
                except Exception as exc:  # noqa: BLE001 - erro fica explicito
                    error = f"{type(exc).__name__}: {exc}"
                    if attempt < max_attempts and retry_delay > 0:
                        time.sleep(retry_delay * attempt)
            if candidates is None:
                status = _error_status(
                    HttpError(error or "")
                    if error and error.startswith("HttpError")
                    else Exception(error or "")
                )
                if historical is not None:
                    fallback = historical.model_copy(deep=True)
                    fallback.lookup_status = status
                    fallback.reasons = list(fallback.reasons) + [f"consulta inconclusiva: {error}"]
                    matches.append(fallback)
                else:
                    fallback = GameMatch(
                        source_slug=slug,
                        source_title=evaluation.game_title or game.title,
                        source_year=game.year,
                        decision=MatchDecision.MANUAL_REVIEW,
                        reasons=[f"consulta inconclusiva: {error}"],
                        lookup_status=status,
                    )
                    matches.append(fallback)
                counts[status] += 1
                by_kind[kind][status] += 1
                items.append(
                    {
                        "slug": slug,
                        "evaluation_kind": kind,
                        "status": "error",
                        "lookup_status": status,
                        "candidate_count": 0,
                        "confidence": 0.0,
                        "error": error,
                    }
                )
                if on_progress is not None:
                    on_progress({"current": index, "total": len(evaluations), "slug": slug})
                continue

        match = match_game(game, candidates)
        status = _status_for(match, candidates)
        match.lookup_status = status  # type: ignore[assignment]
        matches.append(match)
        counts[status] += 1
        by_kind[kind][status] += 1
        if source == "cache":
            counts["cache_hits"] += 1
            by_kind[kind]["cache_hits"] += 1
        else:
            counts["queries"] += 1
            by_kind[kind]["queries"] += 1
        items.append(
            {
                "slug": slug,
                "evaluation_kind": kind,
                "status": source,
                "lookup_status": status,
                "candidate_count": len(match.alternatives),
                "confidence": match.confidence,
                "dest_game_id": match.dest_game_id,
                "dest_name": match.dest_name,
            }
        )
        if on_progress is not None:
            on_progress({"current": index, "total": len(evaluations), "slug": slug})

    report = EvaluationMatchReport(
        counts=dict(counts),
        by_kind={kind: dict(value) for kind, value in by_kind.items()},
        items=items,
        warnings=[],
    )
    return EvaluationMatchResult(matches=matches, report=report)
