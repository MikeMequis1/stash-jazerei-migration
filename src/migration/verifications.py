"""Persistência append-only dos relatórios de verificação."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.config import Config
from src.models.verification import VerificationReport
from src.storage import read_json, sha256_file, verify_manifest, write_json, write_manifest

VERIFICATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class VerificationStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationArtifact:
    verification_id: str
    run_dir: Path
    report_path: Path
    manifest_path: Path


def canonical_report_hash(report: VerificationReport) -> str:
    payload = report.model_dump(mode="json", exclude={"report_sha256"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def allocate_verification_id(
    config: Config,
    *,
    plan_id: str,
    execution_id: str,
    verification_id: str | None = None,
) -> str:
    if verification_id:
        if verification_id in {".", ".."} or not VERIFICATION_ID_RE.fullmatch(verification_id):
            raise VerificationStorageError("verification_id invalido")
        if (config.migration_dir / "verifications" / verification_id).exists():
            raise FileExistsError(f"verificacao ja existe: {verification_id}")
        return verification_id
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-verification-{execution_id}"
    candidate = base
    index = 2
    while (config.migration_dir / "verifications" / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def write_verification_run(
    config: Config,
    report: VerificationReport,
    *,
    verification_id: str,
) -> VerificationArtifact:
    if report.verification_id != verification_id:
        raise VerificationStorageError("verification_id do relatorio diverge")
    if not VERIFICATION_ID_RE.fullmatch(verification_id):
        raise VerificationStorageError("verification_id invalido")
    target = config.migration_dir / "verifications" / verification_id
    if target.exists():
        raise FileExistsError(f"verificacao ja existe: {target}")
    work = config.migration_dir / "verifications" / f".{verification_id}.staging"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        report.report_sha256 = canonical_report_hash(report)
        report_path = work / "report.json"
        write_json(report_path, report)
        write_manifest(
            work,
            {
                "schema_version": 1,
                "kind": "migration-verification",
                "verification_id": verification_id,
                "plan_id": report.plan_id,
                "execution_id": report.execution_id,
                "snapshot_run_id": report.snapshot_run_id,
                "created_at": report.created_at,
                "status": report.status,
                "report_sha256": report.report_sha256,
                "counts": report.counts.model_dump(mode="json"),
            },
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        work.rename(target)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return VerificationArtifact(
        verification_id=verification_id,
        run_dir=target,
        report_path=target / "report.json",
        manifest_path=target / "manifest.json",
    )


def load_verification(config: Config, verification_id: str) -> VerificationReport:
    if verification_id in {".", ".."} or not VERIFICATION_ID_RE.fullmatch(verification_id):
        raise VerificationStorageError("verification_id invalido")
    path = config.migration_dir / "verifications" / verification_id / "report.json"
    if not path.is_file():
        raise VerificationStorageError(f"verificacao ausente: {verification_id}")
    try:
        return VerificationReport.model_validate(read_json(path))
    except Exception as exc:  # noqa: BLE001
        raise VerificationStorageError("report.json invalido") from exc


def verify_verification(config: Config, verification_id: str) -> list[str]:
    root = config.migration_dir / "verifications" / verification_id
    errors = verify_manifest(root)
    if errors:
        return errors
    try:
        report = load_verification(config, verification_id)
    except VerificationStorageError as exc:
        return [str(exc)]
    if report.verification_id != verification_id:
        errors.append("verification_id divergente")
    if report.report_sha256 != canonical_report_hash(report):
        errors.append("report_sha256 divergente")
    return errors


def list_verifications(
    config: Config,
    *,
    plan_id: str | None = None,
    execution_id: str | None = None,
) -> list[dict[str, object]]:
    root = config.migration_dir / "verifications"
    if not root.is_dir():
        return []
    items: list[dict[str, object]] = []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        if not (path / "report.json").is_file():
            continue
        try:
            report = load_verification(config, path.name)
        except VerificationStorageError:
            continue
        if plan_id and report.plan_id != plan_id:
            continue
        if execution_id and report.execution_id != execution_id:
            continue
        items.append(
            {
                "verification_id": report.verification_id,
                "plan_id": report.plan_id,
                "execution_id": report.execution_id,
                "snapshot_run_id": report.snapshot_run_id,
                "created_at": report.created_at,
                "status": report.status,
                "capture_status": report.capture_status,
                "report_status": report.report_status,
                "migration_status": report.migration_status,
                "causal_confirmation_status": report.causal_confirmation_status,
                "plan_blocked": report.plan_blocked,
                "snapshot_complete": report.snapshot_complete,
                "counts": report.counts.model_dump(mode="json"),
                "valid": not verify_verification(config, report.verification_id),
                "manifest_sha256": sha256_file(path / "manifest.json"),
            }
        )
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return items


def latest_verification(
    config: Config,
    *,
    plan_id: str | None = None,
    execution_id: str | None = None,
) -> VerificationReport | None:
    items = list_verifications(config, plan_id=plan_id, execution_id=execution_id)
    if not items:
        return None
    return load_verification(config, str(items[0]["verification_id"]))
