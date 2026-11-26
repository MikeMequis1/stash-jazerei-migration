import json
from pathlib import Path

from src.config import Config
from src.stash.games import collect_games
from src.stash.reviews import collect_reviews
from src.storage import write_json

FIXTURES = Path(__file__).parent / "fixtures"


class FakeStashClient:
    def __init__(self) -> None:
        self.pages: dict[tuple[str, int], str] = {
            ("games", 1): (FIXTURES / "games_page.html").read_text(encoding="utf-8"),
            ("reviews", 1): (FIXTURES / "reviews_page.html").read_text(encoding="utf-8"),
        }

    def profile_url(self, kind: str, page: int = 1) -> str:
        return f"https://stash.games/users/user/{kind}/?page={page}"

    def fetch_page(self, kind: str, page: int = 1) -> str:
        return self.pages[(kind, page)]

    def fetch_url(self, url: str) -> str:
        if "/games/" in url and "/reviews/" not in url:
            return self.pages[("games", 1)]
        return '<div class="review__text">Um jogo lindo, mas repetitivo.</div>'


def test_collect_games_respects_page_cap(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path)
    records, report = collect_games(FakeStashClient(), config, max_pages=1)
    assert len(records) == 3
    assert report.last_page_detected == 3
    assert report.pages_requested == 1
    assert report.pages_fetched == 1
    assert report.complete is False
    assert report.missing_fields["rating"] == 1


def test_collect_games_all_pages_uses_nav_links(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path)
    records, report = collect_games(FakeStashClient(), config)
    assert report.pages_requested == 3
    assert report.pages_fetched == 3
    assert report.complete is True
    assert len(records) == 9
    assert report.duplicates == [
        "b3313",
        "incomplete",
        "sky-children-of-the-light",
    ]


def test_collect_reviews_merges_detail_and_reports(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path)
    records, report = collect_reviews(FakeStashClient(), config, max_pages=1)
    assert len(records) == 2
    assert report.details_checked == 2
    assert records[0].text_source == "detail"
    assert report.text_mismatches == []


def test_write_json_serializes_models_and_lists(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path)
    records, _report = collect_games(FakeStashClient(), config, max_pages=1)
    path = write_json(tmp_path / "games.json", records)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[0]["slug"] == "sky-children-of-the-light"
