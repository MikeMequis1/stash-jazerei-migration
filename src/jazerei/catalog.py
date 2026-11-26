"""Pesquisa e cache local do catalogo do Ja Zerei."""

from __future__ import annotations

from typing import Any

from src.jazerei.client import JazereiClient
from src.models.destination import DestinationGame


def _to_game(item: dict[str, Any]) -> DestinationGame:
    year = item.get("release_year") or item.get("year") or item.get("first_release_date")
    if isinstance(year, str) and year[:4].isdigit():
        year = int(year[:4])
    if isinstance(year, (int, float)) and year > 10000:  # epoch
        from datetime import UTC, datetime

        year = datetime.fromtimestamp(year, tz=UTC).year
    return DestinationGame(
        id=int(item["id"]),
        slug=item.get("slug"),
        name=item.get("name") or item.get("title") or "",
        cover=_cover(item),
        release_year=int(year) if year else None,
        platforms=_names(item.get("platforms")),
        genres=_names(item.get("genres")),
        developer=_developer(item),
    )


def _cover(item: dict[str, Any]) -> str | None:
    cover = item.get("cover")
    if isinstance(cover, str):
        return cover
    if isinstance(cover, dict):
        image_id = cover.get("image_id")
        if image_id:
            return f"https://images.igdb.com/igdb/image/upload/t_cover_big_2x/{image_id}.jpg"
        return cover.get("url") or cover.get("image_url")
    return None


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("name") or entry.get("title")
            if name:
                names.append(str(name))
    return names


def _developer(item: dict[str, Any]) -> str | None:
    involved = item.get("involved_companies")
    if isinstance(involved, list):
        developers: list[str] = []
        for entry in involved:
            if not isinstance(entry, dict):
                continue
            company = entry.get("company")
            name = company.get("name") if isinstance(company, dict) else None
            if name and entry.get("developer"):
                developers.append(str(name))
        if developers:
            return developers[0]
    companies = item.get("companies") or item.get("developers")
    names = _names(companies)
    return names[0] if names else None


def search_games(client: JazereiClient, query: str, limit: int = 8) -> list[DestinationGame]:
    """Busca candidatos; nunca escolhe automaticamente o primeiro resultado."""
    payload = client.public_post_read_json("/api/search/games", {"q": query, "limit": limit})
    return parse_search_payload(payload)


def parse_search_payload(payload: Any) -> list[DestinationGame]:
    """Normaliza a resposta de /api/search/games para uso HTTP e Browser."""
    items = payload.get("results", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    games: list[DestinationGame] = []
    for item in items:
        if isinstance(item, dict) and "id" in item:
            games.append(_to_game(item))
    return games
