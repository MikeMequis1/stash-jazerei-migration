from __future__ import annotations

from src.config import Config
from src.migration.evaluation_matching import EvaluationMatchReport, EvaluationMatchResult
from src.migration.evaluation_store import (
    latest_evaluation_run,
    load_evaluation_run,
    save_match,
    write_evaluation_run,
)
from src.migration.evaluations import build_evaluation_set
from src.models.normalized import GameMatch
from src.models.source import GameRecord


def test_evaluation_run_roundtrip_and_match_persistence(tmp_path) -> None:
    config = Config(data_dir=tmp_path)
    games = [
        GameRecord(
            slug="one",
            title="One",
            source_url="https://stash.games/games/one",
            rating=8.0,
            status="beaten",
        )
    ]
    evaluation_set = build_evaluation_set(games, [], source_run_id="source")
    match = GameMatch(
        source_slug="one",
        source_title="One",
        source_year=2020,
        dest_game_id=10,
        dest_slug="one",
        dest_name="One",
        confidence=0.9,
        lookup_status="candidate",
    )
    report = EvaluationMatchReport(counts={"candidate": 1})
    run = write_evaluation_run(
        config,
        evaluation_set=evaluation_set,
        match_result=EvaluationMatchResult(matches=[match], report=report),
        source_manifest_sha256="a" * 64,
        run_id="test-evaluations",
    )
    assert latest_evaluation_run(config) == run
    loaded_set, loaded_matches, loaded_report = load_evaluation_run(run)
    assert loaded_set.counts.rating_only == 1
    assert loaded_matches[0].source_slug == "one"
    assert loaded_report.counts["candidate"] == 1
    digest = save_match(run, loaded_matches[0])
    assert len(digest) == 64
    _, saved_matches, _ = load_evaluation_run(run)
    assert saved_matches[0].source_slug == "one"
