"""Adaptador Stash via Playwright, reutilizando parsers e coletores HTTP.

O navegador so fornece HTML; `src.stash.parser` e os coletores existentes
continuam sendo a unica fonte de regras de extracao e relatorio.
"""

from __future__ import annotations

import time
from contextlib import suppress
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

from src.browser.session import BrowserSession, BrowserUnavailable
from src.config import Config
from src.stash.client import build_profile_url

CHALLENGE_MARKERS = ("challenge-platform", "Just a moment", "Um momento")
GAME_SELECTOR = ".games-list__item-link"
REVIEW_LIST_SELECTOR = ".user__review-item"
REVIEW_DETAIL_SELECTOR = (
    "[data-review-text], .review__text, .user__review-text, .game__review-comment"
)


def expected_selector(url: str) -> str:
    path = urlsplit(url).path
    if "/reviews/" in path and path.split("/reviews/", 1)[1].strip("/"):
        return REVIEW_DETAIL_SELECTOR
    if path.rstrip("/").endswith("/reviews"):
        return REVIEW_LIST_SELECTOR
    return GAME_SELECTOR


class BrowserStashClient:
    def __init__(self, session: BrowserSession, config: Config) -> None:
        self.session = session
        self.config = config

    def profile_url(self, kind: str = "games", page: int = 1) -> str:
        return build_profile_url(self.config, kind, page)

    def _load(self, url: str) -> str:
        self.session.goto(url)
        self._wait_for_challenge()
        with suppress(Exception):
            self.session.page.wait_for_selector(expected_selector(url), timeout=8_000)
        self.session.wait(300)
        return self.session.page.content()

    def _wait_for_challenge(self) -> None:
        """Cloudflare exige navegador headful; espera a verificacao terminar."""
        deadline = time.monotonic() + min(self.config.timeout, 30)
        while time.monotonic() < deadline:
            html = self.session.page.content()
            title = self.session.title()
            challenged = any(marker in html for marker in CHALLENGE_MARKERS) or any(
                marker in title for marker in CHALLENGE_MARKERS
            )
            if not challenged:
                return
            self.session.wait(1_000)
        raise BrowserUnavailable(
            "Cloudflare ainda esta verificando a sessao. Tente novamente com navegador "
            "headful; se persistir, faca uma verificacao manual com `browser open stash`."
        )

    def fetch_page(self, kind: str, page: int = 1) -> str:
        return self._load(self.profile_url(kind, page))

    def fetch_url(self, url: str) -> str:
        return self._load(url)

    def close(self) -> None:
        """Sessao pertence ao chamador; nao fecha aqui."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
