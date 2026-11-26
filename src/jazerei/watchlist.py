"""Wishlist do Ja Zerei: somente leitura.

Adicao/remocao nao sao suportadas pelo gateway e nao existem helpers
mutaveis neste modulo.
"""

from __future__ import annotations

from src.jazerei.client import JazereiClient
from src.models.destination import WatchlistEntry


def list_watchlist(client: JazereiClient, user_id: str | None = None) -> list[WatchlistEntry]:
    """RPC observada; parametros/semantica ainda pendentes de validacao."""
    uid = user_id or client.auth.require_user_id()
    payload = client.post_read_json("/rest/v1/rpc/get_profile_watchlist", {"p_user_id": uid})
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    entries: list[WatchlistEntry] = []
    for item in items:
        if isinstance(item, dict) and "game_id" in item:
            entries.append(
                WatchlistEntry(
                    user_id=item.get("user_id"),
                    game_id=int(item["game_id"]),
                    game_slug=item.get("game_slug"),
                    game_title=item.get("game_title") or item.get("game_title_name"),
                    cover_url=item.get("cover_url"),
                    release_year=item.get("release_year"),
                    primary_platform=item.get("primary_platform"),
                )
            )
    return entries
