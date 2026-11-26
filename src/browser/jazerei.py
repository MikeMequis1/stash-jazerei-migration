"""Navegacao no Ja Zerei com Playwright, somente leitura.

Busca de catalogo e publica. Reviews/perfil/Wishlist usam a sessao manual do
perfil persistente; respostas observadas da API sao capturadas em memoria para
evitar reconstruir cabecalhos ou tokens.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from src.browser.policy import classify
from src.browser.session import BrowserSession, BrowserUnavailable
from src.jazerei.auth import Auth
from src.jazerei.catalog import parse_search_payload
from src.jazerei.profile import parse_profile_payload
from src.jazerei.reviews import parse_reviews_payload
from src.models.destination import (
    DestinationGame,
    DestinationProfile,
    DestinationReview,
    WatchlistEntry,
)

SEARCH_SELECTORS = (
    'input[type="search"]',
    'input[placeholder*="buscar" i]',
    'input[placeholder*="jogo" i]',
    'input[name="q"]',
)
PROFILE_HINTS = ("/perfil", "/profile", "/u/", "/settings", "/conta")


class BrowserApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoginEvidence:
    logged_in: bool
    firebase_auth: bool
    logout_visible: bool
    login_visible: bool
    path: str
    supabase_auth: bool = False
    indexed_db: bool = False


@dataclass
class SearchOutcome:
    games: list[Any] = field(default_factory=list)
    method: str = "unknown"
    notes: list[str] = field(default_factory=list)


@dataclass
class ReviewsOutcome:
    reviews: list[DestinationReview] = field(default_factory=list)
    method: str = "unknown"
    notes: list[str] = field(default_factory=list)


@dataclass
class CapturedResponse:
    method: str
    path: str
    status: int
    body: Any = None


LOGIN_JS = """
async () => {
  const keys = Object.keys(window.localStorage || {});
  const firebase = keys.some((k) => k.startsWith('firebase:authUser'))
    || keys.some((k) => k === 'firebaseLocalStorageDb');
  let supabase = false;
  for (const key of keys) {
    if (!/^sb-.*-auth-token$/.test(key)) continue;
    try {
      const value = JSON.parse(localStorage.getItem(key) || 'null');
      if (value && (value.access_token || value.user)) supabase = true;
    } catch (error) { /* ignora valor invalido */ }
  }
  let indexedAuth = false;
  try {
    const dbs = (await indexedDB.databases()).map((db) => db.name || '');
    if (dbs.includes('firebaseLocalStorageDb')) {
      indexedAuth = await new Promise((resolve) => {
        const request = indexedDB.open('firebaseLocalStorageDb');
        request.onerror = () => resolve(false);
        request.onsuccess = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains('firebaseLocalStorage')) {
            resolve(false);
            return;
          }
          const store = db.transaction('firebaseLocalStorage', 'readonly')
            .objectStore('firebaseLocalStorage');
          const keysRequest = store.getAllKeys();
          keysRequest.onsuccess = () => resolve(
            keysRequest.result.some((key) => String(key).startsWith('firebase:authUser'))
          );
          keysRequest.onerror = () => resolve(false);
        };
        setTimeout(() => resolve(false), 1500);
      });
    }
  } catch (error) { indexedAuth = false; }
  const body = (document.body && document.body.innerText || '').toLowerCase();
  return {
    firebase,
    supabase,
    indexed: indexedAuth,
    logoutVisible: /\\b(sair|logout|encerrar sessao)\\b/.test(body),
    loginVisible: /\\b(entrar|login|sign in)\\b/.test(body),
    path: window.location.pathname,
  };
}
"""


def login_evidence(page: Any) -> LoginEvidence:
    try:
        data = page.evaluate(LOGIN_JS)
    except Exception as exc:  # noqa: BLE001 - janela pode ter sido fechada
        raise BrowserUnavailable(
            "a janela do navegador foi fechada; rode `browser status --verify` para "
            "verificar se a sessao foi salva"
        ) from exc
    firebase = bool(data.get("firebase"))
    supabase = bool(data.get("supabase"))
    indexed = bool(data.get("indexed"))
    logout = bool(data.get("logoutVisible"))
    login = bool(data.get("loginVisible"))
    return LoginEvidence(
        logged_in=firebase or supabase or indexed or logout,
        firebase_auth=firebase,
        logout_visible=logout,
        login_visible=login,
        path=str(data.get("path") or ""),
        supabase_auth=supabase,
        indexed_db=indexed,
    )


def wait_for_login(page: Any, timeout_seconds: float, poll_ms: int = 1_500) -> LoginEvidence:
    elapsed = 0.0
    evidence = login_evidence(page)
    while not evidence.logged_in and elapsed < timeout_seconds:
        try:
            page.wait_for_timeout(poll_ms)
        except Exception as exc:  # noqa: BLE001 - TargetClosedError e similares
            raise BrowserUnavailable(
                "a janela do navegador foi fechada antes de concluir o login; cookies e "
                "localStorage ja gravados foram preservados"
            ) from exc
        elapsed += poll_ms / 1000
        evidence = login_evidence(page)
    return evidence


def ensure_site(session: BrowserSession) -> None:
    host = urlsplit(session.page.url).netloc.lower()
    if "jazerei.com" not in host:
        session.goto(session.config.jazerei_base_url)


def _api_fetch(page: Any, path: str, *, method: str = "GET", payload: Any = None) -> Any:
    """Fetch same-origin. Cabecalhos sensiveis nunca sao impressos."""
    auth = Auth.from_env()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if auth.api_key:
        headers["apikey"] = auth.api_key
    if auth.id_token:
        headers["Authorization"] = f"Bearer {auth.id_token}"

    try:
        result = page.evaluate(
            """
            async ({ path, method, payload, headers }) => {
              const response = await fetch(path, {
                method,
                headers,
                body: payload === null ? undefined : JSON.stringify(payload),
                credentials: 'include',
              });
              let body = null;
              try { body = await response.json(); } catch (error) { body = null; }
              return { status: response.status, body };
            }
            """,
            {"path": path, "method": method, "payload": payload, "headers": headers},
        )
    except Exception as exc:  # noqa: BLE001 - mensagem sem cabecalhos/tokens
        raise BrowserApiError("falha ao executar fetch no navegador") from exc
    if not isinstance(result, dict):
        raise BrowserApiError("resposta inesperada do fetch no navegador")
    status = int(result.get("status") or 0)
    if status >= 400:
        raise BrowserApiError(f"API retornou HTTP {status}; sessao pode estar expirada")
    return result.get("body")


def search_games(
    session: BrowserSession,
    query: str,
    limit: int = 8,
    *,
    prefer_ui: bool = False,
) -> SearchOutcome:
    """Busca catalogo publico; tenta UI, API same-origin e DOM, nessa ordem."""
    ensure_site(session)
    page = session.page
    outcome = SearchOutcome()

    if prefer_ui:
        ui_outcome = _search_via_ui(session, query, limit)
        if ui_outcome.games:
            return ui_outcome
        outcome.notes.extend(ui_outcome.notes)

    try:
        payload = _api_fetch(
            page,
            "/api/search/games",
            method="POST",
            payload={"q": query, "limit": limit},
        )
        games = parse_search_payload(payload)
        if games:
            outcome.games = games
            outcome.method = "api_same_origin"
            return outcome
        outcome.notes.append("API de busca nao retornou candidatos")
    except BrowserApiError as exc:
        outcome.notes.append(str(exc))

    ui_outcome = _search_via_ui(session, query, limit)
    outcome.notes.extend(ui_outcome.notes)
    if ui_outcome.games:
        return ui_outcome

    dom_outcome = _search_via_dom(page)
    outcome.notes.extend(dom_outcome.notes)
    if dom_outcome.games:
        return dom_outcome

    outcome.method = "none"
    return outcome


def _search_via_ui(session: BrowserSession, query: str, limit: int) -> SearchOutcome:
    page = session.page
    for selector in SEARCH_SELECTORS:
        try:
            field = page.locator(selector).first
            if not field.is_visible(timeout=1_000):
                continue
            with page.expect_response(
                lambda response: (
                    "/api/search/games" in response.url and response.request.method == "POST"
                ),
                timeout=15_000,
            ) as info:
                field.fill(query)
                field.press("Enter")
            payload = info.value.json()
            games = parse_search_payload(payload)[:limit]
            return SearchOutcome(games=games, method="ui_network_capture")
        except Exception:  # noqa: BLE001 - heuristica de UI; segue para fallback
            continue
    return SearchOutcome(method="none", notes=["campo de busca da UI nao localizado"])


def _search_via_dom(page: Any) -> SearchOutcome:
    rows = page.eval_on_selector_all(
        "a[href]",
        """
        (anchors) => anchors.map((a) => ({
          href: a.getAttribute('href') || '',
          text: (a.textContent || '').trim(),
          gameId: a.getAttribute('data-game-id') || '',
          slug: a.getAttribute('data-game-slug') || '',
        }))
        """,
    )
    outcome = SearchOutcome(method="dom")
    for row in rows:
        href = str(row.get("href") or "")
        if "/game/" not in href and "/jogo/" not in href:
            continue
        slug = str(row.get("slug") or href.rstrip("/").rsplit("/", 1)[-1])
        if re.match(r"^\d+-", slug):
            slug = slug.split("-", 1)[1]
        game_id = str(row.get("gameId") or "")
        if not game_id.isdigit():
            match = re.search(r"/(?:game|jogo)/(\d+)", href)
            game_id = match.group(1) if match else ""
        if not game_id.isdigit() or not slug:
            outcome.notes.append("DOM com link de jogo sem id; candidato nao utilizavel")
            continue
        outcome.games.append(
            DestinationGame(
                id=int(game_id),
                slug=slug,
                name=str(row.get("text") or slug),
            )
        )
    if not outcome.games:
        outcome.notes.append("DOM nao expos candidatos com id verificavel")
    return outcome


def fetch_game_detail(session: BrowserSession, slug: str) -> dict[str, Any]:
    """Le detalhes publicos do jogo pelo DOM, sem executar acoes de conta."""
    ensure_site(session)
    page = session.page
    session.goto(f"{session.config.jazerei_base_url}/jogos/{quote(slug, safe='')}")
    session.wait(1_500)
    detail = page.evaluate(
        """
        () => {
          const meta = (property) => {
            const node = document.querySelector(`meta[property="${property}"], meta[name="${property}"]`);
            return node ? node.getAttribute('content') : null;
          };
          const jsonLd = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
            .map((node) => { try { return JSON.parse(node.textContent); } catch (e) { return null; } })
            .filter(Boolean);
          const h1 = document.querySelector('h1');
          return {
            title: document.title,
            h1: h1 ? h1.textContent.trim() : null,
            ogTitle: meta('og:title'),
            ogDescription: meta('og:description'),
            jsonLd,
            path: window.location.pathname,
          };
        }
        """
    )
    game = _video_game(detail.get("jsonLd") or [])
    return {
        "slug": slug,
        "path": detail.get("path"),
        "title": game.get("name")
        or detail.get("h1")
        or detail.get("ogTitle")
        or detail.get("title"),
        "description": game.get("description") or detail.get("ogDescription"),
        "video_game": game,
        "json_ld": detail.get("jsonLd") or [],
    }


def _video_game(json_ld: list[Any]) -> dict[str, Any]:
    """Extrai o no VideoGame do JSON-LD sem presumir estrutura unica."""
    nodes: list[dict[str, Any]] = []
    for entry in json_ld:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("@graph"), list):
            nodes.extend(node for node in entry["@graph"] if isinstance(node, dict))
        else:
            nodes.append(entry)
    for node in nodes:
        if node.get("@type") == "VideoGame":
            return {
                "name": node.get("name"),
                "url": node.get("url"),
                "description": node.get("description"),
                "date_published": node.get("datePublished"),
                "genres": node.get("genre") or [],
                "image": node.get("image"),
            }
    return {}


def _profile_link(page: Any) -> str | None:
    username = os.getenv("JAZEREI_USERNAME", "")
    links = page.eval_on_selector_all(
        "a[href]", "(els) => els.map((a) => a.getAttribute('href') || '')"
    )
    for href in links:
        lowered = href.lower()
        if username and username.lower() in lowered:
            return href
        if any(hint in lowered for hint in PROFILE_HINTS) and "login" not in lowered:
            return href
    return None


def capture_reviews(
    session: BrowserSession,
    *,
    user_id: str | None = None,
    timeout_seconds: float = 8.0,
) -> ReviewsOutcome:
    """Le reviews da propria conta, preferindo respostas reais da aplicacao."""
    ensure_site(session)
    page = session.page
    outcome = ReviewsOutcome()
    captured: list[CapturedResponse] = []

    def on_response(response: Any) -> None:
        try:
            path = urlsplit(response.url).path
            if path != "/rest/v1/reviews":
                return
            decision = classify(response.request.method, response.url)
            if not decision.allowed:
                return
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = None
            captured.append(
                CapturedResponse(
                    method=response.request.method,
                    path=path,
                    status=response.status,
                    body=body,
                )
            )
        except Exception:  # noqa: BLE001 - listener nao pode derrubar navegacao
            return

    page.on("response", on_response)
    try:
        link = _profile_link(page)
        if link:
            target = link if link.startswith("http") else f"{session.config.jazerei_base_url}{link}"
            session.goto(target)
        else:
            session.goto(f"{session.config.jazerei_base_url}/perfil")
        session.wait(int(min(timeout_seconds, 10) * 1000))
    finally:
        page.remove_listener("response", on_response)

    for response in captured:
        if isinstance(response.body, list):
            reviews = parse_reviews_payload(response.body)
            if reviews:
                outcome.reviews = reviews
                outcome.method = "network_capture"
                return outcome

    outcome.notes.append("nenhuma resposta de reviews observada na navegacao")

    if not user_id:
        auth = Auth.from_env()
        user_id = auth.user_id or None
    if not user_id:
        from src.jazerei.identity import stored_user_id

        user_id = stored_user_id(session.config.browser_dir)
    if not user_id:
        outcome.notes.append("JAZEREI_USER_ID ausente para consulta direta")
        return outcome

    path = f"/rest/v1/reviews?select=*&user_id=eq.{quote(user_id, safe='')}&order=created_at.desc"
    try:
        payload = _api_fetch(page, path)
    except BrowserApiError as exc:
        outcome.notes.append(str(exc))
        return outcome
    outcome.reviews = parse_reviews_payload(payload)
    outcome.method = "api_same_origin" if outcome.reviews else "none"
    return outcome


def get_profile(session: BrowserSession) -> tuple[DestinationProfile, dict[str, Any], list[str]]:
    ensure_site(session)
    notes: list[str] = []
    try:
        payload = _api_fetch(session.page, "/rest/v1/rpc/get_my_profile", method="POST", payload={})
        profile, raw = parse_profile_payload(payload)
        return profile, raw, notes
    except BrowserApiError as exc:
        notes.append(str(exc))
        raise


def watchlist(session: BrowserSession, user_id: str) -> tuple[list[WatchlistEntry], list[str]]:
    ensure_site(session)
    notes: list[str] = []
    try:
        payload = _api_fetch(
            session.page,
            "/rest/v1/rpc/get_profile_watchlist",
            method="POST",
            payload={"p_user_id": user_id},
        )
    except BrowserApiError as exc:
        return [], [str(exc)]

    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    entries: list[WatchlistEntry] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and "game_id" in item:
                entries.append(
                    WatchlistEntry(
                        user_id=item.get("user_id"),
                        game_id=int(item["game_id"]),
                        game_slug=item.get("game_slug"),
                        game_title=item.get("game_title"),
                        cover_url=item.get("cover_url"),
                        release_year=item.get("release_year"),
                        primary_platform=item.get("primary_platform"),
                    )
                )
    return entries, notes
