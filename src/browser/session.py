"""Sessao Chromium persistente, exclusiva do projeto.

O perfil fica em `data/browser/profile` (ignorado pelo Git). Nunca usamos o
perfil pessoal do Chrome. Cada comando abre e fecha o navegador; cookies e
localStorage persistem no diretorio do perfil.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

from src.browser.policy import RequestLog, install_policy, sanitize_url
from src.config import Config


class BrowserUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserStatus:
    profile_dir: str
    profile_exists: bool
    has_cookies: bool
    has_local_storage: bool
    last_opened: str | None
    last_closed: str | None
    last_site: str | None
    apparently_usable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_dir": self.profile_dir,
            "profile_exists": self.profile_exists,
            "has_cookies": self.has_cookies,
            "has_local_storage": self.has_local_storage,
            "last_opened": self.last_opened,
            "last_closed": self.last_closed,
            "last_site": self.last_site,
            "apparently_usable": self.apparently_usable,
        }


def _meta_path(config: Config) -> Path:
    return config.browser_dir / "profile-meta.json"


def _read_meta(config: Config) -> dict[str, Any]:
    path = _meta_path(config)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_meta(config: Config, **changes: Any) -> None:
    meta = _read_meta(config)
    meta.update(changes)
    meta["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    path = _meta_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def profile_status(config: Config) -> BrowserStatus:
    profile = config.resolved_browser_profile
    meta = _read_meta(config)
    cookie_candidates = (
        profile / "Default" / "Network" / "Cookies",
        profile / "Default" / "Cookies",
    )
    has_cookies = any(path.exists() and path.stat().st_size > 0 for path in cookie_candidates)
    local_storage = profile / "Default" / "Local Storage" / "leveldb"
    has_local_storage = local_storage.exists() and any(local_storage.iterdir())
    return BrowserStatus(
        profile_dir=str(profile),
        profile_exists=profile.exists(),
        has_cookies=has_cookies,
        has_local_storage=has_local_storage,
        last_opened=meta.get("last_opened"),
        last_closed=meta.get("last_closed"),
        last_site=meta.get("last_site"),
        apparently_usable=profile.exists() and (has_cookies or has_local_storage),
    )


class BrowserSession:
    """Context manager da sessao persistente."""

    def __init__(
        self,
        config: Config,
        *,
        headless: bool | None = None,
        allow_login: bool = False,
    ) -> None:
        self.config = config
        self.headless = config.browser_headless if headless is None else headless
        self.allow_login = allow_login
        self._playwright: Any = None
        self.context: Any = None
        self.page: Any = None
        self.log = RequestLog()

    def __enter__(self) -> Self:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depende do ambiente
            raise BrowserUnavailable(
                "Playwright nao instalado. Rode: pip install playwright && "
                "python -m playwright install chromium"
            ) from exc

        profile = self.config.resolved_browser_profile
        profile.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=self.headless,
                locale="pt-BR",
                viewport={"width": 1366, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:  # noqa: BLE001 - mensagem sem segredo
            self._playwright.stop()
            self._playwright = None
            raise BrowserUnavailable(
                "nao foi possivel abrir o Chromium; verifique se o perfil nao esta em uso "
                "por outro processo e se `python -m playwright install chromium` foi executado"
            ) from exc

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        install_policy(self.context, self.log, allow_login=self.allow_login)
        self._write_opened()
        return self

    def _write_opened(self) -> None:
        _write_meta(
            self.config,
            last_opened=datetime.now(UTC).isoformat(timespec="seconds"),
            headless=self.headless,
        )

    def _ensure_open(self) -> None:
        try:
            closed = self.page is None or self.page.is_closed()
        except Exception:  # noqa: BLE001 - conexao com o navegador pode ter caido
            closed = True
        if closed:
            raise BrowserUnavailable(
                "a janela do navegador foi fechada; reabra com `browser open` ou `browser login`"
            )

    def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> Any:
        self._ensure_open()
        try:
            response = self.page.goto(
                url, wait_until=wait_until, timeout=int(self.config.timeout * 1000)
            )
        except Exception as exc:  # noqa: BLE001 - nao expor detalhes internos
            self._ensure_open()
            raise BrowserUnavailable(f"falha ao navegar para {sanitize_url(url)}") from exc
        host = urlsplit(url).netloc.lower()
        if host:
            _write_meta(self.config, last_site=host)
        return response

    def wait(self, milliseconds: int) -> None:
        self._ensure_open()
        try:
            self.page.wait_for_timeout(milliseconds)
        except Exception as exc:  # noqa: BLE001
            self._ensure_open()
            raise BrowserUnavailable("a janela do navegador foi fechada") from exc

    def content(self) -> str:
        self._ensure_open()
        try:
            return self.page.content()
        except Exception as exc:  # noqa: BLE001
            self._ensure_open()
            raise BrowserUnavailable("nao foi possivel ler a pagina") from exc

    def title(self) -> str:
        self._ensure_open()
        try:
            return self.page.title()
        except Exception as exc:  # noqa: BLE001
            self._ensure_open()
            raise BrowserUnavailable("nao foi possivel ler o titulo da pagina") from exc

    def current_url_sanitized(self) -> str:
        return sanitize_url(self.page.url)

    def close(self) -> None:
        if self.context is not None:
            with suppress(Exception):
                self.context.close()
            self.context = None
        if self._playwright is not None:
            with suppress(Exception):
                self._playwright.stop()
            self._playwright = None
        _write_meta(
            self.config,
            last_closed=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def mark_closed(config: Config) -> None:
    """Marca sessao encerrada. Nao apaga cookies nem storage local."""
    _write_meta(config, last_closed=datetime.now(UTC).isoformat(timespec="seconds"))
