"""Parsers de HTML do Stash baseados nos seletores documentados.

Seletores sao observacoes empiricas; o parser falha de forma tolerante e
reporta campos ausentes em vez de inventar valores.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from src.models.source import GameRecord, ReviewRecord

BASE_URL = "https://stash.games"
RATING_RE = re.compile(r"\d+(?:[.,]\d+)?")
STATUS_ICON_IDS = {
    "playing",
    "playing-now",
    "beaten",
    "completed",
    "want",
    "wishlist",
    "archived",
    "dropped",
    "abandoned",
    "retired",
    "on-hold",
    "paused",
}


def _text(node: Tag | None) -> str | None:
    if node is None:
        return None
    value = node.get_text(" ", strip=True)
    return value or None


def _attr(node: Tag | None, *names: str) -> str | None:
    if node is None:
        return None
    for name in names:
        value = node.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_rating(value: str | None) -> float | None:
    if not value:
        return None
    match = RATING_RE.search(value.replace(",", "."))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value.replace(".", "").replace(",", ""))
    return int(match.group(0)) if match else None


def parse_year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", value)
    return int(match.group(0)) if match else None


def _status_from_icon(card: Tag) -> str | None:
    status_img = card.select_one("img[src*='/images/statuses/']")
    if status_img is not None:
        src = _attr(status_img, "src") or ""
        name = src.rsplit("/", 1)[-1].split(".", 1)[0]
        if name:
            return name

    use = card.select_one("use[href], use[xlink\\:href]")
    if use is None:
        return None
    ref = _attr(use, "href", "xlink:href") or ""
    if "#" not in ref:
        return None
    name = ref.rsplit("#", 1)[-1].strip()
    return name if name in STATUS_ICON_IDS else None


def _review_title(item: Tag, game_link: Tag | None) -> str | None:
    if game_link is not None:
        label = _attr(game_link, "aria-label")
        if label and label.lower().startswith("view game:"):
            return label.split(":", 1)[1].strip() or None
        title = _text(game_link.select_one("span.font-semibold"))
        if title:
            return title
    return _attr(item, "data-game-title")


def _review_counts(item: Tag) -> tuple[int | None, int | None]:
    likes: int | None = None
    comments: int | None = None
    for block in item.select(".user__review-likes-block"):
        icon = block.select_one("use[href]")
        ref = _attr(icon, "href") or ""
        value = parse_int(_text(block.select_one(".user__review-likes")) or _text(block))
        if ref.endswith("like"):
            likes = value
        elif ref.endswith("comment"):
            comments = value
    if likes is None:
        likes = parse_int(_text(item.select_one(".user__review-likes")))
    if comments is None:
        comments = parse_int(_attr(item.select_one(".user__review-comments"), "data-count"))
    return likes, comments


def _review_progress(item: Tag) -> str | None:
    icon = item.select_one("use[href$='#completionists']")
    if icon is None:
        return None
    parent = icon.find_parent("div")
    return _text(parent) if parent is not None else None


def _review_cover(item: Tag) -> str | None:
    images = item.select(".user__review-game img[src]")
    for image in reversed(images):
        src = _attr(image, "src") or ""
        if "/images/statuses/" in src or "empty_cover" in src:
            continue
        return src
    return None


def _find_rating(card: Tag) -> float | None:
    for selector in (
        ".games-list__item-rating",
        "[data-rating]",
        ".user__review-rating",
        "[class*='rating']",
    ):
        node = card.select_one(selector)
        if node is not None:
            return parse_rating(_attr(node, "data-rating") or _text(node))
    return None


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/games/([^/?#]+)", url)
    return match.group(1) if match else None


def parse_games_page(html: str, page_url: str = "") -> list[GameRecord]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[GameRecord] = []

    for card in soup.select(".games-list__item-link"):
        href = _attr(card, "href")
        slug = _slug_from_url(href) or parse_slug_from_card(card)
        title = _text(card.select_one(".games-list__item-name")) or _attr(card, "data-text")
        if not slug or not title:
            continue

        image = card.select_one("img[src], img[data-src]")
        cover = _attr(image, "src", "data-src")
        date_displayed = _text(card.select_one(".games-list__item-date"))
        category = _text(card.select_one(".games-list__category"))

        records.append(
            GameRecord(
                slug=slug,
                title=title,
                source_url=urljoin(BASE_URL, href or f"/games/{slug}"),
                cover_url=urljoin(BASE_URL, cover) if cover else None,
                year=parse_year(_attr(card, "data-year") or date_displayed),
                status=_status_from_icon(card) or _attr(card, "data-status"),
                date_displayed=date_displayed,
                rating=_find_rating(card),
                platform=_attr(card, "data-platform")
                or _text(card.select_one(".games-list__item-platform")),
                category=category,
                raw={"href": href, "date_displayed": date_displayed, "category": category},
            )
        )

    return records


def parse_slug_from_card(card: Tag) -> str | None:
    """Fallback quando o href nao contem /games/: usa data-slug."""
    return _attr(card, "data-slug")


def parse_reviews_page(html: str, page_url: str = "") -> list[ReviewRecord]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[ReviewRecord] = []

    for item in soup.select(".user__review-item"):
        comment = item.select_one(".user__review-comment[href]")
        game_link = item.select_one(".user__review-game")
        href = _attr(comment, "href")
        game_href = _attr(game_link, "href")
        slug = _slug_from_url(href) or _slug_from_url(game_href) or _attr(item, "data-game-slug")
        if not slug:
            continue

        time_node = item.select_one("time.user__review-date[datetime]")
        date = _attr(time_node, "datetime") or _text(time_node)
        likes_count, comments_count = _review_counts(item)
        cover = _review_cover(item)
        text = _text(item.select_one(".user__review-text")) or ""

        records.append(
            ReviewRecord(
                game_slug=slug,
                game_title=_review_title(item, game_link),
                cover_url=urljoin(BASE_URL, cover) if cover else None,
                text=text,
                text_source="listing",
                rating=_find_rating(item),
                date=date,
                status=_status_from_icon(item),
                progress=_review_progress(item),
                likes_count=likes_count,
                comments_count=comments_count,
                game_url=urljoin(BASE_URL, game_href) if game_href else None,
                source_url=urljoin(BASE_URL, href) if href else urljoin(BASE_URL, game_href or ""),
                raw={"review_href": href, "game_href": game_href},
            )
        )

    return records


def parse_review_detail(html: str) -> str | None:
    """Texto integral da review individual; None quando nao encontrado.

    Preserva quebras de linha do original e normaliza apenas espacos.
    """
    soup = BeautifulSoup(html, "html.parser")
    for selector in (
        ".game__review-comment",
        "[data-review-text]",
        ".review__text",
        ".review-detail__text",
        ".user__review-text",
        "article .prose",
    ):
        node = soup.select_one(selector)
        if node is not None:
            text = node.get_text()
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r" *\n *", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if text:
                return text
    return None


def missing_fields(record: Any, fields: tuple[str, ...]) -> list[str]:
    return [name for name in fields if getattr(record, name, None) in (None, "")]


def parse_use_href(card: Tag) -> str | None:
    use = card.select_one("use")
    return _attr(use, "href", "xlink:href") if use else None
