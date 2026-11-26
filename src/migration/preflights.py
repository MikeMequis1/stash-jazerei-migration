"""Persistência versionada de resultados de preflight."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.config import Config
from src.migration.approval import canonical_hash
from src.migration.preflight import SnapshotEvidence
from src.models.preflight import PreflightResult
from src.storage import read_json, sha256_file, utc_now, verify_manifest, write_json, write_manifest

PREFLIGHT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PreflightStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreflightArtifact:
    preflight_id: str
    run_dir: Path
    result_path: Path
    manifest_path: Path


def allocate_preflight_id(
    config: Config,
    *,
    plan_id: str,
    preflight_id: str | None = None,
) -> str:
    if preflight_id:
        if preflight_id in {".", ".."} or not PREFLIGHT_ID_RE.fullmatch(preflight_id):
            raise PreflightStorageError("preflight_id invalido")
        if (config.migration_dir / "preflights" / preflight_id).exists():
            raise FileExistsError(f"preflight ja existe: {preflight_id}")
        return preflight_id
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-preflight-{plan_id}"
    candidate = base
    index = 2
    while (config.migration_dir / "preflights" / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def write_preflight_run(
    config: Config,
    result: PreflightResult,
    snapshots: dict[int, SnapshotEvidence],
    *,
    preflight_id: str,
) -> PreflightArtifact:
    if result.preflight_id != preflight_id:
        raise PreflightStorageError("preflight_id do resultado diverge")
    target = config.migration_dir / "preflights" / preflight_id
    if target.exists():
        raise FileExistsError(f"preflight ja existe: {target}")
    work = config.migration_dir / "preflights" / f".{preflight_id}.staging"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        metadata: list[dict[str, object]] = []
        for game_id, evidence in snapshots.items():
            relative = evidence.ref
            path = work / relative
            write_json(path, evidence.snapshot)
            metadata.append(
                {
                    "game_id": game_id,
                    "path": relative,
                    "sha256": sha256_file(path),
                    "response_hash": evidence.snapshot.capture.response_hash,
                    "exists": evidence.snapshot.result.exists,
                    "user_id": evidence.snapshot.identity.user_id,
                }
            )
        result.snapshots = metadata
        result.result_sha256 = canonical_hash(
            result.model_dump(mode="json", exclude={"captured_at", "result_sha256"})
        )
        write_json(work / "preflight.json", result)
        write_manifest(
            work,
            {
                "schema_version": 1,
                "preflight_id": preflight_id,
                "plan_id": result.plan_id,
                "created_at": utc_now(),
                "outcome": result.result.status.value,
                "approval_id": result.approval_id,
                "approval_status": result.approval_status,
                "captured_at": result.captured_at,
                "preflight_sha256": result.result_sha256,
                "valid_positive": result.result.status.value == "compatible",
            },
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        work.rename(target)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return PreflightArtifact(
        preflight_id=preflight_id,
        run_dir=target,
        result_path=target / "preflight.json",
        manifest_path=target / "manifest.json",
    )


def load_preflight(config: Config, preflight_id: str) -> PreflightResult:
    path = config.migration_dir / "preflights" / preflight_id / "preflight.json"
    if not path.is_file():
        raise PreflightStorageError(f"preflight ausente: {preflight_id}")
    try:
        return PreflightResult.model_validate(read_json(path))
    except Exception as exc:  # noqa: BLE001
        raise PreflightStorageError("preflight.json invalido") from exc


def preflight_manifest_sha256(config: Config, preflight_id: str) -> str:
    path = config.migration_dir / "preflights" / preflight_id / "manifest.json"
    if not path.is_file():
        raise PreflightStorageError(f"manifesto de preflight ausente: {preflight_id}")
    return sha256_file(path)


def verify_preflight(config: Config, preflight_id: str) -> list[str]:
    root = config.migration_dir / "preflights" / preflight_id
    errors = verify_manifest(root)
    if errors:
        return errors
    try:
        result = load_preflight(config, preflight_id)
    except PreflightStorageError as exc:
        return [str(exc)]
    expected = canonical_hash(
        result.model_dump(mode="json", exclude={"captured_at", "result_sha256"})
    )
    if result.result_sha256 != expected:
        errors.append("result_sha256 divergente")
    for entry in result.snapshots:
        path = root / str(entry.get("path"))
        if not path.is_file():
            errors.append(f"snapshot ausente: {entry.get('path')}")
        elif sha256_file(path) != entry.get("sha256"):
            errors.append(f"snapshot alterado: {entry.get('path')}")
    return errors
