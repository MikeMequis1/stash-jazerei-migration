from src.migration.matcher import match_game, score_candidate
from src.models.destination import DestinationGame
from src.models.normalized import MatchDecision
from src.models.source import GameRecord


def source(**overrides) -> GameRecord:
    data = {
        "slug": "sonic-unleashed",
        "title": "Sonic Unleashed",
        "source_url": "https://stash.games/games/sonic-unleashed",
        "year": 2008,
        "platform": "PlayStation 2",
    }
    data.update(overrides)
    return GameRecord(**data)


def candidate(**overrides) -> DestinationGame:
    data = {
        "id": 5169,
        "slug": "sonic-unleashed",
        "name": "Sonic Unleashed",
        "release_year": 2008,
        "platforms": ["PlayStation 2"],
    }
    data.update(overrides)
    return DestinationGame(**data)


def test_exact_match_scores_high_but_is_not_approved() -> None:
    match = match_game(source(), [candidate()])
    assert match.confidence >= 0.9
    assert match.dest_game_id == 5169
    assert match.decision is MatchDecision.CANDIDATE
    assert match.decision is not MatchDecision.APPROVED


def test_first_result_is_not_chosen_when_ambiguous() -> None:
    matches = [
        candidate(id=1, slug="sonic-unleashed", name="Sonic Unleashed"),
        candidate(id=2, slug="sonic-unleashed", name="Sonic Unleashed", platforms=["Wii"]),
    ]
    match = match_game(source(), matches)
    assert match.decision is MatchDecision.MANUAL_REVIEW
    assert any("ambiguidade" in reason for reason in match.reasons)


def test_no_candidates_goes_to_manual_review() -> None:
    match = match_game(source(), [])
    assert match.decision is MatchDecision.MANUAL_REVIEW
    assert match.dest_game_id is None
    assert match.alternatives == []


def test_remake_penalty() -> None:
    original, _ = score_candidate(source(), candidate())
    remake, reasons = score_candidate(source(), candidate(name="Sonic Unleashed Remake"))
    assert remake < original
    assert "risco de versao/remake/port" in reasons


def test_weak_candidate_is_manual_review() -> None:
    match = match_game(
        source(), [candidate(id=99, slug="outro", name="Outro Jogo", release_year=1999)]
    )
    assert match.decision is MatchDecision.MANUAL_REVIEW
