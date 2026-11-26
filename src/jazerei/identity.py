"""Identidade local da sessão do Já Zerei.

O login manual descobre o UID da conta autenticada e o guarda em
`data/browser/session-identity.json`. O arquivo contém apenas o identificador
da conta e metadados de origem: nunca tokens, cookies, senha ou cabeçalhos.

A identidade é usada como valor esperado por preflight e execução quando
`JAZEREI_USER_ID` não estiver definido no ambiente. O valor explícito no
ambiente sempre tem precedência.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.storage import utc_now

if TYPE_CHECKING:  # pragma: no cover - import apenas para tipagem
    from src.browser.session import BrowserSession

IDENTITY_FILENAME = "session-identity.json"
USER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


class SessionIdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionIdentity:
    user_id: str
    source: str


def identity_path(browser_dir: Path) -> Path:
    return browser_dir / IDENTITY_FILENAME


def _valid_user_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 256 or not USER_ID_RE.fullmatch(candidate):
        return None
    return candidate


def load_session_identity(browser_dir: Path) -> SessionIdentity | None:
    path = identity_path(browser_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    user_id = _valid_user_id(payload.get("user_id"))
    if user_id is None:
        return None
    source = payload.get("source")
    return SessionIdentity(
        user_id=user_id,
        source=source if isinstance(source, str) and source else "session_file",
    )


def stored_user_id(browser_dir: Path) -> str | None:
    identity = load_session_identity(browser_dir)
    return identity.user_id if identity else None


def save_session_identity(browser_dir: Path, identity: SessionIdentity) -> Path:
    user_id = _valid_user_id(identity.user_id)
    if user_id is None:
        raise SessionIdentityError("UID da sessão inválido")
    path = identity_path(browser_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "user_id": user_id,
        "source": identity.source,
        "captured_at": utc_now(),
        "note": "identidade esperada; nao contem credenciais",
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def _local_storage_user_id(page: Any) -> str | None:
    try:
        value = page.evaluate(
            """
            () => {
              const keys = Object.keys(window.localStorage || {});
              for (const key of keys) {
                if (!key.startsWith('firebase:authUser')) continue;
                try {
                  const parsed = JSON.parse(localStorage.getItem(key) || 'null');
                  if (parsed && typeof parsed.uid === 'string') return parsed.uid;
                } catch (error) { /* ignora valor invalido */ }
              }
              return null;
            }
            """
        )
    except Exception:  # noqa: BLE001 - janela pode ter sido fechada
        return None
    return _valid_user_id(value)


def discover_session_user_id(session: BrowserSession) -> SessionIdentity | None:
    """Descobre o UID da conta logada sem persistir credenciais.

    Ordem: IndexedDB do Firebase, localStorage do Firebase e, por último, a
    resposta autenticada de `get_my_profile` observada ao abrir `/perfil`.
    """

    from src.jazerei.state import read_profile_user_id, read_session_user_id

    try:
        indexed = _valid_user_id(read_session_user_id(session.page))
    except Exception:  # noqa: BLE001
        indexed = None
    if indexed:
        return SessionIdentity(indexed, "firebase_indexeddb")

    local = _local_storage_user_id(session.page)
    if local:
        return SessionIdentity(local, "firebase_local_storage")

    try:
        profile = _valid_user_id(read_profile_user_id(session))
    except Exception:  # noqa: BLE001
        profile = None
    if profile:
        return SessionIdentity(profile, "get_my_profile")
    return None


def resolve_expected_user_id(*, configured: str, browser_dir: Path) -> str:
    """Prioriza a variável explícita; usa a identidade local como fallback."""
    if configured.strip():
        return configured.strip()
    return stored_user_id(browser_dir) or ""


def identity_source(*, configured: str, browser_dir: Path) -> str | None:
    if configured.strip():
        return "env"
    return "session_file" if stored_user_id(browser_dir) else None
