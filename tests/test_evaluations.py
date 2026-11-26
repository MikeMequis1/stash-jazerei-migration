from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.migration.evaluations import build_evaluation_set
from src.migration.liked import review_version_hash
from src.models.source import GameRecord, ReviewRecord


def game(slug: str, title: str, *, rating: float | None, category: str | None = None) -> GameRecord:
    return GameRecord(
        slug=slug,
        title=title,
        source_url=f"https://stash.games/games/{slug}",
        year=2020,
        rating=rating,
        status="beaten",
        category=category,
    )


def review(slug: str, title: str, *, rating: float | None, text: str = "texto") -> ReviewRecord:
    return ReviewRecord(
        game_slug=slug,
        game_title=title,
        text=text,
        rating=rating,
        status="beaten",
        source_url=f"https://stash.games/games/{slug}/reviews/u",
        raw={"text_listing": text, "text_detail": text},
    )


def test_classifies_written_rating_only_and_no_rating() -> None:
    games = [
        game("written", "Written", rating=8.0),
        game("rating", "Rating", rating=6.5),
        game("none", "None", rating=None),
    ]
    reviews = [review("written", "Written", rating=8.0)]
    result = build_evaluation_set(games, reviews, source_run_id="run")
    by_slug = {item.game_slug: item for item in result.evaluations}
    assert result.counts.total == 3
    assert result.counts.written_review == 1
    assert result.counts.rating_only == 1
    assert result.counts.no_rating == 1
    assert result.counts.inconsistencies == 0
    assert by_slug["written"].evaluation_kind == "written_review"
    assert by_slug["written"].text == "texto"
    assert by_slug["rating"].evaluation_kind == "rating_only"
    assert by_slug["rating"].text == ""
    assert by_slug["rating"].text_source == "none"
    assert by_slug["rating"].source_url == "https://stash.games/games/rating"
    assert "none" not in by_slug


def test_invalid_rating_and_divergence_are_inconsistencies() -> None:
    games = [
        game("bad", "Bad", rating=8.0),
        game("div", "Div", rating=8.0),
    ]
    reviews = [
        review("bad", "Bad", rating=11.0),
        review("div", "Div", rating=9.0),
    ]
    result = build_evaluation_set(games, reviews)
    assert result.counts.inconsistencies == 3
    kinds = {issue.kind for issue in result.inconsistencies}
    assert "invalid_review_rating" in kinds
    assert "rating_divergence" in kinds
    assert not result.evaluations


def test_orphan_and_duplicate_records_are_reported() -> None:
    games = [game("same", "Same", rating=7.0), game("same", "Same", rating=7.0)]
    reviews = [review("orphan", "Orphan", rating=7.0)]
    result = build_evaluation_set(games, reviews)
    kinds = {issue.kind for issue in result.inconsistencies}
    assert "duplicate_game_slug" in kinds
    assert "review_without_game" in kinds
    assert not result.evaluations


def test_rating_only_hash_is_deterministic_and_ignores_evaluation_kind() -> None:
    games = [game("rating", "Rating", rating=7.5)]
    first = build_evaluation_set(games, []).evaluations[0]
    second = build_evaluation_set(games, []).evaluations[0]
    assert review_version_hash(first) == review_version_hash(second)
    assert first.model_dump(mode="json")["evaluation_kind"] == "rating_only"


def test_special_edition_and_duplicate_title_are_preserved() -> None:
    games = [
        game("base", "Same", rating=8.0),
        game("port", "Same", rating=9.0, category="Port"),
    ]
    result = build_evaluation_set(games, [])
    assert len(result.evaluations) == 2
    assert {item.evaluation_kind for item in result.evaluations} == {"rating_only"}
    assert [item["slug"] for item in result.special_editions] == ["port"]
    assert result.duplicate_titles[0]["title"] == "Same"
    assert {item["slug"] for item in result.duplicate_titles[0]["records"]} == {"base", "port"}


def test_real_dataset_counts() -> None:
    root = Path("data/stash/runs/20261126T183458Z-stash-browser")
    if not (root / "games.json").is_file():
        pytest.skip("coleta real ausente")
    games = [
        GameRecord.model_validate(item)
        for item in json.loads((root / "games.json").read_text(encoding="utf-8"))
    ]
    reviews = [
        ReviewRecord.model_validate(item)
        for item in json.loads((root / "reviews.json").read_text(encoding="utf-8"))
    ]
    result = build_evaluation_set(games, reviews)
    assert result.counts.written_review == 76
    assert result.counts.rating_only == 622
    assert result.counts.no_rating == 416
    assert result.counts.inconsistencies == 0
