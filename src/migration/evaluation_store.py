"""Persistencia versionada do conjunto de avaliacoes e do matching read-only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.config import Config
from src.migration.evaluation_matching import EvaluationMatchReport, EvaluationMatchResult
from src.migration.evaluations import EvaluationSet
from src.models.normalized import GameMatch
from src.storage import (
    build_manifest,
    read_json,
    sha256_file,
    utc_now,
    verify_manifest,
    write_json,
    write_manifest,
)

MANIFEST_KIND = "jazerei-evaluation-set"
MATCHES_FILE = "matches.json"
EVALUATIONS_FILE = "evaluations.json"
REPORT_FILE = "match-report.json"
CACHE_FILE = "catalog-cache.jsonl"


class EvaluationStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationRun:
    run_id: str
    run_dir: Path

    @property
    def evaluations_path(self) -> Path:
        return self.run_dir / EVALUATIONS_FILE

    @property
    def matches_path(self) -> Path:
        return self.run_dir / MATCHES_FILE

    @property
    def report_path(self) -> Path:
        return self.run_dir / REPORT_FILE

    @property
    def cache_path(self) -> Path:
        return self.run_dir / CACHE_FILE


def allocate_run_id(config: Config, run_id: str | None = None) -> str:
    root = config.migration_dir / "evaluations"
    if run_id:
        if (root / run_id).exists():
            raise FileExistsError(f"run de avaliacoes ja existe: {root / run_id}")
        return run_id
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-evaluations"
    candidate = base
    index = 2
    while (root / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def create_run(config: Config, run_id: str | None = None) -> EvaluationRun:
    resolved = allocate_run_id(config, run_id)
    run_dir = config.migration_dir / "evaluations" / resolved
    run_dir.mkdir(parents=True, exist_ok=False)
    return EvaluationRun(run_id=resolved, run_dir=run_dir)


def populate_run(
    run: EvaluationRun,
    *,
    evaluation_set: EvaluationSet,
    match_result: EvaluationMatchResult,
    source_manifest_sha256: str,
) -> EvaluationRun:
    write_json(run.evaluations_path, evaluation_set)
    write_json(run.matches_path, match_result.matches)
    write_json(run.report_path, match_result.report)
    write_manifest(
        run.run_dir,
        {
            "schema_version": 1,
            "kind": MANIFEST_KIND,
            "run_id": run.run_id,
            "created_at": utc_now(),
            "source_run_id": evaluation_set.source_run_id,
            "source_manifest_sha256": source_manifest_sha256,
            "counts": evaluation_set.counts.model_dump(mode="json"),
            "match_counts": match_result.report.counts,
            "evaluations_file": EVALUATIONS_FILE,
            "matches_file": MATCHES_FILE,
            "report_file": REPORT_FILE,
            "generated_at": evaluation_set.generated_at,
        },
    )
    return run


def write_evaluation_run(
    config: Config,
    *,
    evaluation_set: EvaluationSet,
    match_result: EvaluationMatchResult,
    source_manifest_sha256: str,
    run_id: str | None = None,
) -> EvaluationRun:
    run = create_run(config, run_id)
    return populate_run(
        run,
        evaluation_set=evaluation_set,
        match_result=match_result,
        source_manifest_sha256=source_manifest_sha256,
    )


def latest_evaluation_run(config: Config) -> EvaluationRun | None:
    root = config.migration_dir / "evaluations"
    if not root.is_dir():
        return None
    candidates: list[EvaluationRun] = []
    for run_dir in root.iterdir():
        manifest = run_dir / "manifest.json"
        if not run_dir.is_dir() or not manifest.is_file():
            continue
        try:
            payload = read_json(manifest)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(payload, dict) and payload.get("kind") == MANIFEST_KIND:
            candidates.append(EvaluationRun(str(payload.get("run_id") or run_dir.name), run_dir))
    return max(candidates, key=lambda run: run.run_dir.stat().st_mtime) if candidates else None


def load_evaluation_run(
    run: EvaluationRun,
) -> tuple[EvaluationSet, list[GameMatch], EvaluationMatchReport]:
    errors = verify_manifest(run.run_dir)
    if errors:
        raise EvaluationStoreError("; ".join(errors))
    evaluation_set = EvaluationSet.model_validate(read_json(run.evaluations_path))
    matches = [GameMatch.model_validate(item) for item in read_json(run.matches_path)]
    report = EvaluationMatchReport.model_validate(read_json(run.report_path))
    return evaluation_set, matches, report


def save_match(run: EvaluationRun, match: GameMatch) -> str:
    """Atualiza uma correspondencia no run de avaliacoes, sem tocar no historico."""
    matches = [GameMatch.model_validate(item) for item in read_json(run.matches_path)]
    replaced = False
    for index, current in enumerate(matches):
        if current.source_slug == match.source_slug:
            matches[index] = match
            replaced = True
            break
    if not replaced:
        matches.append(match)
    write_json(run.matches_path, matches, overwrite=True)
    manifest = read_json(run.run_dir / "manifest.json")
    metadata = {key: value for key, value in manifest.items() if key != "files"}
    write_manifest(run.run_dir, metadata, overwrite=True)
    return sha256_file(run.matches_path)


def matches_sha256(run: EvaluationRun) -> str:
    return sha256_file(run.matches_path)


def refresh_manifest(run: EvaluationRun) -> None:
    """Inclui arquivos novos, como o historico de correspondencias."""
    manifest = read_json(run.run_dir / "manifest.json")
    write_manifest(
        run.run_dir,
        {key: value for key, value in manifest.items() if key != "files"},
        overwrite=True,
    )


def build_run_manifest_preview(run: EvaluationRun) -> dict:
    return build_manifest(
        run.run_dir,
        {
            "schema_version": 1,
            "kind": MANIFEST_KIND,
            "run_id": run.run_id,
            "created_at": utc_now(),
        },
    )
