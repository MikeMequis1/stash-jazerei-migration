"""Cliente de leitura do Ja Zerei.

Nao existe metodo de escrita neste cliente. A allowlist de POST cobre apenas
consultas conhecidas (`post_read_json`). Operacoes mutaveis futuras devem
passar exclusivamente pelo gateway de execucao, com transporte injetado.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

from src.config import Config
from src.jazerei.auth import Auth
from src.transport import HttpClient


class JazereiClient:
    base_url: str

    def __init__(
        self,
        config: Config,
        auth: Auth | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.config = config
        self.auth = auth or Auth.from_env()
        self.base_url = config.jazerei_base_url
        self.http = http or HttpClient(config)

    # -- leitura ---------------------------------------------------------
    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.http.get_json(
            f"{self.base_url}{path}", params=params, headers=self.auth.headers()
        )

    def post_read_json(self, path: str, payload: dict[str, Any]) -> Any:
        """POST autenticado de leitura; o transporte valida a allowlist."""
        return self.http.post_read_json(
            f"{self.base_url}{path}", payload, headers=self.auth.headers()
        )

    def public_post_read_json(self, path: str, payload: dict[str, Any]) -> Any:
        """POST publico de leitura (ex.: busca de catalogo), sem token."""
        return self.http.post_read_json(f"{self.base_url}{path}", payload)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
