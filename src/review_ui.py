"""Interface local de revisão de correspondências.

Servidor HTTP mínimo (stdlib) + HTML/CSS/JS estático. Não cria transporte de
escrita nem aprovação paralela: atualiza `matches.json` (fonte usada pelo
planner) e registra histórico append-only; `liked` usa o mesmo arquivo e as
mesmas funções da CLI.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from src.cancellation import OperationCancelled
from src.config import Config
from src.jazerei.catalog import search_games
from src.jazerei.client import JazereiClient
from src.migration.eligibility import evaluate_eligibility
from src.migration.evaluation_store import (
    latest_evaluation_run,
    load_evaluation_run,
    refresh_manifest,
    save_match,
)
from src.migration.evaluations import build_evaluation_set
from src.migration.liked import (
    append_decision,
    append_deferral,
    load_decisions,
    resolve_liked,
    review_version_hash,
)
from src.migration.match_decisions import (
    MatchDecisionEvent,
    append_match_event,
    latest_by_slug,
    load_match_events,
)
from src.migration.match_decisions import (
    history_conflict as match_history_conflict,
)
from src.migration.matcher import match_game
from src.migration.orchestrator import PrepareOrchestrator
from src.migration.ratification import (
    eligible_liked,
    eligible_matches,
    liked_is_ratified,
    liked_item,
    load_ratifications,
    match_is_ratified,
    match_item,
    match_origin,
    validation_summary,
)
from src.migration.setup import (
    SetupError,
    WizardState,
    account_snapshot_path,
    apply_state_to_service,
    collect_stash,
    identity_snapshot,
    list_destination_runs,
    list_stash_runs,
    login_jazerei,
    open_stash,
    run_summary,
    snapshot_destination,
    verify_destination_run,
    verify_jazerei,
    verify_source_run,
)
from src.migration.verifications import (
    list_verifications,
    load_verification,
    verify_verification,
)
from src.migration.wizard import MigrationError, MigrationService
from src.models.normalized import GameMatch, LikedDecision, MatchDecision
from src.models.snapshot import AccountReviewsSnapshot
from src.models.source import GameRecord, ReviewRecord
from src.storage import read_json, sha256_file, utc_now, verify_manifest, write_json, write_manifest

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_RUN = ROOT / "data/stash/runs/20261126T183458Z-stash-browser"
DEFAULT_DEST_RUN = ROOT / "data/jazerei/runs/20261126T183706Z-account-snapshot"
DEFAULT_MATCHES = ROOT / "data/migration/matches.json"
DEFAULT_LIKED = ROOT / "data/migration/liked-decisions.jsonl"
DEFAULT_HISTORY = ROOT / "data/migration/stage9/match-decisions.jsonl"
DEFAULT_PLANS = ROOT / "data/migration/plans"
WEB_DIR = ROOT / "web"
REVIEW_EDITABLE_FIELDS = ("rating", "text", "status", "progress")


class ReviewUIError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewPaths:
    source_run: Path = DEFAULT_SOURCE_RUN
    destination_run: Path = DEFAULT_DEST_RUN
    matches: Path = DEFAULT_MATCHES
    liked: Path = DEFAULT_LIKED
    history: Path = DEFAULT_HISTORY
    plans: Path = DEFAULT_PLANS
    categories: Path = ROOT / "data/migration/stage9/source-categories.json"
    overrides: Path = ROOT / "data/migration/stage9/review-overrides.jsonl"
    account_snapshot: Path | None = None
    ratifications: Path = ROOT / "data/migration/stage9/ratifications.jsonl"
    evaluation_run: Path | None = None


def _manifest_hash(run: Path) -> str:
    path = run / "manifest.json"
    if not path.is_file():
        raise ReviewUIError(f"manifesto ausente: {path}")
    return sha256_file(path)


def liked_value_for_rating(rating: float | None) -> bool | None:
    """Política solicitada: >=7 recomenda; <=5 não recomenda; resto unresolved."""
    if rating is None:
        return None
    if rating >= 7:
        return True
    if rating <= 5:
        return False
    return None


class ReviewService:
    def __init__(self, config: Config, paths: ReviewPaths | None = None) -> None:
        self.config = config
        self.paths = paths or ReviewPaths()
        self._evaluation_set: Any = None

    # -- carga -----------------------------------------------------------
    def set_inputs(
        self,
        *,
        source_run: Path | None = None,
        destination_run: Path | None = None,
        account_snapshot: Path | None = None,
    ) -> None:
        self.paths = replace(
            self.paths,
            source_run=source_run or self.paths.source_run,
            destination_run=destination_run or self.paths.destination_run,
            account_snapshot=account_snapshot,
        )

    def _load(self) -> tuple[list[ReviewRecord], list[GameRecord], list[GameMatch], list[Any]]:
        errors = verify_manifest(self.paths.source_run)
        if errors:
            raise ReviewUIError(
                f"manifesto inválido em {self.paths.source_run.name}: {'; '.join(errors)}"
            )
        if self.paths.matches.is_file():
            historical = [GameMatch.model_validate(item) for item in read_json(self.paths.matches)]
        else:
            historical = []
        raw_reviews = [
            ReviewRecord.model_validate(item)
            for item in read_json(self.paths.source_run / "reviews.json")
        ]
        games = [
            GameRecord.model_validate(item)
            for item in read_json(self.paths.source_run / "games.json")
        ]
        evaluation_set = build_evaluation_set(
            games, raw_reviews, source_run_id=self.paths.source_run.name
        )
        self._evaluation_set = evaluation_set
        reviews = evaluation_set.evaluations
        evaluation_run = self._evaluation_run()
        evaluation_matches: list[GameMatch] = []
        if evaluation_run is not None:
            manifest = read_json(evaluation_run.run_dir / "manifest.json")
            source_sha = sha256_file(self.paths.source_run / "manifest.json")
            if (
                manifest.get("source_run_id") != self.paths.source_run.name
                or manifest.get("source_manifest_sha256") != source_sha
            ):
                raise ReviewUIError(
                    "run de avaliacoes pertence a outra coleta; reexecute evaluations match"
                )
            _, evaluation_matches, _ = load_evaluation_run(evaluation_run)
        evaluation_by_slug = {match.source_slug: match for match in evaluation_matches}
        matches = [
            match for match in historical if match.source_slug not in evaluation_by_slug
        ] + evaluation_matches
        known = {match.source_slug for match in matches}
        games_by_slug = {game.slug: game for game in games}
        for review in reviews:
            if review.game_slug in known:
                continue
            game = games_by_slug.get(review.game_slug)
            matches.append(
                GameMatch(
                    source_slug=review.game_slug,
                    source_title=review.game_title or review.game_slug,
                    source_year=game.year if game else None,
                    decision=MatchDecision.MANUAL_REVIEW,
                    reasons=["matching nao executado para esta avaliacao"],
                    lookup_status="not_run",
                )
            )
        snapshot_path = self._account_snapshot()
        if snapshot_path is not None:
            errors = verify_manifest(snapshot_path.parent.parent)
            if errors:
                raise ReviewUIError(
                    "manifesto inválido do snapshot de destino: " + "; ".join(errors)
                )
            snapshot = AccountReviewsSnapshot.model_validate(read_json(snapshot_path))
            destination = [review.model_dump(mode="json") for review in snapshot.reviews]
        else:
            errors = verify_manifest(self.paths.destination_run)
            if errors:
                raise ReviewUIError(
                    f"manifesto inválido em {self.paths.destination_run.name}: " + "; ".join(errors)
                )
            destination = [item for item in read_json(self.paths.destination_run / "reviews.json")]
        return reviews, games, matches, destination

    def _evaluation_run(self) -> Any:
        if self.paths.evaluation_run is not None:
            from src.migration.evaluation_store import EvaluationRun

            path = self.paths.evaluation_run
            return EvaluationRun(path.name, path)
        return latest_evaluation_run(self.config)

    def _evaluation_info(self) -> dict[str, Any]:
        run = self._evaluation_run()
        if run is None:
            return {
                "evaluation_run_id": None,
                "evaluations_sha256": None,
                "operator_id": self.config.operator_id or "local",
            }
        return {
            "evaluation_run_id": run.run_id,
            "evaluations_sha256": sha256_file(run.evaluations_path),
            "evaluation_run_dir": str(run.run_dir),
            "operator_id": self.config.operator_id or "local",
        }

    def _evaluation_events(self) -> list[MatchDecisionEvent]:
        run = self._evaluation_run()
        if run is None:
            return []
        return load_match_events(run.run_dir / "match-decisions.jsonl")

    def _evaluation_history(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event in self._evaluation_events():
            payload = event.model_dump(mode="json")
            payload["source_slug"] = payload.get("source_ref")
            rows.append(payload)
        return rows

    def _history(self) -> list[dict[str, Any]]:
        if not self.paths.history.exists():
            return []
        events: list[dict[str, Any]] = []
        for index, line in enumerate(
            self.paths.history.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ReviewUIError(f"histórico inválido na linha {index}") from exc
        return events

    def _combined_history(self) -> list[dict[str, Any]]:
        events = self._history() + self._evaluation_history()
        return sorted(events, key=lambda event: str(event.get("at") or ""))

    def _edition_confirmation(
        self, source_ref: str, candidate_id: int | None
    ) -> tuple[bool, int | None, str]:
        if candidate_id is None:
            return False, None, ""
        for event in reversed(self._evaluation_events()):
            if (
                event.source_ref == source_ref
                and event.edition_confirmed
                and event.candidate_id == candidate_id
            ):
                return True, event.candidate_id, event.reason
        for event in reversed(self._combined_history()):
            if (
                event.get("source_slug") == source_ref
                and event.get("edition_confirmed") is True
                and event.get("candidate_id") == candidate_id
            ):
                return True, candidate_id, str(event.get("reason") or "")
        return False, None, ""

    def _source_details(self) -> dict[str, Any]:
        destination_run = self.paths.destination_run
        destination_manifest = destination_run / "manifest.json"
        account = self._account_snapshot()
        if account is not None:
            destination_run = account.parent.parent
            destination_manifest = destination_run / "manifest.json"
        return {
            "source_run_id": self.paths.source_run.name,
            "source_manifest_sha256": _manifest_hash(self.paths.source_run),
            "destination_run_id": destination_run.name,
            "destination_manifest_sha256": sha256_file(destination_manifest),
        }

    def _history_details(self) -> dict[str, Any]:
        return self._source_details()

    def _categories(self) -> dict[str, str]:
        if not self.paths.categories.exists():
            return {}
        payload = read_json(self.paths.categories)
        values = payload.get("categories", {}) if isinstance(payload, dict) else {}
        return {
            str(slug): str(value)
            for slug, value in values.items()
            if value is not None and str(value).strip()
        }

    def _account_snapshot(self) -> Path | None:
        runs = self.config.jazerei_dir / "runs"
        candidates: list[Path] = []
        if runs.is_dir():
            for run in runs.iterdir():
                manifest = run / "manifest.json"
                if not manifest.is_file():
                    continue
                try:
                    payload = read_json(manifest)
                except Exception:  # noqa: BLE001
                    continue
                if (
                    not isinstance(payload, dict)
                    or payload.get("kind") != "jazerei-account-review-state"
                ):
                    continue
                relative = payload.get("snapshot_file")
                if relative and (run / str(relative)).is_file():
                    candidates.append(run / str(relative))
        latest = max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
        explicit = self.paths.account_snapshot
        if explicit is not None and explicit.is_file():
            if latest is not None and latest.stat().st_mtime > explicit.stat().st_mtime:
                # Presenca atual manda: snapshots antigos nao escondem reviews ja gravadas.
                return latest
            return explicit
        return latest

    def _overrides(self) -> dict[str, dict[str, Any]]:
        if not self.paths.overrides.exists():
            return {}
        latest: dict[str, dict[str, Any]] = {}
        for index, line in enumerate(
            self.paths.overrides.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReviewUIError(f"override inválido na linha {index}") from exc
            slug = event.get("source_ref")
            fields = event.get("fields")
            if slug and isinstance(fields, dict):
                current = latest.setdefault(str(slug), {})
                current.update(
                    {key: value for key, value in fields.items() if key in REVIEW_EDITABLE_FIELDS}
                )
        return latest

    @staticmethod
    def _effective_review(review: ReviewRecord, fields: dict[str, Any]) -> ReviewRecord:
        allowed = {key: value for key, value in fields.items() if key in REVIEW_EDITABLE_FIELDS}
        return review.model_copy(update=allowed) if allowed else review

    # -- estado ----------------------------------------------------------
    def state(self) -> dict[str, Any]:
        reviews, games, matches, destination = self._load()
        games_by_slug = {game.slug: game for game in games}
        matches_by_slug = {match.source_slug: match for match in matches}
        decisions = load_decisions(self.paths.liked)
        events = self._combined_history()
        evaluation_events = self._evaluation_events()
        latest_evaluation_events = latest_by_slug(evaluation_events)
        evaluation_info = self._evaluation_info()
        duplicate_slugs = {
            member["slug"]
            for group in (self._evaluation_set.duplicate_titles if self._evaluation_set else [])
            for member in group.get("records", [])
            if member.get("evaluation_kind")
        }
        details = self._source_details()
        history_details = self._history_details()
        categories = self._categories()
        overrides = self._overrides()
        ratifications = load_ratifications(self.paths.ratifications)
        history_by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            history_by_slug[str(event.get("source_slug"))].append(event)
        destination_ids = {int(item["game_id"]) for item in destination if "game_id" in item}
        refreshed_slugs = self._evaluation_match_slugs()

        items = []
        for index, review in enumerate(sorted(reviews, key=lambda item: item.game_slug), start=1):
            match = matches_by_slug.get(review.game_slug)
            effective = self._effective_review(review, overrides.get(review.game_slug, {}))
            source_hash = review_version_hash(review)
            history = history_by_slug.get(review.game_slug, [])
            initial = self._initial_classification(match, history, destination_ids)
            rejected = {
                int(event["candidate_id"])
                for event in history
                if event.get("action") == "reject" and event.get("candidate_id") is not None
            }
            approved_candidate = (
                match.dest_game_id if match and match.decision.value == "approved" else None
            )
            if approved_candidate in rejected:
                rejected.discard(approved_candidate)
            status = self._review_status(match, history, initial, rejected)
            if review.game_slug in refreshed_slugs and match is not None:
                if match.lookup_status == "candidate" and initial in {
                    "no_candidate",
                    "ambiguous",
                }:
                    initial = "strong_candidate"
                elif match.lookup_status == "manual_review" and initial == "no_candidate":
                    initial = "ambiguous"
                if status == "no_match" and match.lookup_status in {
                    "candidate",
                    "manual_review",
                }:
                    status = "pending"
            latest = history[-1] if history else None
            stale = bool(
                latest
                and (
                    (latest.get("source_hash") and latest.get("source_hash") != source_hash)
                    or (
                        latest.get("source_manifest_sha256")
                        and latest.get("source_manifest_sha256")
                        != history_details["source_manifest_sha256"]
                    )
                    # Trocar de run de destino não invalida sozinho: preflight/plano
                    # detectam mudanças. Só o mesmo run com manifesto alterado é stale.
                    or (
                        latest.get("destination_manifest_sha256")
                        and latest.get("destination_run_id")
                        == history_details["destination_run_id"]
                        and latest.get("destination_manifest_sha256")
                        != history_details["destination_manifest_sha256"]
                    )
                )
                and latest.get("action") != "pending"
            )
            game = games_by_slug.get(review.game_slug)
            category = categories.get(review.game_slug) or (game.category if game else None)
            candidates = self._candidates(match)
            liked = self._liked_state(
                review, match, decisions, destination_ids, ratifications, details
            )
            match_origin_value = match_origin(latest)
            match_validation = "unmatched"
            if match_origin_value == "human_individual":
                match_validation = "human_individual"
            elif match_origin_value == "automatic_unreviewed":
                ratified = match_is_ratified(
                    match=match,
                    latest_event=latest,
                    ratifications=ratifications,
                    source_run_id=details["source_run_id"],
                    source_manifest_sha256=details["source_manifest_sha256"],
                    destination_run_id=details["destination_run_id"],
                    destination_manifest_sha256=details["destination_manifest_sha256"],
                )
                match_validation = "human_batch_review" if ratified else "automatic_unreviewed"
            suggestion = None
            if category:
                suggestion = "no_match" if not candidates else "manual_review"
            needs_review = bool(
                initial in {"ambiguous", "no_candidate", "inconclusive", "not_matched"}
                or status == "pending"
                or (initial != "already_exists" and liked["status"] != "decided")
                or stale
            )
            identity_conflict = bool(
                review.game_slug == "spirit-of-the-north-enhanced-edition"
                and match is not None
                and match.confidence < 0.5
            )
            conflict = bool(
                stale
                or identity_conflict
                or (initial == "ambiguous" and status == "pending")
                or (bool(category) and status == "pending")
            )
            match_status = (
                match.lookup_status
                if match and match.lookup_status
                else (
                    "no_candidate" if match is None or match.dest_game_id is None else "candidate"
                )
            )
            evaluation_event = latest_evaluation_events.get(review.game_slug)
            (
                edition_confirmed,
                edition_confirmed_id,
                edition_reason,
            ) = self._edition_confirmation(
                review.game_slug,
                match.dest_game_id if match else None,
            )
            source_hash_current = (
                evaluation_event is None or evaluation_event.source_hash == source_hash
            )
            evaluation_current = bool(
                evaluation_event is None
                or (
                    evaluation_info.get("evaluation_run_id")
                    and evaluation_event.evaluation_run_id == evaluation_info["evaluation_run_id"]
                )
            )
            history_conflict_reason = match_history_conflict(
                evaluation_event if evaluation_event is not None else latest,
                decision=match.decision.value if match else "manual_review",
                candidate_id=match.dest_game_id if match else None,
            )
            if not source_hash_current or not evaluation_current:
                stale = True
            duplicate_title = review.game_slug in duplicate_slugs
            eligibility = evaluate_eligibility(
                match=match,
                liked=liked,
                special_edition=bool(category),
                duplicate_title=duplicate_title,
                edition_confirmed=edition_confirmed,
                destination_present=initial == "already_exists",
                history_conflict=bool(history_conflict_reason),
                evaluation_current=evaluation_current,
                source_hash_current=source_hash_current,
            )
            if history_conflict_reason or not source_hash_current or not evaluation_current:
                conflict = True
            if initial != "already_exists" and not eligibility.eligible:
                needs_review = True
            destination_evidence = None
            if initial == "already_exists":
                destination_evidence = (
                    "execution_verified"
                    if match is not None and match.decision is MatchDecision.APPROVED
                    else "content_reconciled"
                )
            items.append(
                {
                    "stable_id": f"S9-{index:03d}",
                    "index": index,
                    "slug": review.game_slug,
                    "title": review.game_title,
                    "year": game.year if game else None,
                    "platform": game.platform if game else None,
                    "category": category,
                    "special_category": bool(category),
                    "special_edition": bool(category),
                    "evaluation_kind": review.evaluation_kind,
                    "has_written_text": review.evaluation_kind == "written_review",
                    "text_length": len(review.text),
                    "source_origin": (review.raw or {}).get("origin")
                    or (
                        "reviews.json"
                        if review.evaluation_kind == "written_review"
                        else "games.json"
                    ),
                    "match_status": match_status,
                    "identity_conflict": identity_conflict,
                    "edition_confirmed": edition_confirmed,
                    "edition_confirmed_id": edition_confirmed_id,
                    "edition_reason": edition_reason,
                    "history_conflict": bool(history_conflict_reason),
                    "history_conflict_reason": history_conflict_reason,
                    "duplicate_title": duplicate_title,
                    "eligibility": eligibility.eligible,
                    "eligibility_reasons": [
                        reason.model_dump(mode="json") for reason in eligibility.reasons
                    ],
                    "batch_eligible": eligibility.batch_eligible,
                    "batch_reasons": [
                        reason.model_dump(mode="json") for reason in eligibility.batch_reasons
                    ],
                    "evaluation_run_id": evaluation_info.get("evaluation_run_id"),
                    "evaluations_sha256": evaluation_info.get("evaluations_sha256"),
                    "destination_present": initial == "already_exists",
                    "destination_evidence": destination_evidence,
                    "suggestion": suggestion,
                    "needs_review": needs_review,
                    "conflict": conflict,
                    "original_rating": review.rating,
                    "original_text": review.text,
                    "original_status": review.status,
                    "original_progress": review.progress,
                    "rating": effective.rating,
                    "date": effective.date,
                    "status": effective.status,
                    "progress": effective.progress,
                    "text": effective.text,
                    "edited": bool(overrides.get(review.game_slug)),
                    "text_source": effective.text_source,
                    "text_listing": (review.raw or {}).get("text_listing"),
                    "text_detail": (review.raw or {}).get("text_detail"),
                    "source_hash": source_hash,
                    "source_url": review.source_url,
                    "initial_classification": initial,
                    "review_status": status,
                    "stale": stale,
                    "candidate_count": len(candidates),
                    "candidates": candidates,
                    "selected_candidate_id": match.dest_game_id if match else None,
                    "rejected_candidate_ids": sorted(rejected),
                    "match_decision": match.decision.value if match else "manual_review",
                    "match_origin": match_origin_value,
                    "match_validation": match_validation,
                    "liked": liked,
                    "liked_history": [
                        decision.model_dump(mode="json")
                        for decision in decisions
                        if decision.source_ref == review.game_slug
                    ][-5:],
                    "history": history[-5:],
                }
            )

        summary = self._summary(items)
        summary["no_rating_excluded"] = max(0, len(games) - len(reviews))
        return {
            **details,
            **evaluation_info,
            "generated_at": utc_now(),
            "summary": summary,
            "items": items,
            "destination_reviews": len(destination),
        }

    @staticmethod
    def _candidates(match: GameMatch | None) -> list[dict[str, Any]]:
        if match is None:
            return []
        candidates: list[dict[str, Any]] = []
        seen: set[int] = set()
        for candidate in match.alternatives or []:
            game_id = candidate.get("game_id")
            if game_id is None:
                continue
            seen.add(int(game_id))
            candidates.append(
                {
                    "game_id": int(game_id),
                    "slug": candidate.get("slug"),
                    "name": candidate.get("name"),
                    "year": candidate.get("release_year"),
                    "confidence": candidate.get("confidence"),
                    "platforms": [],
                    "developer": None,
                    "is_selected": int(game_id) == match.dest_game_id,
                    "reasons": list(match.reasons) if int(game_id) == match.dest_game_id else [],
                }
            )
        if match.dest_game_id is not None and match.dest_game_id not in seen:
            candidates.append(
                {
                    "game_id": match.dest_game_id,
                    "slug": match.dest_slug,
                    "name": match.dest_name,
                    "year": match.dest_year,
                    "confidence": match.confidence,
                    "platforms": [],
                    "developer": None,
                    "is_selected": True,
                    "reasons": list(match.reasons),
                }
            )
        return candidates

    @staticmethod
    def _initial_classification(
        match: GameMatch | None, history: list[dict[str, Any]], destination_ids: set[int]
    ) -> str:
        # A presenca atual no destino prevalece sobre a classificacao historica:
        # 17 reviews foram gravadas depois do snapshot usado pelo historico.
        if (
            match is not None
            and match.dest_game_id is not None
            and match.dest_game_id in destination_ids
            and (
                match.decision is MatchDecision.APPROVED
                or (match.dest_slug is not None and match.dest_slug == match.source_slug)
            )
        ):
            return "already_exists"
        for event in history:
            value = event.get("classification_before")
            if value in {"strong_candidate", "ambiguous", "no_candidate", "already_exists"}:
                return str(value)
        if match is None:
            return "no_candidate"
        if match.lookup_status == "not_run":
            return "not_matched"
        if match.lookup_status in {"inconclusive", "error"}:
            return "inconclusive"
        if match.lookup_status == "no_candidate":
            return "no_candidate"
        if match.dest_game_id is None:
            return "no_candidate"
        if match.decision.value == "manual_review":
            return "ambiguous"
        return "strong_candidate"

    @staticmethod
    def _review_status(
        match: GameMatch | None,
        history: list[dict[str, Any]],
        initial: str,
        rejected: set[int],
    ) -> str:
        if initial == "already_exists":
            return "already_exists"
        if match is not None and match.decision.value == "approved":
            latest = history[-1] if history else None
            return "manual" if latest and latest.get("action") == "manual" else "approved"
        latest = history[-1] if history else None
        if latest and latest.get("action") == "no_match":
            return "no_match"
        if latest and latest.get("action") == "reject":
            candidate_ids = {
                int(candidate["game_id"]) for candidate in ReviewService._candidates(match)
            }
            if candidate_ids and candidate_ids <= rejected:
                return "rejected"
        return "pending"

    def _liked_state(
        self,
        review: ReviewRecord,
        match: GameMatch | None,
        decisions: list[LikedDecision],
        destination_ids: set[int],
        ratifications: list[Any],
        details: dict[str, Any],
    ) -> dict[str, Any]:
        expected_game_id = (
            match.dest_game_id
            if match is not None and match.decision is MatchDecision.APPROVED
            else None
        )
        if (match is not None and match.dest_game_id is not None) or any(
            decision.source_ref == review.game_slug for decision in decisions
        ):
            state = resolve_liked(
                decisions,
                source_ref=review.game_slug,
                game_id=expected_game_id,
                source_hash=review_version_hash(review),
            )
            decision = next(
                (
                    item
                    for item in decisions
                    if state.decision_id is not None and item.decision_id == state.decision_id
                ),
                None,
            )
            validation = self._liked_validation(
                decision, state.status.value, ratifications, details
            )
            value = {
                "status": state.status.value,
                "value": state.value,
                "source": state.source,
                "validation": validation,
                "ratification_id": validation.get("ratification_id"),
                "decision_id": state.decision_id,
                "decided_at": state.decided_at,
                "game_id": expected_game_id or state.game_id,
                "event_type": state.event_type,
                "binding_status": state.binding_status,
                "requires_reconfirmation": state.requires_reconfirmation,
            }
            if state.status.value == "unresolved":
                other = [
                    decision
                    for decision in decisions
                    if decision.source_ref == review.game_slug
                    and decision.game_id is not None
                    and expected_game_id is not None
                    and decision.game_id != expected_game_id
                ]
                if other:
                    value.update(
                        {
                            "status": "stale",
                            "validation": {"status": "invalid", "ratification_id": None},
                            "ratification_id": None,
                            "decision_id": other[-1].decision_id,
                            "decided_at": other[-1].decided_at,
                            "game_id": other[-1].game_id,
                        }
                    )
            return value
        old = [decision for decision in decisions if decision.source_ref == review.game_slug]
        if old:
            return {
                "status": "stale",
                "value": None,
                "source": old[-1].source,
                "validation": {"status": "invalid", "ratification_id": None},
                "ratification_id": None,
                "decision_id": old[-1].decision_id,
                "decided_at": old[-1].decided_at,
                "game_id": old[-1].game_id,
                "event_type": old[-1].event_type,
                "binding_status": "stale",
                "requires_reconfirmation": False,
            }
        return {
            "status": "unresolved",
            "value": None,
            "source": None,
            "validation": {"status": "pending", "ratification_id": None},
            "ratification_id": None,
            "decision_id": None,
            "decided_at": None,
            "game_id": None,
            "event_type": None,
            "binding_status": "none",
            "requires_reconfirmation": False,
        }

    def decide_match_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ReviewUIError("lote vazio")
        if len(items) > 50:
            raise ReviewUIError("lote limitado a 50 itens")
        state = self.state()
        by_slug = {item["slug"]: item for item in state["items"]}
        results: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                results.append({"ok": False, "error": "item invalido"})
                continue
            slug = str(raw.get("slug") or "")
            item = by_slug.get(slug)
            candidate_id = raw.get("candidate_id")
            if item is None:
                results.append({"slug": slug, "ok": False, "error": "item inexistente"})
                continue
            if not item.get("batch_eligible"):
                results.append(
                    {
                        "slug": slug,
                        "ok": False,
                        "error": "item nao elegivel para lote",
                        "reasons": item.get("batch_reasons", []),
                    }
                )
                continue
            if candidate_id is not None and int(candidate_id) != item.get("selected_candidate_id"):
                results.append(
                    {"slug": slug, "ok": False, "error": "candidato diverge do candidato do item"}
                )
                continue
            try:
                outcome = self.decide_match(
                    {
                        "slug": slug,
                        "action": "approve",
                        "candidate_id": item.get("selected_candidate_id"),
                        "reason": str(raw.get("reason") or "aprovacao em lote explicita"),
                    },
                    return_state=False,
                )
                results.append({"slug": slug, "ok": True, **(outcome or {})})
            except Exception as exc:  # noqa: BLE001 - resultado por item
                results.append({"slug": slug, "ok": False, "error": str(exc)})
        return {
            "results": results,
            "approved": sum(1 for item in results if item.get("ok")),
            "failed": sum(1 for item in results if not item.get("ok")),
        }

    def decide_liked_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ReviewUIError("lote vazio")
        if len(items) > 50:
            raise ReviewUIError("lote limitado a 50 itens")
        results: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                results.append({"ok": False, "error": "item invalido"})
                continue
            slug = str(raw.get("slug") or "")
            value = raw.get("value")
            if value is not None and type(value) is not bool:
                results.append({"slug": slug, "ok": False, "error": "liked invalido"})
                continue
            try:
                outcome = self.decide_liked(
                    {"slug": slug, "value": value, "reason": str(raw.get("reason") or "")},
                    return_state=False,
                )
                results.append({"slug": slug, "ok": True, **(outcome or {})})
            except Exception as exc:  # noqa: BLE001 - resultado por item
                results.append({"slug": slug, "ok": False, "error": str(exc)})
        return {
            "results": results,
            "decided": sum(1 for item in results if item.get("ok")),
            "failed": sum(1 for item in results if not item.get("ok")),
        }

    def reconciliation_report(self) -> dict[str, Any]:
        state = self.state()
        conflicts = [
            {
                "slug": item["slug"],
                "evaluation_kind": item["evaluation_kind"],
                "reason": item["history_conflict_reason"],
                "match_decision": item["match_decision"],
                "match_status": item["match_status"],
            }
            for item in state["items"]
            if item["history_conflict"]
        ]
        return {
            "evaluation_run_id": state.get("evaluation_run_id"),
            "conflicts": conflicts,
            "count": len(conflicts),
        }

    def rebuild_evaluation_matches(self, *, confirm: str) -> dict[str, Any]:
        if confirm != "reconcile":
            raise ReviewUIError('confirmação deve ser exatamente "reconcile"')
        run = self._evaluation_run()
        if run is None:
            raise ReviewUIError("run de avaliacoes ausente")
        events = load_match_events(run.run_dir / "match-decisions.jsonl")
        if not events:
            raise ReviewUIError("historico de correspondencias ausente")
        matches = [GameMatch.model_validate(item) for item in read_json(run.matches_path)]
        by_slug = {match.source_slug: match for match in matches}
        latest = latest_by_slug(events)
        changes: list[dict[str, Any]] = []
        original = [match.model_dump(mode="json") for match in matches]
        for slug, event in latest.items():
            match = by_slug.get(slug)
            if match is None or event.action in {"confirm_edition", "pending"}:
                continue
            target_decision = MatchDecision(event.new_decision)
            old = (match.decision.value, match.dest_game_id)
            match.decision = target_decision
            match.dest_game_id = (
                event.candidate_id if target_decision is MatchDecision.APPROVED else None
            )
            match.dest_slug = event.candidate_slug if match.dest_game_id else None
            match.dest_name = event.candidate_name if match.dest_game_id else None
            if event.confidence is not None:
                match.confidence = event.confidence
            if old != (match.decision.value, match.dest_game_id):
                changes.append(
                    {
                        "slug": slug,
                        "from": old,
                        "to": (match.decision.value, match.dest_game_id),
                    }
                )
        if changes:
            backup = run.run_dir / f"matches.before-reconcile-{utc_now().replace(':', '')}.json"
            write_json(backup, original)
            write_json(run.matches_path, matches, overwrite=True)
            manifest = read_json(run.run_dir / "manifest.json")
            write_manifest(
                run.run_dir,
                {key: value for key, value in manifest.items() if key != "files"},
                overwrite=True,
            )
        return {"changed": len(changes), "changes": changes}

    @staticmethod
    def _liked_validation(
        decision: LikedDecision | None,
        status: str,
        ratifications: list[Any],
        details: dict[str, Any],
    ) -> dict[str, Any]:
        if status == "unresolved" or decision is None:
            return {"status": "pending", "ratification_id": None}
        if status == "stale":
            return {"status": "invalid", "ratification_id": None}
        if status == "requires_reconfirmation":
            return {"status": "automatic_unreviewed", "ratification_id": None}
        if decision.source == "human":
            return {"status": "human_individual", "ratification_id": None}
        event = liked_is_ratified(
            decision=decision,
            ratifications=ratifications,
            source_run_id=details["source_run_id"],
            source_manifest_sha256=details["source_manifest_sha256"],
            destination_run_id=details["destination_run_id"],
            destination_manifest_sha256=details["destination_manifest_sha256"],
        )
        if event is not None:
            return {"status": "human_batch_review", "ratification_id": event.ratification_id}
        return {"status": "automatic_unreviewed", "ratification_id": None}

    @staticmethod
    def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        statuses = Counter(item["review_status"] for item in items)
        liked = Counter(item["liked"]["status"] for item in items)
        match_validation = Counter(item["match_validation"] for item in items)
        liked_validation = Counter(item["liked"]["validation"]["status"] for item in items)
        evaluation_kinds = Counter(item["evaluation_kind"] for item in items)
        match_statuses = Counter(item["match_status"] for item in items)
        reviewed = sum(
            item["review_status"] in {"approved", "manual"}
            and item["liked"]["status"] == "decided"
            or item["review_status"] in {"no_match", "rejected", "already_exists"}
            for item in items
        )
        eligible = sum(item["eligibility"] for item in items)
        blocked_liked = sum(
            item["review_status"] in {"approved", "manual"} and item["liked"]["status"] != "decided"
            for item in items
        )
        blocked_match = sum(item["review_status"] == "pending" for item in items)
        return {
            "total": len(items),
            "reviewed": reviewed,
            "pending": len(items) - reviewed,
            "eligible": eligible,
            "blocked_liked": blocked_liked,
            "blocked_match": blocked_match,
            "approved": statuses["approved"],
            "manual": statuses["manual"],
            "rejected": statuses["rejected"],
            "no_match": statuses["no_match"],
            "already_exists": statuses["already_exists"],
            "special_category": sum(item["special_category"] for item in items),
            "suggested_no_match": sum(item["suggestion"] == "no_match" for item in items),
            "suggested_manual": sum(item["suggestion"] == "manual_review" for item in items),
            "edited_reviews": sum(item["edited"] for item in items),
            "needs_review": sum(
                item["initial_classification"]
                in {"ambiguous", "no_candidate", "inconclusive", "not_matched"}
                or item["review_status"] == "pending"
                or (
                    item["initial_classification"] != "already_exists"
                    and item["liked"]["status"] != "decided"
                )
                for item in items
            ),
            "conflicts": sum(item["conflict"] for item in items),
            "liked_decided": liked["decided"],
            "liked_pending": liked["unresolved"],
            "liked_stale": liked["stale"],
            "stale_matches": sum(item["stale"] for item in items),
            "match_validation": dict(match_validation),
            "liked_validation": dict(liked_validation),
            "written_review": evaluation_kinds["written_review"],
            "rating_only": evaluation_kinds["rating_only"],
            "no_rating_excluded": 0,
            "special_edition": sum(item["special_edition"] for item in items),
            "identity_conflicts": sum(item["identity_conflict"] for item in items),
            "destination_present": sum(item["destination_present"] for item in items),
            "match_status": dict(match_statuses),
            "match_not_run": match_statuses["not_run"],
            "match_inconclusive": match_statuses["inconclusive"] + match_statuses["error"],
            "rating_only_liked_unresolved": sum(
                item["evaluation_kind"] == "rating_only" and item["liked"]["status"] == "unresolved"
                for item in items
            ),
            "eligible_now": sum(item["eligibility"] for item in items),
            "batch_eligible": sum(item["batch_eligible"] for item in items),
            "edition_unconfirmed": sum(
                item["special_edition"] and not item["edition_confirmed"] for item in items
            ),
            "history_conflicts": sum(item["history_conflict"] for item in items),
            "liked_requires_reconfirmation": sum(
                item["liked"]["status"] == "requires_reconfirmation" for item in items
            ),
            "liked_pending_identity": sum(
                item["liked"]["status"] == "decided"
                and item["liked"].get("binding_status") == "pending_identity"
                for item in items
            ),
        }

    # -- política automática --------------------------------------------
    def apply_automatic_policy(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Aprova strong_candidate e preenche liked por faixa de nota.

        Idempotente. Não sobrescreve decisão humana nem item que já possui
        histórico (inclusive rejeição/pendência).
        """
        reviews, _, matches, destination = self._load()
        if any(review.evaluation_kind == "rating_only" for review in reviews):
            raise ReviewUIError(
                "politica automatica desabilitada: ha avaliacoes somente com nota; "
                "correspondencias e liked exigem decisao humana"
            )
        destination_ids = {int(item["game_id"]) for item in destination if "game_id" in item}
        events = self._history()
        history_by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            history_by_slug[str(event.get("source_slug"))].append(event)
        details = self._source_details()
        reviews_by_slug = {review.game_slug: review for review in reviews}
        categories = self._categories()
        overrides = self._overrides()
        effective_by_slug = {
            review.game_slug: self._effective_review(review, overrides.get(review.game_slug, {}))
            for review in reviews
        }

        approved: list[tuple[GameMatch, ReviewRecord, str]] = []
        reverted: list[tuple[GameMatch, ReviewRecord, str]] = []
        for match in matches:
            review = reviews_by_slug.get(match.source_slug)
            if review is None:
                continue
            history = history_by_slug.get(match.source_slug, [])
            initial = self._initial_classification(match, history, destination_ids)
            category = categories.get(match.source_slug)
            latest = history[-1] if history else None

            # Categoria especial nunca é autoaprovada; se foi aprovada pela
            # política automática anterior, volta para revisão manual.
            if category:
                if (
                    match.decision is MatchDecision.APPROVED
                    and latest
                    and latest.get("action") == "auto_approve"
                ):
                    match.decision = MatchDecision.MANUAL_REVIEW
                    reason = f"categoria especial ({category}): revisao manual"
                    if reason not in match.reasons:
                        match.reasons.append(reason)
                    reverted.append((match, review, initial))
                continue

            if history or initial != "strong_candidate" or match.dest_game_id is None:
                continue
            if match.decision is MatchDecision.APPROVED:
                continue
            previous = match.decision.value
            match.decision = MatchDecision.APPROVED
            reason = "aprovacao automatica: strong_candidate"
            if reason not in match.reasons:
                match.reasons.append(reason)
            approved.append((match, review, previous))

        if (approved or reverted) and not dry_run:
            before = sha256_file(self.paths.matches)
            self._write_matches(matches)
            after = sha256_file(self.paths.matches)
            for match, review, previous in approved:
                self._append_history(
                    {
                        **details,
                        "source_slug": match.source_slug,
                        "source_hash": review_version_hash(review),
                        "action": "auto_approve",
                        "candidate_id": match.dest_game_id,
                        "query": "",
                        "classification_before": "strong_candidate",
                        "previous_decision": previous,
                        "new_decision": match.decision.value,
                        "matches_sha256_before": before,
                        "matches_sha256_after": after,
                    }
                )
            for match, review, initial in reverted:
                self._append_history(
                    {
                        **details,
                        "source_slug": match.source_slug,
                        "source_hash": review_version_hash(review),
                        "action": "auto_revert_category",
                        "candidate_id": match.dest_game_id,
                        "query": "",
                        "classification_before": initial,
                        "previous_decision": "approved",
                        "new_decision": match.decision.value,
                        "matches_sha256_before": before,
                        "matches_sha256_after": after,
                    }
                )

        matches_by_slug = {match.source_slug: match for match in matches}
        decisions = load_decisions(self.paths.liked)
        liked_created = 0
        for original in reviews:
            review = effective_by_slug.get(original.game_slug, original)
            value = liked_value_for_rating(review.rating)
            match = matches_by_slug.get(review.game_slug)
            if value is None or match is None or match.dest_game_id is None:
                continue
            if match.dest_game_id in destination_ids:
                continue
            source_hash = review_version_hash(original)
            latest = next(
                (
                    decision
                    for decision in reversed(decisions)
                    if decision.source_ref == review.game_slug
                    and decision.game_id == match.dest_game_id
                ),
                None,
            )
            if latest is not None and latest.source == "human":
                continue
            if latest is not None and latest.source_hash == source_hash and latest.value is value:
                continue
            if dry_run:
                liked_created += 1
                continue
            append_decision(
                self.paths.liked,
                source_ref=review.game_slug,
                game_id=match.dest_game_id,
                source_hash=source_hash,
                value=value,
                source="automatic",
                replace=True,
            )
            liked_created += 1

        return {
            "auto_approved": len(approved),
            "auto_reverted_category": len(reverted),
            "auto_liked": liked_created,
        }

    def ensure_match_entries(
        self,
        *,
        with_candidates: bool = True,
        progress: Any = None,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        """Cria correspondências apenas para slugs ausentes; nunca sobrescreve."""
        reviews, games, _, _ = self._load()
        games_by_slug = {game.slug: game for game in games}
        matches = (
            [GameMatch.model_validate(item) for item in read_json(self.paths.matches)]
            if self.paths.matches.is_file()
            else []
        )
        known = {match.source_slug for match in matches}
        known |= self._evaluation_match_slugs()
        missing = [
            review
            for review in reviews
            if review.evaluation_kind == "written_review" and review.game_slug not in known
        ]
        added: list[str] = []
        warnings: list[str] = []
        if missing and with_candidates:
            with JazereiClient(self.config) as client:
                for index, review in enumerate(missing, start=1):
                    if should_cancel is not None and should_cancel():
                        raise OperationCancelled("geração de correspondências cancelada")
                    game = games_by_slug.get(review.game_slug)
                    if game is None:
                        game = GameRecord(
                            slug=review.game_slug,
                            title=review.game_title,
                            source_url=review.source_url,
                        )
                    try:
                        candidates = search_games(client, review.game_title, limit=8)
                        item = match_game(game, candidates)
                    except Exception as exc:  # noqa: BLE001 - falha vira revisão manual
                        warnings.append(f"{review.game_slug}: {type(exc).__name__}")
                        item = GameMatch(
                            source_slug=review.game_slug,
                            source_title=review.game_title,
                            source_year=game.year,
                            decision=MatchDecision.MANUAL_REVIEW,
                            reasons=["novo item sem consulta de catálogo"],
                        )
                    matches.append(item)
                    added.append(review.game_slug)
                    if progress is not None:
                        progress(
                            {
                                "kind": "matches",
                                "current": index,
                                "total": len(missing),
                                "added": len(added),
                            }
                        )
        elif missing:
            for review in missing:
                game = games_by_slug.get(review.game_slug)
                matches.append(
                    GameMatch(
                        source_slug=review.game_slug,
                        source_title=review.game_title,
                        source_year=game.year if game else None,
                        decision=MatchDecision.MANUAL_REVIEW,
                        reasons=["novo item sem correspondência gerada"],
                    )
                )
                added.append(review.game_slug)
        if added:
            self._write_matches(matches)
        return {
            "added": len(added),
            "slugs": added,
            "warnings": warnings,
            "total_matches": len(matches),
        }

    # -- ações -----------------------------------------------------------
    def decide_match(self, payload: dict[str, Any], *, return_state: bool = True) -> dict[str, Any]:
        slug = str(payload.get("slug") or "")
        action = str(payload.get("action") or "")
        if not slug or action not in {
            "approve",
            "reject",
            "manual",
            "no_match",
            "pending",
            "confirm_edition",
            "reopen",
        }:
            raise ReviewUIError("ação de correspondência inválida")
        reviews, games, matches, destination = self._load()
        review = next((item for item in reviews if item.game_slug == slug), None)
        if review is None:
            raise ReviewUIError("review de origem inexistente")
        if review.evaluation_kind == "rating_only" and slug not in self._evaluation_match_slugs():
            raise ReviewUIError(
                "matching de avaliacoes somente nota ainda nao executado; "
                "rode `evaluations match` antes de decidir"
            )
        match = next((item for item in matches if item.source_slug == slug), None)
        if match is None:
            game = next((item for item in games if item.slug == slug), None)
            match = GameMatch(
                source_slug=slug,
                source_title=review.game_title,
                source_year=game.year if game else None,
                decision=MatchDecision.MANUAL_REVIEW,
                reasons=["correspondência criada durante a revisão"],
            )
            matches.append(match)
        source_hash = review_version_hash(review)
        history = [event for event in self._combined_history() if event.get("source_slug") == slug]
        destination_ids = {int(item["game_id"]) for item in destination if "game_id" in item}
        initial = self._initial_classification(match, history, destination_ids)
        latest = history[-1] if history else None
        candidate_id = payload.get("candidate_id")
        candidate_id = int(candidate_id) if candidate_id is not None else None
        query = str(payload.get("query") or "")
        reason = str(payload.get("reason") or "").strip()
        edition_confirmed_requested = payload.get("edition_confirmed") is True
        evaluation_info = self._evaluation_info()
        evaluation_events = self._evaluation_events()
        evaluation_event = latest_by_slug(evaluation_events).get(slug)
        game = next((item for item in games if item.slug == slug), None)
        category = self._categories().get(slug) or (game.category if game else None)
        special_edition = bool(category)

        if (
            latest
            and latest.get("source_hash") == source_hash
            and latest.get("action") == action
            and latest.get("candidate_id") == candidate_id
            and ("reason" not in latest or latest.get("reason") in {None, reason})
        ):
            return self.state() if return_state else {"slug": slug, "skipped": True}

        if action == "pending":
            return self.state() if return_state else {"slug": slug, "deferred": True}

        _, before = self._match_target(slug)
        previous_decision = match.decision.value

        if action == "confirm_edition":
            if not special_edition:
                raise ReviewUIError("confirmação de edição só se aplica a edição especial")
            if match.dest_game_id is None:
                raise ReviewUIError("selecione um candidato antes de confirmar a edição")
            if candidate_id is not None and candidate_id != match.dest_game_id:
                raise ReviewUIError("confirmação de edição vinculada a outro candidato")
            if not reason:
                raise ReviewUIError("justificativa obrigatória para confirmar a edição")
            new_decision = previous_decision
            target_path, after = self._match_target(slug)
            after = before
        elif action == "approve":
            candidate = self._find_candidate(match, candidate_id)
            if special_edition:
                confirmed_for_candidate = self._edition_confirmation(slug, candidate["game_id"])[0]
                if not (edition_confirmed_requested or confirmed_for_candidate):
                    raise ReviewUIError("edição especial exige confirmação explícita")
                if edition_confirmed_requested and not reason:
                    raise ReviewUIError("justificativa obrigatória para confirmar a edição")
            match.decision = MatchDecision.APPROVED
            match.dest_game_id = candidate["game_id"]
            match.dest_slug = candidate.get("slug")
            match.dest_name = candidate.get("name")
            match.dest_year = candidate.get("year")
            match.confidence = float(candidate.get("confidence") or match.confidence or 0.0)
            approval_reason = "aprovacao humana via interface"
            if approval_reason not in match.reasons:
                match.reasons.append(approval_reason)
            new_decision = match.decision.value
            target_path, after = self._persist_match(match)
        elif action == "manual":
            candidate = self._manual_candidate(query, candidate_id)
            if special_edition and not (
                edition_confirmed_requested
                or self._edition_confirmation(slug, candidate["game_id"])[0]
            ):
                raise ReviewUIError("edição especial exige confirmação explícita")
            if special_edition and edition_confirmed_requested and not reason:
                raise ReviewUIError("justificativa obrigatória para confirmar a edição")
            match.decision = MatchDecision.APPROVED
            match.dest_game_id = candidate["game_id"]
            match.dest_slug = candidate["slug"]
            match.dest_name = candidate["name"]
            match.dest_year = candidate["year"]
            match.confidence = 1.0
            match.reasons.append(
                f"selecao manual via interface; consulta={query}; game_id={candidate['game_id']}"
            )
            new_decision = match.decision.value
            target_path, after = self._persist_match(match)
        elif action == "reject":
            if candidate_id is None:
                raise ReviewUIError("rejeição exige candidate_id")
            candidate_ids = {int(candidate["game_id"]) for candidate in self._candidates(match)}
            if candidate_id not in candidate_ids:
                raise ReviewUIError("candidato não pertence a esta correspondência")
            if match.dest_game_id == candidate_id:
                match.decision = MatchDecision.MANUAL_REVIEW
                match.dest_game_id = None
                match.dest_slug = None
                match.dest_name = None
                match.dest_year = None
            match.reasons.append(f"candidato rejeitado pelo usuario: {candidate_id}")
            new_decision = match.decision.value
            target_path, after = self._persist_match(match)
        elif action == "no_match":
            match.decision = MatchDecision.MANUAL_REVIEW
            match.dest_game_id = None
            match.dest_slug = None
            match.dest_name = None
            match.dest_year = None
            match.reasons.append("sem correspondencia adequada; decisao humana via interface")
            new_decision = match.decision.value
            target_path, after = self._persist_match(match)
        else:  # reopen
            match.decision = (
                MatchDecision.CANDIDATE
                if match.dest_game_id is not None
                else MatchDecision.MANUAL_REVIEW
            )
            match.reasons.append("decisao reaberta pelo usuario")
            new_decision = match.decision.value
            target_path, after = self._persist_match(match)

        edition_event_value = bool(
            action == "confirm_edition"
            or (
                special_edition
                and action in {"approve", "manual"}
                and (
                    edition_confirmed_requested
                    or self._edition_confirmation(slug, match.dest_game_id)[0]
                )
            )
        )
        event: MatchDecisionEvent | None = None
        if evaluation_info.get("evaluation_run_id"):
            run = self._evaluation_run()
            event = append_match_event(
                run.run_dir / "match-decisions.jsonl",
                source_ref=slug,
                evaluation_run_id=str(evaluation_info["evaluation_run_id"]),
                evaluations_sha256=str(evaluation_info.get("evaluations_sha256") or ""),
                source_run_id=self.paths.source_run.name,
                evaluation_kind=review.evaluation_kind,
                source_hash=source_hash,
                action=action,
                previous_decision=previous_decision,
                new_decision=new_decision,
                candidate_id=match.dest_game_id,
                candidate_slug=match.dest_slug,
                candidate_name=match.dest_name,
                confidence=match.confidence,
                edition_confirmed=edition_event_value,
                operator_id=self.config.operator_id or "local",
                destination_user_id=self.config.jazerei_user_id or None,
                reason=reason,
                supersedes=evaluation_event.event_id if evaluation_event else None,
                matches_sha256_before=before,
                matches_sha256_after=after,
            )
            refresh_manifest(run)
        else:
            self._append_history(
                {
                    **self._source_details(),
                    "source_slug": slug,
                    "source_hash": source_hash,
                    "action": action,
                    "candidate_id": candidate_id,
                    "query": query,
                    "classification_before": initial,
                    "previous_decision": previous_decision,
                    "new_decision": new_decision,
                    "edition_confirmed": edition_event_value,
                    "evaluation_kind": review.evaluation_kind,
                    "operator_id": self.config.operator_id or "local",
                    "destination_user_id": self.config.jazerei_user_id or None,
                    "reason": reason,
                    "matches_sha256_before": before,
                    "matches_sha256_after": after,
                    "matches_file": str(target_path) if target_path else None,
                }
            )
        if return_state:
            return self.state()
        return {
            "slug": slug,
            "action": action,
            "event_id": event.event_id if event else None,
            "decision": new_decision,
        }

    def decide_liked(self, payload: dict[str, Any], *, return_state: bool = True) -> dict[str, Any]:
        slug = str(payload.get("slug") or "")
        value = payload.get("value")
        if value is not None and type(value) is not bool:
            raise ReviewUIError("liked deve ser booleano ou nulo")
        reviews, _, matches, _ = self._load()
        review = next((item for item in reviews if item.game_slug == slug), None)
        match = next((item for item in matches if item.source_slug == slug), None)
        if review is None:
            raise ReviewUIError("review de origem inexistente")
        source_hash = review_version_hash(review)
        decisions = load_decisions(self.paths.liked)
        latest = next(
            (decision for decision in reversed(decisions) if decision.source_ref == slug),
            None,
        )
        reason = str(payload.get("reason") or "").strip()
        evaluation_info = self._evaluation_info()
        game_id = (
            match.dest_game_id
            if match is not None and match.decision is MatchDecision.APPROVED
            else None
        )

        if value is None:
            if latest is None:
                return self.state() if return_state else {"slug": slug, "deferred": False}
            if latest.event_type == "deferral":
                return self.state() if return_state else {"slug": slug, "deferred": True}
            append_deferral(
                self.paths.liked,
                source_ref=slug,
                source_hash=source_hash,
                evaluation_kind=review.evaluation_kind,
                evaluation_run_id=evaluation_info.get("evaluation_run_id"),
                evaluations_sha256=evaluation_info.get("evaluations_sha256"),
                operator_id=self.config.operator_id or "local",
                destination_user_id=self.config.jazerei_user_id or None,
                reason=reason,
            )
            return self.state() if return_state else {"slug": slug, "deferred": True}

        if (
            latest is not None
            and latest.source_hash == source_hash
            and latest.value is value
            and latest.game_id == game_id
            and latest.source == "human"
            and latest.event_type != "deferral"
        ):
            return self.state() if return_state else {"slug": slug, "skipped": True}

        event_type = (
            "reconfirmation" if latest is not None and latest.source == "automatic" else "decision"
        )
        if latest is not None and latest.source == "human" and latest.value is not value:
            event_type = "decision"
        append_decision(
            self.paths.liked,
            source_ref=slug,
            game_id=game_id,
            source_hash=source_hash,
            value=value,
            replace=True,
            event_type=event_type,
            evaluation_kind=review.evaluation_kind,
            evaluation_run_id=evaluation_info.get("evaluation_run_id"),
            evaluations_sha256=evaluation_info.get("evaluations_sha256"),
            operator_id=self.config.operator_id or "local",
            destination_user_id=self.config.jazerei_user_id or None,
            previous_value=latest.value if latest else None,
            previous_status=latest.event_type if latest else None,
            reason=reason,
        )
        return (
            self.state()
            if return_state
            else {"slug": slug, "value": value, "event_type": event_type}
        )

    def decide_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        slug = str(payload.get("slug") or "")
        reviews, _, _, _ = self._load()
        original = next((item for item in reviews if item.game_slug == slug), None)
        if original is None:
            raise ReviewUIError("review de origem inexistente")
        raw_fields = payload.get("fields")
        if not isinstance(raw_fields, dict) or not raw_fields:
            raise ReviewUIError("informe ao menos um campo para editar")
        unknown = set(raw_fields) - set(REVIEW_EDITABLE_FIELDS)
        if unknown:
            raise ReviewUIError(f"campos não editáveis: {sorted(unknown)}")

        fields: dict[str, Any] = {}
        if "rating" in raw_fields:
            rating = raw_fields["rating"]
            if rating is not None:
                if isinstance(rating, bool) or not isinstance(rating, (int, float)):
                    raise ReviewUIError("rating deve ser numérico ou nulo")
                if not 0 <= float(rating) <= 10:
                    raise ReviewUIError("rating deve estar entre 0 e 10")
                rating = float(rating)
            fields["rating"] = rating
        if "text" in raw_fields:
            if not isinstance(raw_fields["text"], str):
                raise ReviewUIError("text deve ser string")
            fields["text"] = raw_fields["text"]
        for key in ("status", "progress"):
            if key in raw_fields:
                value = raw_fields[key]
                if value is not None and not isinstance(value, str):
                    raise ReviewUIError(f"{key} deve ser string ou nulo")
                fields[key] = value or None

        current = self._overrides().get(slug, {})
        effective = self._effective_review(original, current)
        changed = {key: value for key, value in fields.items() if getattr(effective, key) != value}
        if not changed:
            return self.state()
        before = {
            "rating": effective.rating,
            "text": effective.text,
            "status": effective.status,
            "progress": effective.progress,
        }
        self.paths.overrides.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "schema_version": 1,
            "event_id": os.urandom(16).hex(),
            "at": utc_now(),
            "source_ref": slug,
            "source_hash": review_version_hash(original),
            "action": "edit_review",
            "fields": changed,
            "previous": before,
            "original": {
                "rating": original.rating,
                "text": original.text,
                "status": original.status,
                "progress": original.progress,
            },
        }
        with self.paths.overrides.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return self.state()

    def _find_candidate(self, match: GameMatch, candidate_id: int | None) -> dict[str, Any]:
        if candidate_id is None:
            raise ReviewUIError("aprovação exige candidate_id")
        for candidate in match.alternatives or []:
            if candidate.get("game_id") == candidate_id:
                return {
                    "game_id": int(candidate["game_id"]),
                    "slug": candidate.get("slug"),
                    "name": candidate.get("name"),
                    "year": candidate.get("release_year"),
                    "confidence": candidate.get("confidence"),
                }
        if match.dest_game_id == candidate_id:
            return {
                "game_id": candidate_id,
                "slug": match.dest_slug,
                "name": match.dest_name,
                "year": match.dest_year,
                "confidence": match.confidence,
            }
        raise ReviewUIError("candidato não pertence a esta correspondência")

    def _manual_candidate(self, query: str, candidate_id: int | None) -> dict[str, Any]:
        if not query.strip() or candidate_id is None:
            raise ReviewUIError("seleção manual exige consulta e game_id")
        results = self.search_catalog(query)
        candidate = next((item for item in results if item["game_id"] == candidate_id), None)
        if candidate is None:
            raise ReviewUIError("candidato manual não encontrado na consulta de catálogo")
        return candidate

    def search_catalog(self, query: str) -> list[dict[str, Any]]:
        query = query.strip()
        if not query or len(query) > 120:
            raise ReviewUIError("consulta de catálogo inválida")
        with JazereiClient(self.config) as client:
            games = search_games(client, query, limit=12)
        return [
            {
                "game_id": game.id,
                "slug": game.slug,
                "name": game.name,
                "year": game.release_year,
                "platforms": game.platforms,
                "developer": game.developer,
            }
            for game in games
        ]

    def generate_plan(self) -> dict[str, Any]:
        from src.migration.approval import (
            build_bundle,
            decisions_provenance,
            destination_provenance,
            legacy_destination_provenance,
            matching_provenance,
            source_provenance,
        )
        from src.migration.planner import build_plan
        from src.models.destination import DestinationReview
        from src.models.snapshot import AccountReviewsSnapshot

        reviews, games, matches, destination_payload = self._load()
        ratifications = load_ratifications(self.paths.ratifications)
        details = self._source_details()
        account_snapshot = self._account_snapshot()
        if account_snapshot:
            destination_info, snapshot = destination_provenance(self.config, account_snapshot)
            if isinstance(snapshot, AccountReviewsSnapshot):
                destination_payload = [
                    review.model_dump(mode="json") for review in snapshot.reviews
                ]
        else:
            legacy_reviews = self.paths.destination_run / "reviews.json"
            if not legacy_reviews.is_file():
                raise ReviewUIError(
                    "snapshot de destino ausente; capture um snapshot autenticado "
                    "antes de gerar o plano"
                )
            destination_info = legacy_destination_provenance(legacy_reviews)
        overrides = self._overrides()
        effective_reviews = [
            self._effective_review(review, overrides.get(review.game_slug, {}))
            for review in reviews
        ]
        destination = [DestinationReview.model_validate(item) for item in destination_payload]
        decisions = load_decisions(self.paths.liked)
        eligible_match_list, rejected_matches = eligible_matches(
            matches,
            self._history(),
            ratifications,
            source_run_id=details["source_run_id"],
            source_manifest_sha256=details["source_manifest_sha256"],
            destination_run_id=details["destination_run_id"],
            destination_manifest_sha256=details["destination_manifest_sha256"],
        )
        match_by_slug = {match.source_slug: match for match in matches}
        eligible_decisions, rejected_liked = eligible_liked(
            decisions,
            match_by_slug,
            ratifications,
            source_run_id=details["source_run_id"],
            source_manifest_sha256=details["source_manifest_sha256"],
            destination_run_id=details["destination_run_id"],
            destination_manifest_sha256=details["destination_manifest_sha256"],
        )
        plan_id = f"stage9-review-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        base_plan_id = plan_id
        index = 2
        while (self.paths.plans / plan_id).exists():
            plan_id = f"{base_plan_id}-{index}"
            index += 1
        plan = build_plan(
            eligible_match_list,
            effective_reviews,
            games=games,
            destination_reviews=destination,
            liked_decisions=eligible_decisions,
        )
        plan.skipped.extend(rejected_matches)
        plan.skipped.extend(rejected_liked)
        source = source_provenance(
            self.config,
            self.paths.source_run / "games.json",
            self.paths.source_run / "reviews.json",
        )
        matching = matching_provenance(self.paths.matches)
        decisions_info = decisions_provenance(self.paths.liked, plan)
        decisions_info["review_overrides_sha256"] = (
            sha256_file(self.paths.overrides) if self.paths.overrides.exists() else None
        )
        decisions_info["source_categories_sha256"] = (
            sha256_file(self.paths.categories) if self.paths.categories.exists() else None
        )
        decisions_info["ratifications_sha256"] = (
            sha256_file(self.paths.ratifications) if self.paths.ratifications.exists() else None
        )
        decisions_info["ratification_ids"] = [event.ratification_id for event in ratifications]
        decisions_info["validation"] = validation_summary(
            matches=matches,
            history=self._history(),
            decisions=decisions,
            ratifications=ratifications,
            source_run_id=details["source_run_id"],
            source_manifest_sha256=details["source_manifest_sha256"],
            destination_run_id=details["destination_run_id"],
            destination_manifest_sha256=details["destination_manifest_sha256"],
        )
        inputs = {
            "games": str(self.paths.source_run / "games.json"),
            "reviews": str(self.paths.source_run / "reviews.json"),
            "matches": str(self.paths.matches),
            "decisions": str(self.paths.liked),
            "overrides": str(self.paths.overrides),
            "categories": str(self.paths.categories),
            "snapshot": str(account_snapshot) if account_snapshot else "",
            "destination_reviews": (
                "" if account_snapshot else str(self.paths.destination_run / "reviews.json")
            ),
        }
        provenance_complete = bool(
            source.complete
            and destination_info.get("complete")
            and matching.get("complete")
            and decisions_info.get("complete")
        )
        plan_dir, _ = build_bundle(
            self.config,
            plan=plan,
            plan_id=plan_id,
            source=source.data,
            destination=destination_info,
            matching=matching,
            decisions=decisions_info,
            inputs=inputs,
            provenance_complete=provenance_complete,
        )
        result = read_json(plan_dir / "plan.json")
        return {
            "plan_id": plan_id,
            "path": str(plan_dir),
            "actions": len(result.get("actions", [])),
            "unmatched": len(result.get("unmatched", [])),
            "skipped": len(result.get("skipped", [])),
            "provenance_complete": provenance_complete,
            "validation": decisions_info["validation"],
            "warning": (
                "prévia verificável; aprovação/preflight ainda pendentes"
                if provenance_complete
                else "proveniência do destino incompleta; aprovação/preflight ainda necessários"
            ),
        }

    def ratify_automatic(self, plan_id: str) -> dict[str, Any]:
        """Ratifica em lote decisões automáticas e regenera o plano.

        A ratificação é um evento humano append-only vinculado aos hashes da
        origem e do destino; não altera correspondências nem liked.
        """
        from src.migration.approval import load_bundle, verify_bundle
        from src.migration.ratification import append_ratification

        bundle = load_bundle(self.config, plan_id)
        verification = verify_bundle(self.config, bundle)
        if verification.invalid or verification.stale:
            raise ReviewUIError(
                "plano inválido ou desatualizado: "
                + "; ".join(verification.invalid + verification.stale)
            )
        details = self._source_details()
        ratifications = load_ratifications(self.paths.ratifications)

        def already(dimension: str, item: dict[str, Any]) -> bool:
            for event in ratifications:
                if (
                    event.dimension == dimension
                    and event.source_run_id == details["source_run_id"]
                    and event.source_manifest_sha256 == details["source_manifest_sha256"]
                    and event.destination_run_id == details["destination_run_id"]
                    and event.destination_manifest_sha256 == details["destination_manifest_sha256"]
                    and item in event.items
                ):
                    return True
            return False

        _, _, matches, _ = self._load()
        latest_by_slug: dict[str, dict[str, Any]] = {}
        for event in self._history():
            if event.get("source_slug"):
                latest_by_slug[str(event["source_slug"])] = event
        match_items: list[dict[str, Any]] = []
        for match in matches:
            latest = latest_by_slug.get(match.source_slug)
            if match.decision is not MatchDecision.APPROVED or match.dest_game_id is None:
                continue
            if match_origin(latest) != "automatic_unreviewed":
                continue
            item = match_item(match, latest)
            if not already("matches", item):
                match_items.append(item)

        latest_decisions: dict[tuple[str, int], LikedDecision] = {}
        for decision in load_decisions(self.paths.liked):
            latest_decisions[(decision.source_ref, decision.game_id)] = decision
        match_by_slug = {match.source_slug: match for match in matches}
        liked_items: list[dict[str, Any]] = []
        for decision in latest_decisions.values():
            match = match_by_slug.get(decision.source_ref)
            if (
                decision.source != "automatic"
                or match is None
                or match.dest_game_id != decision.game_id
            ):
                continue
            item = liked_item(decision)
            if not already("liked", item):
                liked_items.append(item)

        common = {
            "plan_id": plan_id,
            "plan_sha256": bundle.manifest.plan_sha256,
            "manifest_sha256": bundle.manifest.manifest_sha256,
            "source_run_id": details["source_run_id"],
            "source_manifest_sha256": details["source_manifest_sha256"],
            "destination_run_id": details["destination_run_id"],
            "destination_manifest_sha256": details["destination_manifest_sha256"],
        }
        if match_items:
            append_ratification(
                self.paths.ratifications,
                dimension="matches",
                items=match_items,
                **common,
            )
        if liked_items:
            append_ratification(
                self.paths.ratifications,
                dimension="liked",
                items=liked_items,
                **common,
            )
        result: dict[str, Any] = {
            "ratified_matches": len(match_items),
            "ratified_liked": len(liked_items),
            "plan_id": None,
        }
        if match_items or liked_items:
            regenerated = self.generate_plan()
            result.update(
                {
                    "plan_id": regenerated["plan_id"],
                    "actions": regenerated["actions"],
                    "unmatched": regenerated["unmatched"],
                }
            )
        return result

    # -- persistência ----------------------------------------------------
    def _evaluation_match_slugs(self) -> set[str]:
        run = self._evaluation_run()
        if run is None:
            return set()
        _, matches, _ = load_evaluation_run(run)
        return {match.source_slug for match in matches}

    def _match_target(self, slug: str) -> tuple[Path | None, str]:
        run = self._evaluation_run()
        if run is not None and slug in self._evaluation_match_slugs():
            return run.matches_path, sha256_file(run.matches_path)
        if self.paths.matches.is_file():
            return self.paths.matches, sha256_file(self.paths.matches)
        return None, ""

    def _persist_match(self, match: GameMatch) -> tuple[Path, str]:
        run = self._evaluation_run()
        if run is not None and match.source_slug in self._evaluation_match_slugs():
            digest = save_match(run, match)
            return run.matches_path, digest
        matches = (
            [GameMatch.model_validate(item) for item in read_json(self.paths.matches)]
            if self.paths.matches.is_file()
            else []
        )
        replaced = False
        for index, current in enumerate(matches):
            if current.source_slug == match.source_slug:
                matches[index] = match
                replaced = True
                break
        if not replaced:
            matches.append(match)
        self._write_matches(matches)
        return self.paths.matches, sha256_file(self.paths.matches)

    def _write_matches(self, matches: list[GameMatch]) -> None:
        self.paths.matches.parent.mkdir(parents=True, exist_ok=True)
        temp = self.paths.matches.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(
                [match.model_dump(mode="json") for match in matches],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temp, self.paths.matches)

    def _append_history(self, event: dict[str, Any]) -> None:
        self.paths.history.parent.mkdir(parents=True, exist_ok=True)
        event = {"schema_version": 1, "event_id": os.urandom(16).hex(), "at": utc_now(), **event}
        with self.paths.history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


# -- wizard ---------------------------------------------------------------
ACTIVE_JOB_STATUSES = {"queued", "running", "waiting_user", "cancelling"}


class JobBusy(RuntimeError):
    pass


class OperationGate:
    """Um único acesso por vez ao perfil Chromium e às operações longas."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        return self._lock.acquire(blocking=False)

    def release(self) -> None:
        if self._lock.locked():
            with contextlib.suppress(RuntimeError):
                self._lock.release()

    def busy(self) -> bool:
        return self._lock.locked()


class JobRunner:
    def __init__(self, gate: OperationGate) -> None:
        self.gate = gate
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._job: dict[str, Any] | None = None

    def start(
        self,
        kind: str,
        label: str,
        operation: Any,
        *,
        cancelable: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if self._job and self._job["status"] in ACTIVE_JOB_STATUSES:
                raise JobBusy("já existe uma operação em andamento")
            if not self.gate.acquire():
                raise JobBusy("outra operação está usando o navegador")
            self._cancel.clear()
            self._job = {
                "job_id": uuid.uuid4().hex,
                "kind": kind,
                "label": label,
                "status": "queued",
                "cancelable": cancelable,
                "started_at": utc_now(),
                "finished_at": None,
                "progress": {},
                "result": None,
                "error": None,
            }
            job = self._job
        thread = threading.Thread(
            target=self._run,
            args=(job, operation),
            name=f"wizard-{kind}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            self.gate.release()
            raise
        return self.current() or {}

    def _run(self, job: dict[str, Any], operation: Any) -> None:
        self._update(job, status="running")
        try:
            result = operation(
                progress=lambda payload: self._progress(job, payload),
                should_cancel=self._cancel.is_set,
            )
        except OperationCancelled:
            self._update(job, status="cancelled", error=None, finished_at=utc_now())
        except Exception as exc:  # noqa: BLE001 - erro vira estado observável
            self._update(job, status="failed", error=_job_error(exc), finished_at=utc_now())
        else:
            status = "cancelled" if self._cancel.is_set() else "done"
            self._update(job, status=status, result=result, finished_at=utc_now())
        finally:
            self.gate.release()

    def _progress(self, job: dict[str, Any], payload: dict[str, Any]) -> None:
        progress = dict(job.get("progress") or {})
        progress.update(payload)
        status = payload.get("status")
        if status in {"running", "waiting_user"}:
            progress.pop("status", None)
            self._update(job, status=status, progress=progress)
        else:
            self._update(job, progress=progress)

    def _update(self, job: dict[str, Any], **changes: Any) -> None:
        with self._lock:
            if self._job is not job:
                return
            job.update(changes)

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._job) if self._job else None

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if (
                self._job is None
                or self._job["status"] not in ACTIVE_JOB_STATUSES
                or not self._job["cancelable"]
            ):
                return {"cancelled": False, "job": dict(self._job) if self._job else None}
            self._cancel.set()
            self._job["status"] = "cancelling"
            return {
                "cancelled": True,
                "message": "cancelamento solicitado; a etapa atual será encerrada com segurança",
                "job": dict(self._job),
            }


def _job_error(exc: Exception) -> dict[str, Any]:
    for attr in ("as_dict",):
        method = getattr(exc, attr, None)
        if callable(method):
            payload = method()
            if isinstance(payload, dict):
                return payload
    return {
        "error": str(exc),
        "code": type(exc).__name__,
        "hint": "Consulte os detalhes e tente novamente; nenhum artefato anterior é alterado.",
    }


def _active_plan_id(migration: MigrationService, state: WizardState) -> str | None:
    """Somente seleção explícita; nunca promove histórico a estado atual."""
    if not state.active_plan_id:
        return None
    ids = {str(item["plan_id"]) for item in migration.list_plans()}
    return state.active_plan_id if state.active_plan_id in ids else None


def _step(
    step_id: str,
    phase: str,
    title: str,
    description: str,
    status: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "phase": phase,
        "title": title,
        "description": description,
        "status": status,
        "detail": detail,
    }


def _stage_summary(
    *,
    status: dict[str, Any],
    prepare: dict[str, Any] | None,
    executions: list[dict[str, Any]],
    verifications: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prepare_blocked = bool(prepare and prepare.get("blocked"))
    prepare_ready = bool(prepare and prepare.get("ready"))
    prepare_steps = (prepare or {}).get("steps") or []
    done_steps = sum(step.get("status") in {"done", "skipped"} for step in prepare_steps)
    plan_exists = bool(status.get("exists"))
    plan_valid = plan_exists and not status.get("invalid") and not status.get("stale")
    approved = bool(status.get("approved"))
    preflight_ok = bool(status.get("compatible_preflight"))
    execution = executions[0] if executions else None
    execution_ready = bool(execution and approved and preflight_ok)
    if prepare_blocked:
        prepare_status = "blocked"
    elif prepare_ready:
        prepare_status = "done"
    else:
        prepare_status = "current"
    if prepare_blocked:
        review_status = "blocked"
    elif execution_ready:
        review_status = "done"
    elif plan_valid:
        review_status = "current"
    else:
        review_status = "blocked"
    if verifications:
        result_status = "done"
    elif execution:
        result_status = "current"
    else:
        result_status = "blocked"
    return [
        _step(
            "prepare",
            "1",
            "Preparar",
            "Coleta, snapshot, correspondências, política confirmada, plano e validação.",
            prepare_status,
            f"{done_steps} de {len(prepare_steps) or 7} atividades concluídas."
            if prepare_steps
            else "Ainda não iniciado.",
        ),
        _step(
            "review",
            "2",
            "Revisar e executar",
            "Decisões humanas, aprovação do plano, preflight e execução confirmada.",
            review_status,
            f"{status.get('actions', 0)} ações no plano · aprovação {status.get('approval', {}).get('status') or 'pendente'}."
            if plan_exists
            else "Nenhum plano ativo.",
        ),
        _step(
            "result",
            "3",
            "Conferir resultado",
            "Captura autenticada, confirmações causais, divergências e ambiguidades.",
            result_status,
            f"{len(verifications)} relatório(s) de verificação."
            if verifications
            else (
                "Execução disponível; verificação pendente." if execution else "Nenhuma execução."
            ),
        ),
    ]


def build_wizard_payload(
    *,
    config: Config,
    service: ReviewService,
    migration: MigrationService,
    state: WizardState,
    jobs: JobRunner,
    prepare: PrepareOrchestrator | None = None,
) -> dict[str, Any]:
    plans = migration.list_plans()
    active_plan_id = _active_plan_id(migration, state)
    status = (
        migration.status(active_plan_id) if active_plan_id else {"plan_id": None, "exists": False}
    )
    executions = migration.list_executions(active_plan_id) if active_plan_id else []
    all_executions = migration.list_executions()
    verifications = list_verifications(config)
    execution_ids = {item["execution_id"] for item in executions}
    active_execution_id = (
        state.active_execution_id if state.active_execution_id in execution_ids else None
    )
    active_verifications = [
        item
        for item in verifications
        if item.get("plan_id") == active_plan_id and item.get("execution_id") == active_execution_id
    ]
    last_verification_id = (
        state.last_verification_id
        if any(
            item.get("verification_id") == state.last_verification_id
            for item in active_verifications
        )
        else (str(active_verifications[0]["verification_id"]) if active_verifications else None)
    )
    active_execution = next(
        (item for item in executions if item["execution_id"] == active_execution_id),
        None,
    )
    orchestrator = prepare or PrepareOrchestrator(config, service, state, migration)
    try:
        prepare_payload = orchestrator.scan()
    except Exception as exc:  # noqa: BLE001 - UI mostra a falha sem derrubar o servidor
        prepare_payload = {
            "error": str(exc),
            "steps": [],
            "ready": False,
            "blocked": True,
            "safety": {"blocked": False, "ambiguous": [], "detail": "Varredura indisponível."},
        }
    source = {
        "active": None,
        "runs": list_stash_runs(config),
    }
    try:
        source_summary = run_summary(config, service.paths.source_run.name)
        source["active"] = {
            "run_id": service.paths.source_run.name,
            "valid": True,
            "manifest_sha256": source_summary["manifest_sha256"],
            "outcome": source_summary["manifest"].get("outcome"),
        }
    except Exception:  # noqa: BLE001 - estado invalido aparece como ausente
        source["active"] = {"run_id": service.paths.source_run.name, "valid": False}

    destination = {
        "active": None,
        "runs": list_destination_runs(config),
    }
    snapshot = service._account_snapshot()
    if snapshot is not None:
        manifest_path = snapshot.parent.parent / "manifest.json"
        try:
            manifest = read_json(manifest_path)
        except Exception:  # noqa: BLE001
            manifest = {}
        destination["active"] = {
            "run_id": snapshot.parent.parent.name,
            "valid": True,
            "complete": bool(
                manifest.get("capture_complete") and manifest.get("pagination_complete")
            ),
            "snapshot": str(snapshot),
        }
    elif service.paths.destination_run.is_dir():
        try:
            summary = run_summary(config, service.paths.destination_run.name, destination=True)
            destination["active"] = {
                "run_id": service.paths.destination_run.name,
                "valid": True,
                "complete": True,
                "manifest_sha256": summary["manifest_sha256"],
                "legacy": True,
            }
        except Exception:  # noqa: BLE001
            destination["active"] = {
                "run_id": service.paths.destination_run.name,
                "valid": False,
                "complete": False,
            }

    try:
        review_summary = service.state()["summary"]
    except Exception as exc:  # noqa: BLE001 - UI continua mostrando o erro
        review_summary = {"error": str(exc)}
    identity = identity_snapshot(config, state)
    job = jobs.current()
    steps = _stage_summary(
        status=status,
        prepare=prepare_payload,
        executions=executions,
        verifications=active_verifications,
    )
    return {
        "config": {
            "data_dir": str(config.data_dir),
            "browser_profile": str(config.resolved_browser_profile),
            "write_gates": {
                "allow_writes": config.allow_writes,
                "enable_real_write_transport": config.enable_real_write_transport,
            },
            "operation_busy": jobs.gate.busy(),
        },
        "identity": identity,
        "source": source,
        "destination": destination,
        "review": {"summary": review_summary},
        "plans": plans,
        "active_plan_id": active_plan_id,
        "plan": status,
        "executions": executions,
        "all_executions": all_executions,
        "verifications": verifications,
        "active_verifications": active_verifications,
        "last_verification_id": last_verification_id,
        "active_execution_id": active_execution_id,
        "active_execution": active_execution,
        "prepare": prepare_payload,
        "safety": prepare_payload.get("safety") or {},
        "steps": steps,
        "job": job,
        "state": {
            "active_plan_id": state.active_plan_id,
            "active_execution_id": state.active_execution_id,
            "source_run_id": state.source_run_id,
            "destination_run_id": state.destination_run_id,
            "account_snapshot_run_id": state.account_snapshot_run_id,
            "last_verification_id": state.last_verification_id,
            "updated_at": state.updated_at,
        },
    }


# -- servidor --------------------------------------------------------------
class ReviewUIHandler(BaseHTTPRequestHandler):
    service: ReviewService
    migration_service: MigrationService
    server_version = "StashJazereiReview/1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _json(self, status: int, payload: Any) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _state(self) -> WizardState:
        state = getattr(self, "wizard_state", None)
        if state is None:
            state = WizardState.load(self.service.config)
            self.wizard_state = state
        return state

    def _jobs(self) -> JobRunner:
        runner = getattr(self, "job_runner", None)
        if runner is None:
            runner = JobRunner(OperationGate())
            self.job_runner = runner
        return runner

    def _gate(self) -> OperationGate:
        return self._jobs().gate

    def _active_plan(self) -> str:
        migration = getattr(self, "migration_service", None)
        if migration is None:
            return ""
        return _active_plan_id(migration, self._state()) or ""

    def _prepare_orchestrator(self) -> PrepareOrchestrator:
        migration = getattr(self, "migration_service", None)
        if migration is None:
            raise ReviewUIError("serviço de migração indisponível")
        return PrepareOrchestrator(
            self.service.config,
            self.service,
            self._state(),
            migration,
        )

    def _wizard(self) -> dict[str, Any]:
        migration = getattr(self, "migration_service", None)
        if migration is None:
            raise ReviewUIError("serviço de migração indisponível")
        return build_wizard_payload(
            config=self.service.config,
            service=self.service,
            migration=migration,
            state=self._state(),
            jobs=self._jobs(),
            prepare=self._prepare_orchestrator(),
        )

    def _select(self, payload: dict[str, Any]) -> dict[str, Any]:
        migration = getattr(self, "migration_service", None)
        if migration is None:
            raise ReviewUIError("serviço de migração indisponível")
        state = self._state()
        config = self.service.config

        source_id = payload.get("source_run_id")
        destination_id = payload.get("destination_run_id")
        snapshot_id = payload.get("account_snapshot_run_id")
        plan_id = payload.get("active_plan_id")
        execution_id = payload.get("active_execution_id")

        changing_inputs = any(
            value is not None for value in (source_id, destination_id, snapshot_id)
        ) and (
            (source_id is not None and source_id != state.source_run_id)
            or (destination_id is not None and destination_id != state.destination_run_id)
            or (snapshot_id is not None and snapshot_id != state.account_snapshot_run_id)
        )
        if changing_inputs:
            blocking = [
                item
                for item in migration.list_executions()
                if item["status"] in {"prepared", "awaiting_confirmation", "running"}
                or (item["status"] == "inconclusive" and item["resumable"])
            ]
            if blocking:
                raise SetupError(
                    "há execução preparada, em andamento ou retomável",
                    code="inputs_locked",
                    hint=(
                        "Conclua, cancele ou reconcilie a execução atual antes de "
                        "adotar outra coleta/snapshot. Nenhum artefato foi alterado."
                    ),
                )
        if plan_id is not None:
            status = migration.status(str(plan_id))
            if not status.get("exists"):
                raise SetupError(f"plano inexistente: {plan_id}", code="plan_missing")
        if execution_id is not None:
            known = {item["execution_id"]: item for item in migration.list_executions()}
            selected = known.get(str(execution_id))
            if selected is None:
                raise SetupError(
                    f"execução inexistente: {execution_id}",
                    code="execution_missing",
                )
            if plan_id is not None and selected["plan_id"] != str(plan_id):
                raise SetupError(
                    "execução pertence a outro plano",
                    code="execution_plan_mismatch",
                )
            state.active_plan_id = str(selected["plan_id"])
        if source_id is not None:
            verify_source_run(config, str(source_id))
        if destination_id is not None:
            verify_destination_run(config, str(destination_id))
        if snapshot_id is not None and account_snapshot_path(config, str(snapshot_id)) is None:
            raise SetupError(
                f"snapshot de conta inexistente: {snapshot_id}",
                code="snapshot_missing",
            )

        if source_id is not None:
            state.source_run_id = str(source_id)
        if destination_id is not None:
            state.destination_run_id = str(destination_id)
        if snapshot_id is not None:
            state.account_snapshot_run_id = str(snapshot_id)
        if plan_id is not None:
            state.active_plan_id = str(plan_id)
            state.active_execution_id = None
        if execution_id is not None:
            state.active_execution_id = str(execution_id)
        state.save()
        apply_state_to_service(self.service, state, config)
        return self._wizard()

    def _start_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        config = self.service.config
        state = self._state()
        migration = getattr(self, "migration_service", None)
        runner = self._jobs()

        def identity_operation(site: str, operation: Any) -> Any:
            def run(*, progress: Any, should_cancel: Any) -> Any:
                result = operation(
                    config,
                    progress=progress,
                    should_cancel=should_cancel,
                )
                state.record_identity(site, result)
                state.save()
                return result

            return run

        if kind == "stash_access":
            operation = identity_operation("stash", open_stash)
            label = "Abrindo Stash"
            cancelable = True
        elif kind == "login_jazerei":
            operation = identity_operation("jazerei", login_jazerei)
            label = "Login manual no Já Zerei"
            cancelable = True
        elif kind == "verify_jazerei":
            operation = identity_operation("jazerei", verify_jazerei)
            label = "Verificando sessão do Já Zerei"
            cancelable = True
        elif kind == "collect_stash":
            operation = collect_stash
            label = "Coletando Stash"
            cancelable = True
        elif kind == "snapshot_destination":
            operation = snapshot_destination
            label = "Capturando snapshot do destino"
            cancelable = True
        elif kind == "matches_ensure":

            def matches_operation(*, progress: Any, should_cancel: Any) -> Any:
                return self.service.ensure_match_entries(
                    with_candidates=bool(params.get("with_candidates", True)),
                    progress=progress,
                    should_cancel=should_cancel,
                )

            operation = matches_operation
            label = "Preparando correspondências de itens novos"
            cancelable = True
        elif kind == "prepare_all":
            apply_policy = bool(params.get("apply_policy"))

            def prepare_operation(*, progress: Any, should_cancel: Any) -> Any:
                return self._prepare_orchestrator().run(
                    apply_policy=apply_policy,
                    progress=progress,
                    should_cancel=should_cancel,
                )

            operation = prepare_operation
            label = "Preparando migração"
            cancelable = True
        elif kind == "approve_and_prepare":
            if migration is None:
                raise ReviewUIError("serviço de migração indisponível")
            plan_id = str(params.get("plan_id") or self._active_plan())
            confirm = str(params.get("confirm") or "")
            if not plan_id:
                raise SetupError("plano ativo ausente", code="plan_missing")
            if confirm != plan_id:
                raise SetupError(
                    "confirmação deve ser exatamente o plan_id",
                    code="invalid_confirmation",
                )

            def finalize_operation(*, progress: Any, should_cancel: Any) -> Any:
                status = migration.status(plan_id)
                if not status.get("exists"):
                    raise SetupError(f"plano inexistente: {plan_id}", code="plan_missing")
                if status.get("invalid") or status.get("stale"):
                    raise SetupError(
                        "plano inválido ou desatualizado; prepare novamente",
                        code="plan_invalid",
                        detail="; ".join(
                            [*(status.get("invalid") or []), *(status.get("stale") or [])]
                        ),
                    )
                if not status.get("approved"):
                    migration.approve(plan_id=plan_id, confirm=plan_id)
                    status = migration.status(plan_id)
                if not status.get("compatible_preflight"):
                    migration.preflight(
                        plan_id=plan_id,
                        confirm=plan_id,
                        progress=progress,
                        should_cancel=should_cancel,
                    )
                    status = migration.status(plan_id)
                if not status.get("compatible_preflight"):
                    raise SetupError(
                        "preflight compatível ausente após a execução",
                        code="preflight_missing",
                    )
                prepared = [
                    item
                    for item in migration.list_executions(plan_id)
                    if item["status"] in {"prepared", "awaiting_confirmation"}
                ]
                if prepared:
                    execution_id = str(prepared[0]["execution_id"])
                    reused = True
                else:
                    result = migration.prepare(plan_id=plan_id, confirm=plan_id)
                    execution_id = str(result["execution_id"])
                    reused = False
                state.active_plan_id = plan_id
                state.active_execution_id = execution_id
                state.save()
                return {
                    "plan_id": plan_id,
                    "execution_id": execution_id,
                    "reused": reused,
                    "actions": status.get("actions", 0),
                }

            operation = finalize_operation
            label = "Aprovando plano e preparando execução"
            cancelable = True
        elif kind == "resolve_ambiguity":
            if migration is None:
                raise ReviewUIError("serviço de migração indisponível")
            execution_id = str(params.get("execution_id") or "")
            action_id = str(params.get("action_id") or "")
            if not execution_id or not action_id:
                raise SetupError(
                    "execução e ação são obrigatórias",
                    code="resolve_parameters",
                )

            def resolve_operation(*, progress: Any, should_cancel: Any) -> Any:
                return migration.resolve_ambiguity(
                    execution_id,
                    action_id,
                    progress=progress,
                    should_cancel=should_cancel,
                )

            operation = resolve_operation
            label = "Resolvendo ambiguidade com leitura autenticada"
            cancelable = True
        elif kind == "verify_destination":
            if migration is None:
                raise ReviewUIError("serviço de migração indisponível")
            plan_id = str(params.get("plan_id") or self._active_plan())
            execution_id = str(params.get("execution_id") or "")
            if not execution_id:
                raise SetupError(
                    "execução é obrigatória para verificar o destino",
                    code="verification_parameters",
                )

            def verify_operation(*, progress: Any, should_cancel: Any) -> Any:
                result = migration.verify_destination(
                    plan_id,
                    execution_id,
                    progress=progress,
                    should_cancel=should_cancel,
                )
                state.last_verification_id = result["verification_id"]
                state.save()
                return result

            operation = verify_operation
            label = "Verificando o destino autenticado"
            cancelable = True
        elif kind == "preflight":
            if migration is None:
                raise ReviewUIError("serviço de migração indisponível")
            plan_id = str(params.get("plan_id") or self._active_plan())
            confirm = str(params.get("confirm") or plan_id)

            def operation(*, progress: Any, should_cancel: Any) -> Any:
                return migration.preflight(
                    plan_id=plan_id,
                    preflight_id=params.get("preflight_id") or None,
                    confirm=confirm,
                    progress=progress,
                    should_cancel=should_cancel,
                )

            label = "Executando preflight autenticado"
            cancelable = True
        else:
            raise SetupError(f"operação desconhecida: {kind}", code="unknown_job")
        return runner.start(kind, label, operation, cancelable=cancelable)

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlsplit(self.path).path
            if path == "/":
                return self._send(
                    200, (WEB_DIR / "review.html").read_bytes(), "text/html; charset=utf-8"
                )
            if path == "/review.css":
                return self._send(
                    200, (WEB_DIR / "review.css").read_bytes(), "text/css; charset=utf-8"
                )
            if path == "/review.js":
                return self._send(
                    200, (WEB_DIR / "review.js").read_bytes(), "text/javascript; charset=utf-8"
                )
            if path == "/api/state":
                return self._json(200, self.service.state())
            if path == "/api/review/reconciliation":
                return self._json(200, self.service.reconciliation_report())
            if path == "/api/review/summary":
                state = self.service.state()
                return self._json(
                    200,
                    {
                        "summary": state["summary"],
                        "evaluation_run_id": state.get("evaluation_run_id"),
                        "evaluations_sha256": state.get("evaluations_sha256"),
                        "operator_id": state.get("operator_id"),
                    },
                )
            if path.startswith("/api/review/item/"):
                slug = path.removeprefix("/api/review/item/").strip("/")
                state = self.service.state()
                item = next((entry for entry in state["items"] if entry["slug"] == slug), None)
                return self._json(200 if item else 404, item or {"error": "item nao encontrado"})
            if path.startswith("/api/review/history/"):
                slug = path.removeprefix("/api/review/history/").strip("/")
                events = [
                    event
                    for event in self.service._combined_history()
                    if event.get("source_slug") == slug or event.get("source_ref") == slug
                ]
                decisions = [
                    decision.model_dump(mode="json")
                    for decision in load_decisions(self.service.paths.liked)
                    if decision.source_ref == slug
                ]
                return self._json(200, {"events": events, "liked": decisions})
            if path == "/api/wizard":
                return self._json(200, self._wizard())
            if path == "/api/prepare":
                return self._json(200, self._prepare_orchestrator().scan())
            if path == "/api/jobs/current":
                return self._json(200, {"job": self._jobs().current()})
            if path == "/api/executions":
                migration = getattr(self, "migration_service", None)
                if migration is None:
                    raise ReviewUIError("serviço de migração indisponível")
                return self._json(200, {"executions": migration.list_executions()})
            if path == "/api/verifications":
                query = parse_qs(urlsplit(self.path).query)
                return self._json(
                    200,
                    {
                        "verifications": list_verifications(
                            self.service.config,
                            plan_id=(query.get("plan_id") or [None])[0],
                            execution_id=(query.get("execution_id") or [None])[0],
                        )
                    },
                )
            if path.startswith("/api/verifications/"):
                verification_id = path.removeprefix("/api/verifications/").strip("/")
                if not verification_id:
                    return self._json(404, {"error": "rota não encontrada"})
                report = load_verification(self.service.config, verification_id)
                return self._json(
                    200,
                    {
                        "report": report.model_dump(mode="json"),
                        "valid": not verify_verification(self.service.config, verification_id),
                    },
                )
            if path == "/api/report":
                query = parse_qs(urlsplit(self.path).query)
                verification_id = (query.get("verification_id") or [None])[0]
                if verification_id:
                    report = load_verification(self.service.config, verification_id)
                    return self._json(
                        200,
                        {
                            "report": report.model_dump(mode="json"),
                            "valid": not verify_verification(
                                self.service.config, report.verification_id
                            ),
                        },
                    )
                reports = list_verifications(
                    self.service.config,
                    plan_id=(query.get("plan_id") or [None])[0],
                    execution_id=(query.get("execution_id") or [None])[0],
                )
                if not reports:
                    return self._json(
                        404,
                        {
                            "error": "nenhuma verificação encontrada",
                            "hint": "Execute o job verify_destination para gerar um relatório.",
                        },
                    )
                report = load_verification(self.service.config, str(reports[0]["verification_id"]))
                return self._json(
                    200,
                    {
                        "report": report.model_dump(mode="json"),
                        "valid": not verify_verification(
                            self.service.config, report.verification_id
                        ),
                    },
                )
            if path.startswith("/api/executions/"):
                migration = getattr(self, "migration_service", None)
                if migration is None:
                    raise ReviewUIError("serviço de migração indisponível")
                execution_id = path.removeprefix("/api/executions/").strip("/")
                if not execution_id:
                    return self._json(404, {"error": "rota não encontrada"})
                return self._json(200, migration.execution_detail(execution_id))
            if path == "/api/policy/preview":
                return self._json(200, self.service.apply_automatic_policy(dry_run=True))
            if path == "/api/migration":
                plan_id = parse_qs(urlsplit(self.path).query).get("plan_id", [None])[0]
                return self._json(
                    200,
                    self.migration_service.status(plan_id or self._active_plan()),
                )
            if path == "/api/migration/progress":
                return self._json(200, self.migration_service.progress())
            if path == "/api/catalog":
                query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
                return self._json(200, {"results": self.service.search_catalog(query)})
            return self._json(404, {"error": "rota não encontrada"})
        except (SetupError, MigrationError) as exc:
            return self._json(400, exc.as_dict())
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                return self._json(413, {"error": "payload muito grande"})
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = urlsplit(self.path).path
            if path == "/api/match":
                return self._json(200, self.service.decide_match(payload))
            if path == "/api/match/batch":
                return self._json(200, self.service.decide_match_batch(payload))
            if path == "/api/liked":
                return self._json(200, self.service.decide_liked(payload))
            if path == "/api/liked/batch":
                return self._json(200, self.service.decide_liked_batch(payload))
            if path == "/api/review/reconcile":
                return self._json(
                    200,
                    self.service.rebuild_evaluation_matches(
                        confirm=str(payload.get("confirm") or "")
                    ),
                )
            if path == "/api/review":
                return self._json(200, self.service.decide_review(payload))
            if path == "/api/wizard/select":
                return self._json(200, self._select(payload))
            if path == "/api/jobs":
                return self._json(200, {"job": self._start_job(payload)})
            if path == "/api/prepare/start":
                return self._json(
                    200,
                    {
                        "started": True,
                        "job": self._start_job(
                            {
                                "kind": "prepare_all",
                                "params": {"apply_policy": bool(payload.get("apply_policy"))},
                            }
                        ),
                    },
                )
            if path == "/api/migration/ratify":
                plan_id = str(payload.get("plan_id") or self._active_plan())
                if not plan_id:
                    raise SetupError("plano ativo ausente", code="plan_missing")
                if str(payload.get("confirm") or "") != plan_id:
                    raise SetupError(
                        "confirmação deve ser exatamente o plan_id",
                        code="invalid_confirmation",
                    )
                result = self.service.ratify_automatic(plan_id)
                if result.get("plan_id"):
                    state = self._state()
                    state.active_plan_id = result["plan_id"]
                    state.active_execution_id = None
                    state.save()
                return self._json(200, result)
            if path == "/api/migration/approve-and-prepare":
                return self._json(
                    200,
                    {
                        "started": True,
                        "job": self._start_job(
                            {
                                "kind": "approve_and_prepare",
                                "params": {
                                    "plan_id": payload.get("plan_id"),
                                    "confirm": payload.get("confirm"),
                                },
                            }
                        ),
                    },
                )
            if path == "/api/jobs/cancel":
                return self._json(200, self._jobs().cancel())
            if path == "/api/policy":
                if str(payload.get("confirm") or "") != "apply":
                    raise SetupError(
                        "confirmação obrigatória para aplicar a política automática",
                        code="policy_confirmation_required",
                    )
                return self._json(200, self.service.apply_automatic_policy())
            parts = path.strip("/").split("/")
            if (
                len(parts) == 4
                and parts[0] == "api"
                and parts[1] == "executions"
                and parts[3] == "reconcile"
            ):
                migration = getattr(self, "migration_service", None)
                if migration is None:
                    raise ReviewUIError("serviço de migração indisponível")
                return self._json(200, migration.reconcile(parts[2]))
            if path == "/api/migration/approve":
                return self._json(
                    200,
                    self.migration_service.approve(
                        plan_id=str(payload.get("plan_id") or self._active_plan()),
                        confirm=str(payload.get("confirm") or ""),
                    ),
                )
            if path == "/api/migration/preflight":
                plan_id = str(payload.get("plan_id") or self._active_plan())
                job = self._start_job(
                    {
                        "kind": "preflight",
                        "params": {
                            "plan_id": plan_id,
                            "preflight_id": payload.get("preflight_id"),
                            "confirm": str(payload.get("confirm") or plan_id),
                        },
                    }
                )
                return self._json(200, {"started": True, "job": job})
            if path == "/api/migration/prepare":
                return self._json(
                    200,
                    self.migration_service.prepare(
                        plan_id=str(payload.get("plan_id") or self._active_plan()),
                        preflight_id=payload.get("preflight_id") or None,
                        execution_id=payload.get("execution_id") or None,
                        confirm=str(payload.get("confirm") or ""),
                    ),
                )
            if path == "/api/migration/execute":
                gate = self._gate()
                if not gate.acquire():
                    return self._json(
                        409,
                        {"error": "outra operação está em andamento", "code": "operation_busy"},
                    )
                self.migration_service.on_finished = gate.release
                try:
                    result = self.migration_service.start_execution(
                        execution_id=str(payload.get("execution_id") or ""),
                        plan_id=str(payload.get("plan_id") or self._active_plan()),
                        confirm=str(payload.get("confirm") or ""),
                    )
                except Exception:
                    gate.release()
                    raise
                return self._json(200, result)
            if path == "/api/migration/cancel":
                return self._json(
                    200,
                    self.migration_service.cancel(
                        execution_id=str(payload.get("execution_id") or ""),
                        plan_id=str(payload.get("plan_id") or self._active_plan()),
                        confirm=str(payload.get("confirm") or ""),
                    ),
                )
            if path == "/api/migration/resume":
                gate = self._gate()
                if not gate.acquire():
                    return self._json(
                        409,
                        {"error": "outra operação está em andamento", "code": "operation_busy"},
                    )
                self.migration_service.on_finished = gate.release
                try:
                    result = self.migration_service.resume(
                        execution_id=str(payload.get("execution_id") or ""),
                        plan_id=str(payload.get("plan_id") or self._active_plan()),
                        confirm=str(payload.get("confirm") or ""),
                    )
                except Exception:
                    gate.release()
                    raise
                return self._json(200, result)
            if path == "/api/plan":
                result = self.service.generate_plan()
                state = self._state()
                state.active_plan_id = result["plan_id"]
                state.active_execution_id = None
                state.save()
                return self._json(
                    200,
                    {
                        **result,
                        "active_plan_id": result["plan_id"],
                        "activated": True,
                    },
                )
            return self._json(404, {"error": "rota não encontrada"})
        except JobBusy as exc:
            return self._json(409, {"error": str(exc), "code": "operation_busy"})
        except (SetupError, MigrationError) as exc:
            return self._json(400, exc.as_dict())
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": str(exc)})


def prepare_interface(
    config: Config,
    paths: ReviewPaths | None = None,
) -> tuple[ReviewService, WizardState, MigrationService]:
    """Monta o estado da UI sem aplicar política nem criar artefatos."""
    service = ReviewService(config, paths)
    state = WizardState.load(config)
    apply_state_to_service(service, state, config)
    with contextlib.suppress(Exception):  # data/ vazio após reset; a UI guia a coleta
        service.state()
    migration = MigrationService(
        config,
        approvals=config.migration_dir / "approvals.jsonl",
        headless=False,
    )
    return service, state, migration


def run_review_ui(
    config: Config,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    paths: ReviewPaths | None = None,
) -> None:
    service, state, migration = prepare_interface(config, paths)
    gate = OperationGate()
    jobs = JobRunner(gate)

    class Handler(ReviewUIHandler):
        pass

    Handler.service = service
    Handler.wizard_state = state
    Handler.job_runner = jobs
    Handler.migration_service = migration
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"interface de revisão: {url}")
    print(
        "Ctrl+C para encerrar. A política automática só roda por ação explícita; "
        "escritas no Já Zerei exigem os dois gates de ambiente."
    )
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrando interface")
    finally:
        server.server_close()
