"""Preflight autenticado: relê o destino e compara com o plano aprovado.

Somente leitura. Reutiliza a captura/identidade da Etapa 2 e a aprovação da
Etapa 4; não executa escrita nem altera aprovações/snapshots anteriores.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.browser.session import BrowserSession
from src.config import Config
from src.jazerei.state import (
    build_review_snapshot,
    capture_reviews_window,
)
from src.migration.approval import (
    PlanBundle,
    action_hash,
    approval_status,
    canonical_hash,
    load_bundle,
    verify_bundle,
)
from src.migration.validator import validate_plan
from src.models.approval import ApprovalCheck, ApprovalStatus
from src.models.normalized import MigrationAction
from src.models.preflight import (
    ActionCheck,
    ActionStatus,
    PreflightIdentity,
    PreflightResult,
    PreflightStatus,
    PreflightSummary,
)
from src.models.snapshot import ReviewStateSnapshot
from src.storage import utc_now

REVIEW_FIELDS = ("id", "game_id", "game_slug", "rating", "text", "liked")


class PreflightError(RuntimeError):
    pass


@dataclass
class PreflightPreparation:
    status: PreflightStatus
    reasons: list[str]
    bundle: PlanBundle | None = None
    approval: ApprovalCheck | None = None
    expected_user_id: str | None = None
    checks: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.bundle is not None and not self.reasons


@dataclass
class SnapshotEvidence:
    snapshot: ReviewStateSnapshot
    ref: str
    path_hash: str = ""


def prepare_preflight(config: Config, plan_id: str, approvals_path) -> PreflightPreparation:
    checks = {
        "source_integrity": "unknown",
        "plan_integrity": "unknown",
        "approval": "unknown",
        "destination_identity": "unknown",
        "destination_capture": "not_run",
    }
    reasons: list[str] = []
    try:
        bundle = load_bundle(config, plan_id)
    except Exception as exc:  # noqa: BLE001
        return PreflightPreparation(
            PreflightStatus.INVALID,
            [f"plano/manifesto invalido: {type(exc).__name__}: {exc}"],
            checks=checks,
        )

    verification = verify_bundle(config, bundle)
    if verification.invalid:
        checks["plan_integrity"] = "invalid"
        checks["source_integrity"] = "invalid"
        reasons.extend(verification.invalid)
    if verification.stale:
        checks["plan_integrity"] = "stale"
        checks["source_integrity"] = "stale"
        reasons.extend(verification.stale)
    if verification.invalid or verification.stale:
        return PreflightPreparation(
            PreflightStatus.INVALID,
            reasons,
            bundle=bundle,
            checks=checks,
        )

    checks["source_integrity"] = "valid"
    checks["plan_integrity"] = "valid"
    check = approval_status(config, plan_id, approvals_path)
    checks["approval"] = check.status.value
    if check.status is not ApprovalStatus.APPROVED:
        status = (
            PreflightStatus.INVALID
            if check.status is ApprovalStatus.INVALID
            else PreflightStatus.BLOCKED
        )
        return PreflightPreparation(
            status,
            check.reasons or [f"aprovacao {check.status.value}"],
            bundle=bundle,
            approval=check,
            checks=checks,
        )

    issues = [
        issue
        for issue in validate_plan(bundle.plan, bundle.destination_reviews, require_approval=False)
        if issue.severity.value == "error"
    ]
    if issues:
        checks["plan_integrity"] = "blocked"
        return PreflightPreparation(
            PreflightStatus.BLOCKED,
            [f"{issue.code}: {issue.message}" for issue in issues],
            bundle=bundle,
            approval=check,
            checks=checks,
        )

    expected = config.jazerei_user_id
    plan_user = bundle.manifest.destination.get("user_id")
    if not expected:
        checks["destination_identity"] = "missing_expected"
        return PreflightPreparation(
            PreflightStatus.BLOCKED,
            ["JAZEREI_USER_ID ausente"],
            bundle=bundle,
            approval=check,
            checks=checks,
        )
    if not plan_user:
        checks["destination_identity"] = "missing_plan"
        return PreflightPreparation(
            PreflightStatus.INVALID,
            ["manifesto nao registra user_id do destino"],
            bundle=bundle,
            approval=check,
            checks=checks,
        )
    if plan_user != expected:
        checks["destination_identity"] = "mismatch"
        return PreflightPreparation(
            PreflightStatus.BLOCKED,
            ["identidade esperada difere da identidade aprovada no plano"],
            bundle=bundle,
            approval=check,
            expected_user_id=expected,
            checks=checks,
        )

    checks["source_integrity"] = "valid"
    checks["plan_integrity"] = "valid"
    checks["approval"] = "valid"
    checks["destination_identity"] = "valid"
    return PreflightPreparation(
        PreflightStatus.COMPATIBLE,
        [],
        bundle=bundle,
        approval=check,
        expected_user_id=expected,
        checks=checks,
    )


def capture_snapshots_for_actions(
    session: BrowserSession,
    actions: list[MigrationAction],
    *,
    expected_user_id: str,
    page_size: int,
    max_pages: int,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[dict[int, SnapshotEvidence], list[str]]:
    targets: dict[int, str] = {}
    for action in actions:
        if action.kind != "review":
            continue
        if action.game_id is None:
            raise PreflightError("acao de review sem game_id")
        slug = action.destination_slug or action.source_ref
        if not slug:
            raise PreflightError("acao de review sem slug de destino")
        if action.game_id in targets and targets[action.game_id] != slug:
            raise PreflightError(f"game_id {action.game_id} com slugs divergentes")
        targets[action.game_id] = slug

    if not targets:
        return {}, []

    context, pagination = capture_reviews_window(
        session,
        expected_user_id=expected_user_id,
        page_size=page_size,
        max_pages=max_pages,
        on_page=on_page,
        should_cancel=should_cancel,
    )
    snapshots: dict[int, SnapshotEvidence] = {}
    for game_id, slug in targets.items():
        snapshot = build_review_snapshot(
            game_id=game_id,
            slug=slug,
            expected_user_id=expected_user_id,
            pagination=pagination,
            page_size=page_size,
            max_pages=max_pages,
            identity_sources=context.sources,
        )
        ref = f"snapshots/{slug}-{game_id}.json"
        snapshots[game_id] = SnapshotEvidence(snapshot=snapshot, ref=ref)
    return snapshots, list(context.sources)


def compare_actions(
    actions: list[MigrationAction],
    snapshots: dict[int, SnapshotEvidence],
    *,
    expected_user_id: str | None = None,
) -> tuple[list[ActionCheck], PreflightSummary]:
    checks: list[ActionCheck] = []
    for action in actions:
        action_id = action_hash(action)
        if action.kind != "review":
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    game_id=action.game_id,
                    status=ActionStatus.UNSUPPORTED,
                    reason="action_kind_not_supported_in_preflight",
                )
            )
            continue
        if action.game_id is None:
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    status=ActionStatus.INVALID,
                    reason="missing_game_id",
                )
            )
            continue
        evidence = snapshots.get(action.game_id)
        if evidence is None:
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    game_id=action.game_id,
                    status=ActionStatus.INCONCLUSIVE,
                    reason="missing_current_snapshot",
                )
            )
            continue
        snapshot = evidence.snapshot
        if (
            not snapshot.capture.complete
            or not snapshot.pagination.complete
            or not snapshot.identity.verified
        ):
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    game_id=action.game_id,
                    status=ActionStatus.INCONCLUSIVE,
                    reason="capture_incomplete",
                    snapshot_ref=evidence.ref,
                )
            )
            continue
        if snapshot.query.game_id != action.game_id:
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    game_id=action.game_id,
                    status=ActionStatus.INVALID,
                    reason="snapshot_game_id_mismatch",
                    snapshot_ref=evidence.ref,
                )
            )
            continue
        if expected_user_id and snapshot.identity.user_id != expected_user_id:
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    game_id=action.game_id,
                    status=ActionStatus.INVALID,
                    reason="snapshot_identity_mismatch",
                    snapshot_ref=evidence.ref,
                )
            )
            continue
        if snapshot.identity.user_id is None:
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    game_id=action.game_id,
                    status=ActionStatus.INVALID,
                    reason="snapshot_identity_missing",
                    snapshot_ref=evidence.ref,
                )
            )
            continue
        current = snapshot.result.review
        if (
            current is not None
            and expected_user_id
            and current.user_id
            and current.user_id != expected_user_id
        ):
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    game_id=action.game_id,
                    status=ActionStatus.INVALID,
                    reason="current_review_user_mismatch",
                    snapshot_ref=evidence.ref,
                )
            )
            continue
        before = action.before
        if before is None and current is None:
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    game_id=action.game_id,
                    status=ActionStatus.COMPATIBLE,
                    reason="destination_still_absent",
                    snapshot_ref=evidence.ref,
                )
            )
        elif before is None and current is not None:
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    game_id=action.game_id,
                    status=ActionStatus.CONFLICT,
                    reason="review_created_after_approval",
                    snapshot_ref=evidence.ref,
                )
            )
        elif before is not None and current is None:
            checks.append(
                ActionCheck(
                    action_id=action_id,
                    kind=action.kind,
                    source_ref=action.source_ref,
                    game_id=action.game_id,
                    status=ActionStatus.CONFLICT,
                    reason="review_removed_after_approval",
                    snapshot_ref=evidence.ref,
                )
            )
        else:
            current_data = current.model_dump(mode="json") if current else {}
            differences = [
                field for field in REVIEW_FIELDS if before.get(field) != current_data.get(field)
            ]
            if differences:
                checks.append(
                    ActionCheck(
                        action_id=action_id,
                        kind=action.kind,
                        source_ref=action.source_ref,
                        game_id=action.game_id,
                        status=ActionStatus.CONFLICT,
                        reason="review_modified_after_approval",
                        snapshot_ref=evidence.ref,
                        fields=differences,
                    )
                )
            else:
                checks.append(
                    ActionCheck(
                        action_id=action_id,
                        kind=action.kind,
                        source_ref=action.source_ref,
                        game_id=action.game_id,
                        status=ActionStatus.COMPATIBLE,
                        reason="destination_unchanged",
                        snapshot_ref=evidence.ref,
                    )
                )
    return checks, summarize_checks(checks)


def summarize_checks(checks: list[ActionCheck]) -> PreflightSummary:
    compatible = sum(check.status is ActionStatus.COMPATIBLE for check in checks)
    conflicts = sum(check.status is ActionStatus.CONFLICT for check in checks)
    unsupported = sum(check.status is ActionStatus.UNSUPPORTED for check in checks)
    inconclusive = sum(check.status is ActionStatus.INCONCLUSIVE for check in checks)
    invalid = sum(check.status is ActionStatus.INVALID for check in checks)
    if invalid:
        status = PreflightStatus.INVALID
    elif unsupported:
        status = PreflightStatus.BLOCKED
    elif conflicts:
        status = PreflightStatus.CONFLICT
    elif inconclusive:
        status = PreflightStatus.INCONCLUSIVE
    else:
        status = PreflightStatus.COMPATIBLE
    return PreflightSummary(
        status=status,
        compatible_actions=compatible,
        conflicts=conflicts,
        unsupported=unsupported,
        inconclusive=inconclusive,
        invalid=invalid,
    )


def build_result(
    *,
    preflight_id: str,
    preparation: PreflightPreparation,
    checks: list[ActionCheck],
    summary: PreflightSummary,
    snapshots: list[dict[str, Any]],
    reasons: list[str] | None = None,
    identity_verified: bool = False,
    identity_user_id: str | None = None,
    checks_status: dict[str, str] | None = None,
    captured_at: str | None = None,
) -> PreflightResult:
    bundle = preparation.bundle
    result = PreflightResult(
        preflight_id=preflight_id,
        plan_id=bundle.plan_id if bundle else "",
        manifest_sha256=bundle.manifest.manifest_sha256 if bundle else "",
        approval_id=(
            preparation.approval.approval.approval_id
            if preparation.approval and preparation.approval.approval
            else None
        ),
        approval_status=(
            preparation.approval.status.value if preparation.approval else "unavailable"
        ),
        identity=PreflightIdentity(
            verified=identity_verified,
            user_id=identity_user_id,
        ),
        checks=checks_status or preparation.checks,
        actions=checks,
        snapshots=snapshots,
        result=summary,
        reasons=reasons or [],
        captured_at=captured_at or utc_now(),
    )
    result.result_sha256 = canonical_hash(
        result.model_dump(mode="json", exclude={"captured_at", "result_sha256"})
    )
    return result
