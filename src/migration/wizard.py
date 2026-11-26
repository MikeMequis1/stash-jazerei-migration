"""Orquestração da migração pela interface gráfica.

Reutiliza aprovação, preflight, preparação e gateway existentes. Nenhuma regra
de negócio é reimplementada aqui.
"""

from __future__ import annotations

import contextlib
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.cancellation import OperationCancelled
from src.config import Config
from src.jazerei.identity import stored_user_id
from src.jazerei.snapshots import allocate_run_id, write_account_snapshot_run, write_snapshot_run
from src.jazerei.state import validate_snapshot_semantics
from src.migration.approval import (
    approval_status,
    approve_plan,
    load_bundle,
    verify_bundle,
)
from src.migration.execution import (
    ExecutionError,
    build_browser_write_transport,
    cancel_execution,
    confirm_action,
    execute_action,
    prepare_execution,
    resolve_ambiguous_action,
    resume_execution,
)
from src.migration.executions import (
    allocate_execution_id,
    create_execution,
    load_confirmations,
    load_events,
    load_execution,
    reconcile_execution,
    verify_execution,
)
from src.migration.preflights import (
    allocate_preflight_id,
    load_preflight,
    verify_preflight,
)
from src.migration.verifications import (
    allocate_verification_id,
    write_verification_run,
)
from src.migration.verifier import verify_plan
from src.models.approval import ApprovalStatus
from src.models.execution import ActionExecutionStatus, ExecutionStatus
from src.models.preflight import PreflightStatus
from src.models.snapshot import AccountReviewsSnapshot, ReviewStateSnapshot
from src.models.verification import VerificationAction, VerificationCounts, VerificationReport
from src.storage import read_json, sha256_file, utc_now, verify_manifest

DEFAULT_PLAN_ID = "stage9-review-20261125T201911Z"
UNSAFE_ACTION_STATUSES = {
    ActionExecutionStatus.WRITING,
    ActionExecutionStatus.WRITE_CONFIRMED,
    ActionExecutionStatus.VERIFICATION_PENDING,
    ActionExecutionStatus.WRITE_AMBIGUOUS,
    ActionExecutionStatus.CONFLICT,
}
RETRYABLE_FAILURES = {
    "pre_write_read_failed",
    "pre_write_read_inconclusive",
    "write_failed_before_send",
}
RESEND_FORBIDDEN_STATUSES = {
    ActionExecutionStatus.VERIFIED,
    ActionExecutionStatus.WRITE_AMBIGUOUS,
    ActionExecutionStatus.CONFLICT,
}


def _mask_user_id(user_id: str | None) -> str | None:
    if not user_id:
        return None
    if len(user_id) <= 8:
        return user_id[:2] + "…"
    return f"{user_id[:4]}…{user_id[-4:]}"


def _action_summary(action: Any) -> dict[str, Any]:
    return {
        "action_id": action.action_id,
        "source_ref": action.source_ref,
        "destination_slug": action.destination_slug,
        "game_id": action.game_id,
        "status": action.status.value,
        "reason": action.reason,
        "error": action.error,
        "write_result": action.write_result,
        "verification": action.verification,
    }


class MigrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: str | None = None,
        execution_id: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.execution_id = execution_id
        self.hint = hint

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": str(self)}
        for key, value in (
            ("code", self.code),
            ("detail", self.detail),
            ("execution_id", self.execution_id),
            ("hint", self.hint),
        ):
            if value:
                payload[key] = value
        return payload


@dataclass
class MigrationService:
    config: Config
    approvals: Path
    headless: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _run: dict[str, Any] | None = field(default=None, repr=False)
    _cancel_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    on_finished: Callable[[], None] | None = field(default=None, repr=False)

    def _effective_config(self) -> Config:
        """Atualiza a identidade quando o login ocorreu após a UI abrir."""
        current = self.__dict__["config"]
        source = current.jazerei_user_id_source
        if source == "env":
            return current
        if source == "" and current.jazerei_user_id:
            return current
        stored = stored_user_id(current.browser_dir)
        if stored and stored != current.jazerei_user_id:
            return replace(
                current,
                jazerei_user_id=stored,
                jazerei_user_id_source="session_file",
            )
        return current

    def _record_progress(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            if self._run is not None:
                if self._run.get("status") == "cancelling" and snapshot.get("status") == "running":
                    snapshot = {**snapshot, "status": "cancelling"}
                self._run.update(snapshot)
                self._run["updated_at"] = utc_now()

    def progress(self) -> dict[str, Any]:
        with self._lock:
            run = dict(self._run) if self._run else None
        if run is None:
            return {"running": False, "status": "idle"}
        execution_id = run.get("execution_id")
        if execution_id:
            try:
                execution = load_execution(self.config, execution_id)
            except Exception:  # noqa: BLE001 - execucao pode nao existir mais
                execution = None
            if execution is not None:
                run["execution_status"] = execution.status.value
                run["summary"] = execution.summary.model_dump(mode="json")
                run["actions"] = [
                    {
                        "action_id": action.action_id,
                        "source_ref": action.source_ref,
                        "game_id": action.game_id,
                        "status": action.status.value,
                        "reason": action.reason,
                    }
                    for action in execution.actions
                ]
        return run

    def start_execution(self, *, execution_id: str, plan_id: str, confirm: str) -> dict[str, Any]:
        if confirm != plan_id:
            raise MigrationError(
                "confirmação deve ser exatamente o plan_id",
                code="invalid_confirmation",
                execution_id=execution_id,
            )
        if not (self.config.allow_writes and self.config.enable_real_write_transport):
            raise MigrationError(
                "gates de escrita desabilitados; habilite allow_writes e "
                "enable_real_write_transport apenas para a execução autorizada",
                code="write_gates_disabled",
                execution_id=execution_id,
            )
        with self._lock:
            if self._run is not None and self._run.get("running"):
                raise MigrationError(
                    "já existe uma execução em andamento",
                    code="run_in_progress",
                    execution_id=str(self._run.get("execution_id") or ""),
                )
            execution = load_execution(self.config, execution_id)
            if execution.plan_id != plan_id:
                raise MigrationError(
                    "execução pertence a outro plano",
                    code="execution_plan_mismatch",
                    execution_id=execution_id,
                )
            if execution.status.value not in {"prepared", "awaiting_confirmation"}:
                raise MigrationError(
                    f"execução não está pronta para iniciar: {execution.status.value}",
                    code="execution_not_ready",
                    execution_id=execution_id,
                    hint="Crie uma nova execução com Preparar execução.",
                )
            self._cancel_requested.clear()
            self._run = {
                "running": True,
                "status": "starting",
                "execution_id": execution_id,
                "plan_id": plan_id,
                "started_at": utc_now(),
                "finished_at": None,
                "current_index": 0,
                "total": len(execution.actions),
                "current_action": None,
                "last_action": None,
                "cancel_requested": False,
                "error": None,
                "execution_status": execution.status.value,
                "summary": execution.summary.model_dump(mode="json"),
            }
        worker = threading.Thread(
            target=self._execute_worker,
            args=(execution_id, plan_id),
            name=f"migration-{execution_id}",
            daemon=True,
        )
        worker.start()
        return {
            "execution_id": execution_id,
            "status": "running",
            "started": True,
            "total": len(execution.actions),
        }

    def cancel(self, *, execution_id: str, plan_id: str, confirm: str) -> dict[str, Any]:
        if confirm != plan_id:
            raise MigrationError(
                "confirmação deve ser exatamente o plan_id",
                code="invalid_confirmation",
                execution_id=execution_id,
            )
        with self._lock:
            active = (
                self._run is not None
                and self._run.get("running")
                and self._run.get("execution_id") == execution_id
            )
            if active:
                self._cancel_requested.set()
                self._run["status"] = "cancelling"
                self._run["cancel_requested"] = True
                return {
                    "execution_id": execution_id,
                    "status": "cancelling",
                    "cancel_requested": True,
                    "message": "cancelamento solicitado; a ação atual será concluída antes de parar",
                }
        execution = load_execution(self.config, execution_id)
        interrupted = [
            action
            for action in execution.actions
            if action.status
            in {
                ActionExecutionStatus.WRITING,
                ActionExecutionStatus.VERIFICATION_PENDING,
            }
        ]
        if interrupted:
            raise MigrationError(
                "execução possui escrita em curso/interrompida; reconcilie antes de cancelar",
                code="execution_interrupted",
                execution_id=execution_id,
                hint="Use execution-reconcile/resolve; cancelar agora perderia a evidência da escrita.",
            )
        if execution.status.value in {"prepared", "awaiting_confirmation"}:
            cancelled = cancel_execution(
                self.config, execution_id=execution_id, reason="cancel_requested_manual"
            )
            return {
                "execution_id": execution_id,
                "status": cancelled.status.value,
                "cancel_requested": True,
            }
        raise MigrationError(
            f"execução não pode ser cancelada no estado {execution.status.value}",
            code="cancel_not_possible",
            execution_id=execution_id,
            hint="Execuções inconclusivas precisam de reconcile/resolve; não há cancelamento retroativo.",
        )

    def resume(self, *, execution_id: str, plan_id: str, confirm: str) -> dict[str, Any]:
        if confirm != plan_id:
            raise MigrationError(
                "confirmação deve ser exatamente o plan_id",
                code="invalid_confirmation",
                execution_id=execution_id,
            )
        config = self._effective_config()
        if not (config.allow_writes and config.enable_real_write_transport):
            raise MigrationError(
                "gates de escrita desabilitados; habilite allow_writes e "
                "enable_real_write_transport apenas para a execução autorizada",
                code="write_gates_disabled",
                execution_id=execution_id,
            )
        with self._lock:
            if self._run is not None and self._run.get("running"):
                raise MigrationError(
                    "já existe uma execução em andamento",
                    code="run_in_progress",
                    execution_id=str(self._run.get("execution_id") or ""),
                )
        execution = load_execution(config, execution_id)
        if execution.plan_id != plan_id:
            raise MigrationError(
                "execução pertence a outro plano",
                code="execution_plan_mismatch",
                execution_id=execution_id,
            )
        try:
            reconcile_execution(config, execution_id)
            resumed = resume_execution(config, execution_id=execution_id)
        except ExecutionError as exc:
            raise MigrationError(
                str(exc),
                code="resume_not_possible",
                detail=str(exc),
                execution_id=execution_id,
                hint="Resolva ações ambíguas/conflitos antes de retomar.",
            ) from exc
        return self.start_execution(
            execution_id=resumed.execution_id,
            plan_id=plan_id,
            confirm=confirm,
        )

    def _execute_worker(self, execution_id: str, plan_id: str) -> None:
        try:
            self.execute(
                execution_id=execution_id,
                plan_id=plan_id,
                confirm=plan_id,
                cancel_event=self._cancel_requested,
                on_progress=self._record_progress,
            )
            record = load_execution(self.config, execution_id)
            status = "cancelled" if record.status.value == "cancelled" else "finished"
            error = None
            if record.status.value in {
                "inconclusive",
                "blocked",
                "failed",
                "completed_with_errors",
            }:
                bad = next(
                    (
                        action
                        for action in record.actions
                        if action.status.value
                        in {"write_ambiguous", "failed", "conflict", "blocked"}
                    ),
                    None,
                )
                if bad is not None:
                    browser_lost = "read_failed" in (bad.reason or "") or "Browser" in (
                        bad.error or ""
                    )
                    error = {
                        "code": "browser_closed"
                        if browser_lost
                        else (bad.reason or record.status.value),
                        "message": (
                            "Navegador fechado ou leitura autenticada interrompida."
                            if browser_lost
                            else f"execução interrompida em {bad.source_ref}"
                        ),
                        "detail": self._action_error_detail(bad),
                        "hint": (
                            "A execução foi interrompida. Não reenvie a ação ambígua; "
                            "crie uma nova execução após revisar o estado."
                            if browser_lost
                            else None
                        ),
                        "action_id": bad.action_id,
                        "source_ref": bad.source_ref,
                    }
            self._finish_run(status=status, execution=record, error=error)
        except Exception as exc:  # noqa: BLE001 - erro do worker vira estado observavel
            reconciled = None
            try:
                reconciled = reconcile_execution(self.config, execution_id)
            except Exception:  # noqa: BLE001 - reconciliacao nao pode esconder o erro
                reconciled = None
            self._finish_run(
                status="error",
                execution=reconciled,
                error=self._describe_error(exc, execution_id=execution_id),
            )

    def _finish_run(
        self,
        *,
        status: str,
        execution: Any,
        error: dict[str, Any] | None,
    ) -> None:
        with self._lock:
            if self._run is None:
                return
            self._run["running"] = False
            self._run["status"] = status
            self._run["finished_at"] = utc_now()
            self._run["cancel_requested"] = self._cancel_requested.is_set()
            self._run["error"] = error
            if execution is not None:
                self._run["execution_status"] = execution.status.value
                self._run["summary"] = execution.summary.model_dump(mode="json")
        callback = self.on_finished
        self.on_finished = None
        if callback is not None:
            with contextlib.suppress(Exception):
                callback()

    @staticmethod
    def _action_error_detail(action: Any) -> str:
        parts = [f"status={action.status.value}"]
        if action.reason:
            parts.append(f"reason={action.reason}")
        if action.write_result:
            parts.append(f"http_status={action.write_result.get('status_code')}")
        if action.error:
            parts.append(f"error={action.error}")
        return "; ".join(parts)

    @staticmethod
    def _describe_error(exc: Exception, *, execution_id: str) -> dict[str, Any]:
        message = str(exc)
        lowered = message.lower()
        browser_closed = any(
            marker in lowered
            for marker in (
                "janela",
                "navegador",
                "browser",
                "target closed",
                "has been closed",
                "playwright",
            )
        )
        if browser_closed:
            return {
                "code": "browser_closed",
                "message": "Navegador fechado ou sessão interrompida.",
                "detail": message,
                "hint": (
                    "A execução foi reconciliada. Se houver ação ambígua, "
                    "não reenvie automaticamente; crie uma nova execução após revisar."
                ),
                "execution_id": execution_id,
            }
        return {
            "code": "execution_error",
            "message": "Falha inesperada durante a execução.",
            "detail": f"{type(exc).__name__}: {message}",
            "hint": "Consulte execution-status e execution-reconcile antes de tentar novamente.",
            "execution_id": execution_id,
        }

    def _execution_entry(self, record: Any) -> dict[str, Any]:
        config = self._effective_config()
        counts = Counter(action.status.value for action in record.actions)
        run_active = bool(
            self._run
            and self._run.get("running")
            and self._run.get("execution_id") == record.execution_id
        )
        has_writing = any(
            action.status
            in {
                ActionExecutionStatus.WRITING,
                ActionExecutionStatus.VERIFICATION_PENDING,
            }
            for action in record.actions
        )
        interrupted = (has_writing or record.status is ExecutionStatus.RUNNING) and not run_active
        ambiguous = [
            _action_summary(action)
            for action in record.actions
            if action.status is ActionExecutionStatus.WRITE_AMBIGUOUS
        ]
        conflicts = [
            _action_summary(action)
            for action in record.actions
            if action.status is ActionExecutionStatus.CONFLICT
        ]
        transient = [
            _action_summary(action)
            for action in record.actions
            if action.status is ActionExecutionStatus.FAILED
        ]
        resume_actions = [
            _action_summary(action)
            for action in record.actions
            if action.status is ActionExecutionStatus.FAILED and action.reason in RETRYABLE_FAILURES
        ]
        never_resend = [
            _action_summary(action)
            for action in record.actions
            if action.status in RESEND_FORBIDDEN_STATUSES
        ]
        unsafe = any(action.status in UNSAFE_ACTION_STATUSES for action in record.actions)
        gates = {
            "allow_writes": config.allow_writes,
            "enable_real_write_transport": config.enable_real_write_transport,
        }
        gates_on = gates["allow_writes"] and gates["enable_real_write_transport"]
        resumable = (
            record.status.value == "inconclusive"
            and not unsafe
            and all(
                action.status is not ActionExecutionStatus.FAILED
                or action.reason in RETRYABLE_FAILURES
                for action in record.actions
            )
            and record.status is not ExecutionStatus.CANCELLED
        )
        can_execute = (
            record.status.value in {"prepared", "awaiting_confirmation"}
            and gates_on
            and not interrupted
        )
        can_resume = resumable and gates_on and not ambiguous and not conflicts
        can_cancel = (
            record.status.value in {"prepared", "awaiting_confirmation", "running"}
            and not interrupted
        )
        can_reconcile = interrupted
        can_resolve = bool(ambiguous)
        write_operation_relevant = (
            record.status.value in {"prepared", "awaiting_confirmation"} or resumable
        )
        blocked_reasons: list[str] = []
        if not gates_on and write_operation_relevant:
            blocked_reasons.append("gates de escrita desligados")
        if interrupted:
            blocked_reasons.append("há escrita em curso ou interrompida; reconcilie primeiro")
        if ambiguous:
            blocked_reasons.append(
                f"{len(ambiguous)} ação(ões) ambígua(s) exigem resolução com evidência"
            )
        if conflicts:
            blocked_reasons.append(f"{len(conflicts)} conflito(s) impedem retomada")
        if record.status.value == "inconclusive" and not resumable and not unsafe:
            blocked_reasons.append("nenhuma falha transitória elegível para retomada")
        control_reasons = {
            "execute": ["gates de escrita desligados"]
            if not gates_on and record.status.value in {"prepared", "awaiting_confirmation"}
            else [],
            "resume": ["gates de escrita desligados"] if not gates_on and resumable else [],
            "cancel": ["escrita interrompida exige reconciliação"] if interrupted else [],
            "reconcile": ["há escrita em curso ou interrompida"] if interrupted else [],
            "resolve": [f"{len(ambiguous)} ação(ões) ambígua(s) exigem resolução"]
            if ambiguous
            else [],
        }
        last_failure = next(
            (
                _action_summary(action)
                for action in reversed(record.actions)
                if action.status
                in {
                    ActionExecutionStatus.FAILED,
                    ActionExecutionStatus.WRITE_AMBIGUOUS,
                    ActionExecutionStatus.CONFLICT,
                    ActionExecutionStatus.BLOCKED,
                }
            ),
            None,
        )
        return {
            "execution_id": record.execution_id,
            "plan_id": record.plan_id,
            "status": record.status.value,
            "summary": record.summary.model_dump(mode="json"),
            "counts": dict(counts),
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "resumable": resumable,
            "has_ambiguous": bool(ambiguous),
            "has_conflicts": bool(conflicts),
            "has_interrupted": interrupted,
            "ambiguous_actions": ambiguous,
            "conflict_actions": conflicts,
            "transient_failures": transient,
            "resume_actions": resume_actions,
            "never_resend": never_resend,
            "last_failure": last_failure,
            "blocking": [
                _action_summary(action)
                for action in record.actions
                if action.status in UNSAFE_ACTION_STATUSES
                or action.status is ActionExecutionStatus.FAILED
            ],
            "gates": gates,
            "controls": {
                "can_execute": can_execute,
                "can_resume": can_resume,
                "can_cancel": can_cancel,
                "can_reconcile": can_reconcile,
                "can_resolve": can_resolve,
                "blocked_reasons": blocked_reasons,
                "reasons": control_reasons,
            },
        }

    def list_executions(self, plan_id: str | None = None) -> list[dict[str, Any]]:
        root = self.config.migration_dir / "executions"
        if not root.is_dir():
            return []
        executions: list[dict[str, Any]] = []
        for path in sorted(root.iterdir(), reverse=True):
            if not path.is_dir() or not (path / "execution.json").is_file():
                continue
            try:
                record = load_execution(self.config, path.name)
            except Exception:  # noqa: BLE001 - artefato invalido nao derruba a lista
                continue
            if plan_id and record.plan_id != plan_id:
                continue
            executions.append(self._execution_entry(record))
        return executions

    def list_plans(self) -> list[dict[str, Any]]:
        root = self.config.migration_dir / "plans"
        if not root.is_dir():
            return []
        plans: list[dict[str, Any]] = []
        for path in root.iterdir():
            if not path.is_dir() or not (path / "manifest.json").is_file():
                continue
            try:
                bundle = load_bundle(self.config, path.name)
                verification = verify_bundle(self.config, bundle)
                approval = approval_status(self.config, path.name, self.approvals)
            except Exception:  # noqa: BLE001 - plano quebrado continua visivel no historico?
                continue
            executions = self.list_executions(path.name)
            plans.append(
                {
                    "plan_id": path.name,
                    "created_at": bundle.manifest.created_at,
                    "actions": len(bundle.plan.actions),
                    "unmatched": len(bundle.plan.unmatched),
                    "skipped": len(bundle.plan.skipped),
                    "provenance_complete": bundle.manifest.provenance_complete,
                    "approval_status": approval.status.value,
                    "invalid": verification.invalid,
                    "stale": verification.stale,
                    "executions": len(executions),
                    "latest_execution": executions[0] if executions else None,
                }
            )
        plans.sort(key=lambda item: str(item.get("created_at") or item["plan_id"]), reverse=True)
        return plans

    def resolve_plan_id(self, preferred: str | None = None) -> str:
        if preferred:
            return preferred
        plans = self.list_plans()
        if not plans:
            return DEFAULT_PLAN_ID
        approved = [item for item in plans if item["approval_status"] == "approved"]
        return str((approved or plans)[0]["plan_id"])

    def execution_detail(self, execution_id: str) -> dict[str, Any]:
        record = load_execution(self.config, execution_id)
        events = load_events(self.config, execution_id)
        confirmations = load_confirmations(self.config, execution_id)
        errors = verify_execution(self.config, execution_id)
        titles: dict[str, str] = {}
        try:
            bundle = load_bundle(self.config, record.plan_id)
            for action in bundle.plan.actions:
                payload = action.source_payload or {}
                title = payload.get("game_title") or action.source_ref
                titles[action.source_ref] = str(title)
        except Exception:  # noqa: BLE001 - plano antigo nao impede o detalhe
            pass
        actions = []
        for action in record.actions:
            payload = action.payload or {}
            planned = action.planned_after or {}
            text = str(payload.get("text") or planned.get("text") or "")
            actions.append(
                {
                    "action_id": action.action_id,
                    "kind": action.kind,
                    "title": titles.get(action.source_ref),
                    "source_ref": action.source_ref,
                    "destination_slug": action.destination_slug,
                    "game_id": action.game_id,
                    "status": action.status.value,
                    "reason": action.reason,
                    "error": action.error,
                    "rating": payload.get("rating", planned.get("rating")),
                    "liked": payload.get("liked", planned.get("liked")),
                    "text": text,
                    "text_preview": text[:240],
                    "payload": action.payload,
                    "planned_before": action.planned_before,
                    "planned_after": action.planned_after,
                    "write_result": action.write_result,
                    "verification": action.verification,
                    "confirmation_id": action.confirmation_id,
                    "confirmed_at": action.confirmed_at,
                }
            )
        entry = self._execution_entry(record)
        return {
            **entry,
            "user_id_masked": _mask_user_id(record.user_id),
            "plan_sha256": record.plan_sha256,
            "manifest_sha256": record.manifest_sha256,
            "preflight_id": record.preflight_id,
            "preflight_sha256": record.preflight_sha256,
            "preflight_manifest_sha256": record.preflight_manifest_sha256,
            "approval_id": record.approval_id,
            "identity_verified": record.identity_verified,
            "actions": actions,
            "confirmations": [
                {
                    "action_id": item.action_id,
                    "confirmation_id": item.confirmation_id,
                    "game_id": item.game_id,
                    "confirmed_at": item.confirmed_at,
                    "confirmed_by": item.confirmed_by,
                    "status": item.status,
                    "payload_sha256": item.payload_sha256,
                }
                for item in confirmations
            ],
            "confirmations_total": len(confirmations),
            "events": [event.model_dump(mode="json") for event in events],
            "events_total": len(events),
            "artifact_errors": errors,
        }

    def _ensure_not_active_run(self, execution_id: str) -> None:
        run = self._run
        if run and run.get("running") and run.get("execution_id") == execution_id:
            raise MigrationError(
                "execução em andamento; aguarde a parada ou use Cancelar execução",
                code="run_in_progress",
                execution_id=execution_id,
            )

    def reconcile(self, execution_id: str) -> dict[str, Any]:
        self._ensure_not_active_run(execution_id)
        record = reconcile_execution(self.config, execution_id)
        return self.execution_detail(record.execution_id)

    def resolve_ambiguity(
        self,
        execution_id: str,
        action_id: str,
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Resolve uma ambiguidade com releitura autenticada; nunca reenvia."""
        from src.browser.session import BrowserSession
        from src.jazerei.state import capture_review_state

        self._ensure_not_active_run(execution_id)
        config = self._effective_config()
        execution = load_execution(config, execution_id)
        action = next((item for item in execution.actions if item.action_id == action_id), None)
        if action is None:
            raise MigrationError(
                "ação inexistente",
                code="action_missing",
                execution_id=execution_id,
            )
        if action.status is not ActionExecutionStatus.WRITE_AMBIGUOUS:
            raise MigrationError(
                f"ação não está ambígua: {action.status.value}",
                code="action_not_ambiguous",
                execution_id=execution_id,
                hint="Reconcilie uma escrita interrompida antes de resolver.",
            )
        expected_user_id = config.jazerei_user_id
        if not expected_user_id:
            raise MigrationError(
                "identidade do Já Zerei ausente",
                code="identity_missing",
                execution_id=execution_id,
                hint="Faça login/verifique a sessão antes de resolver a ambiguidade.",
            )
        if action.game_id is None:
            raise MigrationError(
                "ação ambígua sem game_id",
                code="action_without_game",
                execution_id=execution_id,
            )
        slug = action.destination_slug or action.source_ref
        try:
            run_id = allocate_run_id(config, slug=slug, game_id=action.game_id)
            with BrowserSession(config, headless=self.headless) as session:
                snapshot = capture_review_state(
                    session,
                    game_id=action.game_id,
                    slug=slug,
                    expected_user_id=expected_user_id,
                    page_size=config.jazerei_page_size,
                    on_page=progress,
                    should_cancel=should_cancel,
                )
            artifact = write_snapshot_run(config, snapshot, run_id=run_id)
        except OperationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - captura inconclusiva não resolve
            raise MigrationError(
                "não foi possível capturar evidência autenticada",
                code="resolution_capture_failed",
                detail=f"{type(exc).__name__}: {exc}",
                execution_id=execution_id,
                hint="A ação permanece ambígua; nenhum novo POST foi feito.",
            ) from exc
        try:
            updated = resolve_ambiguous_action(
                config,
                execution_id=execution_id,
                action_id=action_id,
                observed=snapshot,
            )
        except ExecutionError as exc:
            raise MigrationError(
                str(exc),
                code="resolution_inconclusive",
                detail=f"snapshot={artifact.run_id}; {exc}",
                execution_id=execution_id,
                hint=(
                    "A evidência não foi suficiente. A ação permanece ambígua e "
                    "não deve ser reenviada."
                ),
            ) from exc
        return {
            **self.execution_detail(updated.execution_id),
            "resolution_snapshot": {
                "run_id": artifact.run_id,
                "snapshot": str(artifact.snapshot_path) if artifact.snapshot_path else None,
                "manifest": str(artifact.manifest_path),
                "exists": snapshot.result.exists,
                "complete": snapshot.capture.complete,
                "response_hash": snapshot.capture.response_hash,
            },
        }

    def _load_account_snapshot(
        self, config: Config, snapshot_path: Path | str
    ) -> tuple[Path, AccountReviewsSnapshot, Path]:
        root = (config.jazerei_dir / "runs").resolve()
        path = Path(snapshot_path).resolve()
        if path != root and root not in path.parents:
            raise MigrationError(
                "snapshot fora do diretório permitido",
                code="snapshot_path_invalid",
            )
        if not path.is_file():
            raise MigrationError(
                f"snapshot ausente: {path.name}",
                code="snapshot_missing",
            )
        run_dir = path.parent.parent
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            raise MigrationError("manifesto do snapshot ausente", code="snapshot_manifest_missing")
        errors = verify_manifest(run_dir)
        if errors:
            raise MigrationError(
                "snapshot inválido",
                code="snapshot_invalid",
                detail="; ".join(errors),
            )
        try:
            snapshot = AccountReviewsSnapshot.model_validate(read_json(path))
        except Exception as exc:  # noqa: BLE001
            raise MigrationError(
                "snapshot de conta inválido",
                code="snapshot_invalid",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        return path, snapshot, manifest_path

    def _prior_evidence(
        self,
        config: Config,
        bundle: Any,
        execution: Any,
        actions: list[Any],
    ) -> tuple[dict[int, str], str | None, list[str]]:
        prior: dict[int, str] = {}
        sources: list[str] = []
        prior_run_id: str | None = None
        snapshot_input = bundle.manifest.inputs.get("snapshot") or ""
        if snapshot_input:
            try:
                path, snapshot, _manifest = self._load_account_snapshot(config, snapshot_input)
                validation = validate_snapshot_semantics(
                    snapshot, expected_user_id=execution.user_id
                )
                if validation.status == "valid":
                    prior_run_id = path.parent.parent.name
                    found = {review.game_id for review in snapshot.reviews}
                    for action in actions:
                        if action.game_id is not None:
                            prior[action.game_id] = "found" if action.game_id in found else "absent"
                    sources.append("plano")
            except MigrationError:
                pass
        if execution.preflight_id:
            try:
                if verify_preflight(config, execution.preflight_id):
                    raise MigrationError("preflight invalido")
                preflight = load_preflight(config, execution.preflight_id)
                root = config.migration_dir / "preflights" / execution.preflight_id
                for entry in preflight.snapshots:
                    game_id = entry.get("game_id")
                    relative = entry.get("path")
                    if not isinstance(game_id, int) or not isinstance(relative, str):
                        continue
                    snapshot_path = root / relative
                    if not snapshot_path.is_file():
                        continue
                    snapshot = ReviewStateSnapshot.model_validate(read_json(snapshot_path))
                    validation = validate_snapshot_semantics(
                        snapshot,
                        expected_user_id=execution.user_id,
                        expected_game_id=game_id,
                    )
                    if validation.status != "valid":
                        continue
                    prior[game_id] = "found" if snapshot.result.review else "absent"
                sources.append("preflight")
            except Exception:  # noqa: BLE001 - evidência antiga não bloqueia o relatório
                pass
        return prior, prior_run_id, sources

    @staticmethod
    def _rating_match(planned: Any, found: Any) -> bool | None:
        if planned is None and found is None:
            return None
        if planned is None or found is None:
            return False
        try:
            return float(planned) == float(found)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _text_match(planned: str, found: str | None) -> bool:
        if found is None:
            return False
        return planned == found

    def _before_read_presence(
        self,
        execution_action: Any,
        *,
        game_id: int | None,
        expected_slug: str | None,
        expected_user_id: str,
    ) -> tuple[str, list[str], dict[str, Any]]:
        if not execution_action or not execution_action.before_read:
            return "unavailable", [], {"status": "unavailable"}
        raw = execution_action.before_read
        try:
            snapshot = ReviewStateSnapshot.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - estrutura invalida nao prova ausencia
            return (
                "unverifiable",
                [f"before_read inválido: {type(exc).__name__}"],
                {"status": "invalid", "error": type(exc).__name__},
            )
        validation = validate_snapshot_semantics(
            snapshot,
            expected_user_id=expected_user_id,
            expected_game_id=game_id,
            expected_slug=expected_slug,
        )
        evidence = {
            "status": validation.status,
            "hash_status": validation.hash_status,
            "complete": validation.complete,
            "identity_ok": validation.identity_ok,
            "errors": validation.errors,
            "game_id": snapshot.query.game_id,
            "slug": snapshot.query.slug,
            "result_exists": snapshot.result.exists,
            "review_present": snapshot.result.review is not None,
        }
        if validation.status != "valid":
            return (
                "unverifiable",
                [f"before_read não validado: {validation.status}"] + validation.errors,
                evidence,
            )
        if snapshot.result.review is not None:
            return "found", [], evidence
        return "absent", [], evidence

    def _verification_action(
        self,
        *,
        plan_action: Any,
        execution_action: Any,
        reviews_by_game: dict[int, list[Any]],
        snapshot_run_id: str,
        snapshot_complete: bool,
        snapshot_semantic_valid: bool,
        snapshot_semantic_status: str,
        identity_verified: bool,
        execution_user_id: str,
        prior: dict[int, str],
        execution_errors: list[str],
        plan_blocked: bool,
        plan_block_reasons: list[str],
    ) -> VerificationAction:
        game_id = plan_action.game_id or (execution_action.game_id if execution_action else None)
        planned = (
            (execution_action.payload if execution_action and execution_action.payload else None)
            or plan_action.after
            or {}
        )
        planned_rating = planned.get("rating")
        planned_text = str(planned.get("text") or "")
        planned_liked = planned.get("liked")
        planned_slug = plan_action.destination_slug or (
            execution_action.destination_slug if execution_action else None
        )
        execution_status = execution_action.status.value if execution_action else "not_prepared"
        title = (plan_action.source_payload or {}).get("game_title") or plan_action.source_ref
        reasons: list[str] = []
        candidates = reviews_by_game.get(game_id, []) if game_id is not None else []
        found = candidates[0] if len(candidates) == 1 else None
        presence: str
        content_status: str = "not_compared"
        rating_match: bool | None = None
        text_match: bool | None = None
        liked_status: str = "not_planned"
        game_slug_match: bool | None = None
        user_match: bool | None = None
        found_rating = None
        found_text = None
        found_liked = None
        review_id = None

        if not snapshot_complete:
            presence = "inconclusive"
            reasons.append("captura do destino incompleta")
        elif not identity_verified:
            presence = "inconclusive"
            reasons.append("identidade do snapshot não confere com a execução")
        elif game_id is None:
            presence = "inconclusive"
            reasons.append("ação sem game_id")
        elif len(candidates) > 1:
            presence = "duplicate"
            reasons.append(f"{len(candidates)} reviews encontradas para o mesmo game_id")
        elif not candidates:
            presence = "absent"
            reasons.append("review ausente no snapshot posterior")
        else:
            found = candidates[0]
            review_id = str(found.id) if found.id else None
            found_rating = found.rating
            found_text = found.text
            found_liked = found.liked
            rating_match = self._rating_match(planned_rating, found_rating)
            text_match = self._text_match(planned_text, found_text)
            if planned_liked is None:
                liked_status = "not_planned"
            elif found_liked is None:
                liked_status = "unavailable"
                reasons.append("liked indisponível no snapshot; valor não inferido")
            else:
                liked_status = "match" if bool(planned_liked) is bool(found_liked) else "mismatch"
            if planned_slug and found.game_slug:
                game_slug_match = planned_slug == found.game_slug
            if found.user_id:
                user_match = found.user_id == execution_user_id
            checks = [rating_match, text_match, game_slug_match, user_match]
            if planned_liked is not None and liked_status != "unavailable":
                checks.append(liked_status == "match")
            if any(value is False for value in checks):
                content_status = "mismatch"
                presence = "divergent"
                if rating_match is False:
                    reasons.append(
                        f"rating divergente (planejado {planned_rating}, encontrado {found_rating})"
                    )
                if text_match is False:
                    reasons.append("texto divergente")
                if liked_status == "mismatch":
                    reasons.append(
                        f"liked divergente (planejado {planned_liked}, encontrado {found_liked})"
                    )
                if game_slug_match is False:
                    reasons.append(
                        f"slug divergente (planejado {planned_slug}, encontrado {found.game_slug})"
                    )
                if user_match is False:
                    reasons.append("usuário da review diverge da execução")
            elif any(value is None for value in checks) or liked_status == "unavailable":
                content_status = (
                    "partial" if any(value is True for value in checks) else "unverifiable"
                )
                presence = "found"
            else:
                content_status = "full"
                presence = "found"

        prior_presence = "unavailable"
        before_status, before_reasons, before_evidence = self._before_read_presence(
            execution_action,
            game_id=game_id,
            expected_slug=planned_slug or plan_action.source_ref,
            expected_user_id=execution_user_id,
        )
        reasons.extend(before_reasons)
        if before_status != "unavailable":
            prior_presence = before_status
        elif game_id is not None and game_id in prior:
            prior_presence = prior[game_id]

        causal = "insufficient"
        execution_verification_review = None
        if execution_action and execution_action.verification:
            verification_review = execution_action.verification.get("review")
            if isinstance(verification_review, dict):
                execution_verification_review = verification_review.get("id")
        if execution_status == "verified":
            if not review_id or not execution_verification_review:
                causal = "insufficient"
            elif str(execution_verification_review) != str(review_id):
                causal = "verified_id_mismatch"
                reasons.append("review encontrada não é a mesma observada pela execução")
            elif prior_presence != "absent":
                causal = "prior_presence_unknown_or_found"
                reasons.append("não há evidência de ausência anterior suficiente")
            elif content_status != "full":
                causal = "content_incomplete"
            else:
                causal = "verified_id_match_prior_absent"
        elif execution_status in {"write_ambiguous"}:
            causal = "ambiguous_not_attributed"
        elif execution_status in {"conflict"}:
            causal = "conflict_not_attributed"
        elif execution_status in {"failed"}:
            causal = "failed_not_attributed"
        elif (
            execution_status in {"ready", "prepared", "awaiting_confirmation", "cancelled"}
            or execution_status == "not_prepared"
        ):
            causal = "not_executed"

        confirmation: str
        if execution_status == "write_ambiguous":
            confirmation = "ambiguous"
            if not snapshot_complete:
                reasons.append("captura incompleta; ambiguidade permanece")
        elif execution_status == "conflict":
            confirmation = "conflict"
            if not snapshot_complete:
                reasons.append("captura incompleta; conflito permanece")
        elif (
            not snapshot_complete
            or not snapshot_semantic_valid
            or not identity_verified
            or execution_errors
        ):
            confirmation = "inconclusive"
            if not snapshot_semantic_valid:
                reasons.append(f"snapshot sem validade semântica: {snapshot_semantic_status}")
            if execution_errors:
                reasons.append("artefatos da execução não passaram em verify_execution")
        elif execution_status in {"ready", "prepared", "awaiting_confirmation", "cancelled"}:
            if presence in {"found", "divergent", "duplicate"}:
                confirmation = "found_not_attributed"
                reasons.append("review encontrada, mas a ação não foi executada nesta execução")
            else:
                confirmation = "not_executed"
        elif execution_status == "failed":
            confirmation = (
                "found_not_attributed"
                if presence in {"found", "divergent", "duplicate"}
                else "absent"
            )
            if not reasons:
                reasons.append("execução falhou antes de confirmar escrita")
        elif execution_status == "verified":
            if presence == "absent":
                confirmation = "absent"
                reasons.append("execução verificada, mas review ausente no snapshot posterior")
            elif presence in {"duplicate", "inconclusive"}:
                confirmation = "inconclusive"
            elif presence == "divergent":
                confirmation = "divergent"
            elif liked_status == "unavailable":
                confirmation = "inconclusive"
                reasons.append("liked planejado não pôde ser verificado no snapshot")
            elif causal == "verified_id_match_prior_absent" and content_status == "full":
                confirmation = "confirmed"
            elif content_status in {"full", "partial"}:
                confirmation = "found_not_attributed"
                reasons.append("review encontrada, mas vínculo causal não comprovado")
            else:
                confirmation = "inconclusive"
        else:
            confirmation = "inconclusive"
            reasons.append(f"estado de execução não classificado: {execution_status}")

        confirmation_blocked_by_plan = False
        if plan_blocked and confirmation == "confirmed":
            confirmation = "inconclusive"
            confirmation_blocked_by_plan = True
            reasons.append(
                "confirmação global bloqueada pelo plano: "
                + "; ".join(plan_block_reasons or ["plano inválido"])
            )

        return VerificationAction(
            action_id=execution_action.action_id if execution_action else plan_action.source_ref,
            source_ref=plan_action.source_ref,
            title=str(title) if title else None,
            destination_slug=planned_slug,
            game_id=game_id,
            execution_status=execution_status,
            presence=presence,
            confirmation=confirmation,
            content_status=content_status,
            planned_rating=planned_rating,
            found_rating=found_rating,
            rating_match=rating_match,
            planned_text=planned_text,
            found_text=found_text,
            text_match=text_match,
            planned_liked=planned_liked,
            found_liked=found_liked,
            liked_status=liked_status,
            game_slug_match=game_slug_match,
            user_match=user_match,
            prior_presence=prior_presence,
            causal=causal,
            confirmation_blocked_by_plan=confirmation_blocked_by_plan,
            review_id=review_id,
            snapshot_ref=snapshot_run_id,
            reasons=reasons,
            evidence={
                "execution_action_id": execution_action.action_id if execution_action else None,
                "confirmation_id": execution_action.confirmation_id if execution_action else None,
                "confirmed_at": execution_action.confirmed_at if execution_action else None,
                "execution_verification_review_id": execution_verification_review,
                "write_result": execution_action.write_result if execution_action else None,
                "before_read_present": bool(execution_action.before_read)
                if execution_action
                else False,
                "before_read": before_evidence,
                "snapshot_semantic_status": snapshot_semantic_status,
                "prior_presence": prior_presence,
            },
        )

    def verification_report(
        self,
        plan_id: str,
        execution_id: str,
        snapshot_path: Path | str,
        *,
        verification_id: str = "pending",
    ) -> VerificationReport:
        config = self._effective_config()
        bundle = load_bundle(config, plan_id)
        verification = verify_bundle(config, bundle)
        execution = load_execution(config, execution_id)
        if execution.plan_id != plan_id:
            raise MigrationError(
                "execução pertence a outro plano",
                code="execution_plan_mismatch",
                execution_id=execution_id,
            )
        path, snapshot, manifest_path = self._load_account_snapshot(config, snapshot_path)
        run_id = path.parent.parent.name
        snapshot_validation = validate_snapshot_semantics(
            snapshot, expected_user_id=execution.user_id
        )
        snapshot_complete = snapshot_validation.complete
        identity_verified = snapshot_validation.identity_ok
        snapshot_semantic_valid = snapshot_validation.status == "valid"
        execution_errors = verify_execution(config, execution_id)
        plan_block_reasons = list(verification.invalid) + list(verification.stale)
        if (
            not bundle.manifest.provenance_complete
            and "proveniencia incompleta" not in plan_block_reasons
        ):
            plan_block_reasons.append("proveniencia incompleta")
        plan_blocked = bool(plan_block_reasons)
        prior, prior_run_id, prior_sources = self._prior_evidence(
            config, bundle, execution, bundle.plan.actions
        )
        reviews_by_game: dict[int, list[Any]] = {}
        for review in snapshot.reviews:
            reviews_by_game.setdefault(review.game_id, []).append(review)
        execution_by_source = {action.source_ref: action for action in execution.actions}
        plan_verification = verify_plan(bundle.plan, snapshot.reviews).model_dump(mode="json")
        actions: list[VerificationAction] = []
        for plan_action in bundle.plan.actions:
            actions.append(
                self._verification_action(
                    plan_action=plan_action,
                    execution_action=execution_by_source.get(plan_action.source_ref),
                    reviews_by_game=reviews_by_game,
                    snapshot_run_id=run_id,
                    snapshot_complete=snapshot_complete,
                    snapshot_semantic_valid=snapshot_semantic_valid,
                    snapshot_semantic_status=snapshot_validation.status,
                    identity_verified=identity_verified,
                    execution_user_id=execution.user_id,
                    prior=prior,
                    execution_errors=execution_errors,
                    plan_blocked=plan_blocked,
                    plan_block_reasons=plan_block_reasons,
                )
            )
        events_by_action: dict[str, list[dict[str, Any]]] = {}
        for event in load_events(config, execution_id):
            if event.action_id:
                events_by_action.setdefault(event.action_id, []).append(
                    event.model_dump(mode="json")
                )
        for action in actions:
            action.evidence["execution_events"] = events_by_action.get(action.action_id, [])
        counts = VerificationCounts(
            planned_actions=len(bundle.plan.actions), unmatched=len(bundle.plan.unmatched)
        )
        for action in actions:
            status = action.execution_status
            if status == "verified":
                counts.execution_verified += 1
            elif status in {"ready", "prepared", "awaiting_confirmation"}:
                counts.execution_ready += 1
            elif status == "failed":
                counts.execution_failed += 1
            elif status == "write_ambiguous":
                counts.execution_ambiguous += 1
            elif status == "conflict":
                counts.execution_conflicts += 1
            elif status == "cancelled":
                counts.execution_cancelled += 1
            else:
                counts.execution_other += 1
            if action.presence in {"found", "divergent", "duplicate"}:
                counts.presence_found += 1
            if action.presence == "absent":
                counts.presence_absent += 1
            if action.presence == "duplicate":
                counts.presence_duplicate += 1
            if action.presence == "inconclusive":
                counts.presence_inconclusive += 1
            if action.content_status == "full":
                counts.content_full += 1
            elif action.content_status == "partial":
                counts.content_partial += 1
            elif action.content_status == "mismatch":
                counts.content_mismatch += 1
            elif action.content_status == "unverifiable":
                counts.content_unverifiable += 1
            if action.confirmation == "confirmed":
                counts.confirmed += 1
            elif action.confirmation == "found_not_attributed":
                counts.found_not_attributed += 1
            elif action.confirmation == "not_executed":
                counts.not_executed += 1
            elif action.confirmation == "inconclusive":
                counts.insufficient += 1
            if action.presence == "divergent":
                counts.divergent += 1
        reasons: list[str] = []
        if verification.invalid:
            reasons.extend(verification.invalid)
        if verification.stale:
            reasons.extend(verification.stale)
        if plan_blocked:
            reasons.append("confirmação global bloqueada pelo plano")
        if not snapshot_complete:
            reasons.append("captura do destino incompleta")
        if not snapshot_semantic_valid:
            reasons.append(f"snapshot sem validade semântica: {snapshot_validation.status}")
            reasons.extend(snapshot_validation.errors)
        if not identity_verified:
            reasons.append("identidade do snapshot diverge da execução")
        if execution_errors:
            reasons.append("verify_execution encontrou inconsistências")
        status = "complete" if not reasons else "inconclusive"
        capture_status = "complete" if snapshot_complete else "incomplete"
        if snapshot_validation.status == "invalid":
            capture_status = "invalid"
        elif snapshot_validation.status == "unverifiable":
            capture_status = "unverifiable"
        if all(action.execution_status == "verified" for action in actions):
            migration_status = "completed"
        elif execution.status is ExecutionStatus.CANCELLED:
            migration_status = "cancelled"
        elif execution.status is ExecutionStatus.INCONCLUSIVE:
            migration_status = "inconclusive"
        elif any(
            action.execution_status in {"ready", "prepared", "awaiting_confirmation"}
            for action in actions
        ):
            migration_status = "incomplete"
        elif any(
            action.execution_status in {"failed", "conflict", "blocked"} for action in actions
        ):
            migration_status = "failed"
        else:
            migration_status = "inconclusive"
        confirmed = sum(action.confirmation == "confirmed" for action in actions)
        if confirmed == len(actions) and actions:
            causal_status = "all"
        elif confirmed:
            causal_status = "partial"
        else:
            causal_status = "none"
        return VerificationReport(
            verification_id=verification_id,
            plan_id=plan_id,
            execution_id=execution_id,
            snapshot_run_id=run_id,
            snapshot_file=str(path.relative_to(path.parent.parent)),
            snapshot_manifest_sha256=sha256_file(manifest_path),
            snapshot_response_hash=snapshot.capture.response_hash,
            snapshot_complete=snapshot_complete,
            identity_verified=identity_verified,
            plan_sha256=bundle.manifest.plan_sha256,
            manifest_sha256=bundle.manifest.manifest_sha256,
            execution_sha256=sha256_file(
                config.migration_dir / "executions" / execution_id / "execution.json"
            ),
            approval_id=execution.approval_id,
            preflight_id=execution.preflight_id,
            prior_snapshot_run_id=prior_run_id,
            prior_sources=prior_sources,
            created_at=utc_now(),
            status=status,
            report_status="processed",
            capture_status=capture_status,
            migration_status=migration_status,
            causal_confirmation_status=causal_status,
            snapshot_semantic_status=snapshot_validation.status,
            snapshot_semantic_errors=snapshot_validation.errors,
            plan_blocked=plan_blocked,
            plan_block_reasons=plan_block_reasons,
            reasons=reasons,
            counts=counts,
            actions=actions,
            plan_verification=plan_verification,
        )

    def verify_destination(
        self,
        plan_id: str,
        execution_id: str,
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        from src.browser.session import BrowserSession
        from src.jazerei.state import capture_account_reviews

        config = self._effective_config()
        bundle = load_bundle(config, plan_id)
        execution = load_execution(config, execution_id)
        if execution.plan_id != plan_id:
            raise MigrationError(
                "execução pertence a outro plano",
                code="execution_plan_mismatch",
                execution_id=execution_id,
            )
        expected_user_id = config.jazerei_user_id
        if not expected_user_id:
            raise MigrationError(
                "identidade do Já Zerei ausente",
                code="identity_missing",
                execution_id=execution_id,
                hint="Faça login/verifique a sessão antes de verificar o destino.",
            )
        if bundle.manifest.destination.get("user_id") not in {None, expected_user_id}:
            raise MigrationError(
                "identidade da aprovação diverge da sessão atual",
                code="identity_mismatch",
                execution_id=execution_id,
            )
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snapshot_run_id = f"{stamp}-account-snapshot"
        index = 2
        while (config.jazerei_dir / "runs" / snapshot_run_id).exists():
            snapshot_run_id = f"{stamp}-account-snapshot-{index}"
            index += 1
        try:
            with BrowserSession(config, headless=self.headless) as session:
                snapshot = capture_account_reviews(
                    session,
                    expected_user_id=expected_user_id,
                    page_size=config.jazerei_page_size,
                    on_page=progress,
                    should_cancel=should_cancel,
                )
        except OperationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - captura inconclusiva nao vira relatorio final
            raise MigrationError(
                "não foi possível capturar o destino para verificação",
                code="verification_capture_failed",
                detail=f"{type(exc).__name__}: {exc}",
                execution_id=execution_id,
                hint="Nenhum relatório final foi publicado; snapshots e relatórios anteriores foram preservados.",
            ) from exc
        artifact = write_account_snapshot_run(config, snapshot, run_id=snapshot_run_id)
        if artifact.snapshot_path is None:
            raise MigrationError(
                "snapshot de verificação não foi gravado",
                code="verification_snapshot_missing",
                execution_id=execution_id,
            )
        verification_id = allocate_verification_id(
            config, plan_id=plan_id, execution_id=execution_id
        )
        report = self.verification_report(
            plan_id,
            execution_id,
            artifact.snapshot_path,
            verification_id=verification_id,
        )
        report_artifact = write_verification_run(config, report, verification_id=verification_id)
        return {
            "verification_id": verification_id,
            "status": report.status,
            "report": report.model_dump(mode="json"),
            "snapshot_run_id": report.snapshot_run_id,
            "snapshot": str(artifact.snapshot_path),
            "manifest": str(report_artifact.manifest_path),
            "counts": report.counts.model_dump(mode="json"),
        }

    def status(self, plan_id: str | None = None) -> dict[str, Any]:
        config = self._effective_config()
        plan_id = plan_id or DEFAULT_PLAN_ID
        try:
            bundle = load_bundle(config, plan_id)
        except Exception as exc:  # noqa: BLE001
            return {"plan_id": plan_id, "exists": False, "error": str(exc)}
        verification = verify_bundle(config, bundle)
        approval = approval_status(config, plan_id, self.approvals)
        preflights = []
        preflight_root = config.migration_dir / "preflights"
        if preflight_root.is_dir():
            for path in sorted(preflight_root.iterdir()):
                result_path = path / "preflight.json"
                if not result_path.is_file():
                    continue
                try:
                    result = load_preflight(config, path.name)
                except Exception:  # noqa: BLE001
                    continue
                if result.plan_id != plan_id:
                    continue
                preflights.append(
                    {
                        "preflight_id": result.preflight_id,
                        "status": result.result.status.value,
                        "captured_at": result.captured_at,
                        "valid": not verify_preflight(config, result.preflight_id),
                        "reasons": list(result.reasons),
                        "actions": result.result.model_dump(mode="json"),
                        "identity_verified": result.identity.verified,
                        "identity_user_id": result.identity.user_id,
                    }
                )
        executions = []
        execution_root = config.migration_dir / "executions"
        if execution_root.is_dir():
            for path in sorted(execution_root.iterdir(), reverse=True):
                if not (path / "execution.json").is_file():
                    continue
                try:
                    record = load_execution(config, path.name)
                except Exception:  # noqa: BLE001
                    continue
                if record.plan_id == plan_id:
                    executions.append(self._execution_entry(record))
        approved_preflights = [
            item for item in preflights if item["status"] == "compatible" and item["valid"]
        ]
        return {
            "plan_id": plan_id,
            "exists": True,
            "invalid": verification.invalid,
            "stale": verification.stale,
            "provenance_complete": bundle.manifest.provenance_complete,
            "actions": len(bundle.plan.actions),
            "unmatched": len(bundle.plan.unmatched),
            "approval": approval.model_dump(mode="json"),
            "approved": approval.status is ApprovalStatus.APPROVED,
            "preflights": preflights,
            "compatible_preflight": approved_preflights[-1] if approved_preflights else None,
            "executions": executions,
            "run": self.progress(),
            "write_gates": {
                "allow_writes": config.allow_writes,
                "enable_real_write_transport": config.enable_real_write_transport,
            },
        }

    def approve(self, *, plan_id: str, confirm: str) -> dict[str, Any]:
        config = self._effective_config()
        if confirm != plan_id:
            raise MigrationError("confirmação deve ser exatamente o plan_id")
        bundle = load_bundle(config, plan_id)
        verification = verify_bundle(config, bundle)
        if verification.invalid or verification.stale:
            raise MigrationError("; ".join(verification.invalid + verification.stale))
        if not bundle.manifest.provenance_complete:
            raise MigrationError("proveniência incompleta; plano não pode ser aprovado")
        record = approve_plan(config, plan_id, self.approvals)
        return record.model_dump(mode="json")

    def preflight(
        self,
        *,
        plan_id: str,
        preflight_id: str | None = None,
        confirm: str | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        config = self._effective_config()
        if confirm != plan_id:
            raise MigrationError("confirmação deve ser exatamente o plan_id")
        from src import cli

        allocated = allocate_preflight_id(config, plan_id=plan_id, preflight_id=preflight_id)
        args = SimpleNamespace(
            plan_id=plan_id,
            preflight_id=allocated,
            approvals=str(self.approvals),
            page_size=config.jazerei_page_size,
            max_pages=100,
            headless=self.headless,
        )
        code = cli.cmd_migration_preflight(
            config,
            args,
            on_page=progress,
            should_cancel=should_cancel,
        )
        result = load_preflight(config, allocated)
        if code != 0 or result.result.status is not PreflightStatus.COMPATIBLE:
            raise MigrationError(
                f"preflight não compatível: {result.result.status.value}",
                code="preflight_not_compatible",
                detail="; ".join(result.reasons) or None,
                hint="Corrija as pendências exibidas no preflight antes de preparar a execução.",
            )
        errors = verify_preflight(config, allocated)
        if errors:
            raise MigrationError("; ".join(errors))
        return result.model_dump(mode="json")

    def prepare(
        self,
        *,
        plan_id: str,
        preflight_id: str | None = None,
        execution_id: str | None = None,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        config = self._effective_config()
        if confirm != plan_id:
            raise MigrationError("confirmação deve ser exatamente o plan_id")
        status = self.status(plan_id)
        if not status.get("approved"):
            raise MigrationError("plano não aprovado")
        selected = preflight_id or (status.get("compatible_preflight") or {}).get("preflight_id")
        if not selected:
            raise MigrationError("preflight compatível ausente")
        preflight = load_preflight(config, selected)
        if preflight.result.status is not PreflightStatus.COMPATIBLE:
            raise MigrationError("preflight não compatível")
        allocated = allocate_execution_id(config, plan_id=plan_id, execution_id=execution_id)
        record = prepare_execution(
            config,
            plan_id=plan_id,
            preflight_id=selected,
            execution_id=allocated,
            approvals_path=self.approvals,
        )
        create_execution(config, record)
        for action in record.actions:
            confirm_action(
                config,
                execution_id=allocated,
                action_id=action.action_id,
                confirmation=allocated,
                approvals_path=self.approvals,
            )
        return {
            "execution_id": allocated,
            "status": load_execution(config, allocated).status.value,
            "actions": len(record.actions),
            "preflight_id": selected,
        }

    def execute(
        self,
        *,
        execution_id: str,
        plan_id: str,
        confirm: str,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        config = self._effective_config()
        if confirm != plan_id:
            raise MigrationError(
                "confirmação deve ser exatamente o plan_id",
                code="invalid_confirmation",
                execution_id=execution_id,
            )
        if not (config.allow_writes and config.enable_real_write_transport):
            raise MigrationError(
                "gates de escrita desabilitados; habilite allow_writes e "
                "enable_real_write_transport apenas para a execução autorizada",
                code="write_gates_disabled",
                execution_id=execution_id,
            )
        from src.browser.session import BrowserSession
        from src.jazerei.state import authenticate, capture_review_state

        execution = load_execution(config, execution_id)
        if execution.plan_id != plan_id:
            raise MigrationError(
                "execução pertence a outro plano",
                code="execution_plan_mismatch",
                execution_id=execution_id,
            )
        actions = list(execution.actions)
        total = len(actions)

        def emit(phase: str, index: int, action: Any) -> None:
            if on_progress is None:
                return
            terminal = {"verified", "conflict", "failed", "write_ambiguous", "cancelled"}
            counts: dict[str, int] = {}
            for item in execution.actions:
                counts[item.status.value] = counts.get(item.status.value, 0) + 1
            current = None
            if action is not None:
                current = {
                    "action_id": action.action_id,
                    "source_ref": action.source_ref,
                    "game_id": action.game_id,
                    "status": action.status.value,
                    "reason": action.reason,
                }
            on_progress(
                {
                    "status": "running",
                    "phase": phase,
                    "execution_status": execution.status.value,
                    "current_index": index,
                    "total": total,
                    "current_action": current,
                    "last_action": current,
                    "counts": counts,
                    "completed": sum(counts.get(status, 0) for status in terminal),
                }
            )

        with BrowserSession(config, headless=self.headless) as session:
            auth_context = authenticate(session, config.jazerei_user_id)
            transport = build_browser_write_transport(
                config, session, auth_headers=auth_context.headers
            )
            emit("starting", 0, None)
            cancelled = False
            for index, action in enumerate(actions, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if action.status.value != "ready":
                    continue
                if action.game_id is None:
                    continue
                slug = action.destination_slug or action.source_ref

                def read_state(game_id: int, slug: str = slug):
                    return capture_review_state(
                        session,
                        game_id=game_id,
                        slug=slug,
                        expected_user_id=config.jazerei_user_id,
                        page_size=config.jazerei_page_size,
                    )

                emit("running", index, action)
                execution = execute_action(
                    config,
                    execution_id=execution_id,
                    action_id=action.action_id,
                    transport=transport,
                    read_state=read_state,
                    approvals_path=self.approvals,
                )
                updated = next(
                    (item for item in execution.actions if item.action_id == action.action_id),
                    action,
                )
                emit("action_completed", index, updated)
                if execution.status.value in {"inconclusive", "blocked", "failed"}:
                    break

            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
            if cancelled and execution.status.value in {
                "prepared",
                "awaiting_confirmation",
                "running",
            }:
                with contextlib.suppress(ExecutionError):
                    execution = cancel_execution(
                        config,
                        execution_id=execution_id,
                        reason="cancel_requested",
                    )
                emit("cancelled", 0, None)
        return {
            "execution_id": execution.execution_id,
            "status": execution.status.value,
            "summary": execution.summary.model_dump(mode="json"),
            "actions": [
                {
                    "action_id": item.action_id,
                    "source_ref": item.source_ref,
                    "status": item.status.value,
                    "reason": item.reason,
                }
                for item in execution.actions
            ],
        }
