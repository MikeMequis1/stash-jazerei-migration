from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.config import Config
from src.jazerei.snapshots import write_account_snapshot_run
from src.jazerei.state import PaginationResult, build_account_review_snapshot
from src.migration.executions import load_execution, save_execution
from src.migration.setup import WizardState
from src.migration.verifications import (
    VerificationStorageError,
    allocate_verification_id,
    list_verifications,
    load_verification,
    verify_verification,
    write_verification_run,
)
from src.migration.wizard import MigrationError, MigrationService
from src.models.destination import DestinationReview
from src.models.execution import ActionExecutionStatus, ExecutionStatus
from src.models.snapshot import canonical_account_hash
from src.models.verification import VerificationReport
from src.review_ui import ReviewUIHandler
from src.storage import read_json, sha256_file, write_json

from tests.test_execution import confirm, make_execution, workspace
from tests.test_preflight import current_snapshot


def destination_review(**overrides: object) -> DestinationReview:
    data: dict[str, object] = {
        "id": "review-1",
        "user_id": "user-1",
        "game_id": 26570,
        "game_slug": "demolition-girl",
        "rating": 2.0,
        "text": "Texto original",
        "liked": False,
    }
    data.update(overrides)
    return DestinationReview.model_validate(data)


def snapshot_for(
    ws: SimpleNamespace,
    reviews: list[DestinationReview],
    *,
    complete: bool = True,
    user_id: str = "user-1",
    run_id: str = "verification-snapshot",
    response_hash: str | None = None,
) -> Path:
    pagination = PaginationResult(
        reviews=reviews,
        pages_fetched=1,
        records_received=len(reviews),
        duplicates_discarded=0,
        termination_reason="short_page" if reviews else "empty_page",
        content_range_total=None,
    )
    snapshot = build_account_review_snapshot(
        expected_user_id="user-1",
        pagination=pagination,
        page_size=50,
        max_pages=100,
    )
    if user_id != "user-1":
        snapshot.identity.user_id = user_id
    if not complete:
        snapshot.capture.complete = False
        snapshot.pagination.complete = False
    snapshot.capture.response_hash = canonical_account_hash(snapshot)
    if response_hash is not None:
        snapshot.capture.response_hash = response_hash
    artifact = write_account_snapshot_run(ws.config, snapshot, run_id=run_id)
    assert artifact.snapshot_path is not None
    return artifact.snapshot_path


def prepared_execution(
    tmp_path: Path,
    *,
    status: ActionExecutionStatus = ActionExecutionStatus.VERIFIED,
    review_id: str | None = "review-1",
    before_present: bool = False,
    reason: str | None = None,
    write_result: dict | None = None,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    ws = workspace(tmp_path)
    execution = make_execution(ws)
    confirm(ws, execution)
    record = load_execution(ws.config, execution.execution_id)
    action = record.actions[0]
    action.status = status
    action.reason = reason
    action.write_result = write_result
    before = current_snapshot(
        game_id=26570,
        slug="demolition-girl",
        review=destination_review(id="old-review") if before_present else None,
    )
    action.before_read = before.model_dump(mode="json")
    if review_id:
        action.verification = {
            "fields": [],
            "review": destination_review(id=review_id).model_dump(mode="json"),
        }
    record.status = (
        ExecutionStatus.COMPLETED
        if status is ActionExecutionStatus.VERIFIED
        else ExecutionStatus.INCONCLUSIVE
    )
    save_execution(ws.config, record, reason="test_verification")
    return ws, execution


def build_report(
    ws: SimpleNamespace,
    execution: SimpleNamespace,
    snapshot: Path,
) -> VerificationReport:
    service = MigrationService(ws.config, approvals=ws.approvals)
    return service.verification_report(
        "plan-1",
        execution.execution_id,
        snapshot,
        verification_id="verification-1",
    )


def test_confirmed_when_presence_content_causality_and_identity_match(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert report.status == "complete"
    assert action.presence == "found"
    assert action.confirmation == "confirmed"
    assert action.content_status == "full"
    assert action.causal == "verified_id_match_prior_absent"
    assert action.prior_presence == "absent"
    assert report.counts.confirmed == 1
    assert report.counts.execution_verified == 1
    assert report.plan_verification["missing"] == []


def test_found_but_not_attributed_when_execution_ids_differ(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path, review_id="review-execution")
    snapshot = snapshot_for(ws, [destination_review(id="review-snapshot")])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.presence == "found"
    assert action.confirmation == "found_not_attributed"
    assert action.causal == "verified_id_mismatch"
    assert report.counts.found_not_attributed == 1
    assert report.counts.confirmed == 0


def test_found_but_not_attributed_when_prior_presence_is_found(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path, before_present=True)
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.prior_presence == "found"
    assert action.confirmation == "found_not_attributed"
    assert "não há evidência de ausência anterior suficiente" in " ".join(action.reasons)


def test_absent_review_is_not_created_or_confirmed(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.presence == "absent"
    assert action.confirmation == "absent"
    assert report.counts.presence_absent == 1
    assert report.counts.confirmed == 0


def test_current_account_snapshot_is_semantically_valid() -> None:
    from src.jazerei.state import validate_snapshot_semantics
    from src.models.snapshot import AccountReviewsSnapshot
    from src.storage import read_json

    config = Config()
    if not (
        config.jazerei_dir
        / "runs"
        / "stage9-account-snapshot-20261125"
        / "review-state"
        / "account-reviews.json"
    ).is_file():
        pytest.skip("snapshot histórico removido pelo reset absoluto")
    payload = read_json(
        config.jazerei_dir
        / "runs"
        / "stage9-account-snapshot-20261125"
        / "review-state"
        / "account-reviews.json"
    )
    snapshot = AccountReviewsSnapshot.model_validate(payload)
    validation = validate_snapshot_semantics(
        snapshot, expected_user_id=snapshot.identity.user_id
    )
    assert validation.status == "valid", validation.errors


def test_current_verified_before_read_artifacts_pass_hardened_validation() -> None:
    from src.jazerei.state import validate_snapshot_semantics
    from src.models.snapshot import ReviewStateSnapshot

    config = Config()
    if not (
        config.migration_dir
        / "executions"
        / "20261125T224312Z-execution-stage9-review-20261125T201911Z"
    ).is_dir():
        pytest.skip("execução histórica removida pelo reset absoluto")
    execution = load_execution(config, "20261125T224312Z-execution-stage9-review-20261125T201911Z")
    checked = 0
    for action in execution.actions:
        if action.status is not ActionExecutionStatus.VERIFIED:
            continue
        snapshot = ReviewStateSnapshot.model_validate(action.before_read)
        validation = validate_snapshot_semantics(
            snapshot,
            expected_user_id=execution.user_id,
            expected_game_id=action.game_id,
            expected_slug=action.destination_slug or action.source_ref,
        )
        assert validation.status == "valid", (action.source_ref, validation.errors)
        assert snapshot.result.review is None
        checked += 1
    assert checked == 18


def test_validated_before_read_absence_confirms(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.prior_presence == "absent"
    assert action.evidence["before_read"]["status"] == "valid"
    assert action.confirmation == "confirmed"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda raw: raw["capture"].update({"complete": False}), "unverifiable"),
        (lambda raw: raw["pagination"].update({"complete": False}), "unverifiable"),
        (lambda raw: raw["identity"].update({"user_id": "other-user"}), "unverifiable"),
        (lambda raw: raw["query"].update({"game_id": 999}), "unverifiable"),
        (lambda raw: raw.pop("result"), "unverifiable"),
        (lambda raw: raw.update({"unexpected": True}), "unverifiable"),
        (lambda raw: raw["capture"].update({"response_hash": "0" * 64}), "unverifiable"),
        (lambda raw: raw["result"].update({"exists": True}), "unverifiable"),
    ],
)
def test_invalid_before_read_never_proves_absence(tmp_path: Path, mutation, expected: str) -> None:
    ws, execution = prepared_execution(tmp_path)
    record = load_execution(ws.config, execution.execution_id)
    raw = record.actions[0].before_read
    mutation(raw)
    record.actions[0].before_read = raw
    save_execution(ws.config, record, reason="test_before_read_tamper")
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.prior_presence == expected
    assert action.confirmation != "confirmed"
    assert any("before_read" in reason for reason in action.reasons)


def test_invalid_before_read_structure_does_not_prove_absence(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    record = load_execution(ws.config, execution.execution_id)
    record.actions[0].before_read = {"error": "query failed"}
    save_execution(ws.config, record, reason="test_before_read_invalid")
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.prior_presence == "unverifiable"
    assert action.confirmation != "confirmed"


def test_semantically_divergent_snapshot_cannot_confirm(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()], response_hash="0" * 64)
    report = build_report(ws, execution, snapshot)
    assert report.snapshot_semantic_status == "invalid"
    assert report.status == "inconclusive"
    assert report.actions[0].confirmation != "confirmed"
    assert any("response_hash" in error for error in report.snapshot_semantic_errors)


def test_unverifiable_legacy_snapshot_cannot_confirm(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()], response_hash="pending")
    report = build_report(ws, execution, snapshot)
    assert report.snapshot_semantic_status == "unverifiable"
    assert report.status == "inconclusive"
    assert report.actions[0].confirmation != "confirmed"


def test_snapshot_content_change_without_manifest_update_is_rejected(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()])
    payload = read_json(snapshot)
    payload["reviews"][0]["text"] = "conteúdo alterado"
    write_json(snapshot, payload, overwrite=True)
    with pytest.raises(MigrationError) as error:
        build_report(ws, execution, snapshot)
    assert error.value.as_dict()["code"] == "snapshot_invalid"


def test_plan_stale_blocks_confirmation_but_preserves_evidence(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()])
    plan_path = ws.config.migration_dir / "plans" / "plan-1" / "plan.json"
    plan = read_json(plan_path)
    plan["actions"][0]["after"]["text"] = "texto adulterado no plano"
    write_json(plan_path, plan, overwrite=True)
    report = build_report(ws, execution, snapshot)
    assert report.plan_blocked is True
    assert report.plan_block_reasons
    assert report.status == "inconclusive"
    action = report.actions[0]
    assert action.presence == "found"
    assert action.content_status == "full"
    assert action.confirmation == "inconclusive"
    assert action.confirmation_blocked_by_plan is True
    assert any("plano" in reason for reason in action.reasons)


def test_plan_invalid_manifest_blocks_confirmation(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()])
    manifest_path = ws.config.migration_dir / "plans" / "plan-1" / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["manifest_sha256"] = "0" * 64
    write_json(manifest_path, manifest, overwrite=True)
    report = build_report(ws, execution, snapshot)
    assert report.plan_blocked is True
    assert report.plan_block_reasons
    assert report.actions[0].confirmation != "confirmed"
    assert report.actions[0].confirmation_blocked_by_plan is True


def test_capture_complete_with_incomplete_migration_has_independent_status(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path, status=ActionExecutionStatus.READY, review_id=None)
    snapshot = snapshot_for(ws, [])
    report = build_report(ws, execution, snapshot)
    assert report.capture_status in {"complete", "incomplete"}
    assert report.report_status == "processed"
    assert report.migration_status in {"incomplete", "inconclusive"}
    assert report.causal_confirmation_status == "none"


def test_capture_complete_with_failure_has_independent_status(tmp_path: Path) -> None:
    ws, execution = prepared_execution(
        tmp_path,
        status=ActionExecutionStatus.FAILED,
        review_id=None,
        reason="write_failed_before_send",
    )
    snapshot = snapshot_for(ws, [])
    report = build_report(ws, execution, snapshot)
    assert report.report_status == "processed"
    assert report.migration_status in {"inconclusive", "failed"}
    assert report.causal_confirmation_status == "none"


def test_semantically_valid_and_complete_migration_reports_all(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    assert report.snapshot_semantic_status == "valid"
    assert report.capture_status == "complete"
    assert report.migration_status == "completed"
    assert report.causal_confirmation_status == "all"


def test_verification_counts_can_be_partial(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    assert report.causal_confirmation_status in {"all", "partial"}
    assert report.counts.confirmed <= report.counts.planned_actions


def test_rating_divergence_is_preserved(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review(rating=9.0)])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.presence == "divergent"
    assert action.confirmation == "divergent"
    assert action.rating_match is False
    assert any("rating divergente" in reason for reason in action.reasons)
    assert report.counts.divergent == 1


def test_text_divergence_is_preserved(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review(text="Outro texto")])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.text_match is False
    assert any("texto divergente" in reason for reason in action.reasons)
    assert action.confirmation == "divergent"


def test_liked_unavailable_is_not_inferred(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review(liked=None)])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.liked_status == "unavailable"
    assert action.found_liked is None
    assert action.confirmation == "inconclusive"
    assert any("liked indisponível" in reason for reason in action.reasons)


def test_liked_divergence_is_reported_when_available(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review(liked=True)])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.liked_status == "mismatch"
    assert action.found_liked is True
    assert action.confirmation == "divergent"
    assert any("liked divergente" in reason for reason in action.reasons)


def test_duplicate_review_for_same_game_is_inconclusive(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(
        ws,
        [
            destination_review(id="review-1"),
            destination_review(id="review-2"),
        ],
    )
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.presence == "duplicate"
    assert action.confirmation == "inconclusive"
    assert report.counts.presence_duplicate == 1


def test_ambiguous_execution_remains_ambiguous_even_with_matching_review(tmp_path: Path) -> None:
    ws, execution = prepared_execution(
        tmp_path,
        status=ActionExecutionStatus.WRITE_AMBIGUOUS,
        reason="http_401_unconfirmed",
        write_result={"status_code": 401, "body": ""},
    )
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.execution_status == "write_ambiguous"
    assert action.presence == "found"
    assert action.confirmation == "ambiguous"
    assert report.counts.execution_ambiguous == 1
    assert report.counts.confirmed == 0


def test_ready_execution_is_not_executed_and_found_review_not_attributed(
    tmp_path: Path,
) -> None:
    ws, execution = prepared_execution(tmp_path, status=ActionExecutionStatus.READY, review_id=None)
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert action.execution_status == "ready"
    assert action.presence == "found"
    assert action.confirmation == "found_not_attributed"
    assert report.counts.execution_ready == 1
    assert report.counts.confirmed == 0


def test_incomplete_snapshot_never_becomes_final(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()], complete=False)
    report = build_report(ws, execution, snapshot)
    action = report.actions[0]
    assert report.status == "inconclusive"
    assert report.snapshot_complete is False
    assert report.snapshot_semantic_status == "incomplete"
    assert report.capture_status == "incomplete"
    assert action.presence == "inconclusive"
    assert action.confirmation == "inconclusive"
    assert report.counts.confirmed == 0


def test_incorrect_snapshot_identity_never_confirms(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()], user_id="other-user")
    report = build_report(ws, execution, snapshot)
    assert report.identity_verified is False
    assert report.status == "inconclusive"
    assert report.snapshot_semantic_status == "invalid"
    assert report.actions[0].confirmation == "inconclusive"


def test_snapshot_path_outside_runs_is_rejected(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    service = MigrationService(ws.config, approvals=ws.approvals)
    with pytest.raises(MigrationError) as error:
        service.verification_report("plan-1", execution.execution_id, outside)
    assert error.value.as_dict()["code"] == "snapshot_path_invalid"


def test_verification_persistence_is_append_only_and_integrity_checked(tmp_path: Path) -> None:
    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    first_id = allocate_verification_id(
        ws.config, plan_id="plan-1", execution_id=execution.execution_id, verification_id="ver-1"
    )
    first_report = report.model_copy(update={"verification_id": "ver-1"})
    first = write_verification_run(ws.config, first_report, verification_id=first_id)
    first_hash = sha256_file(first.report_path)
    second_report = report.model_copy(update={"verification_id": "ver-2"})
    second = write_verification_run(ws.config, second_report, verification_id="ver-2")
    assert first_hash == sha256_file(first.report_path)
    assert second.report_path.is_file()
    items = list_verifications(ws.config, plan_id="plan-1")
    assert {item["verification_id"] for item in items} == {"ver-1", "ver-2"}
    assert verify_verification(ws.config, "ver-1") == []
    loaded = load_verification(ws.config, "ver-1")
    assert loaded.report_sha256
    with pytest.raises(VerificationStorageError):
        load_verification(ws.config, "..")


def test_verify_destination_cancel_publishes_nothing(tmp_path: Path, monkeypatch) -> None:
    ws, execution = prepared_execution(tmp_path)
    from src.cancellation import OperationCancelled

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("src.browser.session.BrowserSession", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr(
        "src.jazerei.state.capture_account_reviews",
        lambda *args, **kwargs: (_ for _ in ()).throw(OperationCancelled("cancelado")),
    )
    monkeypatch.setattr(
        "src.migration.execution.BrowserWriteTransport",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("transporte não deve ser criado")
        ),
    )
    service = MigrationService(ws.config, approvals=ws.approvals)
    before_runs = set((ws.config.jazerei_dir / "runs").iterdir())
    before_reports = (
        set((ws.config.migration_dir / "verifications").iterdir())
        if (ws.config.migration_dir / "verifications").is_dir()
        else set()
    )
    with pytest.raises(OperationCancelled):
        service.verify_destination("plan-1", execution.execution_id)
    assert set((ws.config.jazerei_dir / "runs").iterdir()) == before_runs
    reports = (
        set((ws.config.migration_dir / "verifications").iterdir())
        if (ws.config.migration_dir / "verifications").is_dir()
        else set()
    )
    assert reports == before_reports


def test_verify_destination_success_persists_report_without_post(
    tmp_path: Path, monkeypatch
) -> None:
    ws, execution = prepared_execution(tmp_path)
    from src.jazerei.state import build_account_review_snapshot

    pagination = PaginationResult(
        reviews=[destination_review()],
        pages_fetched=1,
        records_received=1,
        duplicates_discarded=0,
        termination_reason="short_page",
        content_range_total=None,
    )
    snapshot = build_account_review_snapshot(
        expected_user_id="user-1",
        pagination=pagination,
        page_size=50,
        max_pages=100,
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("src.browser.session.BrowserSession", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr(
        "src.jazerei.state.capture_account_reviews",
        lambda *args, **kwargs: snapshot,
    )
    monkeypatch.setattr(
        "src.migration.execution.BrowserWriteTransport",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("transporte não deve ser criado")
        ),
    )
    service = MigrationService(ws.config, approvals=ws.approvals)
    result = service.verify_destination("plan-1", execution.execution_id)
    assert result["status"] == "complete"
    assert result["counts"]["confirmed"] == 1
    report_path = (
        ws.config.migration_dir / "verifications" / result["verification_id"] / "report.json"
    )
    assert report_path.is_file()
    stored = load_execution(ws.config, execution.execution_id)
    assert stored.status is ExecutionStatus.COMPLETED
    assert stored.actions[0].status is ActionExecutionStatus.VERIFIED


def test_verify_destination_http_job_updates_wizard_state(tmp_path: Path, monkeypatch) -> None:
    import time
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen

    from src.jazerei.state import build_account_review_snapshot
    from src.review_ui import JobRunner, OperationGate

    ws, execution = prepared_execution(tmp_path)
    state = WizardState(path=ws.config.migration_dir / "wizard-state.json")
    pagination = PaginationResult(
        reviews=[destination_review()],
        pages_fetched=1,
        records_received=1,
        duplicates_discarded=0,
        termination_reason="short_page",
        content_range_total=None,
    )
    snapshot = build_account_review_snapshot(
        expected_user_id="user-1",
        pagination=pagination,
        page_size=50,
        max_pages=100,
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("src.browser.session.BrowserSession", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr(
        "src.jazerei.state.capture_account_reviews",
        lambda *args, **kwargs: snapshot,
    )
    migration = MigrationService(ws.config, approvals=ws.approvals)
    gate = OperationGate()

    class Handler(ReviewUIHandler):
        pass

    Handler.service = SimpleNamespace(config=ws.config)
    Handler.wizard_state = state
    Handler.migration_service = migration
    Handler.job_runner = JobRunner(gate)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = Request(
            f"{base}/api/jobs",
            data=json.dumps(
                {
                    "kind": "verify_destination",
                    "params": {
                        "plan_id": "plan-1",
                        "execution_id": execution.execution_id,
                    },
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["job"]["kind"] == "verify_destination"
        deadline = time.monotonic() + 10
        job = None
        while time.monotonic() < deadline:
            with urlopen(f"{base}/api/jobs/current", timeout=10) as response:
                job = json.loads(response.read().decode("utf-8"))["job"]
            if job and job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert job["status"] == "done", job
        loaded = WizardState.load(ws.config)
        assert loaded.last_verification_id
        assert (
            ws.config.migration_dir / "verifications" / loaded.last_verification_id / "report.json"
        ).is_file()
        assert gate.busy() is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_wizard_payload_recovers_last_verification_without_capture(
    tmp_path: Path, monkeypatch
) -> None:
    from src.review_ui import (
        JobRunner,
        OperationGate,
        build_wizard_payload,
    )

    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    write_verification_run(
        ws.config,
        report.model_copy(update={"verification_id": "ver-recover"}),
        verification_id="ver-recover",
    )
    state = WizardState(path=ws.config.migration_dir / "wizard-state.json")
    state.active_plan_id = "plan-1"
    state.active_execution_id = execution.execution_id
    state.last_verification_id = "ver-recover"
    state.save()

    def no_capture(*args, **kwargs):
        raise AssertionError("a abertura do wizard não pode capturar o destino")

    monkeypatch.setattr("src.jazerei.state.capture_account_reviews", no_capture)
    service = SimpleNamespace(
        config=ws.config,
        paths=SimpleNamespace(
            source_run=ws.source_run,
            destination_run=ws.snapshot_path.parent.parent,
        ),
        state=lambda: {
            "summary": {
                "total": 1,
                "pending": 0,
                "needs_review": 0,
                "stale_matches": 0,
                "liked_stale": 0,
                "conflicts": 0,
            }
        },
        _account_snapshot=lambda: None,
    )
    payload = build_wizard_payload(
        config=ws.config,
        service=service,
        migration=MigrationService(ws.config, approvals=ws.approvals),
        state=WizardState.load(ws.config),
        jobs=JobRunner(OperationGate()),
    )
    assert payload["last_verification_id"] == "ver-recover"
    assert payload["verifications"][0]["verification_id"] == "ver-recover"


def test_wizard_state_keeps_last_verification_only(tmp_path: Path) -> None:
    state = WizardState(path=tmp_path / "migration" / "wizard-state.json")
    state.active_plan_id = "plan-1"
    state.active_execution_id = "exec-1"
    state.last_verification_id = "ver-1"
    state.save()
    loaded = WizardState.load(Config(data_dir=tmp_path))
    assert loaded.last_verification_id == "ver-1"
    assert loaded.active_execution_id == "exec-1"


def test_http_report_endpoints_are_read_only(tmp_path: Path) -> None:
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import urlopen

    ws, execution = prepared_execution(tmp_path)
    snapshot = snapshot_for(ws, [destination_review()])
    report = build_report(ws, execution, snapshot)
    write_verification_run(
        ws.config,
        report.model_copy(update={"verification_id": "ver-http"}),
        verification_id="ver-http",
    )
    report_path = ws.config.migration_dir / "verifications" / "ver-http" / "report.json"
    before = sha256_file(report_path)

    class Handler(ReviewUIHandler):
        pass

    Handler.service = SimpleNamespace(config=ws.config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/api/verifications", timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["verifications"][0]["verification_id"] == "ver-http"
        with urlopen(f"{base}/api/verifications/ver-http", timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["report"]["counts"]["confirmed"] == 1
        with urlopen(
            f"{base}/api/report?plan_id=plan-1&execution_id={execution.execution_id}",
            timeout=10,
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["report"]["verification_id"] == "ver-http"
        with pytest.raises(HTTPError) as error:
            urlopen(f"{base}/api/verifications/missing", timeout=10)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert sha256_file(report_path) == before
