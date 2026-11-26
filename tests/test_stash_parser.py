from pathlib import Path

from src.stash import parser
from src.stash.pagination import parse_last_page, parse_page_links

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_games_page() -> None:
    records = parser.parse_games_page(read("games_page.html"))
    assert [record.slug for record in records] == [
        "sky-children-of-the-light",
        "b3313",
        "incomplete",
    ]

    sky, b3313, incomplete = records
    assert sky.title == "Sky: Children of the Light"
    assert sky.status == "beaten"
    assert sky.rating == 7
    assert sky.year == 2021
    assert sky.cover_url == "https://images.example/sky.jpg"

    assert b3313.status == "playing"
    assert b3313.platform == "PC"
    assert b3313.cover_url == "https://images.example/b3313.jpg"
    assert b3313.category == "Mod"

    assert incomplete.rating is None
    assert incomplete.status is None
    assert incomplete.category is None


def test_parse_reviews_page() -> None:
    records = parser.parse_reviews_page(read("reviews_page.html"))
    assert len(records) == 2

    sky, b3313 = records
    assert sky.game_slug == "sky-children-of-the-light"
    assert sky.game_title == "Sky: Children of the Light"
    assert sky.cover_url == "https://images.igdb.com/igdb/image/upload/t_cover_big/sky.webp"
    assert sky.rating == 7
    assert sky.date == "2024-05-01"
    assert sky.status == "beaten"
    assert sky.progress == "Main story + extras"
    assert sky.likes_count == 12
    assert sky.comments_count == 3
    assert sky.text_source == "listing"

    assert b3313.text == ""
    assert b3313.rating == 8
    assert b3313.status == "playing"
    assert b3313.game_title == "B3313"


def test_parse_last_page() -> None:
    assert parse_last_page(read("games_page.html")) == 3
    assert parse_last_page(read("reviews_page.html")) == 2
    assert parse_last_page("<html><body></body></html>") == 1


def test_parse_page_links_uses_real_hrefs() -> None:
    base = "https://stash.games/users/MikeMequis1/games/"
    links = parse_page_links(read("games_page.html"), base)
    assert links[1] == base
    assert links[2].endswith("?page=2")
    assert links[3].endswith("?page=3")


def test_parse_review_detail() -> None:
    html = '<div class="review__text">Texto <strong>integral</strong>.</div>'
    assert parser.parse_review_detail(html) == "Texto integral."

    detail = '<article><p class="game__review-comment game__review-comment--full">Texto detalhe</p></article>'
    assert parser.parse_review_detail(detail) == "Texto detalhe"


def test_parse_rating_tolerates_locale() -> None:
    assert parser.parse_rating("8,5") == 8.5
    assert parser.parse_rating("sem nota") is None
