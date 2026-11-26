"""Transporte HTTP de leitura: timeout, retry limitado e rate limit.

Este modulo nao expoe transporte mutavel. A unica forma de emitir POST e
`post_read_json`, restrita a endpoints de consulta conhecidos. Escritas sao
uma responsabilidade futura e exclusiva do gateway de execucao; nao existe
cliente de escrita neste modulo.

Regras:
- retry apenas em GET/HEAD, nunca em POST;
- nenhum cabecalho, token ou cookie e logado;
- intervalo minimo entre requisicoes.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config import Config

SENSITIVE_KEY_PARTS = ("token", "cookie", "authorization", "api_key", "apikey", "secret")

# POSTs publicos/autenticados usados apenas para leitura.
READ_POST_PATHS = frozenset({"/api/search/games", "/api/igdb/query"})
READ_RPC_PREFIXES = (
    "/rest/v1/rpc/get_",
    "/rest/v1/rpc/public_get_",
    "/rest/v1/rpc/list_",
    "/rest/v1/rpc/count_",
    "/rest/v1/rpc/check_",
)


class HttpError(RuntimeError):
    """Falha de rede/HTTP sem expor credenciais."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ReadOnlyViolation(PermissionError):
    """Tentativa de usar o transporte de leitura para uma escrita."""


def redact(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Retorna copia segura para logging/relatorio."""
    safe: dict[str, Any] = {}
    for key, value in mapping.items():
        lowered = str(key).lower()
        if any(part in lowered for part in SENSITIVE_KEY_PARTS):
            safe[str(key)] = "***"
        elif isinstance(value, Mapping):
            safe[str(key)] = redact(value)
        else:
            safe[str(key)] = value
    return safe


def is_read_post(url: str) -> bool:
    """Permite POST somente nos endpoints de consulta conhecidos."""
    path = urlsplit(url).path
    if path in READ_POST_PATHS:
        return True
    return path.startswith(READ_RPC_PREFIXES)


class HttpClient:
    """Cliente HTTP sincrono somente-leitura, com politeness e sem retry em POST."""

    def __init__(self, config: Config, headers: Mapping[str, str] | None = None) -> None:
        self.config = config
        self._last_request_at = 0.0
        self._session = requests.Session()
        retry = Retry(
            total=config.max_retries,
            connect=config.max_retries,
            read=config.max_retries,
            status=config.max_retries,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self._session.headers.update({"User-Agent": config.user_agent})
        if headers:
            self._session.headers.update(headers)

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.config.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if method not in {"GET", "HEAD"}:
            raise ReadOnlyViolation(f"metodo {method} nao e permitido no transporte de leitura")
        self._wait()
        kwargs.setdefault("timeout", self.config.timeout)
        kwargs.setdefault("allow_redirects", False)
        try:
            response = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            # requests pode embutir a URL, mas nunca o token no cabecalho.
            raise HttpError(f"{method} {url} falhou: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise HttpError(
                f"{method} {url} retornou HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return response

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self._request("GET", url, **kwargs).text

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self._request("GET", url, **kwargs).json()

    def post_read_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        """POST de consulta; recusa qualquer endpoint fora da allowlist de leitura."""
        if not is_read_post(url):
            raise ReadOnlyViolation(f"POST de leitura nao permitido: {urlsplit(url).path}")
        if not isinstance(payload, dict):
            raise TypeError("payload de leitura deve ser um objeto JSON")
        self._wait()
        kwargs.setdefault("timeout", self.config.timeout)
        kwargs.setdefault("allow_redirects", False)
        try:
            # requests e usado com metodo explicito, sem retry de POST.
            response = self._session.post(url, json=payload, **kwargs)
        except requests.RequestException as exc:
            raise HttpError(f"POST {url} falhou: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise HttpError(
                f"POST {url} retornou HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return response.json()

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
