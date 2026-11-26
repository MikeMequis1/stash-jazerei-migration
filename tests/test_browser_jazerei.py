from pathlib import Path

import pytest
from src.browser.jazerei import (
    fetch_game_detail,
    login_evidence,
    search_games,
    wait_for_login,
)
from src.browser.session import BrowserUnavailable
from src.browser.stash import BrowserStashClient, expected_selector
from src.config import Config
from src.jazerei.auth import Auth
from src.jazerei.catalog import search_games as http_search_games
from src.jazerei.client import JazereiClient
from src.stash.games import collect_games

FIXTURES = Path(__file__).parent / "fixtures"

REAL_SEARCH_ITEM = {
    "id": 5169,
    "cover": {"id": 133729, "image_id": "co2v6p"},
    "first_release_date": 1227139200,
    "genres": [{"id": 8, "name": "Platform"}],
    "involved_companies": [
        {"company": {"id": 395, "name": "Sonic Team"}, "developer": True},
        {"company": {"id": 112, "name": "Sega"}, "developer": False},
    ],
    "name": "Sonic Unleashed",
    "platforms": [{"id": 9, "name": "PlayStation 3"}],
    "slug": "sonic-unleashed",
}


class FakePage:
    url = "https://jazerei.com/"

    def __init__(self) -> None:
        self.fetch_result: dict | Exception = {
            "status": 200,
            "body": [REAL_SEARCH_ITEM],
        }
        self.login_data = {
            "firebase": False,
            "logoutVisible": False,
            "loginVisible": True,
            "path": "/",
        }
        self.links: list[dict] = []
        self.detail: dict = {
            "title": "Sonic Unleashed | Ja Zerei",
            "h1": "Sonic Unleashed",
            "ogTitle": "Sonic Unleashed",
            "ogDescription": "Jogo de plataforma",
            "jsonLd": [],
            "path": "/game/sonic-unleashed",
        }

    def evaluate(self, script: str, arg=None):
        if "localStorage" in script:
            return self.login_data
        if "fetch(path" in script:
            if isinstance(self.fetch_result, Exception):
                raise self.fetch_result
            return self.fetch_result
        if "meta[property" in script:
            return self.detail
        raise AssertionError("script inesperado no teste")

    def locator(self, selector: str):
        raise RuntimeError("sem campo de busca simulado")

    def eval_on_selector_all(self, selector: str, script: str):
        return self.links

    def wait_for_timeout(self, ms: int) -> None:
        return

    def wait(self, ms: int) -> None:
        return


class FakeSession:
    def __init__(self, page: FakePage | None = None, config: Config | None = None) -> None:
        self.page = page or FakePage()
        self.config = config or Config()
        self.goto_calls: list[str] = []

    def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append(url)
        self.page.url = url

    def wait(self, ms: int) -> None:
        return


def test_search_api_matches_http_models() -> None:
    session = FakeSession()
    outcome = search_games(session, "Sonic Unleashed", limit=3)
    assert outcome.method == "api_same_origin"
    assert len(outcome.games) == 1
    assert outcome.games[0].id == 5169
    assert outcome.games[0].developer == "Sonic Team"
    assert outcome.games[0].cover.endswith("co2v6p.jpg")
    assert outcome.games[0].release_year == 2008

    class FakeHttp:
        def post_read_json(self, url: str, payload, **kwargs):
            return [REAL_SEARCH_ITEM]

    config = Config()
    client = JazereiClient(
        config,
        auth=Auth(api_key="k", user_id="u", id_token="token"),
        http=FakeHttp(),
    )
    http_games = http_search_games(client, "Sonic Unleashed", limit=3)
    assert [game.model_dump() for game in http_games] == [
        game.model_dump() for game in outcome.games
    ]


def test_search_dom_fallback() -> None:
    page = FakePage()
    page.fetch_result = RuntimeError("fetch indisponivel")
    page.links = [
        {"href": "/game/5169-sonic-unleashed", "text": "Sonic Unleashed", "gameId": "", "slug": ""},
        {"href": "/sobre", "text": "Sobre", "gameId": "", "slug": ""},
    ]
    outcome = search_games(FakeSession(page), "Sonic", limit=3)
    assert outcome.method == "dom"
    assert outcome.games[0].id == 5169
    assert outcome.games[0].slug == "sonic-unleashed"


def test_login_evidence_does_not_claim_auth_without_marker() -> None:
    page = FakePage()
    evidence = login_evidence(page)
    assert evidence.logged_in is False
    assert evidence.login_visible is True

    page.login_data = {
        "firebase": True,
        "logoutVisible": True,
        "loginVisible": False,
        "path": "/perfil",
    }
    evidence = login_evidence(page)
    assert evidence.logged_in is True
    assert evidence.firebase_auth is True


def test_login_evidence_detects_supabase_or_indexed_db() -> None:
    page = FakePage()
    page.login_data = {
        "firebase": False,
        "supabase": True,
        "indexed": False,
        "logoutVisible": False,
        "loginVisible": False,
        "path": "/",
    }
    assert login_evidence(page).logged_in is True

    page.login_data.update({"supabase": False, "indexed": True})
    assert login_evidence(page).logged_in is True


def test_wait_for_login_reports_closed_window() -> None:
    class ClosedPage:
        def evaluate(self, script):
            return {
                "firebase": False,
                "logoutVisible": False,
                "loginVisible": True,
                "path": "/",
            }

        def wait_for_timeout(self, ms):
            raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(BrowserUnavailable):
        wait_for_login(ClosedPage(), timeout_seconds=1, poll_ms=100)


def test_game_detail_from_dom() -> None:
    session = FakeSession()
    detail = fetch_game_detail(session, "sonic-unleashed")
    assert detail["title"] == "Sonic Unleashed"
    assert detail["path"] == "/game/sonic-unleashed"
    assert session.goto_calls[-1].endswith("/jogos/sonic-unleashed")


def test_browser_stash_adapter_reuses_parser(tmp_path: Path) -> None:
    assert expected_selector("https://stash.games/users/user/reviews/") == ".user__review-item"
    assert (
        expected_selector("https://stash.games/users/user/reviews/?page=4") == ".user__review-item"
    )
    assert ".game__review-comment" in expected_selector(
        "https://stash.games/games/demolition-girl/reviews/MikeMequis1"
    )
    assert expected_selector("https://stash.games/users/user/games/") == ".games-list__item-link"

    class Page:
        url = "https://stash.games/users/MikeMequis1/games/"

        def __init__(self) -> None:
            self.html = (FIXTURES / "games_page.html").read_text(encoding="utf-8")

        def content(self) -> str:
            return self.html

        def wait_for_selector(self, selector: str, timeout: int) -> None:
            return

        def wait_for_timeout(self, ms: int) -> None:
            return

    class Session:
        page = Page()

        def goto(self, url: str, **kwargs) -> None:
            self.page.url = url

        def title(self) -> str:
            return "Games | Stash"

        def wait(self, ms: int) -> None:
            return

    config = Config(data_dir=tmp_path)
    adapter = BrowserStashClient(Session(), config)
    records, report = collect_games(adapter, config, max_pages=1)
    assert len(records) == 3
    assert report.pages_fetched == 1
    assert records[0].title == "Sky: Children of the Light"
