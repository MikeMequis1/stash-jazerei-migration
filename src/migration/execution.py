"""Gateway de execução controlada — somente simulado nesta etapa."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from src.config import Config
from src.migration.approval import (
    action_hash,
    approval_status,
    canonical_hash,
    load_bundle,
    verify_bundle,
)
from src.migration.executions import (
    append_confirmation,
    append_event,
    compute_confirmation_id,
    load_confirmations,
    load_execution,
    save_execution,
    verify_execution,
)
from src.migration.preflight import (
    SnapshotEvidence,  # noqa: F401
    capture_snapshots_for_actions,  # noqa: F401
)
from src.migration.preflights import (
    load_preflight,
    preflight_manifest_sha256,
    verify_preflight,
)
from src.migration.validator import validate_plan
from src.models.approval import ApprovalStatus
from src.models.execution import (
    ActionExecutionStatus,
    ConfirmationRecord,
    ExecutionAction,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionSummary,
)
from src.models.normalized import LikedStatus, MigrationAction
from src.models.snapshot import ReviewStateSnapshot
from src.storage import utc_now

EXECUTION_FIELDS = ("game_id", "rating", "text", "liked")
REVIEW_FIELDS = ("game_id", "game_slug", "rating", "text", "liked")
# Nesta etapa nao existe transporte real e a escrita permanece bloqueada,
# independentemente de MIGRATION_ALLOW_WRITES.
REAL_WRITE_ENABLED = False


class ExecutionError(RuntimeError):
    pass


class WriteBlockedError(ExecutionError):
    pass


class PayloadError(ExecutionError):
    pass


class ConfirmationError(ExecutionError):
    pass


class WriteTimeout(ExecutionError):
    pass


class WriteNotSent(ExecutionError):
    """Falha comprovadamente anterior ao envio; unica excecao que vira `failed`."""


@dataclass
class WriteResult:
    status_code: int
    body: Any = None


class SimulatedWriteTransport(Protocol):
    """Transporte injetado. O gateway só aceita transporte explicitamente simulado."""

    simulated: bool

    def post_review(self, payload: dict[str, Any]) -> WriteResult: ...


ACTION_TRANSITIONS: dict[ActionExecutionStatus, set[ActionExecutionStatus]] = {
    ActionExecutionStatus.PREPARED: {
        ActionExecutionStatus.AWAITING_CONFIRMATION,
        ActionExecutionStatus.BLOCKED,
        ActionExecutionStatus.CANCELLED,
    },
    ActionExecutionStatus.AWAITING_CONFIRMATION: {
        ActionExecutionStatus.READY,
        ActionExecutionStatus.BLOCKED,
        ActionExecutionStatus.CANCELLED,
    },
    ActionExecutionStatus.READY: {
        ActionExecutionStatus.WRITING,
        ActionExecutionStatus.CONFLICT,
        ActionExecutionStatus.FAILED,
        ActionExecutionStatus.CANCELLED,
    },
    ActionExecutionStatus.WRITING: {
        ActionExecutionStatus.WRITE_CONFIRMED,
        ActionExecutionStatus.WRITE_AMBIGUOUS,
        ActionExecutionStatus.FAILED,
    },
    ActionExecutionStatus.WRITE_CONFIRMED: {
        ActionExecutionStatus.VERIFICATION_PENDING,
        ActionExecutionStatus.WRITE_AMBIGUOUS,
    },
    ActionExecutionStatus.VERIFICATION_PENDING: {
        ActionExecutionStatus.VERIFIED,
        ActionExecutionStatus.CONFLICT,
        ActionExecutionStatus.WRITE_AMBIGUOUS,
    },
    ActionExecutionStatus.WRITE_AMBIGUOUS: {
        ActionExecutionStatus.CONFLICT,
        ActionExecutionStatus.VERIFIED,
    },
    ActionExecutionStatus.VERIFIED: set(),
    ActionExecutionStatus.CONFLICT: set(),
    ActionExecutionStatus.FAILED: set(),
    ActionExecutionStatus.BLOCKED: set(),
    ActionExecutionStatus.CANCELLED: set(),
}

EXECUTION_TRANSITIONS: dict[ExecutionStatus, set[ExecutionStatus]] = {
    ExecutionStatus.PREPARED: {
        ExecutionStatus.BLOCKED,
        ExecutionStatus.AWAITING_CONFIRMATION,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.AWAITING_CONFIRMATION: {
        ExecutionStatus.BLOCKED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.INCONCLUSIVE,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.RUNNING: {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.COMPLETED_WITH_ERRORS,
        ExecutionStatus.INCONCLUSIVE,
        ExecutionStatus.FAILED,
        ExecutionStatus.AWAITING_CONFIRMATION,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.INCONCLUSIVE: {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.COMPLETED_WITH_ERRORS,
        ExecutionStatus.AWAITING_CONFIRMATION,
    },
    ExecutionStatus.BLOCKED: set(),
    ExecutionStatus.COMPLETED: set(),
    ExecutionStatus.COMPLETED_WITH_ERRORS: set(),
    ExecutionStatus.FAILED: set(),
    ExecutionStatus.CANCELLED: set(),
}


def transition_action(action: ExecutionAction, target: ActionExecutionStatus) -> None:
    if target not in ACTION_TRANSITIONS[action.status]:
        raise ExecutionError(f"transicao de acao invalida: {action.status.value} -> {target.value}")
    action.status = target


def transition_execution(execution: ExecutionRecord, target: ExecutionStatus) -> None:
    if target not in EXECUTION_TRANSITIONS[execution.status]:
        raise ExecutionError(
            f"transicao de execucao invalida: {execution.status.value} -> {target.value}"
        )
    execution.status = target


def build_review_payload(action: MigrationAction) -> tuple[dict[str, Any], str]:
    if action.kind != "review":
        raise PayloadError("somente criacao de review e suportada")
    if action.before is not None:
        raise PayloadError("atualizacao de review existente nao e suportada")
    if action.game_id is None or action.game_id < 1:
        raise PayloadError("game_id deve ser inteiro positivo")
    if not action.after:
        raise PayloadError("acao sem payload planejado")
    unknown = set(action.after) - set(EXECUTION_FIELDS)
    if unknown:
        raise PayloadError(f"campos nao permitidos no payload: {sorted(unknown)}")
    if action.after.get("game_id") != action.game_id:
        raise PayloadError("game_id do payload diverge da acao")
    rating = action.after.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, (int, float)):
        raise PayloadError("rating deve ser numerico")
    if not 0 <= float(rating) <= 10:
        raise PayloadError("rating fora do intervalo 0..10")
    text = action.after.get("text")
    if not isinstance(text, str):
        raise PayloadError("text deve ser string")
    liked = action.after.get("liked")
    if type(liked) is not bool:
        raise PayloadError("liked deve ser booleano explicito")
    if action.liked.value is not liked:
        raise PayloadError("liked diverge da decisao humana")
    if action.liked.status is not LikedStatus.DECIDED:
        raise PayloadError("liked nao possui decisao humana decidida")
    payload = {
        "game_id": action.game_id,
        "rating": rating,
        "text": text,
        "liked": liked,
    }
    return payload, canonical_hash(payload)


def validate_payload(payload: dict[str, Any], *, game_id: int) -> str:
    if set(payload) != set(EXECUTION_FIELDS):
        raise PayloadError("payload deve conter exatamente game_id, rating, text e liked")
    if payload.get("game_id") != game_id or not isinstance(game_id, int) or game_id < 1:
        raise PayloadError("game_id invalido")
    rating = payload.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, (int, float)):
        raise PayloadError("rating invalido")
    if not 0 <= float(rating) <= 10:
        raise PayloadError("rating fora do intervalo")
    if not isinstance(payload.get("text"), str):
        raise PayloadError("text invalido")
    if type(payload.get("liked")) is not bool:
        raise PayloadError("liked deve ser booleano explicito")
    return canonical_hash(payload)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def prepare_execution(
    config: Config,
    *,
    plan_id: str,
    preflight_id: str,
    execution_id: str,
    approvals_path: Any,
) -> ExecutionRecord:
    bundle = load_bundle(config, plan_id)
    verification = verify_bundle(config, bundle)
    if verification.invalid or verification.stale:
        raise ExecutionError("; ".join(verification.invalid + verification.stale))
    approval = approval_status(config, plan_id, approvals_path)
    if approval.status is not ApprovalStatus.APPROVED or approval.approval is None:
        raise ExecutionError(f"aprovacao nao valida: {approval.status.value}")

    preflight_errors = verify_preflight(config, preflight_id)
    if preflight_errors:
        raise ExecutionError("; ".join(preflight_errors))
    preflight = load_preflight(config, preflight_id)
    if preflight.plan_id != plan_id:
        raise ExecutionError("preflight pertence a outro plano")
    if preflight.manifest_sha256 != bundle.manifest.manifest_sha256:
        raise ExecutionError("preflight usa outro manifesto de plano")
    if preflight.approval_id != approval.approval.approval_id:
        raise ExecutionError("preflight usa outra aprovacao")
    if preflight.approval_status != "approved":
        raise ExecutionError("preflight nao registra aprovacao valida")
    if _timestamp(preflight.captured_at) < _timestamp(approval.approval.approved_at):
        raise ExecutionError("preflight anterior a aprovacao atual")
    if preflight.result.status.value != "compatible":
        raise ExecutionError(f"preflight nao e compativel: {preflight.result.status.value}")
    expected = config.jazerei_user_id
    if not expected or preflight.identity.user_id != expected:
        raise ExecutionError("identidade do preflight diverge da esperada")
    if bundle.manifest.destination.get("user_id") != expected:
        raise ExecutionError("identidade aprovada diverge da esperada")

    issues = [
        issue
        for issue in validate_plan(bundle.plan, bundle.destination_reviews, require_approval=False)
        if issue.severity.value == "error"
    ]
    if issues:
        raise ExecutionError("plano bloqueado: " + "; ".join(issue.code for issue in issues))

    preflight_by_id = {check.action_id: check for check in preflight.actions}
    plan_ids = [action_hash(action) for action in bundle.plan.actions]
    if len(set(plan_ids)) != len(plan_ids):
        raise ExecutionError("acoes duplicadas no plano")
    if set(plan_ids) != set(preflight_by_id):
        raise ExecutionError("acoes do plano e preflight nao coincidem")
    for check in preflight.actions:
        if check.status.value != "compatible":
            raise ExecutionError(
                f"acao {check.action_id} nao esta compativel: {check.status.value}"
            )

    actions: list[ExecutionAction] = []
    for action in bundle.plan.actions:
        action_id = action_hash(action)
        if action.kind != "review":
            raise ExecutionError(f"acao nao suportada: {action.kind}")
        if action.before is not None:
            raise ExecutionError("atualizacao/existente nao suportada")
        payload, payload_hash = build_review_payload(action)
        actions.append(
            ExecutionAction(
                action_id=action_id,
                kind=action.kind,
                game_id=action.game_id,
                source_ref=action.source_ref,
                destination_slug=action.destination_slug,
                planned_before=action.before,
                planned_after=dict(action.after or {}),
                payload=payload,
                payload_sha256=payload_hash,
                status=ActionExecutionStatus.AWAITING_CONFIRMATION,
            )
        )
    if not actions:
        raise ExecutionError("plano sem acoes executaveis")

    execution = ExecutionRecord(
        execution_id=execution_id,
        plan_id=plan_id,
        plan_sha256=bundle.manifest.plan_sha256,
        manifest_sha256=bundle.manifest.manifest_sha256,
        preflight_id=preflight_id,
        preflight_manifest_sha256=preflight_manifest_sha256(config, preflight_id),
        preflight_sha256=preflight.result_sha256,
        approval_id=approval.approval.approval_id,
        user_id=expected,
        identity_verified=True,
        created_at=utc_now(),
        status=ExecutionStatus.AWAITING_CONFIRMATION,
        actions=actions,
    )
    _refresh_summary(execution)
    return execution


def confirm_action(
    config: Config,
    *,
    execution_id: str,
    action_id: str,
    confirmation: str,
    approvals_path: Any = None,
) -> ConfirmationRecord:
    if confirmation != execution_id:
        raise ConfirmationError("confirmacao nao corresponde ao execution_id")
    errors = validate_execution(config, execution_id, approvals_path)
    if errors:
        raise ConfirmationError("; ".join(errors))
    execution = load_execution(config, execution_id)
    if execution.status not in {
        ExecutionStatus.PREPARED,
        ExecutionStatus.AWAITING_CONFIRMATION,
    }:
        raise ConfirmationError("execucao nao esta aguardando confirmacao")
    action = next((item for item in execution.actions if item.action_id == action_id), None)
    if action is None:
        raise ConfirmationError("acao inexistente")
    if action.status not in {
        ActionExecutionStatus.PREPARED,
        ActionExecutionStatus.AWAITING_CONFIRMATION,
    }:
        raise ConfirmationError("acao nao esta aguardando confirmacao")
    if not action.payload or not action.payload_sha256 or not action.game_id:
        raise ConfirmationError("payload da acao ausente")
    if action.payload_sha256 != validate_payload(action.payload, game_id=action.game_id):
        raise ConfirmationError("payload da acao foi alterado")
    existing = load_confirmations(config, execution_id)
    if any(record.action_id == action_id for record in existing):
        raise ConfirmationError("acao ja possui confirmacao")

    record = ConfirmationRecord(
        confirmation_id="pending",
        execution_id=execution_id,
        plan_id=execution.plan_id,
        plan_sha256=execution.plan_sha256,
        manifest_sha256=execution.manifest_sha256,
        preflight_id=execution.preflight_id,
        preflight_manifest_sha256=execution.preflight_manifest_sha256,
        action_id=action_id,
        game_id=action.game_id,
        payload_sha256=action.payload_sha256,
        user_id=execution.user_id,
        confirmed_at=utc_now(),
    )
    record.confirmation_id = compute_confirmation_id(record)
    append_confirmation(config, record)
    transition_action(action, ActionExecutionStatus.READY)
    action.confirmation_id = record.confirmation_id
    action.confirmed_at = record.confirmed_at
    append_event(
        config,
        execution_id,
        action_id=action_id,
        to_status=ActionExecutionStatus.READY.value,
        reason="human_confirmation",
    )
    _refresh_summary(execution)
    save_execution(config, execution, reason="action_confirmed")
    return record


def validate_execution(config: Config, execution_id: str, approvals_path: Any = None) -> list[str]:
    errors = verify_execution(config, execution_id)
    try:
        execution = load_execution(config, execution_id)
    except Exception as exc:  # noqa: BLE001
        return errors + [str(exc)]
    bundle = load_bundle(config, execution.plan_id)
    verification = verify_bundle(config, bundle)
    errors.extend(verification.invalid + verification.stale)
    if execution.manifest_sha256 != bundle.manifest.manifest_sha256:
        errors.append("manifesto do plano mudou")
    if execution.plan_sha256 != bundle.manifest.plan_sha256:
        errors.append("hash do plano mudou")
    approval = approval_status(
        config,
        execution.plan_id,
        approvals_path or (config.migration_dir / "approvals.jsonl"),
    )
    if approval.status is not ApprovalStatus.APPROVED or approval.approval is None:
        errors.append(f"aprovacao nao valida: {approval.status.value}")
    elif approval.approval.approval_id != execution.approval_id:
        errors.append("execucao vinculada a outra aprovacao")
    if execution.user_id and config.jazerei_user_id and execution.user_id != config.jazerei_user_id:
        errors.append("identidade da execucao diverge da configurada")
    try:
        errors.extend(verify_preflight(config, execution.preflight_id))
        preflight = load_preflight(config, execution.preflight_id)
        if preflight.plan_id != execution.plan_id:
            errors.append("preflight de outro plano")
        if preflight.manifest_sha256 != execution.manifest_sha256:
            errors.append("preflight de outro manifesto")
        if preflight.result_sha256 != execution.preflight_sha256:
            errors.append("resultado do preflight mudou")
        if preflight_manifest_sha256(config, execution.preflight_id) != (
            execution.preflight_manifest_sha256
        ):
            errors.append("manifesto do preflight mudou")
        if preflight.approval_id != execution.approval_id:
            errors.append("preflight vinculado a outra aprovacao")
        if preflight.result.status.value != "compatible":
            errors.append(f"preflight nao compativel: {preflight.result.status.value}")
        if not preflight.identity.verified or preflight.identity.user_id != execution.user_id:
            errors.append("identidade do preflight diverge da execucao")
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
    expected_action_ids = {action_hash(action) for action in bundle.plan.actions}
    if expected_action_ids != {action.action_id for action in execution.actions}:
        errors.append("acoes da execucao divergem do plano")
    for action in execution.actions:
        if action.kind != "review":
            errors.append(f"acao {action.action_id} nao suportada: {action.kind}")
        if action.planned_before is not None:
            errors.append(f"acao {action.action_id} atualiza review existente")
        if action.payload is None or action.payload_sha256 is None:
            errors.append(f"acao {action.action_id} sem payload")
            continue
        try:
            expected_hash = validate_payload(action.payload, game_id=action.game_id or 0)
        except PayloadError as exc:
            errors.append(f"{action.action_id}: {exc}")
            continue
        if expected_hash != action.payload_sha256:
            errors.append(f"{action.action_id}: hash do payload diverge")
    return errors


def execute_action(
    config: Config,
    *,
    execution_id: str,
    action_id: str,
    transport: SimulatedWriteTransport,
    read_state: Callable[[int], ReviewStateSnapshot],
    approvals_path: Any = None,
) -> ExecutionRecord:
    """Executa uma ação apenas com transporte explicitamente autorizado."""
    simulated = getattr(transport, "simulated", False) is True
    if not simulated:
        flags_ok = config.allow_writes and config.enable_real_write_transport
        if not flags_ok or not isinstance(transport, BrowserWriteTransport):
            raise WriteBlockedError(
                "execucao real bloqueada: exige MIGRATION_ALLOW_WRITES=true, "
                "MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=true e transporte do gateway"
            )
    execution = load_execution(config, execution_id)
    interrupted = [
        action
        for action in execution.actions
        if action.status
        in {ActionExecutionStatus.WRITING, ActionExecutionStatus.VERIFICATION_PENDING}
    ]
    if interrupted or execution.status is ExecutionStatus.RUNNING:
        raise ExecutionError(
            "execucao interrompida; use execution-reconcile antes de qualquer nova tentativa"
        )
    if execution.status not in {
        ExecutionStatus.PREPARED,
        ExecutionStatus.AWAITING_CONFIRMATION,
    }:
        raise ExecutionError(f"execucao nao permite nova acao: {execution.status.value}")
    if any(action.status is ActionExecutionStatus.WRITE_AMBIGUOUS for action in execution.actions):
        raise ExecutionError(
            "execucao possui acao ambigua; use execution-resolve antes de qualquer nova acao"
        )
    errors = validate_execution(config, execution_id, approvals_path)
    if errors:
        raise ExecutionError("; ".join(errors))
    action = next((item for item in execution.actions if item.action_id == action_id), None)
    if action is None:
        raise ExecutionError("acao inexistente")
    if action.status is ActionExecutionStatus.VERIFIED:
        raise ExecutionError("acao ja verificada; repeticao bloqueada")
    if action.status is not ActionExecutionStatus.READY:
        raise ExecutionError(f"acao nao esta pronta: {action.status.value}")
    confirmation = next(
        (
            record
            for record in load_confirmations(config, execution_id)
            if record.action_id == action_id
        ),
        None,
    )
    if confirmation is None or confirmation.confirmation_id != action.confirmation_id:
        raise ConfirmationError("confirmacao ausente")
    if confirmation.payload_sha256 != action.payload_sha256:
        raise ConfirmationError("payload diverge da confirmacao")
    if action.payload is None or action.game_id is None:
        raise ExecutionError("payload ausente")

    try:
        current = read_state(action.game_id)
    except Exception as exc:  # noqa: BLE001 - leitura falhou; nao pode virar ausencia
        action.status = ActionExecutionStatus.FAILED
        action.reason = "pre_write_read_failed"
        action.error = type(exc).__name__
        transition_execution(execution, ExecutionStatus.INCONCLUSIVE)
        _refresh_summary(execution)
        save_execution(config, execution, reason="pre_write_read_failed")
        return execution
    if not current.capture.complete or not current.pagination.complete:
        action.status = ActionExecutionStatus.FAILED
        action.reason = "pre_write_read_inconclusive"
        transition_execution(execution, ExecutionStatus.INCONCLUSIVE)
        _refresh_summary(execution)
        save_execution(config, execution, reason="pre_write_read_inconclusive")
        return execution
    if not current.identity.verified or current.identity.user_id != execution.user_id:
        action.status = ActionExecutionStatus.FAILED
        action.reason = "pre_write_identity_mismatch"
        transition_execution(execution, ExecutionStatus.BLOCKED)
        _refresh_summary(execution)
        save_execution(config, execution, reason="pre_write_identity_mismatch")
        return execution
    if current.query.game_id != action.game_id:
        action.status = ActionExecutionStatus.FAILED
        action.reason = "pre_write_game_mismatch"
        transition_execution(execution, ExecutionStatus.BLOCKED)
        _refresh_summary(execution)
        save_execution(config, execution, reason="pre_write_game_mismatch")
        return execution
    if current.result.review is not None:
        transition_action(action, ActionExecutionStatus.CONFLICT)
        action.reason = "review_appeared_before_write"
        transition_execution(execution, ExecutionStatus.BLOCKED)
        _refresh_summary(execution)
        save_execution(config, execution, reason="pre_write_conflict")
        return execution
    action.before_read = current.model_dump(mode="json")
    transition_execution(execution, ExecutionStatus.RUNNING)
    execution.started_at = utc_now()
    transition_action(action, ActionExecutionStatus.WRITING)
    append_event(
        config,
        execution_id,
        action_id=action.action_id,
        from_status=ActionExecutionStatus.READY.value,
        to_status=ActionExecutionStatus.WRITING.value,
        reason="pre_write_check_passed",
    )
    save_execution(config, execution, reason="writing_started")
    try:
        result = transport.post_review(action.payload)
    except WriteTimeout:
        action.status = ActionExecutionStatus.WRITE_AMBIGUOUS
        action.reason = "write_timeout_after_send"
        _finalize_execution(execution)
        _refresh_summary(execution)
        save_execution(config, execution, reason="write_timeout")
        return execution
    except WriteNotSent as exc:
        action.status = ActionExecutionStatus.FAILED
        action.reason = "write_failed_before_send"
        action.error = type(exc).__name__
        _finalize_execution(execution)
        _refresh_summary(execution)
        save_execution(config, execution, reason="write_failed_before_send")
        return execution
    except Exception as exc:  # noqa: BLE001 - envio pode ter ocorrido
        action.status = ActionExecutionStatus.WRITE_AMBIGUOUS
        action.reason = "write_outcome_unknown"
        action.error = type(exc).__name__
        _finalize_execution(execution)
        _refresh_summary(execution)
        save_execution(config, execution, reason="write_outcome_unknown")
        return execution

    action.write_result = {
        "status_code": result.status_code,
        "body": result.body,
    }
    if not 200 <= result.status_code < 300:
        action.status = ActionExecutionStatus.WRITE_AMBIGUOUS
        action.reason = f"http_{result.status_code}_unconfirmed"
        _finalize_execution(execution)
        _refresh_summary(execution)
        save_execution(config, execution, reason="write_http_unconfirmed")
        return execution
    transition_action(action, ActionExecutionStatus.WRITE_CONFIRMED)
    transition_action(action, ActionExecutionStatus.VERIFICATION_PENDING)
    save_execution(config, execution, reason="write_confirmed")
    try:
        after = read_state(action.game_id)
    except Exception as exc:  # noqa: BLE001
        action.status = ActionExecutionStatus.WRITE_AMBIGUOUS
        action.reason = "verification_read_failed"
        action.error = f"verification_read_failed:{type(exc).__name__}"
        _finalize_execution(execution)
        _refresh_summary(execution)
        save_execution(config, execution, reason="verification_read_failed")
        return execution
    if not after.capture.complete or not after.pagination.complete:
        action.status = ActionExecutionStatus.WRITE_AMBIGUOUS
        action.reason = "verification_inconclusive"
        _finalize_execution(execution)
        _refresh_summary(execution)
        save_execution(config, execution, reason="verification_inconclusive")
        return execution
    if not after.identity.verified or after.identity.user_id != execution.user_id:
        action.status = ActionExecutionStatus.WRITE_AMBIGUOUS
        action.reason = "verification_identity_mismatch"
        _finalize_execution(execution)
        _refresh_summary(execution)
        save_execution(config, execution, reason="verification_identity_mismatch")
        return execution
    if after.query.game_id != action.game_id:
        action.status = ActionExecutionStatus.WRITE_AMBIGUOUS
        action.reason = "verification_game_mismatch"
        _finalize_execution(execution)
        _refresh_summary(execution)
        save_execution(config, execution, reason="verification_game_mismatch")
        return execution
    review = after.result.review
    if review is None:
        action.status = ActionExecutionStatus.WRITE_AMBIGUOUS
        action.reason = "review_not_found_after_write"
        _finalize_execution(execution)
        _refresh_summary(execution)
        save_execution(config, execution, reason="verification_missing")
        return execution
    differences = _review_differences(action, execution, review)
    action.verification = {"fields": differences, "review": review.model_dump(mode="json")}
    if differences:
        action.status = ActionExecutionStatus.CONFLICT
        action.reason = "post_write_verification_mismatch"
    else:
        action.status = ActionExecutionStatus.VERIFIED
        action.reason = "post_write_verified"
    _finalize_execution(execution)
    _refresh_summary(execution)
    save_execution(config, execution, reason="verification_completed")
    return execution


def _refresh_summary(execution: ExecutionRecord) -> None:
    summary = ExecutionSummary(total_actions=len(execution.actions))
    for action in execution.actions:
        if action.status is ActionExecutionStatus.READY:
            summary.ready += 1
        elif action.status is ActionExecutionStatus.BLOCKED:
            summary.blocked += 1
        elif action.status is ActionExecutionStatus.VERIFIED:
            summary.verified += 1
        elif action.status is ActionExecutionStatus.CONFLICT:
            summary.conflicts += 1
        elif action.status in {
            ActionExecutionStatus.FAILED,
            ActionExecutionStatus.WRITE_CONFIRMED,
            ActionExecutionStatus.VERIFICATION_PENDING,
        }:
            summary.failed += 1
        elif action.status is ActionExecutionStatus.WRITE_AMBIGUOUS:
            summary.ambiguous += 1
    execution.summary = summary


def _review_differences(
    action: ExecutionAction, execution: ExecutionRecord, review: Any
) -> list[str]:
    expected = action.payload or {}
    expected_fields = {
        field: expected.get(field) for field in ("game_id", "rating", "text", "liked")
    }
    expected_fields["user_id"] = execution.user_id
    if action.destination_slug:
        expected_fields["game_slug"] = action.destination_slug
    return [
        field
        for field, expected_value in expected_fields.items()
        if getattr(review, field, None) != expected_value
    ]


REVIEW_UPSERT_URL = "https://jazerei.com/api/reviews/upsert"
JAZEREI_WRITE_HOSTS = frozenset({"jazerei.com", "www.jazerei.com"})
WRITE_AUTH_HEADERS = frozenset({"authorization"})


class BrowserWriteTransport:
    """Transporte real, exclusivo do gateway.

    Usa a sessao persistente do Chromium via `context.request`, sem ler ou
    persistir cookies/tokens. Nao expoe metodo generico e recusa qualquer
    endpoint fora de `/api/reviews/upsert`.

    O frontend envia `Authorization: Bearer <Firebase ID token>` no upsert; o
    mesmo cabecalho e capturado da sessao autenticada e mantido apenas em
    memoria. Sem ele, o endpoint responde `401 Authentication required`.
    """

    simulated = False

    def __init__(
        self,
        config: Config,
        session: Any,
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        self.config = config
        self._session = session
        self._auth_headers = {
            key.lower(): str(value)
            for key, value in (auth_headers or {}).items()
            if key.lower() in WRITE_AUTH_HEADERS and value
        }
        self._require_flags()

    def _require_flags(self) -> None:
        if not (self.config.allow_writes and self.config.enable_real_write_transport):
            raise WriteBlockedError(
                "escrita real exige MIGRATION_ALLOW_WRITES=true e "
                "MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=true"
            )

    def _validate_session_domain(self) -> None:
        page = getattr(self._session, "page", None)
        page_url = str(getattr(page, "url", "") or "")
        parts = urlsplit(page_url)
        if parts.scheme not in {"https", "http"} or (parts.hostname or "") not in (
            JAZEREI_WRITE_HOSTS
        ):
            raise WriteBlockedError("sessao nao esta no dominio do Jazerei")

    def post_review(self, payload: dict[str, Any]) -> WriteResult:
        self._require_flags()
        game_id = payload.get("game_id")
        if type(game_id) is not int or game_id < 1:
            raise PayloadError("game_id deve ser inteiro positivo")
        validate_payload(payload, game_id=game_id)
        self._validate_session_domain()
        context = getattr(self._session, "context", None)
        request = getattr(context, "request", None)
        if request is None or not hasattr(request, "post"):
            raise WriteBlockedError("sessao sem contexto HTTP para escrita")
        try:
            response = request.post(
                REVIEW_UPSERT_URL,
                data=payload,
                headers={"Content-Type": "application/json", **self._auth_headers},
                max_redirects=0,
                timeout=int(self.config.timeout * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - resultado desconhecido
            if "timeout" in type(exc).__name__.lower():
                raise WriteTimeout("timeout no transporte de escrita") from None
            raise ExecutionError("falha no transporte de escrita; resultado desconhecido") from None
        status = int(getattr(response, "status", 0))
        body: Any = None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - corpo vazio/nao JSON
            body = None
        return WriteResult(status_code=status, body=body)


def build_browser_write_transport(
    config: Config,
    session: Any,
    auth_headers: dict[str, str] | None = None,
) -> BrowserWriteTransport:
    """Unica fabrica do transporte real; usada somente pela CLI do gateway."""
    return BrowserWriteTransport(config, session, auth_headers)


def _finalize_execution(execution: ExecutionRecord) -> None:
    """Deriva o estado global sem nunca tratar ambiguidade como conclusao."""
    statuses = [action.status for action in execution.actions]
    if ActionExecutionStatus.WRITE_AMBIGUOUS in statuses:
        if execution.status is not ExecutionStatus.INCONCLUSIVE:
            transition_execution(execution, ExecutionStatus.INCONCLUSIVE)
        return
    if any(
        status
        in {
            ActionExecutionStatus.CONFLICT,
            ActionExecutionStatus.FAILED,
            ActionExecutionStatus.BLOCKED,
        }
        for status in statuses
    ):
        if execution.status is not ExecutionStatus.COMPLETED_WITH_ERRORS:
            transition_execution(execution, ExecutionStatus.COMPLETED_WITH_ERRORS)
        execution.finished_at = utc_now()
        return
    if all(status is ActionExecutionStatus.VERIFIED for status in statuses):
        if execution.status is not ExecutionStatus.COMPLETED:
            transition_execution(execution, ExecutionStatus.COMPLETED)
        execution.finished_at = utc_now()
        return
    if execution.status is ExecutionStatus.RUNNING:
        transition_execution(execution, ExecutionStatus.AWAITING_CONFIRMATION)


def resume_execution(
    config: Config,
    *,
    execution_id: str,
    reason: str = "resume_after_transient_failure",
) -> ExecutionRecord:
    """Retoma uma execução sem reenviar ações verificadas nem ambíguas.

    Só aceita falhas transitórias anteriores ao envio. Ações ambíguas,
    conflitos e escritas pendentes exigem reconciliação/resolução humana.
    """
    execution = load_execution(config, execution_id)
    if execution.status is ExecutionStatus.CANCELLED:
        raise ExecutionError("execucao cancelada nao pode ser retomada")
    unsafe = {
        ActionExecutionStatus.WRITING,
        ActionExecutionStatus.WRITE_CONFIRMED,
        ActionExecutionStatus.VERIFICATION_PENDING,
        ActionExecutionStatus.WRITE_AMBIGUOUS,
        ActionExecutionStatus.CONFLICT,
    }
    if any(action.status in unsafe for action in execution.actions):
        raise ExecutionError(
            "execucao possui escrita ambigua/em curso ou conflito; use execution-reconcile/resolve"
        )
    if execution.status not in {
        ExecutionStatus.INCONCLUSIVE,
        ExecutionStatus.AWAITING_CONFIRMATION,
    }:
        raise ExecutionError(f"execucao nao pode ser retomada: {execution.status.value}")
    retryable = {
        "pre_write_read_failed",
        "pre_write_read_inconclusive",
        "write_failed_before_send",
    }
    reset = 0
    for action in execution.actions:
        if action.status is ActionExecutionStatus.FAILED and action.reason in retryable:
            action.status = ActionExecutionStatus.READY
            action.reason = None
            action.error = None
            reset += 1
    if execution.status is not ExecutionStatus.AWAITING_CONFIRMATION:
        transition_execution(execution, ExecutionStatus.AWAITING_CONFIRMATION)
    execution.finished_at = None
    _refresh_summary(execution)
    save_execution(config, execution, reason=f"{reason}:{reset}_failed_reset")
    return execution


def cancel_execution(
    config: Config,
    *,
    execution_id: str,
    reason: str = "cancel_requested",
) -> ExecutionRecord:
    """Cancela ações ainda não iniciadas; nunca interrompe uma escrita em curso."""
    execution = load_execution(config, execution_id)
    if execution.status not in {
        ExecutionStatus.PREPARED,
        ExecutionStatus.AWAITING_CONFIRMATION,
        ExecutionStatus.RUNNING,
    }:
        raise ExecutionError(f"execucao nao pode ser cancelada: {execution.status.value}")
    cancellable = {
        ActionExecutionStatus.PREPARED,
        ActionExecutionStatus.AWAITING_CONFIRMATION,
        ActionExecutionStatus.READY,
    }
    for action in execution.actions:
        if action.status in cancellable:
            transition_action(action, ActionExecutionStatus.CANCELLED)
    transition_execution(execution, ExecutionStatus.CANCELLED)
    execution.finished_at = utc_now()
    _refresh_summary(execution)
    save_execution(config, execution, reason=reason)
    return execution


def resolve_ambiguous_action(
    config: Config,
    *,
    execution_id: str,
    action_id: str,
    observed: ReviewStateSnapshot,
) -> ExecutionRecord:
    """Resolve uma escrita ambigua somente com evidencia autenticada e completa.

    Nao executa nova escrita. Se a review observada nao confirmar o payload,
    a acao vira conflito; se estiver ausente, a ambiguidade permanece.
    """
    execution = load_execution(config, execution_id)
    action = next((item for item in execution.actions if item.action_id == action_id), None)
    if action is None:
        raise ExecutionError("acao inexistente")
    if action.status is not ActionExecutionStatus.WRITE_AMBIGUOUS:
        raise ExecutionError(f"acao nao esta ambigua: {action.status.value}")
    if not observed.capture.complete or not observed.pagination.complete:
        raise ExecutionError("evidencia inconclusiva; ambiguidade permanece")
    if not observed.identity.verified or observed.identity.user_id != execution.user_id:
        raise ExecutionError("identidade divergente na evidencia")
    if observed.query.game_id != action.game_id:
        raise ExecutionError("evidencia de outro jogo")
    review = observed.result.review
    if review is None:
        raise ExecutionError("review ausente; ambiguidade permanece sem evidencia de aplicacao")
    differences = _review_differences(action, execution, review)
    action.verification = {"fields": differences, "review": review.model_dump(mode="json")}
    if differences:
        action.status = ActionExecutionStatus.CONFLICT
        action.reason = "ambiguous_resolved_conflict"
    else:
        action.status = ActionExecutionStatus.VERIFIED
        action.reason = "ambiguous_resolved_verified"
    execution.verification = {
        "resolution": "human_review",
        "action_id": action_id,
        "fields": differences,
    }
    _finalize_execution(execution)
    _refresh_summary(execution)
    save_execution(config, execution, reason="ambiguous_resolved")
    return execution
