"""Conexão, coleta e descoberta de artefatos para o wizard da interface.

Nenhuma regra de migração mora aqui: o módulo só reutiliza os coletores,
snapshots e comandos existentes para transformá-los em operações observáveis.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.browser.jazerei import login_evidence
from src.browser.session import BrowserSession, BrowserUnavailable, profile_status
from src.browser.stash import CHALLENGE_MARKERS, GAME_SELECTOR
from src.cancellation import OperationCancelled
from src.config import Config
from src.jazerei.identity import (
    discover_session_user_id,
    load_session_identity,
    save_session_identity,
    stored_user_id,
)
from src.jazerei.snapshots import write_account_snapshot_run
from src.jazerei.state import capture_account_reviews
from src.storage import read_json, sha256_file, utc_now, verify_manifest

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
WIZARD_STATE_FILENAME = "wizard-state.json"


class SetupError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.hint = hint

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": str(self)}
        for key, value in (("code", self.code), ("detail", self.detail), ("hint", self.hint)):
            if value:
                payload[key] = value
        return payload


def mask_user_id(user_id: str | None) -> str | None:
    if not user_id:
        return None
    if len(user_id) <= 8:
        return user_id[:2] + "…"
    return f"{user_id[:4]}…{user_id[-4:]}"


@dataclass
class WizardState:
    """Seleção visual persistida; nunca contém credenciais nem autoriza escrita."""

    path: Path
    active_plan_id: str | None = None
    active_execution_id: str | None = None
    source_run_id: str | None = None
    destination_run_id: str | None = None
    account_snapshot_run_id: str | None = None
    last_verification_id: str | None = None
    identity: dict[str, Any] = field(default_factory=dict)
    updated_at: str | None = None

    @classmethod
    def load(cls, config: Config) -> WizardState:
        path = config.migration_dir / WIZARD_STATE_FILENAME
        state = cls(path=path)
        if not path.is_file():
            return state
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return state
        if not isinstance(payload, dict):
            return state
        state.active_plan_id = _string_or_none(payload.get("active_plan_id"))
        state.active_execution_id = _string_or_none(payload.get("active_execution_id"))
        state.source_run_id = _string_or_none(payload.get("source_run_id"))
        state.destination_run_id = _string_or_none(payload.get("destination_run_id"))
        state.account_snapshot_run_id = _string_or_none(payload.get("account_snapshot_run_id"))
        state.last_verification_id = _string_or_none(payload.get("last_verification_id"))
        identity = payload.get("identity")
        state.identity = identity if isinstance(identity, dict) else {}
        state.updated_at = _string_or_none(payload.get("updated_at"))
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = utc_now()
        payload = {
            "schema_version": 1,
            "active_plan_id": self.active_plan_id,
            "active_execution_id": self.active_execution_id,
            "source_run_id": self.source_run_id,
            "destination_run_id": self.destination_run_id,
            "account_snapshot_run_id": self.account_snapshot_run_id,
            "last_verification_id": self.last_verification_id,
            "identity": self.identity,
            "updated_at": self.updated_at,
            "note": "selecao da interface; nao autoriza escrita",
        }
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def record_identity(self, site: str, result: dict[str, Any]) -> None:
        self.identity[site] = {
            "logged_in": bool(result.get("logged_in")),
            "user_id": result.get("user_id"),
            "source": result.get("source"),
            "checked_at": utc_now(),
            "label": result.get("label"),
        }


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _allocate_run_id(root: Path, base: str) -> str:
    candidate = base
    index = 2
    while (root / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def stash_run_dir(config: Config, run_id: str) -> Path:
    return config.stash_dir / "runs" / run_id


def destination_run_dir(config: Config, run_id: str) -> Path:
    return config.jazerei_dir / "runs" / run_id


def account_snapshot_path(config: Config, run_id: str) -> Path | None:
    run = destination_run_dir(config, run_id)
    manifest_path = run / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = read_json(manifest_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("kind") != "jazerei-account-review-state":
        return None
    relative = manifest.get("snapshot_file")
    if not isinstance(relative, str):
        return None
    path = run / relative
    return path if path.is_file() else None


def list_stash_runs(config: Config, *, limit: int = 20) -> list[dict[str, Any]]:
    root = config.stash_dir / "runs"
    if not root.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = read_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        runs.append(
            {
                "run_id": path.name,
                "created_at": manifest.get("created_at"),
                "outcome": manifest.get("outcome"),
                "method": manifest.get("method"),
                "slug": manifest.get("slug"),
                "valid": not verify_manifest(path),
                "files": len(manifest.get("files") or []),
            }
        )
    runs.sort(key=lambda item: str(item.get("created_at") or item["run_id"]), reverse=True)
    return runs[:limit]


def list_destination_runs(config: Config, *, limit: int = 20) -> list[dict[str, Any]]:
    root = config.jazerei_dir / "runs"
    if not root.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = read_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        kind = manifest.get("kind")
        if kind not in {
            "jazerei-account-review-state",
            "jazerei-account-reviews",
            "jazerei-review-state",
        }:
            continue
        runs.append(
            {
                "run_id": path.name,
                "kind": kind,
                "created_at": manifest.get("created_at"),
                "user_id": manifest.get("user_id"),
                "review_count": manifest.get("review_count"),
                "complete": bool(
                    manifest.get("capture_complete") is not False
                    and manifest.get("pagination_complete") is not False
                ),
                "outcome": manifest.get("outcome"),
                "valid": not verify_manifest(path),
                "is_account_snapshot": kind == "jazerei-account-review-state",
                "snapshot_file": manifest.get("snapshot_file"),
            }
        )
    runs.sort(key=lambda item: str(item.get("created_at") or item["run_id"]), reverse=True)
    return runs[:limit]


def run_summary(config: Config, run_id: str, *, destination: bool = False) -> dict[str, Any]:
    run = destination_run_dir(config, run_id) if destination else stash_run_dir(config, run_id)
    manifest_path = run / "manifest.json"
    if not manifest_path.is_file():
        raise SetupError(f"run ausente: {run_id}", code="run_missing")
    errors = verify_manifest(run)
    if errors:
        raise SetupError(
            f"run inválido: {run_id}",
            code="run_invalid",
            detail="; ".join(errors),
        )
    manifest = read_json(manifest_path)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "manifest_sha256": sha256_file(manifest_path),
        "manifest": manifest,
    }
    if destination:
        snapshot_path = account_snapshot_path(config, run_id)
        payload["snapshot_path"] = str(snapshot_path) if snapshot_path else None
    else:
        report_path = run / "export-report.json"
        if report_path.is_file():
            payload["report"] = read_json(report_path)
    return payload


def verify_source_run(config: Config, run_id: str) -> dict[str, Any]:
    summary = run_summary(config, run_id)
    manifest = summary["manifest"]
    if manifest.get("outcome") not in {"complete", "directed", "empty"}:
        raise SetupError(
            f"coleta não concluída: {manifest.get('outcome')}",
            code="source_run_incomplete",
            hint="Gere uma nova coleta antes de adotar esta origem.",
        )
    return summary


def verify_destination_run(config: Config, run_id: str) -> dict[str, Any]:
    summary = run_summary(config, run_id, destination=True)
    manifest = summary["manifest"]
    if manifest.get("kind") == "jazerei-account-review-state":
        if not manifest.get("capture_complete") or not manifest.get("pagination_complete"):
            raise SetupError(
                "snapshot de destino incompleto",
                code="destination_run_incomplete",
                hint="Capture novamente antes de adotar este destino.",
            )
        if not summary.get("snapshot_path"):
            raise SetupError("snapshot de conta ausente", code="destination_snapshot_missing")
    return summary


def apply_state_to_service(service: Any, state: WizardState, config: Config) -> None:
    """Aplica seleções válidas; qualquer id inválido mantém os defaults seguros."""
    source = stash_run_dir(config, state.source_run_id) if state.source_run_id else None
    destination = (
        destination_run_dir(config, state.destination_run_id) if state.destination_run_id else None
    )
    snapshot = (
        account_snapshot_path(config, state.account_snapshot_run_id)
        if state.account_snapshot_run_id
        else None
    )
    try:
        if source is not None:
            verify_manifest(source)
        if destination is not None:
            verify_manifest(destination)
        if state.account_snapshot_run_id and snapshot is None:
            raise SetupError("snapshot selecionado não existe")
    except Exception:  # noqa: BLE001 - estado antigo/invalido volta ao default
        return
    service.set_inputs(
        source_run=source,
        destination_run=destination,
        account_snapshot=snapshot,
    )


def open_stash(
    config: Config,
    *,
    timeout: float = 180.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Abre o perfil Stash e espera a página real ficar disponível."""
    url = f"{config.stash_base_url}/users/{config.stash_username}/games/"
    deadline = datetime.now(UTC).timestamp() + timeout
    with BrowserSession(config, headless=False) as session:
        session.goto(url)
        while True:
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("acesso ao Stash cancelado")
            if datetime.now(UTC).timestamp() >= deadline:
                raise SetupError(
                    "Stash não ficou disponível dentro do tempo limite",
                    code="stash_access_timeout",
                    hint=(
                        "Conclua a verificação do Cloudflare na janela do Chromium e "
                        "tente novamente. O Stash exige navegador headful neste ambiente."
                    ),
                )
            try:
                title = session.title()
                html = session.page.content()
                cards = session.page.locator(GAME_SELECTOR).count()
            except Exception as exc:  # noqa: BLE001 - janela fechada/interrompida
                raise BrowserUnavailable(
                    "a janela do Stash foi fechada antes da confirmação"
                ) from exc
            challenged = any(marker in html for marker in CHALLENGE_MARKERS) or any(
                marker in title for marker in CHALLENGE_MARKERS
            )
            if not challenged and (cards > 0 or f"@{config.stash_username}" in html):
                if progress is not None:
                    progress(
                        {
                            "status": "running",
                            "message": "Perfil Stash disponível",
                            "cards": cards,
                        }
                    )
                return {
                    "site": "stash",
                    "logged_in": True,
                    "cards": cards,
                    "title": title,
                    "user": config.stash_username,
                    "label": f"@{config.stash_username}",
                    "profile_dir": str(config.resolved_browser_profile),
                }
            if progress is not None:
                progress(
                    {
                        "status": "waiting_user",
                        "message": "Conclua a verificação/login na janela do Chromium",
                    }
                )
            session.wait(500)


def login_jazerei(
    config: Config,
    *,
    timeout: float = 300.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Abre o Já Zerei para login manual e persiste apenas o UID descoberto."""
    deadline = datetime.now(UTC).timestamp() + timeout
    with BrowserSession(config, headless=False, allow_login=True) as session:
        session.goto(config.jazerei_base_url)
        while True:
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("login cancelado")
            if datetime.now(UTC).timestamp() >= deadline:
                raise SetupError(
                    "login não confirmado dentro do tempo limite",
                    code="login_timeout",
                    hint="Feche ou repita o login; o perfil persistido não foi alterado.",
                )
            try:
                evidence = login_evidence(session.page)
            except Exception as exc:  # noqa: BLE001 - janela fechada
                raise BrowserUnavailable(
                    "a janela do Já Zerei foi fechada antes da confirmação"
                ) from exc
            if evidence.logged_in:
                identity = discover_session_user_id(session)
                if identity is not None:
                    save_session_identity(config.browser_dir, identity)
                user_id = identity.user_id if identity else None
                if progress is not None:
                    progress({"status": "running", "message": "Login confirmado"})
                return {
                    "site": "jazerei",
                    "logged_in": True,
                    "user_id": user_id,
                    "source": identity.source if identity else None,
                    "identity_saved": identity is not None,
                    "path": evidence.path,
                    "label": mask_user_id(user_id),
                    "profile_dir": str(config.resolved_browser_profile),
                }
            if progress is not None:
                progress(
                    {
                        "status": "waiting_user",
                        "message": "Conclua o login na janela do Chromium",
                    }
                )
            session.wait(750)


def verify_jazerei(
    config: Config,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if should_cancel is not None and should_cancel():
        raise OperationCancelled("verificação cancelada")
    with BrowserSession(config, headless=True) as session:
        session.goto(config.jazerei_base_url)
        session.wait(1_000)
        evidence = login_evidence(session.page)
        identity = discover_session_user_id(session) if evidence.logged_in else None
        if identity is not None:
            save_session_identity(config.browser_dir, identity)
        if progress is not None:
            progress({"status": "running", "message": "Sessão verificada"})
        return {
            "site": "jazerei",
            "logged_in": evidence.logged_in,
            "user_id": identity.user_id if identity else None,
            "source": identity.source if identity else None,
            "identity_saved": identity is not None,
            "path": evidence.path,
            "label": mask_user_id(identity.user_id if identity else None),
        }


def collect_stash(
    config: Config,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Executa a coleta completa existente em staging e publica só no fim."""
    from src import cli

    run_id = _allocate_run_id(config.stash_dir / "runs", f"{_timestamp()}-stash-browser")
    args = SimpleNamespace(
        kind="both",
        method="browser",
        browser_headless=False,
        all_pages=True,
        max_pages=1,
        slug=None,
        game_page=None,
        review_page=None,
        run_id=run_id,
        overwrite=False,
        save_html=False,
        no_details=False,
    )
    code = cli.cmd_export(config, args, on_page=progress, should_cancel=should_cancel)
    run_dir = stash_run_dir(config, run_id)
    report_path = run_dir / "export-report.json"
    if not report_path.is_file():
        raise SetupError(
            f"coleta não produziu relatório (exit {code})",
            code="collection_failed",
            hint="Verifique o console/servidor e tente novamente.",
        )
    report = read_json(report_path)
    outcome = report.get("outcome")
    if outcome == "failed" or code == 2:
        raise SetupError(
            "coleta falhou",
            code="collection_failed",
            detail=f"outcome={outcome} exit={code}",
            hint="A coleta anterior foi preservada; tente novamente com o navegador aberto.",
        )
    return {
        "run_id": run_id,
        "outcome": outcome,
        "exit_code": code,
        "report": report,
        "manifest": str(run_dir / "manifest.json"),
        "warning": "coleta parcial" if outcome == "partial" else None,
    }


def snapshot_destination(
    config: Config,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    expected_user_id = config.jazerei_user_id or stored_user_id(config.browser_dir) or ""
    if not expected_user_id:
        raise SetupError(
            "identidade do Já Zerei ausente",
            code="identity_missing",
            hint="Faça login manual no Já Zerei antes de capturar o snapshot.",
        )
    run_id = _allocate_run_id(config.jazerei_dir / "runs", f"{_timestamp()}-account-snapshot")
    with BrowserSession(config, headless=True) as session:
        snapshot = capture_account_reviews(
            session,
            expected_user_id=expected_user_id,
            page_size=config.jazerei_page_size,
            on_page=progress,
            should_cancel=should_cancel,
        )
    artifact = write_account_snapshot_run(config, snapshot, run_id=run_id)
    return {
        "run_id": artifact.run_id,
        "reviews": len(snapshot.reviews),
        "complete": snapshot.capture.complete,
        "pagination_complete": snapshot.pagination.complete,
        "snapshot": str(artifact.snapshot_path) if artifact.snapshot_path else None,
        "manifest": str(artifact.manifest_path),
    }


def identity_snapshot(config: Config, state: WizardState) -> dict[str, Any]:
    stored = load_session_identity(config.browser_dir)
    checks = state.identity
    return {
        "jazerei": {
            "user_id": config.jazerei_user_id or (stored.user_id if stored else None),
            "masked_user_id": mask_user_id(
                config.jazerei_user_id or (stored.user_id if stored else None)
            ),
            "source": config.jazerei_user_id_source or (stored.source if stored else None),
            "last_check": checks.get("jazerei"),
            "profile": profile_status(config).as_dict(),
        },
        "stash": {
            "user": config.stash_username,
            "last_check": checks.get("stash"),
        },
    }
