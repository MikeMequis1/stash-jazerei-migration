"""Armazenamento versionado de snapshots do Já Zerei."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.config import Config
from src.models.snapshot import AccountReviewsSnapshot, ReviewStateSnapshot
from src.storage import utc_now, write_json, write_manifest

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SENSITIVE_MESSAGE_RE = re.compile(
    r"(?i)(bearer\s+)[^\s,;]+|((?:token|access_token|refresh_token|authorization|apikey|api_key|key)=)[^&\s,;]+"
)


class SnapshotStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotArtifact:
    run_id: str
    run_dir: Path
    snapshot_path: Path | None
    manifest_path: Path
    outcome: str


def _validate_run_id(run_id: str) -> None:
    if run_id in {".", ".."} or not RUN_ID_RE.fullmatch(run_id):
        raise SnapshotStorageError(
            "run_id aceita apenas letras, numeros, ponto, underscore e hifen, iniciando por alfanumerico"
        )


def allocate_run_id(
    config: Config,
    *,
    slug: str,
    game_id: int,
    run_id: str | None = None,
) -> str:
    if not RUN_ID_RE.fullmatch(slug) or slug in {".", ".."}:
        raise SnapshotStorageError("slug invalido para nome de run")
    if run_id:
        _validate_run_id(run_id)
        if (config.jazerei_dir / "runs" / run_id).exists():
            raise FileExistsError(
                f"snapshot run ja existe: {config.jazerei_dir / 'runs' / run_id}; "
                "gere uma nova execucao"
            )
        return run_id

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-review-state-{slug}-{game_id}"
    candidate = base
    index = 2
    while (config.jazerei_dir / "runs" / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def write_snapshot_run(
    config: Config,
    snapshot: ReviewStateSnapshot,
    *,
    run_id: str,
) -> SnapshotArtifact:
    _validate_run_id(run_id)
    if not RUN_ID_RE.fullmatch(snapshot.query.slug) or snapshot.query.slug in {".", ".."}:
        raise SnapshotStorageError("slug invalido para nome de snapshot")
    if snapshot.query.game_id < 1:
        raise SnapshotStorageError("game_id invalido")
    target = config.jazerei_dir / "runs" / run_id
    if target.exists():
        raise FileExistsError(f"snapshot run ja existe: {target}")
    work = _staging_dir(config, run_id)
    try:
        relative = Path("review-state") / f"{snapshot.query.slug}-{snapshot.query.game_id}.json"
        write_json(work / relative, snapshot)
        write_manifest(
            work,
            {
                "schema_version": 1,
                "run_id": run_id,
                "created_at": utc_now(),
                "kind": "jazerei-review-state",
                "outcome": "exists" if snapshot.result.exists else "absent",
                "game_id": snapshot.query.game_id,
                "slug": snapshot.query.slug,
                "user_id": snapshot.identity.user_id,
                "capture_complete": snapshot.capture.complete,
                "response_hash": snapshot.capture.response_hash,
                "snapshot_file": relative.as_posix(),
            },
        )
        _publish(work, target)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return SnapshotArtifact(
        run_id=run_id,
        run_dir=target,
        snapshot_path=target / relative,
        manifest_path=target / "manifest.json",
        outcome="exists" if snapshot.result.exists else "absent",
    )


def write_account_snapshot_run(
    config: Config,
    snapshot: AccountReviewsSnapshot,
    *,
    run_id: str,
) -> SnapshotArtifact:
    """Grava um snapshot completo da conta, sem sobrescrever runs anteriores."""
    _validate_run_id(run_id)
    target = config.jazerei_dir / "runs" / run_id
    if target.exists():
        raise FileExistsError(f"snapshot run ja existe: {target}")
    work = _staging_dir(config, run_id)
    try:
        relative = Path("review-state") / "account-reviews.json"
        write_json(work / relative, snapshot)
        write_manifest(
            work,
            {
                "schema_version": 1,
                "run_id": run_id,
                "created_at": utc_now(),
                "kind": "jazerei-account-review-state",
                "outcome": "complete" if snapshot.capture.complete else "incomplete",
                "user_id": snapshot.identity.user_id,
                "review_count": len(snapshot.reviews),
                "capture_complete": snapshot.capture.complete,
                "pagination_complete": snapshot.pagination.complete,
                "response_hash": snapshot.capture.response_hash,
                "snapshot_file": relative.as_posix(),
            },
        )
        _publish(work, target)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return SnapshotArtifact(
        run_id=run_id,
        run_dir=target,
        snapshot_path=target / relative,
        manifest_path=target / "manifest.json",
        outcome="complete" if snapshot.capture.complete else "incomplete",
    )


def safe_error_message(message: str) -> str:
    return SENSITIVE_MESSAGE_RE.sub(
        lambda match: (match.group(1) or match.group(2) or "") + "***", message
    )


def write_failure_run(
    config: Config,
    *,
    run_id: str,
    game_id: int,
    slug: str,
    expected_user_id: str,
    error_code: str,
    error_message: str,
) -> SnapshotArtifact:
    _validate_run_id(run_id)
    target = config.jazerei_dir / "runs" / run_id
    if target.exists():
        raise FileExistsError(f"snapshot run ja existe: {target}")
    work = _staging_dir(config, run_id)
    try:
        write_json(
            work / "capture-failure.json",
            {
                "schema_version": 1,
                "source": "jazerei",
                "status": "failed",
                "error_code": error_code,
                "error": safe_error_message(error_message),
                "game_id": game_id,
                "slug": slug,
                "expected_user_id": expected_user_id,
                "captured_at": utc_now(),
                "valid_snapshot": False,
            },
        )
        write_manifest(
            work,
            {
                "schema_version": 1,
                "run_id": run_id,
                "created_at": utc_now(),
                "kind": "jazerei-review-state-failure",
                "outcome": "failed",
                "game_id": game_id,
                "slug": slug,
                "error_code": error_code,
                "valid_snapshot": False,
            },
        )
        _publish(work, target)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return SnapshotArtifact(
        run_id=run_id,
        run_dir=target,
        snapshot_path=None,
        manifest_path=target / "manifest.json",
        outcome="failed",
    )


def _staging_dir(config: Config, run_id: str) -> Path:
    work = config.jazerei_dir / "runs" / f".{run_id}.staging"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    return work


def _publish(work: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    work.rename(target)
