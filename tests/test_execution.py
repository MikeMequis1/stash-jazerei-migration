from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import src.migration.execution as execution_module
from src import cli
from src.config import Config
from src.migration.approval import (
    compute_approval_id,
    load_bundle,
)
from src.migration.execution import (
    ConfirmationError,
    ExecutionError,
    PayloadError,
    WriteBlockedError,
    WriteNotSent,
    WriteResult,
    WriteTimeout,
    build_review_payload,
    cancel_execution,
    confirm_action,
    execute_action,
    prepare_execution,
    resolve_ambiguous_action,
    resume_execution,
    validate_execution,
    validate_payload,
)
from src.migration.executions import (
    allocate_execution_id,
    create_execution,
    load_confirmations,
    load_execution,
    reconcile_execution,
    save_execution,
    verify_execution,
)
from src.migration.liked import resolve_liked  # noqa: F401 - contrato de decisao liked
from src.migration.preflight import (
    build_result,
    compare_actions,
    prepare_preflight,
)
from src.migration.preflights import load_preflight, write_preflight_run
from src.models.approval import ApprovalRecord
from src.models.destination import DestinationReview
from src.models.execution import (
    ActionExecutionStatus,
    ExecutionStatus,
)
from src.models.normalized import LikedStatus, MigrationAction
from src.storage import read_json, write_json

from tests.test_preflight import current_snapshot, evidence, workspace


def make_preflight(
    ws: SimpleNamespace,
    *,
    review: DestinationReview | None = None,
    preflight_id: str = "pf-exec",
    captured_at: str | None = None,
    identity_user_id: str = "user-1",
) -> str:
    prep = prepare_preflight(ws.config, "plan-1", ws.approvals)
    assert prep.ready
    bundle = prep.bundle
    assert bundle is not None
    snapshot = current_snapshot(game_id=26570, slug="demolition-girl", review=review)
    checks, summary = compare_actions(
        bundle.plan.actions,
        {26570: evidence(snapshot)},
        expected_user_id="user-1",
    )
    result = build_result(
        preflight_id=preflight_id,
        preparation=prep,
        checks=checks,
        summary=summary,
        snapshots=[],
        identity_verified=True,
        identity_user_id=identity_user_id,
        captured_at=captured_at,
    )
    write_preflight_run(
        ws.config,
        result,
        {26570: evidence(snapshot)},
        preflight_id=preflight_id,
    )
    return preflight_id


def make_execution(ws: SimpleNamespace) -> SimpleNamespace:
    preflight_id = make_preflight(ws)
    execution_id = allocate_execution_id(ws.config, plan_id="plan-1", execution_id="exec-1")
    execution = prepare_execution(
        ws.config,
        plan_id="plan-1",
        preflight_id=preflight_id,
        execution_id=execution_id,
        approvals_path=ws.approvals,
    )
    create_execution(ws.config, execution)
    return SimpleNamespace(
        execution_id=execution_id,
        action_id=execution.actions[0].action_id,
        preflight_id=preflight_id,
    )


class FakeTransport:
    simulated = True

    def __init__(self, *, response: WriteResult | Exception | None = None) -> None:
        self.response = response or WriteResult(200, {"ok": True})
        self.calls = 0
        self.payloads: list[dict] = []

    def post_review(self, payload: dict):
        self.calls += 1
        self.payloads.append(payload)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_payload_strict_validation_and_hash() -> None:
    action = MigrationAction(
        kind="review",
        source_ref="demolition-girl",
        game_id=26570,
        after={"game_id": 26570, "rating": 2.0, "text": "texto", "liked": False},
    )
    action.liked.value = False
    from src.models.normalized import LikedStatus

    action.liked.status = LikedStatus.DECIDED
    payload, digest = build_review_payload(action)
    assert payload == {"game_id": 26570, "rating": 2.0, "text": "texto", "liked": False}
    assert digest == validate_payload(payload, game_id=26570)
    invalid = [
        {"game_id": 0, "rating": 2, "text": "x", "liked": False},
        {"game_id": 26570, "rating": 11, "text": "x", "liked": False},
        {"game_id": 26570, "rating": 2, "text": 1, "liked": False},
        {"game_id": 26570, "rating": 2, "text": "x", "liked": None},
        {"game_id": 26570, "rating": 2, "text": "x", "liked": 0},
        {"game_id": 26570, "rating": 2, "text": "x", "liked": False, "extra": 1},
    ]
    for payload in invalid:
        with pytest.raises(PayloadError):
            validate_payload(payload, game_id=payload.get("game_id", 0))


def test_prepare_and_confirmation_roundtrip(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    record = load_execution(ws.config, execution.execution_id)
    assert record.status is ExecutionStatus.AWAITING_CONFIRMATION
    assert validate_execution(ws.config, record.execution_id, ws.approvals) == []
    with pytest.raises(ConfirmationError):
        confirm_action(
            ws.config,
            execution_id=record.execution_id,
            action_id=execution.action_id,
            confirmation="wrong",
            approvals_path=ws.approvals,
        )
    confirmation = confirm_action(
        ws.config,
        execution_id=record.execution_id,
        action_id=execution.action_id,
        confirmation=record.execution_id,
        approvals_path=ws.approvals,
    )
    assert confirmation.payload_sha256
    ready = load_execution(ws.config, record.execution_id)
    assert ready.actions[0].status is ActionExecutionStatus.READY
    with pytest.raises(ConfirmationError):
        confirm_action(
            ws.config,
            execution_id=record.execution_id,
            action_id=execution.action_id,
            confirmation=record.execution_id,
            approvals_path=ws.approvals,
        )


def test_cancel_execution_marks_pending_actions_cancelled(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    cancelled = cancel_execution(ws.config, execution_id=execution.execution_id)
    assert cancelled.status is ExecutionStatus.CANCELLED
    assert all(action.status is ActionExecutionStatus.CANCELLED for action in cancelled.actions)


def test_resume_resets_transient_pre_write_failure(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    record = load_execution(ws.config, execution.execution_id)
    record.actions[0].status = ActionExecutionStatus.FAILED
    record.actions[0].reason = "pre_write_read_failed"
    record.status = ExecutionStatus.INCONCLUSIVE
    save_execution(ws.config, record, reason="test_pre_write_failure")
    resumed = resume_execution(ws.config, execution_id=execution.execution_id)
    assert resumed.status is ExecutionStatus.AWAITING_CONFIRMATION
    assert resumed.actions[0].status is ActionExecutionStatus.READY
    assert resumed.actions[0].reason is None


def test_resume_blocks_ambiguous_action(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    record = load_execution(ws.config, execution.execution_id)
    record.actions[0].status = ActionExecutionStatus.WRITE_AMBIGUOUS
    record.actions[0].reason = "write_timeout_after_send"
    record.status = ExecutionStatus.INCONCLUSIVE
    save_execution(ws.config, record, reason="test_ambiguous")
    with pytest.raises(ExecutionError):
        resume_execution(ws.config, execution_id=execution.execution_id)


def test_simulated_execution_verifies_once_and_blocks_repeat(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        confirmation=execution.execution_id,
        approvals_path=ws.approvals,
    )
    after = DestinationReview(
        id="user-1_26570",
        game_id=26570,
        game_slug="demolition-girl",
        user_id="user-1",
        rating=2.0,
        text="Texto original",
        liked=False,
    )
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        current_snapshot(game_id=26570, slug="demolition-girl", review=after),
    ]
    transport = FakeTransport()
    result = execute_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        transport=transport,
        read_state=lambda game_id: states.pop(0),
        approvals_path=ws.approvals,
    )
    assert transport.calls == 1
    assert result.actions[0].status is ActionExecutionStatus.VERIFIED
    assert result.status is ExecutionStatus.COMPLETED
    with pytest.raises(ExecutionError):
        execute_action(
            ws.config,
            execution_id=execution.execution_id,
            action_id=execution.action_id,
            transport=transport,
            read_state=lambda game_id: states[-1],
            approvals_path=ws.approvals,
        )
    assert transport.calls == 1


def test_timeout_is_ambiguous_and_not_retried(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        confirmation=execution.execution_id,
        approvals_path=ws.approvals,
    )
    transport = FakeTransport(response=WriteTimeout("timeout"))
    result = execute_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        transport=transport,
        read_state=lambda game_id: current_snapshot(
            game_id=26570, slug="demolition-girl", review=None
        ),
        approvals_path=ws.approvals,
    )
    assert transport.calls == 1
    assert result.actions[0].status is ActionExecutionStatus.WRITE_AMBIGUOUS
    assert result.status is ExecutionStatus.INCONCLUSIVE
    with pytest.raises(ExecutionError):
        execute_action(
            ws.config,
            execution_id=execution.execution_id,
            action_id=execution.action_id,
            transport=transport,
            read_state=lambda game_id: current_snapshot(
                game_id=26570, slug="demolition-girl", review=None
            ),
            approvals_path=ws.approvals,
        )
    assert transport.calls == 1


def test_pre_write_conflict_does_not_call_transport(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        confirmation=execution.execution_id,
        approvals_path=ws.approvals,
    )
    existing = DestinationReview(
        id="user-1_26570",
        game_id=26570,
        game_slug="demolition-girl",
        user_id="user-1",
        rating=9,
        text="criada manualmente",
    )
    transport = FakeTransport()
    result = execute_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        transport=transport,
        read_state=lambda game_id: current_snapshot(
            game_id=26570, slug="demolition-girl", review=existing
        ),
        approvals_path=ws.approvals,
    )
    assert result.actions[0].status is ActionExecutionStatus.CONFLICT
    assert transport.calls == 0


def test_post_write_mismatch_is_not_verified(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        confirmation=execution.execution_id,
        approvals_path=ws.approvals,
    )
    wrong = DestinationReview(
        id="user-1_26570",
        game_id=26570,
        game_slug="demolition-girl",
        user_id="user-1",
        rating=8,
        text="texto errado",
        liked=True,
    )
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        current_snapshot(game_id=26570, slug="demolition-girl", review=wrong),
    ]
    result = execute_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        transport=FakeTransport(),
        read_state=lambda game_id: states.pop(0),
        approvals_path=ws.approvals,
    )
    assert result.actions[0].status is ActionExecutionStatus.CONFLICT
    assert result.status is ExecutionStatus.COMPLETED_WITH_ERRORS


def test_real_transport_is_blocked(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        confirmation=execution.execution_id,
        approvals_path=ws.approvals,
    )

    class RealLikeTransport:
        simulated = False

        def post_review(self, payload):
            raise AssertionError("real transport must not be called")

    with pytest.raises(WriteBlockedError):
        execute_action(
            ws.config,
            execution_id=execution.execution_id,
            action_id=execution.action_id,
            transport=RealLikeTransport(),
            read_state=lambda game_id: current_snapshot(
                game_id=26570, slug="demolition-girl", review=None
            ),
            approvals_path=ws.approvals,
        )


def test_execution_artifacts_verify_and_not_overwrite(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    assert verify_execution(ws.config, execution.execution_id) == []
    with pytest.raises(FileExistsError):
        allocate_execution_id(ws.config, plan_id="plan-1", execution_id=execution.execution_id)


def test_execution_cli_prepare_show_status_and_confirm(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    preflight_id = make_preflight(ws)
    prepare_args = SimpleNamespace(
        plan_id="plan-1",
        preflight_id=preflight_id,
        execution_id="exec-cli",
        approvals=str(ws.approvals),
    )
    assert cli.cmd_execution_prepare(ws.config, prepare_args) == 0
    execution = load_execution(ws.config, "exec-cli")
    assert cli.cmd_execution_show(ws.config, SimpleNamespace(execution_id="exec-cli")) == 0
    assert cli.cmd_execution_status(ws.config, SimpleNamespace(execution_id="exec-cli")) == 0
    assert (
        cli.cmd_execution_validate(
            ws.config,
            SimpleNamespace(execution_id="exec-cli", approvals=str(ws.approvals)),
        )
        == 0
    )
    confirm_args = SimpleNamespace(
        execution_id="exec-cli",
        action_id=execution.actions[0].action_id,
        confirm="exec-cli",
        approvals=str(ws.approvals),
    )
    assert cli.cmd_execution_confirm(ws.config, confirm_args) == 0
    assert cli.cmd_execution_verify(ws.config, SimpleNamespace(execution_id="exec-cli")) == 0
    # Confirmacao e documental; a acao permanece pronta, nao executada.
    assert load_execution(ws.config, "exec-cli").actions[0].status is ActionExecutionStatus.READY


def confirm(ws: SimpleNamespace, execution: SimpleNamespace) -> None:
    confirm_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        confirmation=execution.execution_id,
        approvals_path=ws.approvals,
    )


def applied_review(**overrides: object) -> DestinationReview:
    data: dict[str, object] = {
        "id": "user-1_26570",
        "user_id": "user-1",
        "game_id": 26570,
        "game_slug": "demolition-girl",
        "rating": 2.0,
        "text": "Texto original",
        "liked": False,
    }
    data.update(overrides)
    return DestinationReview.model_validate(data)


def run_action(
    ws: SimpleNamespace,
    execution: SimpleNamespace,
    *,
    transport: FakeTransport,
    states: list[object],
):
    return execute_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        transport=transport,
        read_state=lambda game_id: states.pop(0),
        approvals_path=ws.approvals,
    )


def test_prepare_rejects_tampered_plan_and_manifest(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    preflight_id = make_preflight(ws)
    plan_path = ws.config.migration_dir / "plans" / "plan-1" / "plan.json"
    original = read_json(plan_path)
    tampered = json.loads(json.dumps(original))
    tampered["actions"][0]["after"]["rating"] = 9.0
    write_json(plan_path, tampered, overwrite=True)
    with pytest.raises(ExecutionError):
        prepare_execution(
            ws.config,
            plan_id="plan-1",
            preflight_id=preflight_id,
            execution_id="exec-tamper",
            approvals_path=ws.approvals,
        )

    write_json(plan_path, original, overwrite=True)
    manifest_path = ws.config.migration_dir / "plans" / "plan-1" / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["provenance_complete"] = False
    write_json(manifest_path, manifest, overwrite=True)
    with pytest.raises(ExecutionError):
        prepare_execution(
            ws.config,
            plan_id="plan-1",
            preflight_id=preflight_id,
            execution_id="exec-tamper",
            approvals_path=ws.approvals,
        )


def test_prepare_requires_current_approval(tmp_path: Path) -> None:
    ws = workspace(tmp_path, approve=False)
    with pytest.raises(ExecutionError) as excinfo:
        prepare_execution(
            ws.config,
            plan_id="plan-1",
            preflight_id="pf-missing",
            execution_id="exec-x",
            approvals_path=ws.approvals,
        )
    assert "aprovacao" in str(excinfo.value)


def test_prepare_rejects_stale_approval(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    preflight_id = make_preflight(ws)
    bundle = load_bundle(ws.config, "plan-1")
    stale = ApprovalRecord(
        approval_id="pending",
        plan_id="plan-1",
        manifest_sha256="0" * 64,
        plan_sha256="0" * 64,
        approved_at="2099-01-01T00:00:00+00:00",
        approved_actions=[],
    )
    stale.approval_id = compute_approval_id(stale)
    with ws.approvals.open("a", encoding="utf-8") as handle:
        handle.write(stale.model_dump_json() + "\n")
    with pytest.raises(ExecutionError) as excinfo:
        prepare_execution(
            ws.config,
            plan_id="plan-1",
            preflight_id=preflight_id,
            execution_id="exec-stale",
            approvals_path=ws.approvals,
        )
    assert "aprovacao" in str(excinfo.value)
    assert bundle.manifest.manifest_sha256


def test_prepare_rejects_missing_incompatible_foreign_and_old_preflight(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    with pytest.raises(ExecutionError):
        prepare_execution(
            ws.config,
            plan_id="plan-1",
            preflight_id="pf-absent",
            execution_id="exec-a",
            approvals_path=ws.approvals,
        )

    incompatible = make_preflight(ws, preflight_id="pf-conflict", review=applied_review())
    assert load_preflight(ws.config, incompatible).result.status.value == "conflict"
    with pytest.raises(ExecutionError):
        prepare_execution(
            ws.config,
            plan_id="plan-1",
            preflight_id=incompatible,
            execution_id="exec-b",
            approvals_path=ws.approvals,
        )

    old = make_preflight(ws, preflight_id="pf-old", captured_at="2020-01-01T00:00:00+00:00")
    with pytest.raises(ExecutionError) as excinfo:
        prepare_execution(
            ws.config,
            plan_id="plan-1",
            preflight_id=old,
            execution_id="exec-c",
            approvals_path=ws.approvals,
        )
    assert "anterior" in str(excinfo.value)


def test_prepare_rejects_foreign_preflight_identity(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    preflight_id = make_preflight(ws, identity_user_id="other-user")
    with pytest.raises(ExecutionError) as excinfo:
        prepare_execution(
            ws.config,
            plan_id="plan-1",
            preflight_id=preflight_id,
            execution_id="exec-id",
            approvals_path=ws.approvals,
        )
    assert "identidade" in str(excinfo.value)


def test_payload_rejects_unsupported_update_and_unresolved_liked() -> None:
    base = {
        "kind": "review",
        "source_ref": "demolition-girl",
        "game_id": 26570,
        "after": {"game_id": 26570, "rating": 2.0, "text": "texto", "liked": False},
    }
    favorite = MigrationAction.model_validate({**base, "kind": "favorite"})
    with pytest.raises(PayloadError):
        build_review_payload(favorite)

    existing = MigrationAction.model_validate(
        {**base, "before": {"id": "user-1_26570", "game_id": 26570}}
    )
    with pytest.raises(PayloadError):
        build_review_payload(existing)

    unresolved = MigrationAction.model_validate(base)
    assert unresolved.liked.status is LikedStatus.UNRESOLVED
    with pytest.raises(PayloadError):
        build_review_payload(unresolved)

    stale = MigrationAction.model_validate(base)
    stale.liked.status = LikedStatus.STALE
    stale.liked.value = False
    with pytest.raises(PayloadError):
        build_review_payload(stale)


def test_payload_hash_is_deterministic_and_changes_with_content(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = load_execution(ws.config, make_execution(ws).execution_id)
    action = execution.actions[0]
    assert action.payload is not None
    first = validate_payload(action.payload, game_id=26570)
    second = validate_payload(dict(action.payload), game_id=26570)
    assert first == second == action.payload_sha256
    changed = dict(action.payload)
    changed["rating"] = 9.0
    assert validate_payload(changed, game_id=26570) != first


def test_confirmation_is_bound_to_execution_action_and_payload(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    with pytest.raises(ConfirmationError):
        confirm_action(
            ws.config,
            execution_id=execution.execution_id,
            action_id=execution.action_id,
            confirmation="other-execution",
            approvals_path=ws.approvals,
        )
    with pytest.raises(ConfirmationError):
        confirm_action(
            ws.config,
            execution_id=execution.execution_id,
            action_id="ghost-action",
            confirmation=execution.execution_id,
            approvals_path=ws.approvals,
        )
    confirm(ws, execution)
    again = SimpleNamespace(execution_id=execution.execution_id, action_id=execution.action_id)
    with pytest.raises(ConfirmationError):
        confirm(ws, again)
    record = load_confirmations(ws.config, execution.execution_id)[0]
    stored = load_execution(ws.config, execution.execution_id)
    assert record.payload_sha256 == stored.actions[0].payload_sha256
    assert record.plan_id == "plan-1"
    assert record.user_id == "user-1"


def test_execute_requires_confirmation_and_blocks_missing_action(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    transport = FakeTransport()
    with pytest.raises(ExecutionError):
        run_action(
            ws,
            execution,
            transport=transport,
            states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
        )
    assert transport.calls == 0

    confirm(ws, execution)
    with pytest.raises(ExecutionError):
        execute_action(
            ws.config,
            execution_id=execution.execution_id,
            action_id="ghost-action",
            transport=transport,
            read_state=lambda game_id: current_snapshot(
                game_id=26570, slug="demolition-girl", review=None
            ),
            approvals_path=ws.approvals,
        )
    assert transport.calls == 0


def test_payload_tamper_after_confirmation_blocks_execution(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    path = ws.config.migration_dir / "executions" / execution.execution_id / "execution.json"
    data = read_json(path)
    data["actions"][0]["payload"]["rating"] = 9.0
    write_json(path, data, overwrite=True)
    transport = FakeTransport()
    with pytest.raises(ExecutionError):
        run_action(
            ws,
            execution,
            transport=transport,
            states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
        )
    assert transport.calls == 0


def test_plan_tamper_after_confirmation_blocks_execution(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    plan_path = ws.config.migration_dir / "plans" / "plan-1" / "plan.json"
    data = read_json(plan_path)
    data["actions"][0]["after"]["text"] = "adulterado"
    write_json(plan_path, data, overwrite=True)
    transport = FakeTransport()
    with pytest.raises(ExecutionError):
        run_action(
            ws,
            execution,
            transport=transport,
            states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
        )
    assert transport.calls == 0


def test_pre_write_read_failures_never_become_absence(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)

    incomplete = current_snapshot(
        game_id=26570, slug="demolition-girl", review=None, complete=False
    )
    transport = FakeTransport()
    result = run_action(ws, execution, transport=transport, states=[incomplete])
    assert result.actions[0].status is ActionExecutionStatus.FAILED
    assert result.status is ExecutionStatus.INCONCLUSIVE
    assert transport.calls == 0


def test_pre_write_authentication_error_is_inconclusive(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)

    def boom(game_id: int):
        raise RuntimeError("auth missing")

    transport = FakeTransport()
    result = execute_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        transport=transport,
        read_state=boom,
        approvals_path=ws.approvals,
    )
    assert result.actions[0].status is ActionExecutionStatus.FAILED
    assert result.actions[0].reason == "pre_write_read_failed"
    assert result.status is ExecutionStatus.INCONCLUSIVE
    assert transport.calls == 0


def test_pre_write_identity_and_game_mismatch_block(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    snapshot = current_snapshot(game_id=26570, slug="demolition-girl", review=None)
    snapshot.identity.user_id = "other-user"
    transport = FakeTransport()
    result = run_action(ws, execution, transport=transport, states=[snapshot])
    assert result.actions[0].reason == "pre_write_identity_mismatch"
    assert result.status is ExecutionStatus.BLOCKED
    assert transport.calls == 0


def test_http_error_is_ambiguous_and_not_retried(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    transport = FakeTransport(response=WriteResult(500, {"error": "boom"}))
    result = run_action(
        ws,
        execution,
        transport=transport,
        states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
    )
    assert transport.calls == 1
    assert result.actions[0].status is ActionExecutionStatus.WRITE_AMBIGUOUS
    assert result.actions[0].reason == "http_500_unconfirmed"
    assert result.status is ExecutionStatus.INCONCLUSIVE


def test_http_4xx_is_ambiguous_and_not_retried(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    transport = FakeTransport(response=WriteResult(422, {"error": "invalid"}))
    result = run_action(
        ws,
        execution,
        transport=transport,
        states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
    )
    assert transport.calls == 1
    assert result.actions[0].status is ActionExecutionStatus.WRITE_AMBIGUOUS
    assert result.actions[0].reason == "http_422_unconfirmed"
    assert result.status is ExecutionStatus.INCONCLUSIVE


def test_connection_error_after_send_is_ambiguous(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    transport = FakeTransport(response=ConnectionResetError("reset after send"))
    result = run_action(
        ws,
        execution,
        transport=transport,
        states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
    )
    assert transport.calls == 1
    assert result.actions[0].status is ActionExecutionStatus.WRITE_AMBIGUOUS
    assert result.actions[0].reason == "write_outcome_unknown"
    assert result.status is ExecutionStatus.INCONCLUSIVE


def test_failure_before_send_is_failed_without_retry(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    transport = FakeTransport(response=WriteNotSent("dns failure before connect"))
    result = run_action(
        ws,
        execution,
        transport=transport,
        states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
    )
    assert transport.calls == 1
    assert result.actions[0].status is ActionExecutionStatus.FAILED
    assert result.actions[0].reason == "write_failed_before_send"
    assert result.status is ExecutionStatus.COMPLETED_WITH_ERRORS


def test_post_write_identity_mismatch_is_ambiguous(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    after = current_snapshot(game_id=26570, slug="demolition-girl", review=applied_review())
    after.identity.user_id = "other-user"
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        after,
    ]
    result = run_action(ws, execution, transport=FakeTransport(), states=states)
    assert result.actions[0].status is ActionExecutionStatus.WRITE_AMBIGUOUS
    assert result.actions[0].reason == "verification_identity_mismatch"
    assert result.status is ExecutionStatus.INCONCLUSIVE


def test_post_write_missing_review_stays_ambiguous(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
    ]
    transport = FakeTransport()
    result = run_action(ws, execution, transport=transport, states=states)
    assert transport.calls == 1
    assert result.actions[0].status is ActionExecutionStatus.WRITE_AMBIGUOUS
    assert result.actions[0].reason == "review_not_found_after_write"
    assert result.status is ExecutionStatus.INCONCLUSIVE


def test_post_write_incomplete_verification_is_ambiguous(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    incomplete = current_snapshot(
        game_id=26570, slug="demolition-girl", review=applied_review(), complete=False
    )
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        incomplete,
    ]
    result = run_action(ws, execution, transport=FakeTransport(), states=states)
    assert result.actions[0].status is ActionExecutionStatus.WRITE_AMBIGUOUS
    assert result.actions[0].status is not ActionExecutionStatus.VERIFIED
    assert result.status is ExecutionStatus.INCONCLUSIVE


def test_repeated_absent_resolutions_keep_ambiguous_and_never_resend(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    transport = FakeTransport(response=WriteTimeout("timeout"))
    run_action(
        ws,
        execution,
        transport=transport,
        states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
    )
    absent = current_snapshot(game_id=26570, slug="demolition-girl", review=None)
    for _ in range(2):
        with pytest.raises(ExecutionError):
            resolve_ambiguous_action(
                ws.config,
                execution_id=execution.execution_id,
                action_id=execution.action_id,
                observed=absent,
            )
        record = load_execution(ws.config, execution.execution_id)
        assert record.actions[0].status is ActionExecutionStatus.WRITE_AMBIGUOUS
        assert record.status is ExecutionStatus.INCONCLUSIVE
    assert transport.calls == 1


def test_ambiguous_execution_blocks_any_further_action(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    transport = FakeTransport(response=WriteTimeout("timeout"))
    run_action(
        ws,
        execution,
        transport=transport,
        states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
    )
    with pytest.raises(ExecutionError):
        execute_action(
            ws.config,
            execution_id=execution.execution_id,
            action_id="next-action",
            transport=transport,
            read_state=lambda game_id: current_snapshot(
                game_id=26570, slug="demolition-girl", review=None
            ),
            approvals_path=ws.approvals,
        )
    assert transport.calls == 1


test_cases = [
    ({"rating": 9.0}, "rating"),
    ({"text": "outro texto"}, "text"),
    ({"liked": True}, "liked"),
    ({"game_slug": "outro-slug"}, "game_slug"),
]


@pytest.mark.parametrize(("overrides", "field"), test_cases)
def test_post_write_field_mismatch_is_conflict(
    tmp_path: Path, overrides: dict[str, object], field: str
) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        current_snapshot(game_id=26570, slug="demolition-girl", review=applied_review(**overrides)),
    ]
    result = run_action(ws, execution, transport=FakeTransport(), states=states)
    action = result.actions[0]
    assert action.status is ActionExecutionStatus.CONFLICT
    assert field in (action.verification or {})["fields"]
    assert result.status is ExecutionStatus.COMPLETED_WITH_ERRORS


def test_interrupted_write_is_ambiguous_reconciled_and_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    original_save = execution_module.save_execution

    def flaky_save(config, record, *, reason):
        if reason == "write_confirmed":
            raise OSError("disk full")
        return original_save(config, record, reason=reason)

    monkeypatch.setattr(execution_module, "save_execution", flaky_save)
    transport = FakeTransport()
    states = [
        current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        current_snapshot(game_id=26570, slug="demolition-girl", review=applied_review()),
    ]
    with pytest.raises(OSError):
        run_action(ws, execution, transport=transport, states=states)
    assert transport.calls == 1
    persisted = load_execution(ws.config, execution.execution_id)
    assert persisted.status is ExecutionStatus.RUNNING
    assert persisted.actions[0].status is ActionExecutionStatus.WRITING

    with pytest.raises(ExecutionError):
        run_action(ws, execution, transport=transport, states=[])

    reconciled = reconcile_execution(ws.config, execution.execution_id)
    assert reconciled.actions[0].status is ActionExecutionStatus.WRITE_AMBIGUOUS
    assert reconciled.status is ExecutionStatus.INCONCLUSIVE

    resolved = resolve_ambiguous_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        observed=current_snapshot(game_id=26570, slug="demolition-girl", review=applied_review()),
    )
    assert resolved.actions[0].status is ActionExecutionStatus.VERIFIED
    assert resolved.status is ExecutionStatus.COMPLETED
    assert transport.calls == 1


def test_ambiguous_resolution_requires_conclusive_evidence(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    transport = FakeTransport(response=WriteTimeout("timeout"))
    run_action(
        ws,
        execution,
        transport=transport,
        states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
    )
    assert transport.calls == 1

    with pytest.raises(ExecutionError):
        resolve_ambiguous_action(
            ws.config,
            execution_id=execution.execution_id,
            action_id=execution.action_id,
            observed=current_snapshot(
                game_id=26570, slug="demolition-girl", review=None, complete=False
            ),
        )
    with pytest.raises(ExecutionError):
        resolve_ambiguous_action(
            ws.config,
            execution_id=execution.execution_id,
            action_id=execution.action_id,
            observed=current_snapshot(game_id=26570, slug="demolition-girl", review=None),
        )
    assert (
        load_execution(ws.config, execution.execution_id).actions[0].status
        is ActionExecutionStatus.WRITE_AMBIGUOUS
    )

    resolved = resolve_ambiguous_action(
        ws.config,
        execution_id=execution.execution_id,
        action_id=execution.action_id,
        observed=current_snapshot(
            game_id=26570, slug="demolition-girl", review=applied_review(rating=9.0)
        ),
    )
    assert resolved.actions[0].status is ActionExecutionStatus.CONFLICT
    assert resolved.status is ExecutionStatus.COMPLETED_WITH_ERRORS


def test_real_transport_blocked_even_with_allow_writes_true(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    permissive = Config(
        data_dir=ws.config.data_dir,
        jazerei_user_id="user-1",
        allow_writes=True,
    )

    class RealTransport:
        simulated = False

        def post_review(self, payload):
            raise AssertionError("real transport must never be used")

    with pytest.raises(WriteBlockedError):
        execute_action(
            permissive,
            execution_id=execution.execution_id,
            action_id=execution.action_id,
            transport=RealTransport(),
            read_state=lambda game_id: current_snapshot(
                game_id=26570, slug="demolition-girl", review=None
            ),
            approvals_path=ws.approvals,
        )
    assert Config().allow_writes is False


def test_cli_has_no_real_execution_command_and_no_force() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["migration", "execution-run", "--execution-id", "exec-1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["migration", "execution-execute", "--execution-id", "exec-1"])
    assert parser.parse_args(
        ["migration", "execution-prepare", "--plan-id", "p", "--preflight-id", "f"]
    )


def test_execution_artifacts_contain_no_credentials(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    run_action(
        ws,
        execution,
        transport=FakeTransport(),
        states=[
            current_snapshot(game_id=26570, slug="demolition-girl", review=None),
            current_snapshot(game_id=26570, slug="demolition-girl", review=applied_review()),
        ],
    )
    root = ws.config.migration_dir / "executions" / execution.execution_id
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl"}
    ).lower()
    for forbidden in ("authorization", "cookie", "id_token", "refresh_token", "bearer "):
        assert forbidden not in text
    assert verify_execution(ws.config, execution.execution_id) == []


def test_cli_reconcile_and_resolve_commands(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    transport = FakeTransport(response=WriteTimeout("timeout"))
    run_action(
        ws,
        execution,
        transport=transport,
        states=[current_snapshot(game_id=26570, slug="demolition-girl", review=None)],
    )
    snapshot_path = tmp_path / "observed.json"
    write_json(
        snapshot_path,
        current_snapshot(game_id=26570, slug="demolition-girl", review=applied_review()),
    )
    assert (
        cli.cmd_execution_resolve(
            ws.config,
            SimpleNamespace(
                execution_id=execution.execution_id,
                action_id=execution.action_id,
                snapshot=str(snapshot_path),
            ),
        )
        == 0
    )
    final = load_execution(ws.config, execution.execution_id)
    assert final.status is ExecutionStatus.COMPLETED
    assert (
        cli.cmd_execution_reconcile(ws.config, SimpleNamespace(execution_id=execution.execution_id))
        == 0
    )
