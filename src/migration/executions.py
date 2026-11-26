"""Persistência e verificação de execuções controladas."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.config import Config
from src.migration.approval import canonical_hash
from src.models.execution import (
    ActionExecutionStatus,
    ConfirmationRecord,
    ExecutionEvent,
    ExecutionRecord,
    ExecutionStatus,
)
from src.storage import read_json, sha256_file, utc_now, verify_manifest, write_json, write_manifest

EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ExecutionStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionArtifact:
    execution_id: str
    run_dir: Path
    execution_path: Path
    manifest_path: Path


def allocate_execution_id(
    config: Config,
    *,
    plan_id: str,
    execution_id: str | None = None,
) -> str:
    if execution_id:
        if execution_id in {".", ".."} or not EXECUTION_ID_RE.fullmatch(execution_id):
            raise ExecutionStorageError("execution_id invalido")
        if (config.migration_dir / "executions" / execution_id).exists():
            raise FileExistsError(f"execucao ja existe: {execution_id}")
        return execution_id
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-execution-{plan_id}"
    candidate = base
    index = 2
    while (config.migration_dir / "executions" / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def create_execution(config: Config, execution: ExecutionRecord) -> ExecutionArtifact:
    target = config.migration_dir / "executions" / execution.execution_id
    if target.exists():
        raise FileExistsError(f"execucao ja existe: {target}")
    work = config.migration_dir / "executions" / f".{execution.execution_id}.staging"
    if work.exists():
        import shutil

        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        write_json(work / "execution.json", execution)
        (work / "events.jsonl").write_text(
            json.dumps(
                ExecutionEvent(
                    at=utc_now(),
                    execution_id=execution.execution_id,
                    to_status=execution.status.value,
                    reason="execution_created",
                ).model_dump(mode="json"),
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (work / "confirmations.jsonl").write_text("", encoding="utf-8")
        write_manifest(
            work,
            {
                "schema_version": 1,
                "kind": "migration-execution",
                "execution_id": execution.execution_id,
                "plan_id": execution.plan_id,
                "created_at": execution.created_at,
                "status": execution.status.value,
                "write_enabled": False,
            },
        )
        work.rename(target)
    except Exception:
        import shutil

        shutil.rmtree(work, ignore_errors=True)
        raise
    return ExecutionArtifact(
        execution.execution_id, target, target / "execution.json", target / "manifest.json"
    )


def load_execution(config: Config, execution_id: str) -> ExecutionRecord:
    path = config.migration_dir / "executions" / execution_id / "execution.json"
    if not path.is_file():
        raise ExecutionStorageError(f"execucao ausente: {execution_id}")
    try:
        return ExecutionRecord.model_validate(read_json(path))
    except Exception as exc:  # noqa: BLE001
        raise ExecutionStorageError("execution.json invalido") from exc


def save_execution(config: Config, execution: ExecutionRecord, *, reason: str) -> ExecutionArtifact:
    root = config.migration_dir / "executions" / execution.execution_id
    if not root.is_dir():
        raise ExecutionStorageError("execucao inexistente")
    target = root / "execution.json"
    temporary = root / "execution.json.tmp"
    temporary.write_text(
        json.dumps(execution.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    append_event(config, execution.execution_id, to_status=execution.status.value, reason=reason)
    write_manifest(
        root,
        {
            "schema_version": 1,
            "kind": "migration-execution",
            "execution_id": execution.execution_id,
            "plan_id": execution.plan_id,
            "created_at": execution.created_at,
            "status": execution.status.value,
            "write_enabled": False,
        },
        overwrite=True,
    )
    return ExecutionArtifact(execution.execution_id, root, target, root / "manifest.json")


def append_event(
    config: Config,
    execution_id: str,
    *,
    to_status: str,
    reason: str | None = None,
    action_id: str | None = None,
    from_status: str | None = None,
) -> None:
    path = config.migration_dir / "executions" / execution_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = ExecutionEvent(
        at=utc_now(),
        execution_id=execution_id,
        action_id=action_id,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")


def load_events(config: Config, execution_id: str) -> list[ExecutionEvent]:
    path = config.migration_dir / "executions" / execution_id / "events.jsonl"
    events: list[ExecutionEvent] = []
    if not path.exists():
        return events
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                events.append(ExecutionEvent.model_validate(json.loads(line)))
            except Exception as exc:  # noqa: BLE001
                raise ExecutionStorageError(f"evento invalido na linha {line_number}") from exc
    return events


def compute_confirmation_id(record: ConfirmationRecord) -> str:
    return canonical_hash(record.model_dump(mode="json", exclude={"confirmation_id"}))


def load_confirmations(config: Config, execution_id: str) -> list[ConfirmationRecord]:
    path = config.migration_dir / "executions" / execution_id / "confirmations.jsonl"
    if not path.exists():
        return []
    records: list[ConfirmationRecord] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = ConfirmationRecord.model_validate(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            raise ExecutionStorageError(f"confirmacao invalida na linha {line_number}") from exc
        if record.confirmation_id != compute_confirmation_id(record):
            raise ExecutionStorageError(f"confirmation_id inconsistente na linha {line_number}")
        records.append(record)
    return records


def append_confirmation(config: Config, record: ConfirmationRecord) -> None:
    path = config.migration_dir / "executions" / record.execution_id / "confirmations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")


def verify_execution(config: Config, execution_id: str) -> list[str]:
    root = config.migration_dir / "executions" / execution_id
    errors = verify_manifest(root)
    if errors:
        return errors
    try:
        execution = load_execution(config, execution_id)
        confirmations = load_confirmations(config, execution_id)
        events = load_events(config, execution_id)
    except ExecutionStorageError as exc:
        return [str(exc)]
    if execution.execution_id != execution_id:
        errors.append("execution_id divergente")
    if execution.status.value == "completed" and any(
        action.status.value != "verified" for action in execution.actions
    ):
        errors.append("execucao completed contem acao nao verificada")
    action_by_id = {action.action_id: action for action in execution.actions}
    seen_confirmations: set[str] = set()
    for confirmation in confirmations:
        if confirmation.action_id in seen_confirmations:
            errors.append("confirmacao duplicada para a mesma acao")
        seen_confirmations.add(confirmation.action_id)
        action = action_by_id.get(confirmation.action_id)
        if action is None:
            errors.append("confirmacao para acao inexistente")
            continue
        if action.confirmation_id != confirmation.confirmation_id:
            errors.append("acao nao referencia a confirmacao registrada")
        if action.payload_sha256 != confirmation.payload_sha256:
            errors.append("payload da acao diverge da confirmacao")
        if action.game_id != confirmation.game_id:
            errors.append("game_id da confirmacao diverge da acao")
        for field in (
            "execution_id",
            "plan_id",
            "plan_sha256",
            "manifest_sha256",
            "preflight_id",
            "preflight_manifest_sha256",
            "user_id",
        ):
            if getattr(confirmation, field) != getattr(execution, field):
                errors.append(f"confirmacao com {field} divergente da execucao")
    requires_confirmation = {
        ActionExecutionStatus.READY,
        ActionExecutionStatus.WRITING,
        ActionExecutionStatus.WRITE_CONFIRMED,
        ActionExecutionStatus.WRITE_AMBIGUOUS,
        ActionExecutionStatus.VERIFICATION_PENDING,
        ActionExecutionStatus.VERIFIED,
    }
    for action in execution.actions:
        if action.status in requires_confirmation and action.confirmation_id is None:
            errors.append(f"acao {action.action_id} sem confirmacao registrada")
    if not events:
        errors.append("historico de eventos ausente")
    return errors


def reconcile_execution(config: Config, execution_id: str) -> ExecutionRecord:
    """Classifica escritas interrompidas como ambiguas; nunca reenvia.

    Um processo novo encontra acoes persistidas em `writing` ou
    `verification_pending`. O resultado do POST e desconhecido; essas acoes
    passam para `write_ambiguous` e exigem verificacao/resolucao humana.
    """
    from src.migration.execution import transition_execution  # import tardio: evita ciclo

    execution = load_execution(config, execution_id)
    interrupted = [
        action
        for action in execution.actions
        if action.status
        in {ActionExecutionStatus.WRITING, ActionExecutionStatus.VERIFICATION_PENDING}
    ]
    if not interrupted and execution.status is not ExecutionStatus.RUNNING:
        return execution
    for action in interrupted:
        previous = action.status
        action.status = ActionExecutionStatus.WRITE_AMBIGUOUS
        action.reason = action.reason or "interrupted_before_result"
        append_event(
            config,
            execution_id,
            action_id=action.action_id,
            from_status=previous.value,
            to_status=ActionExecutionStatus.WRITE_AMBIGUOUS.value,
            reason="interrupted_write_reconciled",
        )
    if execution.status is ExecutionStatus.RUNNING:
        transition_execution(execution, ExecutionStatus.INCONCLUSIVE)
    save_execution(config, execution, reason="execution_reconciled")
    return execution


def execution_manifest_hash(config: Config, execution_id: str) -> str:
    path = config.migration_dir / "executions" / execution_id / "manifest.json"
    if not path.is_file():
        raise ExecutionStorageError("manifesto de execucao ausente")
    return sha256_file(path)
