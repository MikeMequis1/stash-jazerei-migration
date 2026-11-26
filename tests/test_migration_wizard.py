from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from src.migration.executions import load_execution, save_execution
from src.migration.wizard import MigrationError, MigrationService
from src.models.execution import ActionExecutionStatus

from tests.test_execution import make_execution, workspace


def test_progress_is_idle_without_run(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    service = MigrationService(ws.config, approvals=ws.approvals)
    assert service.progress() == {"running": False, "status": "idle"}


def test_cancel_prepared_execution_without_active_run(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    config = replace(ws.config, allow_writes=True, enable_real_write_transport=True)
    service = MigrationService(config, approvals=ws.approvals)
    result = service.cancel(
        execution_id=execution.execution_id,
        plan_id="plan-1",
        confirm="plan-1",
    )
    assert result["status"] == "cancelled"
    assert result["cancel_requested"] is True


def test_cancel_requires_exact_plan_confirmation(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    service = MigrationService(ws.config, approvals=ws.approvals)
    try:
        service.cancel(
            execution_id=execution.execution_id,
            plan_id="plan-1",
            confirm="wrong",
        )
    except MigrationError as exc:
        assert exc.as_dict()["code"] == "invalid_confirmation"
    else:  # pragma: no cover
        raise AssertionError("cancelamento deveria exigir confirmacao exata")


def test_list_plans_and_executions_reads_persisted_state(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    service = MigrationService(ws.config, approvals=ws.approvals)

    plans = service.list_plans()
    assert any(item["plan_id"] == "plan-1" for item in plans)
    executions = service.list_executions("plan-1")
    assert executions[0]["execution_id"] == execution.execution_id
    assert executions[0]["status"] == "awaiting_confirmation"
    detail = service.execution_detail(execution.execution_id)
    assert detail["actions"][0]["action_id"] == execution.action_id
    assert isinstance(detail["confirmations"], list)


def test_cancel_refuses_interrupted_write_without_reconcile(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    record = load_execution(ws.config, execution.execution_id)
    record.actions[0].status = ActionExecutionStatus.WRITING
    save_execution(ws.config, record, reason="test_interrupted")
    service = MigrationService(ws.config, approvals=ws.approvals)
    with pytest.raises(MigrationError) as error:
        service.cancel(
            execution_id=execution.execution_id,
            plan_id="plan-1",
            confirm="plan-1",
        )
    assert error.value.as_dict()["code"] == "execution_interrupted"
