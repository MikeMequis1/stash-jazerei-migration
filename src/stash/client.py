"""Cliente Stash: somente leitura de paginas publicas de perfil."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from src.config import Config
from src.transport import HttpClient

VALID_KINDS = {"games", "reviews"}


def build_profile_url(config: Config, kind: str, page: int = 1) -> str:
    if kind not in VALID_KINDS:
        raise ValueError(f"kind invalido: {kind!r}")
    base = f"{config.stash_base_url}/users/{config.stash_username}/{kind}/"
    if page <= 1:
        return base
    return f"{base}?page={page}"


class StashClient:
    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        self.http = http or HttpClient(config)

    def profile_url(self, kind: str = "games", page: int = 1) -> str:
        return build_profile_url(self.config, kind, page)

    def fetch_page(self, kind: str, page: int = 1) -> str:
        return self.http.get_text(self.profile_url(kind, page))

    def fetch_url(self, url: str) -> str:
        return self.http.get_text(url)

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
