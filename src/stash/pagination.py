"""Deteccao dinamica de paginacao via nav.profile-pagination.

Os totais observados (38/13) nao sao fixados: os links reais da navegacao
definem as URLs de cada pagina. Fallback para `?page=N` apenas se ausente.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

PAGE_PATTERNS = (
    re.compile(r"[?&]page=(\d+)"),
    re.compile(r"/page/(\d+)"),
)


def _page_number(href: str) -> int | None:
    for pattern in PAGE_PATTERNS:
        if match := pattern.search(href):
            return int(match.group(1))
    return None


def parse_page_links(html: str, base_url: str) -> dict[int, str]:
    """Mapa pagina -> URL real observada na navegacao; pagina 1 = base_url."""
    soup = BeautifulSoup(html, "html.parser")
    nav = soup.select_one("nav.profile-pagination")
    links: dict[int, str] = {1: base_url}
    if nav is None:
        return links

    for anchor in nav.select("a[href]"):
        href = anchor.get("href", "")
        page = _page_number(href)
        if page is not None and page != 1:
            links[page] = urljoin(base_url, href)
    return dict(sorted(links.items()))


def parse_last_page(html: str) -> int:
    """Maior numero de pagina encontrado na navegacao; 1 quando ausente."""
    soup = BeautifulSoup(html, "html.parser")
    nav = soup.select_one("nav.profile-pagination")
    if nav is None:
        return 1

    pages = [
        page
        for anchor in nav.select("a[href]")
        if (page := _page_number(anchor.get("href", ""))) is not None
    ]
    return max(pages, default=1)
