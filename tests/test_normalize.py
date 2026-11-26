from src.migration.normalize import (
    constrain_rating,
    edition_tokens,
    extract_year,
    normalize_slug,
    normalize_title,
)


def test_normalize_title() -> None:
    assert normalize_title("Sky: Children of the Light") == "sky children of the light"
    assert normalize_title("Pokémon & Cia.") == "pokemon and cia"


def test_normalize_slug() -> None:
    assert normalize_slug("Sky: Children of the Light") == "sky-children-of-the-light"
    assert normalize_slug("  --B3313--  ") == "b3313"


def test_extract_year() -> None:
    assert extract_year("2021-03-04") == 2021
    assert extract_year("sem data") is None


def test_edition_tokens() -> None:
    assert edition_tokens("Sonic Unleashed HD Remaster") == {"hd", "remaster"}
    assert edition_tokens("Sonic Unleashed") == set()


def test_constrain_rating() -> None:
    assert constrain_rating(7) == 7.0
    assert constrain_rating(7.3) == 7.5
    assert constrain_rating(None) is None
    assert constrain_rating(42) == 10.0
