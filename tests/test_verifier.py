from src.migration.planner import build_plan
from src.migration.verifier import verify_plan
from src.models.destination import DestinationReview
from src.models.normalized import GameMatch, MatchDecision
from src.models.source import ReviewRecord


def make_plan():
    match = GameMatch(
        source_slug="sky",
        source_title="Sky",
        dest_game_id=1,
        dest_slug="sky",
        dest_name="Sky",
        confidence=0.95,
        decision=MatchDecision.APPROVED,
    )
    review = ReviewRecord(
        game_slug="sky",
        text="Bom jogo",
        rating=7,
        source_url="https://stash.games/games/sky/reviews/MikeMequis1",
    )
    plan = build_plan([match], [review])
    for action in plan.actions:
        action.approved = True
        assert action.after is not None
        action.after["liked"] = False
    plan.approved = True
    return plan


def test_verify_marks_confirmed_review() -> None:
    plan = make_plan()
    report = verify_plan(plan, [DestinationReview(game_id=1, rating=7, text="Bom jogo")])
    assert report.checked == 1
    assert report.verified == 1
    assert report.missing == []
    assert report.mismatched == []


def test_verify_reports_missing_and_mismatch() -> None:
    plan = make_plan()
    report = verify_plan(plan, [DestinationReview(game_id=1, rating=5, text="outro")])
    assert report.mismatched == ["review:sky"]

    missing = verify_plan(plan, [])
    assert missing.missing == ["review:sky"]
