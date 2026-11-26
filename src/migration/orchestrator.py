"""Orquestração da preparação sem duplicar regras de migração.

O orquestrador só decide *quais* operações existentes precisam rodar e em que
ordem. Coleta, snapshot, correspondências, política, plano e validação
continuam morando nos serviços originais.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.cancellation import OperationCancelled
from src.config import Config
from src.migration.setup import (
    SetupError,
    WizardState,
    apply_state_to_service,
    collect_stash,
    login_jazerei,
    open_stash,
    snapshot_destination,
    verify_jazerei,
)
from src.storage import read_json, utc_now, verify_manifest

TERMINAL_OK = {"done", "skipped"}


class PrepareError(SetupError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "prepare_failed",
        detail: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, code=code, detail=detail, hint=hint)


@dataclass(frozen=True)
class PrepareOperations:
    open_stash: Callable[..., dict[str, Any]] = open_stash
    collect_stash: Callable[..., dict[str, Any]] = collect_stash
    verify_jazerei: Callable[..., dict[str, Any]] = verify_jazerei
    login_jazerei: Callable[..., dict[str, Any]] = login_jazerei
    snapshot_destination: Callable[..., dict[str, Any]] = snapshot_destination

    def ensure_matches(self, service: Any, **kwargs: Any) -> dict[str, Any]:
        return service.ensure_match_entries(with_candidates=True, **kwargs)


class PrepareStore:
    """Manifesto por execução + eventos append-only da preparação."""

    def __init__(self, config: Config) -> None:
        self.root = config.migration_dir / "prepare-runs"

    def start(self, steps: list[dict[str, Any]]) -> str:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-prepare")
        base = run_id
        index = 2
        while (self.root / run_id).exists():
            run_id = f"{base}-{index}"
            index += 1
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_manifest(
            run_id,
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": "running",
                "started_at": utc_now(),
                "finished_at": None,
                "steps": steps,
                "result": None,
                "error": None,
            },
        )
        return run_id

    def event(self, run_id: str, payload: dict[str, Any]) -> None:
        path = self.root / run_id / "events.jsonl"
        event = {"schema_version": 1, "event_id": os.urandom(16).hex(), "at": utc_now(), **payload}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        steps: list[dict[str, Any]],
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        manifest = self._load_manifest(run_id)
        manifest.update(
            {
                "status": status,
                "finished_at": utc_now(),
                "steps": steps,
                "result": result,
                "error": error,
            }
        )
        self._write_manifest(run_id, manifest)
        self.event(
            run_id,
            {"step": "run", "status": status, "detail": (result or error or {}).get("error")},
        )

    def latest(self) -> dict[str, Any] | None:
        if not self.root.is_dir():
            return None
        candidates = sorted(
            (path for path in self.root.iterdir() if (path / "manifest.json").is_file()),
            reverse=True,
        )
        for path in candidates:
            try:
                payload = self._load_manifest(path.name)
            except (OSError, json.JSONDecodeError):
                continue
            events: list[dict[str, Any]] = []
            events_path = path / "events.jsonl"
            if events_path.is_file():
                for line in events_path.read_text(encoding="utf-8").splitlines()[-20:]:
                    if line.strip():
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            payload["events"] = events
            payload["interrupted"] = payload.get("status") == "running"
            return payload
        return None

    def _load_manifest(self, run_id: str) -> dict[str, Any]:
        return read_json(self.root / run_id / "manifest.json")

    def _write_manifest(self, run_id: str, payload: dict[str, Any]) -> None:
        directory = self.root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / ".manifest.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, directory / "manifest.json")


def _step(
    step_id: str,
    label: str,
    status: str,
    detail: str,
    *,
    action: str | None = None,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "status": status,
        "detail": detail,
        "action": action,
    }


class PrepareOrchestrator:
    def __init__(
        self,
        config: Config,
        service: Any,
        state: WizardState,
        migration: Any | None = None,
        *,
        operations: PrepareOperations | None = None,
        store: PrepareStore | None = None,
    ) -> None:
        self.config = config
        self.service = service
        self.state = state
        self.migration = migration
        self.operations = operations or PrepareOperations()
        self.store = store or PrepareStore(config)

    # -- varredura -------------------------------------------------------
    def scan(self) -> dict[str, Any]:
        source_ok, source_detail = self._source_status()
        destination_ok, destination_detail = self._destination_status(source_ok)
        matches_ok, matches_detail, matches_blocked = self._matches_status(source_ok)
        policy_preview, policy_error = self._policy_preview()
        policy_needed = bool(
            policy_preview
            and (
                policy_preview.get("auto_approved")
                or policy_preview.get("auto_reverted_category")
                or policy_preview.get("auto_liked")
            )
        )
        plan_status = self._plan_status()
        plan_ok = bool(
            plan_status
            and plan_status.get("exists")
            and not plan_status.get("invalid")
            and not plan_status.get("stale")
        )
        validation_ok = bool(plan_ok and plan_status and plan_status.get("provenance_complete"))
        safety = self._safety_status()
        auth_ok, auth_detail = self._auth_status()

        steps: list[dict[str, Any]] = []
        steps.append(
            _step(
                "safety",
                "Segurança de execuções anteriores",
                "blocked" if safety["blocked"] else "done",
                safety["detail"],
                action="resolve_ambiguity" if safety["blocked"] else None,
            )
        )
        steps.append(
            _step(
                "auth",
                "Sessão do Já Zerei",
                "done" if auth_ok else "needed",
                auth_detail,
            )
        )
        steps.append(
            _step(
                "source",
                "Coleta do Stash",
                "done" if source_ok else "needed",
                source_detail,
            )
        )
        if not destination_ok and (not source_ok or not auth_ok):
            destination_status = "pending"
            destination_detail = (
                "Aguardando coleta válida." if not source_ok else "Aguardando login no Já Zerei."
            )
        else:
            destination_status = "done" if destination_ok else "needed"
        steps.append(
            _step(
                "destination",
                "Snapshot do destino",
                destination_status,
                destination_detail,
            )
        )
        if not source_ok or not destination_ok:
            matches_status, matches_detail = "pending", "Aguardando coleta e snapshot."
        elif matches_blocked:
            matches_status = "blocked"
        else:
            matches_status = "done" if matches_ok else "needed"
        steps.append(
            _step(
                "matches",
                "Correspondências",
                matches_status,
                matches_detail,
                action="review" if matches_blocked else None,
            )
        )
        if matches_status != "done" or not auth_ok:
            policy_status = "pending"
        elif policy_error:
            policy_status = "blocked"
        elif policy_needed:
            policy_status = "needs_confirmation"
        else:
            policy_status = "done"
        steps.append(
            _step(
                "policy",
                "Política automática",
                policy_status,
                policy_error
                or (
                    self._policy_detail(policy_preview)
                    if policy_status == "needs_confirmation"
                    else "Nada a aplicar sem sobrescrever decisões humanas."
                ),
                action="apply_policy" if policy_status == "needs_confirmation" else None,
            )
        )
        if policy_status == "needs_confirmation":
            plan_status_value, plan_detail = "pending", "Confirme a política para seguir."
        elif policy_status not in TERMINAL_OK:
            plan_status_value, plan_detail = "pending", "Aguardando etapas anteriores."
        elif plan_ok:
            plan_status_value = "done"
            plan_detail = f"Plano reutilizado: {plan_status.get('plan_id')}."
        else:
            plan_status_value, plan_detail = "needed", "Nenhum plano válido para os insumos atuais."
        steps.append(_step("plan", "Plano consolidado", plan_status_value, plan_detail))
        if plan_status_value != "done":
            validation_status, validation_detail = "pending", "Aguardando plano válido."
        elif validation_ok:
            validation_status, validation_detail = (
                "done",
                f"{plan_status.get('actions', 0)} ações publicadas no plano.",
            )
        else:
            validation_status, validation_detail = (
                "blocked",
                "Plano sem proveniência completa ou inválido.",
            )
        steps.append(
            _step(
                "validation", "Validação final da preparação", validation_status, validation_detail
            )
        )

        blocked = any(step["status"] == "blocked" for step in steps)
        ready = all(step["status"] in TERMINAL_OK for step in steps)
        latest = self.store.latest()
        return {
            "steps": steps,
            "ready": ready,
            "blocked": blocked,
            "needs_action": any(
                step["status"] in {"needed", "needs_confirmation"} for step in steps
            ),
            "needs_policy_confirmation": policy_status == "needs_confirmation",
            "policy_preview": policy_preview,
            "safety": safety,
            "plan": plan_status,
            "latest": latest,
            "interrupted": bool(latest and latest.get("interrupted")),
            "source": self.service.paths.source_run.name,
            "destination": (
                self.service._account_snapshot().parent.parent.name
                if self.service._account_snapshot()
                else self.service.paths.destination_run.name
            ),
        }

    def run(
        self,
        *,
        apply_policy: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        scan = self.scan()
        run_id = self.store.start(scan["steps"])

        def emit(payload: dict[str, Any]) -> None:
            if progress is not None:
                progress(payload)

        def check_cancel() -> None:
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("preparação cancelada")

        try:
            if scan["blocked"] and scan["safety"]["blocked"]:
                raise PrepareError(
                    "há escrita ambígua de execução anterior sem reconciliação",
                    code="prior_write_ambiguous",
                    detail="; ".join(
                        f"{item['execution_id']}/{item['source_ref']}"
                        for item in scan["safety"]["ambiguous"]
                    ),
                    hint=(
                        "Abra a seção Avançado e resolva a ambiguidade com leitura "
                        "autenticada antes de preparar uma nova execução."
                    ),
                )
            if scan["blocked"]:
                blocked = next(step for step in scan["steps"] if step["status"] == "blocked")
                raise PrepareError(
                    blocked["detail"],
                    code=f"prepare_blocked_{blocked['id']}",
                    hint="Resolva a pendência indicada antes de continuar.",
                )

            auth_ok, _ = self._auth_status()
            auth_checked = False
            if not auth_ok:
                check_cancel()
                self._ensure_auth(run_id, progress, should_cancel)
                auth_checked = True

            source_ok, _ = self._source_status()
            if not source_ok:
                check_cancel()
                emit({"step": "source", "status": "running", "message": "Abrindo Stash"})
                self._event(run_id, "source", "running", "Abrindo Stash")
                self.operations.open_stash(
                    self.config, progress=progress, should_cancel=should_cancel
                )
                emit({"step": "source", "status": "running", "message": "Coletando Stash"})
                result = self.operations.collect_stash(
                    self.config, progress=progress, should_cancel=should_cancel
                )
                self._adopt_source(result["run_id"])
                self._event(run_id, "source", "done", result["run_id"])
            else:
                self._event(run_id, "source", "done", "Coleta existente reutilizada")

            check_cancel()
            destination_ok, _ = self._destination_status(True)
            if not destination_ok:
                if not auth_checked:
                    self._ensure_auth(run_id, progress, should_cancel)
                emit(
                    {
                        "step": "destination",
                        "status": "running",
                        "message": "Capturando snapshot do destino",
                    }
                )
                result = self.operations.snapshot_destination(
                    self.config, progress=progress, should_cancel=should_cancel
                )
                if not result.get("complete") or not result.get("pagination_complete"):
                    raise PrepareError(
                        "snapshot do destino incompleto",
                        code="destination_snapshot_incomplete",
                        hint="A captura anterior foi preservada; repita a captura.",
                    )
                self._adopt_snapshot(result["run_id"])
                self._event(run_id, "destination", "done", result["run_id"])
            else:
                self._event(run_id, "destination", "done", "Snapshot existente reutilizado")

            check_cancel()
            matches_ok, matches_detail, matches_blocked = self._matches_status(True)
            if matches_blocked:
                raise PrepareError(
                    matches_detail,
                    code="matches_stale",
                    hint="Reabra os itens desatualizados na revisão antes de gerar o plano.",
                )
            if not matches_ok:
                emit(
                    {
                        "step": "matches",
                        "status": "running",
                        "message": "Gerando correspondências ausentes",
                    }
                )
                self._event(run_id, "matches", "running", matches_detail)
                result = self.operations.ensure_matches(
                    self.service, progress=progress, should_cancel=should_cancel
                )
                self._event(run_id, "matches", "done", f"{result['added']} adicionadas")
            else:
                self._event(run_id, "matches", "done", "Correspondências reutilizadas")

            check_cancel()
            preview, policy_error = self._policy_preview()
            if policy_error:
                raise PrepareError(
                    "não foi possível calcular a política automática",
                    code="policy_preview_failed",
                    detail=policy_error,
                )
            needed = bool(
                preview
                and (
                    preview.get("auto_approved")
                    or preview.get("auto_reverted_category")
                    or preview.get("auto_liked")
                )
            )
            if needed and not apply_policy:
                steps = self._steps_with(
                    "policy", "needs_confirmation", self._policy_detail(preview)
                )
                self.store.finish(
                    run_id,
                    status="waiting_confirmation",
                    steps=steps,
                    result={"pause": "policy", "preview": preview},
                )
                return {
                    "status": "waiting_confirmation",
                    "pause": "policy",
                    "preview": preview,
                    "run_id": run_id,
                }
            if needed:
                emit(
                    {
                        "step": "policy",
                        "status": "running",
                        "message": "Aplicando política automática confirmada",
                    }
                )
                applied = self.service.apply_automatic_policy()
                self._event(run_id, "policy", "done", json.dumps(applied, ensure_ascii=False))
            else:
                self._event(run_id, "policy", "done", "Nada a aplicar")

            check_cancel()
            plan_status = self._plan_status()
            plan_ok = bool(
                plan_status
                and plan_status.get("exists")
                and not plan_status.get("invalid")
                and not plan_status.get("stale")
            )
            if plan_ok:
                plan_id = str(plan_status["plan_id"])
                self._event(run_id, "plan", "done", f"Plano reutilizado: {plan_id}")
            else:
                emit(
                    {
                        "step": "plan",
                        "status": "running",
                        "message": "Gerando plano consolidado",
                    }
                )
                self._event(run_id, "plan", "running", "Gerando plano")
                result = self.service.generate_plan()
                plan_id = str(result["plan_id"])
                self.state.active_plan_id = plan_id
                self.state.active_execution_id = None
                self.state.save()
                plan_status = self._plan_status()
                self._event(run_id, "plan", "done", plan_id)

            if not plan_status or not plan_status.get("exists"):
                raise PrepareError("plano não foi publicado", code="plan_missing")
            if plan_status.get("invalid") or plan_status.get("stale"):
                raise PrepareError(
                    "plano inválido ou desatualizado após a geração",
                    code="plan_invalid",
                    detail="; ".join(
                        [*(plan_status.get("invalid") or []), *(plan_status.get("stale") or [])]
                    ),
                )
            if not plan_status.get("provenance_complete"):
                raise PrepareError(
                    "plano sem proveniência completa",
                    code="plan_provenance_incomplete",
                    hint="Não publique este plano como pronto para aprovação.",
                )

            result = {
                "plan_id": plan_id,
                "actions": int(plan_status.get("actions") or 0),
                "unmatched": int(plan_status.get("unmatched") or 0),
                "source_run_id": self.service.paths.source_run.name,
                "destination_run_id": self.state.account_snapshot_run_id,
                "run_id": run_id,
            }
            steps = self._completed_steps(plan_status)
            self.store.finish(run_id, status="done", steps=steps, result=result)
            emit({"step": "done", "status": "done", "message": "Preparação concluída"})
            return {"status": "done", **result}
        except OperationCancelled:
            self.store.finish(
                run_id,
                status="cancelled",
                steps=self.scan()["steps"],
                error={"error": "preparação cancelada", "code": "cancelled"},
            )
            raise
        except Exception as exc:
            payload = exc.as_dict() if isinstance(exc, SetupError) else {"error": str(exc)}
            self.store.finish(run_id, status="failed", steps=self.scan()["steps"], error=payload)
            raise

    # -- leitura de estado ----------------------------------------------
    def _auth_status(self) -> tuple[bool, str]:
        identity = (self.state.identity or {}).get("jazerei") or {}
        if identity.get("logged_in"):
            checked = identity.get("checked_at") or "nesta sessão"
            return True, f"Sessão verificada em {checked}."
        return False, "Login manual no Já Zerei necessário antes de preparar a execução."

    def _source_status(self) -> tuple[bool, str]:
        run = self.service.paths.source_run
        try:
            errors = verify_manifest(run)
            if errors:
                return False, "Manifesto da coleta inválido."
            read_json(run / "manifest.json")
            reviews = read_json(run / "reviews.json")
            games = read_json(run / "games.json")
        except Exception as exc:  # noqa: BLE001 - ausência vira etapa necessária
            return False, f"Coleta ausente ou inválida: {type(exc).__name__}."
        if not isinstance(reviews, list) or not isinstance(games, list):
            return False, "Coleta incompleta."
        return True, f"{len(reviews)} reviews reutilizáveis em {run.name}."

    def _destination_status(self, source_ok: bool) -> tuple[bool, str]:
        if not source_ok:
            return False, "Aguardando coleta válida."
        snapshot = self.service._account_snapshot()
        if snapshot is None:
            return False, "Nenhum snapshot completo da conta."
        run = snapshot.parent.parent
        try:
            if verify_manifest(run):
                return False, "Manifesto do snapshot inválido."
            manifest = read_json(run / "manifest.json")
        except Exception as exc:  # noqa: BLE001
            return False, f"Snapshot inválido: {type(exc).__name__}."
        if not manifest.get("capture_complete") or not manifest.get("pagination_complete"):
            return False, "Snapshot incompleto."
        if self._older_than_source(manifest, run):
            return False, "Snapshot mais antigo que a coleta; precisa ser atualizado."
        return True, f"Snapshot reutilizável: {run.name}."

    def _older_than_source(self, manifest: dict[str, Any], run: Path) -> bool:
        try:
            source_manifest = read_json(self.service.paths.source_run / "manifest.json")
        except Exception:  # noqa: BLE001
            return False
        source_created = str(source_manifest.get("created_at") or "")
        snapshot_created = str(manifest.get("created_at") or "")
        if not source_created or not snapshot_created:
            return False
        return snapshot_created < source_created

    def _matches_status(self, source_ok: bool) -> tuple[bool, str, bool]:
        if not source_ok:
            return False, "Aguardando coleta.", False
        try:
            reviews = read_json(self.service.paths.source_run / "reviews.json")
            matches = (
                read_json(self.service.paths.matches)
                if self.service.paths.matches.is_file()
                else []
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"Coleta de origem ilegível: {type(exc).__name__}.", True
        known = {str(item.get("source_slug")) for item in matches}
        missing = [item for item in reviews if str(item.get("game_slug")) not in known]
        if not matches:
            return False, f"{len(missing)} review(s) sem correspondência.", False
        try:
            summary = self.service.state()["summary"]
        except Exception as exc:  # noqa: BLE001
            return False, f"Correspondências ilegíveis: {type(exc).__name__}.", True
        if summary.get("stale_matches"):
            return (
                False,
                f"{summary['stale_matches']} decisão(ões) de correspondência desatualizada(s).",
                True,
            )
        if missing:
            return False, f"{len(missing)} review(s) sem correspondência.", False
        return True, f"{len(matches)} correspondências reutilizáveis.", False

    def _policy_preview(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            return self.service.apply_automatic_policy(dry_run=True), None
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _policy_detail(preview: dict[str, Any] | None) -> str:
        if not preview:
            return "Prévia indisponível."
        return (
            f"{preview.get('auto_approved', 0)} correspondência(s), "
            f"{preview.get('auto_reverted_category', 0)} reversão(ões) por categoria, "
            f"{preview.get('auto_liked', 0)} liked. Requer confirmação explícita."
        )

    def _plan_status(self) -> dict[str, Any] | None:
        plan_id = self.state.active_plan_id
        if not plan_id:
            return None
        if self.migration is None:
            return None
        try:
            status = self.migration.status(plan_id)
        except Exception:  # noqa: BLE001
            return None
        return status if status.get("exists") else None

    def _safety_status(self) -> dict[str, Any]:
        if self.migration is None:
            return {"blocked": False, "ambiguous": [], "detail": "Sem histórico consultável."}
        ambiguous: list[dict[str, Any]] = []
        try:
            for entry in self.migration.list_executions():
                if int((entry.get("summary") or {}).get("ambiguous") or 0) <= 0:
                    continue
                detail = self.migration.execution_detail(entry["execution_id"])
                for action in detail.get("actions") or []:
                    if action.get("status") == "write_ambiguous":
                        ambiguous.append(
                            {
                                "execution_id": entry["execution_id"],
                                "action_id": action.get("action_id"),
                                "source_ref": action.get("source_ref"),
                                "game_id": action.get("game_id"),
                            }
                        )
        except Exception as exc:  # noqa: BLE001 - histórico ilegível bloqueia
            return {
                "blocked": True,
                "ambiguous": [],
                "detail": f"Histórico de execuções ilegível: {type(exc).__name__}.",
            }
        if ambiguous:
            return {
                "blocked": True,
                "ambiguous": ambiguous,
                "detail": (
                    f"{len(ambiguous)} escrita(s) ambígua(s) de execução anterior. "
                    "Reconcilie com leitura autenticada antes de novo plano."
                ),
            }
        return {
            "blocked": False,
            "ambiguous": [],
            "detail": "Nenhuma escrita ambígua pendente no histórico.",
        }

    # -- mutações auxiliares --------------------------------------------
    def _ensure_auth(
        self,
        run_id: str,
        progress: Callable[[dict[str, Any]], None] | None,
        should_cancel: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        if progress is not None:
            progress(
                {
                    "step": "auth",
                    "status": "running",
                    "message": "Verificando sessão do Já Zerei",
                }
            )
        self._event(run_id, "auth", "running", "Verificando sessão")
        identity = self.operations.verify_jazerei(
            self.config, progress=progress, should_cancel=should_cancel
        )
        if not identity.get("logged_in"):
            if progress is not None:
                progress(
                    {
                        "step": "auth",
                        "status": "waiting_user",
                        "message": "Faça login manual no Já Zerei na janela aberta",
                    }
                )
            self._event(run_id, "auth", "waiting_user", "Login manual")
            identity = self.operations.login_jazerei(
                self.config, progress=progress, should_cancel=should_cancel
            )
        if not identity.get("logged_in"):
            raise PrepareError(
                "login do Já Zerei não confirmado",
                code="login_required",
                hint="Faça login manual na janela do Chromium e tente novamente.",
            )
        self.state.record_identity("jazerei", identity)
        self.state.save()
        self._event(run_id, "auth", "done", identity.get("label"))
        return identity

    def _adopt_source(self, run_id: str) -> None:
        self.state.source_run_id = run_id
        self.state.active_plan_id = None
        self.state.active_execution_id = None
        self.state.save()
        apply_state_to_service(self.service, self.state, self.config)

    def _adopt_snapshot(self, run_id: str) -> None:
        self.state.account_snapshot_run_id = run_id
        self.state.active_plan_id = None
        self.state.active_execution_id = None
        self.state.save()
        apply_state_to_service(self.service, self.state, self.config)

    def _event(self, run_id: str, step: str, status: str, detail: str | None) -> None:
        self.store.event(run_id, {"step": step, "status": status, "detail": detail})

    def _steps_with(self, step_id: str, status: str, detail: str) -> list[dict[str, Any]]:
        steps = self.scan()["steps"]
        for step in steps:
            if step["id"] == step_id:
                step["status"] = status
                step["detail"] = detail
        return steps

    def _completed_steps(self, plan_status: dict[str, Any]) -> list[dict[str, Any]]:
        steps = self.scan()["steps"]
        for step in steps:
            if step["status"] not in {"blocked", "pending"}:
                step["status"] = "done"
        for step in steps:
            if step["id"] == "validation":
                step["detail"] = f"{plan_status.get('actions', 0)} ações publicadas."
        return steps
