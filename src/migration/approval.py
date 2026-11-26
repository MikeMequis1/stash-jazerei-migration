"""Manifesto canônico de plano e aprovação humana auditável."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import Config
from src.migration.validator import ValidationIssue, validate_plan
from src.models.approval import (
    ApprovalCheck,
    ApprovalRecord,
    ApprovalStatus,
    PlanManifest,
)
from src.models.destination import DestinationReview
from src.models.normalized import MigrationAction, MigrationPlan
from src.models.snapshot import AccountReviewsSnapshot, ReviewStateSnapshot
from src.storage import read_json, sha256_file, utc_now, verify_manifest, write_json


class ApprovalError(RuntimeError):
    pass


class BundleError(ApprovalError):
    pass


@dataclass
class PlanBundle:
    plan_id: str
    plan_dir: Path
    plan: MigrationPlan
    manifest: PlanManifest
    destination_reviews: list[DestinationReview]


@dataclass
class BundleVerification:
    invalid: list[str]
    stale: list[str]
    destination_reviews: list[DestinationReview]


@dataclass
class SourceProvenance:
    data: dict[str, Any]
    complete: bool


def canonical_json(value: Any) -> str:
    """JSON determinístico: chaves ordenadas, sem espaços, Unicode preservado."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def action_content(action: MigrationAction) -> dict[str, Any]:
    """Conteúdo lógico da ação; exclui campos de aprovação/execução."""
    content = {
        "kind": action.kind,
        "source_ref": action.source_ref,
        "source_payload": action.source_payload,
        "game_id": action.game_id,
        "before": action.before,
        "after": action.after,
        "liked": action.liked.model_dump(mode="json"),
    }
    if action.destination_slug is not None:
        content["destination_slug"] = action.destination_slug
    return content


def action_hash(action: MigrationAction) -> str:
    return canonical_hash(action_content(action))


def plan_actions_payload(plan: MigrationPlan) -> list[dict[str, Any]]:
    return [
        {"action_id": action_hash(action), "content": action_content(action)}
        for action in plan.actions
    ]


def plan_summary(plan: MigrationPlan) -> dict[str, Any]:
    actions = [
        {
            "action_id": action_hash(action),
            "kind": action.kind,
            "source_ref": action.source_ref,
            "game_id": action.game_id,
            "hash": action_hash(action),
        }
        for action in plan.actions
    ]
    return {
        "plan_version": plan.plan_version,
        "action_count": len(actions),
        "actions_sha256": canonical_hash([action["action_id"] for action in actions]),
        "actions": actions,
    }


def plan_payload(
    plan: MigrationPlan,
    *,
    source: dict[str, Any],
    destination: dict[str, Any],
    matching: dict[str, Any],
    decisions: dict[str, Any],
    provenance_complete: bool,
) -> dict[str, Any]:
    return {
        "source": source,
        "destination": destination,
        "matching": matching,
        "decisions": decisions,
        "provenance_complete": provenance_complete,
        "plan_version": plan.plan_version,
        "plan_id": plan.plan_id,
        "unmatched": sorted(plan.unmatched),
        "skipped": sorted(plan.skipped, key=canonical_json),
        "actions": plan_actions_payload(plan),
    }


def build_manifest(
    plan: MigrationPlan,
    *,
    plan_id: str,
    source: dict[str, Any],
    destination: dict[str, Any],
    matching: dict[str, Any],
    decisions: dict[str, Any],
    inputs: dict[str, str],
    provenance_complete: bool,
    plan_file_sha256: str,
    created_at: str | None = None,
) -> PlanManifest:
    payload = plan_payload(
        plan,
        source=source,
        destination=destination,
        matching=matching,
        decisions=decisions,
        provenance_complete=provenance_complete,
    )
    plan_sha256 = canonical_hash(payload)
    content = {
        "schema_version": 1,
        "plan_id": plan_id,
        "source": source,
        "destination": destination,
        "matching": matching,
        "decisions": decisions,
        "plan": plan_summary(plan),
        "provenance_complete": provenance_complete,
        "plan_sha256": plan_sha256,
        "plan_file_sha256": plan_file_sha256,
    }
    manifest_sha256 = canonical_hash(content)
    return PlanManifest(
        plan_id=plan_id,
        created_at=created_at or utc_now(),
        source=source,
        destination=destination,
        matching=matching,
        decisions=decisions,
        plan=plan_summary(plan),
        inputs=inputs,
        provenance_complete=provenance_complete,
        plan_sha256=plan_sha256,
        plan_file_sha256=plan_file_sha256,
        manifest_sha256=manifest_sha256,
    )


def manifest_hash(manifest: PlanManifest) -> str:
    content = manifest.model_dump(
        mode="json",
        exclude={"created_at", "manifest_sha256", "inputs"},
    )
    return canonical_hash(content)


def source_provenance(config: Config, games_path: Path, reviews_path: Path) -> SourceProvenance:
    issues: list[str] = []
    if not games_path.is_file() or not reviews_path.is_file():
        issues.append("arquivo de origem ausente")
        return SourceProvenance({"complete": False, "issues": issues}, False)
    if games_path.parent != reviews_path.parent:
        issues.append("games.json e reviews.json estao em runs diferentes")
        return SourceProvenance({"complete": False, "issues": issues}, False)

    run_dir = games_path.parent
    manifest_path = run_dir / "manifest.json"
    data: dict[str, Any] = {
        "run_id": run_dir.name,
        "manifest_sha256": None,
        "files": {},
        "complete": False,
        "issues": issues,
    }
    if not manifest_path.is_file():
        issues.append("manifesto de origem ausente")
        return SourceProvenance(data, False)

    issues.extend(verify_manifest(run_dir))
    manifest = read_json(manifest_path)
    run_id = manifest.get("run_id") if isinstance(manifest, dict) else None
    data["run_id"] = run_id or run_dir.name
    data["manifest_sha256"] = sha256_file(manifest_path)
    listed = {entry.get("path") for entry in manifest.get("files", []) if isinstance(entry, dict)}
    for path in (games_path, reviews_path):
        relative = path.relative_to(run_dir).as_posix()
        if relative not in listed:
            issues.append(f"arquivo nao listado no manifesto de origem: {relative}")
        data["files"][relative] = sha256_file(path)
    data["complete"] = not issues
    data["issues"] = issues
    return SourceProvenance(data, not issues)


def destination_provenance(
    config: Config, snapshot_path: Path
) -> tuple[dict[str, Any], ReviewStateSnapshot | AccountReviewsSnapshot | None]:
    issues: list[str] = []
    data: dict[str, Any] = {
        "snapshot_run_id": None,
        "manifest_sha256": None,
        "snapshot_file": None,
        "snapshot_sha256": None,
        "response_hash": None,
        "user_id": None,
        "game_id": None,
        "slug": None,
        "exists": None,
        "complete": False,
        "issues": issues,
    }
    if not snapshot_path.is_file():
        issues.append("snapshot de destino ausente")
        return data, None
    payload = read_json(snapshot_path)
    is_account = (
        isinstance(payload, dict)
        and isinstance(payload.get("query"), dict)
        and payload["query"].get("type") == "account-review-state"
    )
    try:
        snapshot = (
            AccountReviewsSnapshot.model_validate(payload)
            if is_account
            else ReviewStateSnapshot.model_validate(payload)
        )
    except Exception as exc:  # noqa: BLE001
        issues.append(f"snapshot invalido: {type(exc).__name__}")
        return data, None

    run_dir = (
        snapshot_path.parent.parent
        if snapshot_path.parent.name == "review-state"
        else snapshot_path.parent
    )
    manifest_path = run_dir / "manifest.json"
    data.update(
        {
            "snapshot_run_id": run_dir.name,
            "snapshot_file": snapshot_path.relative_to(run_dir).as_posix()
            if snapshot_path.is_relative_to(run_dir)
            else snapshot_path.name,
            "snapshot_sha256": sha256_file(snapshot_path),
            "response_hash": snapshot.capture.response_hash,
            "user_id": snapshot.identity.user_id,
            "kind": "account" if is_account else "game",
        }
    )
    if is_account:
        data["review_count"] = len(snapshot.reviews)
        data["game_id"] = None
        data["slug"] = None
        data["exists"] = None
    else:
        data["game_id"] = snapshot.query.game_id
        data["slug"] = snapshot.query.slug
        data["exists"] = snapshot.result.exists
    if not manifest_path.is_file():
        issues.append("manifesto do snapshot ausente")
    else:
        issues.extend(verify_manifest(run_dir))
        manifest = read_json(manifest_path)
        data["snapshot_run_id"] = (
            manifest.get("run_id") if isinstance(manifest, dict) else run_dir.name
        ) or run_dir.name
        data["manifest_sha256"] = sha256_file(manifest_path)
    if not snapshot.capture.complete or not snapshot.identity.verified:
        issues.append("snapshot incompleto ou identidade nao verificada")
    if not snapshot.pagination.complete:
        issues.append("paginacao do snapshot incompleta")
    data["complete"] = not issues
    data["issues"] = issues
    return data, snapshot


def legacy_destination_provenance(path: Path | None) -> dict[str, Any]:
    return {
        "snapshot_run_id": None,
        "manifest_sha256": None,
        "snapshot_file": None,
        "snapshot_sha256": None,
        "response_hash": None,
        "user_id": None,
        "game_id": None,
        "slug": None,
        "exists": None,
        "complete": False,
        "issues": ["destino legado sem snapshot versionado"],
        "legacy_file": str(path) if path else None,
    }


def matching_provenance(path: Path) -> dict[str, Any]:
    issues: list[str] = []
    if not path.is_file():
        issues.append("arquivo de correspondencias ausente")
        return {
            "matches_sha256": None,
            "matcher_version": "1",
            "complete": False,
            "issues": issues,
        }
    return {
        "matches_sha256": sha256_file(path),
        "matcher_version": "1",
        "complete": True,
        "issues": issues,
    }


def decisions_provenance(path: Path, plan: MigrationPlan) -> dict[str, Any]:
    used = [
        action.liked.model_dump(mode="json")
        for action in plan.actions
        if action.kind == "review" and action.liked.decision_id
    ]
    exists = path.is_file()
    issues: list[str] = []
    if used and not exists:
        issues.append("decisoes de liked usadas mas arquivo ausente")
    return {
        "decisions_sha256": sha256_file(path) if exists else None,
        "used": used,
        "complete": not issues,
        "issues": issues,
    }


def build_bundle(
    config: Config,
    *,
    plan: MigrationPlan,
    plan_id: str,
    source: dict[str, Any],
    destination: dict[str, Any],
    matching: dict[str, Any],
    decisions: dict[str, Any],
    inputs: dict[str, str],
    provenance_complete: bool,
) -> tuple[Path, PlanManifest]:
    plan_dir = config.migration_dir / "plans" / plan_id
    if plan_dir.exists():
        raise FileExistsError(f"plano ja existe: {plan_dir}")
    plan.plan_id = plan_id
    plan_path = write_json(plan_dir / "plan.json", plan)
    plan_file_sha256 = sha256_file(plan_path)
    manifest = build_manifest(
        plan,
        plan_id=plan_id,
        source=source,
        destination=destination,
        matching=matching,
        decisions=decisions,
        inputs=inputs,
        provenance_complete=provenance_complete,
        plan_file_sha256=plan_file_sha256,
    )
    write_json(plan_dir / "manifest.json", manifest)
    return plan_dir, manifest


def load_bundle(
    config: Config, plan_id: str, *, destination_reviews: list[DestinationReview] | None = None
) -> PlanBundle:
    plan_dir = config.migration_dir / "plans" / plan_id
    plan_path = plan_dir / "plan.json"
    manifest_path = plan_dir / "manifest.json"
    if not plan_path.is_file() or not manifest_path.is_file():
        raise BundleError(f"plano ou manifesto ausente: {plan_dir}")
    try:
        plan = MigrationPlan.model_validate(read_json(plan_path))
        manifest = PlanManifest.model_validate(read_json(manifest_path))
    except Exception as exc:  # noqa: BLE001
        raise BundleError(f"plano/manifesto invalido: {type(exc).__name__}") from exc
    if manifest.plan_id != plan_id or plan.plan_id not in (None, plan_id):
        raise BundleError("plan_id de plano e manifesto nao coincide")
    if destination_reviews is None:
        destination_reviews = _destination_reviews_from_manifest(config, manifest)
    return PlanBundle(plan_id, plan_dir, plan, manifest, destination_reviews)


def _destination_reviews_from_manifest(
    config: Config, manifest: PlanManifest
) -> list[DestinationReview]:
    inputs = manifest.inputs
    snapshot_path = inputs.get("snapshot")
    if snapshot_path:
        path = Path(snapshot_path)
        if path.is_file():
            try:
                snapshot = ReviewStateSnapshot.model_validate(read_json(path))
                return [snapshot.result.review] if snapshot.result.review else []
            except Exception:  # noqa: BLE001
                return []
    reviews_path = inputs.get("destination_reviews")
    if reviews_path and Path(reviews_path).is_file():
        try:
            payload = read_json(Path(reviews_path))
            return [DestinationReview.model_validate(item) for item in payload]
        except Exception:  # noqa: BLE001
            return []
    return []


def verify_bundle(config: Config, bundle: PlanBundle) -> BundleVerification:
    invalid: list[str] = []
    stale: list[str] = []
    manifest = bundle.manifest
    if manifest.schema_version != 1:
        invalid.append(f"schema de manifesto desconhecido: {manifest.schema_version}")
    if manifest_hash(manifest) != manifest.manifest_sha256:
        invalid.append("hash canonico do manifesto divergente")
    if sha256_file(bundle.plan_dir / "plan.json") != manifest.plan_file_sha256:
        stale.append("arquivo plan.json mudou desde o manifesto")
    expected_plan_hash = canonical_hash(
        plan_payload(
            bundle.plan,
            source=manifest.source,
            destination=manifest.destination,
            matching=manifest.matching,
            decisions=manifest.decisions,
            provenance_complete=manifest.provenance_complete,
        )
    )
    if expected_plan_hash != manifest.plan_sha256:
        stale.append("hash canonico do plano divergente")
    expected_actions = plan_summary(bundle.plan)
    if expected_actions["actions"] != manifest.plan.get("actions"):
        stale.append("acoes do plano divergiram do manifesto")
    if expected_actions["actions_sha256"] != manifest.plan.get("actions_sha256"):
        stale.append("hash das acoes divergiu do manifesto")
    if not manifest.provenance_complete:
        invalid.append("proveniencia incompleta")

    _verify_source(config, manifest, invalid, stale)
    _verify_destination(config, manifest, invalid, stale)
    _verify_external_file(
        manifest.inputs.get("matches"),
        manifest.matching.get("matches_sha256"),
        "correspondencias",
        invalid,
        stale,
    )
    _verify_external_file(
        manifest.inputs.get("decisions"),
        manifest.decisions.get("decisions_sha256"),
        "decisoes liked",
        invalid,
        stale,
    )
    return BundleVerification(invalid, stale, bundle.destination_reviews)


def _verify_source(
    config: Config, manifest: PlanManifest, invalid: list[str], stale: list[str]
) -> None:
    run_id = manifest.source.get("run_id")
    if not run_id:
        invalid.append("run de origem ausente no manifesto")
        return
    run_dir = config.stash_dir / "runs" / str(run_id)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        invalid.append(f"manifesto de origem ausente: {run_id}")
        return
    if sha256_file(manifest_path) != manifest.source.get("manifest_sha256"):
        stale.append("manifesto de origem mudou")
    errors = verify_manifest(run_dir)
    if errors:
        stale.extend(f"origem: {error}" for error in errors)
    for relative, expected in manifest.source.get("files", {}).items():
        path = run_dir / relative
        if not path.is_file():
            invalid.append(f"arquivo de origem ausente: {relative}")
        elif sha256_file(path) != expected:
            stale.append(f"arquivo de origem mudou: {relative}")


def _verify_destination(
    config: Config, manifest: PlanManifest, invalid: list[str], stale: list[str]
) -> None:
    run_id = manifest.destination.get("snapshot_run_id")
    if not run_id:
        invalid.append("snapshot de destino ausente no manifesto")
        return
    run_dir = config.jazerei_dir / "runs" / str(run_id)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        invalid.append(f"manifesto de snapshot ausente: {run_id}")
        return
    if sha256_file(manifest_path) != manifest.destination.get("manifest_sha256"):
        stale.append("manifesto de snapshot mudou")
    errors = verify_manifest(run_dir)
    if errors:
        stale.extend(f"destino: {error}" for error in errors)
    relative = manifest.destination.get("snapshot_file")
    if not relative:
        invalid.append("arquivo de snapshot ausente no manifesto")
        return
    path = run_dir / str(relative)
    if not path.is_file():
        invalid.append(f"snapshot ausente: {relative}")
        return
    if sha256_file(path) != manifest.destination.get("snapshot_sha256"):
        stale.append("snapshot de destino mudou")
    try:
        payload = read_json(path)
        is_account = (
            isinstance(payload, dict)
            and isinstance(payload.get("query"), dict)
            and payload["query"].get("type") == "account-review-state"
        )
        snapshot = (
            AccountReviewsSnapshot.model_validate(payload)
            if is_account
            else ReviewStateSnapshot.model_validate(payload)
        )
    except Exception:  # noqa: BLE001
        invalid.append("snapshot de destino ilegivel")
        return
    if snapshot.capture.response_hash != manifest.destination.get("response_hash"):
        stale.append("response_hash do snapshot mudou")
    if is_account:
        if len(snapshot.reviews) != manifest.destination.get("review_count"):
            stale.append("quantidade de reviews do snapshot mudou")
        if snapshot.identity.user_id != manifest.destination.get("user_id"):
            stale.append("usuario do snapshot mudou")
    elif snapshot.result.exists != manifest.destination.get("exists"):
        stale.append("existencia de review mudou")


def _verify_external_file(
    path_value: str | None,
    expected_hash: str | None,
    label: str,
    invalid: list[str],
    stale: list[str],
) -> None:
    if not path_value:
        if expected_hash:
            invalid.append(f"caminho de {label} ausente")
        return
    path = Path(path_value)
    if not path.is_file():
        if expected_hash is not None:
            invalid.append(f"arquivo de {label} ausente")
        return
    if expected_hash and sha256_file(path) != expected_hash:
        stale.append(f"arquivo de {label} mudou")


def _structural_issues(bundle: PlanBundle) -> list[ValidationIssue]:
    return [
        issue
        for issue in validate_plan(bundle.plan, bundle.destination_reviews, require_approval=False)
        if issue.severity.value == "error"
    ]


def _action_ids(bundle: PlanBundle) -> list[str]:
    return [action_hash(action) for action in bundle.plan.actions]


def load_approvals(path: Path) -> list[ApprovalRecord]:
    if not path.exists():
        return []
    records: list[ApprovalRecord] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = ApprovalRecord.model_validate(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            raise ApprovalError(f"aprovacao invalida na linha {line_number}") from exc
        if record.approval_id != compute_approval_id(record):
            raise ApprovalError(f"approval_id inconsistente na linha {line_number}")
        records.append(record)
    return records


def compute_approval_id(record: ApprovalRecord) -> str:
    content = record.model_dump(
        mode="json",
        exclude={"approval_id"},
    )
    return canonical_hash(content)


def append_approval(
    path: Path, *, plan_id: str, bundle: PlanBundle, approved_actions: list[str]
) -> ApprovalRecord:
    existing = load_approvals(path)
    for record in existing:
        if (
            record.plan_id == plan_id
            and record.manifest_sha256 == bundle.manifest.manifest_sha256
            and record.plan_sha256 == bundle.manifest.plan_sha256
        ):
            raise ApprovalError("este plano e manifesto ja possuem aprovacao")
    draft = ApprovalRecord(
        approval_id="pending",
        plan_id=plan_id,
        manifest_sha256=bundle.manifest.manifest_sha256,
        plan_sha256=bundle.manifest.plan_sha256,
        approved_at=utc_now(),
        approved_actions=approved_actions,
    )
    draft.approval_id = compute_approval_id(draft)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(draft.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return draft


def approval_status(config: Config, plan_id: str, approvals_path: Path) -> ApprovalCheck:
    try:
        bundle = load_bundle(config, plan_id)
    except BundleError as exc:
        return ApprovalCheck(status=ApprovalStatus.INVALID, reasons=[str(exc)])
    verification = verify_bundle(config, bundle)
    if verification.invalid:
        return ApprovalCheck(status=ApprovalStatus.INVALID, reasons=verification.invalid)
    if verification.stale:
        return ApprovalCheck(status=ApprovalStatus.STALE, reasons=verification.stale)
    issues = _structural_issues(bundle)
    if issues:
        return ApprovalCheck(
            status=ApprovalStatus.BLOCKED,
            reasons=[
                f"{issue.code}{f' [{issue.source_ref}]' if issue.source_ref else ''}: "
                f"{issue.message}"
                for issue in issues
            ],
        )
    try:
        records = load_approvals(approvals_path)
    except ApprovalError as exc:
        return ApprovalCheck(status=ApprovalStatus.INVALID, reasons=[str(exc)])
    matching = [record for record in records if record.plan_id == plan_id]
    if not matching:
        return ApprovalCheck(
            status=ApprovalStatus.UNAPPROVED, reasons=["nenhuma aprovacao registrada"]
        )
    latest = matching[-1]
    if (
        latest.manifest_sha256 != bundle.manifest.manifest_sha256
        or latest.plan_sha256 != bundle.manifest.plan_sha256
    ):
        return ApprovalCheck(
            status=ApprovalStatus.STALE,
            reasons=["aprovacao vinculada a outro manifesto/plano"],
            approval=latest,
        )
    if latest.approved_actions != _action_ids(bundle):
        return ApprovalCheck(
            status=ApprovalStatus.STALE,
            reasons=["acoes aprovadas divergem do plano atual"],
            approval=latest,
        )
    return ApprovalCheck(status=ApprovalStatus.APPROVED, approval=latest)


def approve_plan(
    config: Config,
    plan_id: str,
    approvals_path: Path,
) -> ApprovalRecord:
    """Cria aprovação somente para plano íntegro, completo e estruturalmente válido."""
    bundle = load_bundle(config, plan_id)
    verification = verify_bundle(config, bundle)
    if verification.invalid:
        raise ApprovalError("; ".join(verification.invalid))
    if verification.stale:
        raise ApprovalError("; ".join(verification.stale))
    issues = _structural_issues(bundle)
    if issues:
        raise ApprovalError(
            "; ".join(
                f"{issue.code}{f' [{issue.source_ref}]' if issue.source_ref else ''}: "
                f"{issue.message}"
                for issue in issues
            )
        )
    if not bundle.manifest.provenance_complete:
        raise ApprovalError("proveniencia incompleta; plano nao pode ser aprovado")
    return append_approval(
        approvals_path,
        plan_id=plan_id,
        bundle=bundle,
        approved_actions=_action_ids(bundle),
    )


def approval_summary(bundle: PlanBundle) -> dict[str, Any]:
    return {
        "plan_id": bundle.plan_id,
        "manifest_sha256": bundle.manifest.manifest_sha256,
        "plan_sha256": bundle.manifest.plan_sha256,
        "provenance_complete": bundle.manifest.provenance_complete,
        "actions": bundle.manifest.plan.get("actions", []),
        "actions_detail": [
            {
                "action_id": action_hash(action),
                "kind": action.kind,
                "source_ref": action.source_ref,
                "game_id": action.game_id,
                "before": action.before,
                "after": action.after,
                "liked": action.liked.model_dump(mode="json"),
            }
            for action in bundle.plan.actions
        ],
        "source": bundle.manifest.source,
        "destination": bundle.manifest.destination,
        "decisions": bundle.manifest.decisions,
    }
