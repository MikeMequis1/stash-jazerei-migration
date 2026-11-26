from src.migration.planner import approve_plan, build_plan
from src.migration.validator import Severity, validate_plan
from src.models.destination import DestinationReview
from src.models.normalized import GameMatch, LikedState, LikedStatus, MatchDecision
from src.models.source import ReviewRecord


def approved_match(slug: str, game_id: int) -> GameMatch:
    return GameMatch(
        source_slug=slug,
        source_title=slug.replace("-", " ").title(),
        dest_game_id=game_id,
        dest_slug=slug,
        dest_name=slug.replace("-", " ").title(),
        dest_year=2020,
        confidence=0.95,
        decision=MatchDecision.APPROVED,
    )


def review(slug: str, text: str = "Bom jogo", rating: float = 7) -> ReviewRecord:
    return ReviewRecord(
        game_slug=slug,
        game_title=slug,
        text=text,
        rating=rating,
        source_url=f"https://stash.games/games/{slug}/reviews/MikeMequis1",
    )


def test_plan_starts_unapproved() -> None:
    plan = build_plan([approved_match("sky", 1)], [review("sky")])
    assert not plan.approved
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.kind == "review"
    assert action.game_id == 1
    assert not action.approved
    assert action.before is None
    assert action.after is not None
    assert action.after["rating"] == 7.0


def test_plan_preserves_existing_review_without_update() -> None:
    existing = DestinationReview(game_id=1, rating=5, text="review antiga", liked=True)
    plan = build_plan(
        [approved_match("sky", 1)],
        [review("sky")],
        destination_reviews=[existing],
    )
    assert plan.actions == []
    assert plan.skipped[0]["reason"].startswith("review existente")
    assert plan.skipped[0]["before"]["liked"] is True


def test_unmatched_game_is_preserved() -> None:
    plan = build_plan([], [review("sem-correspondencia")])
    assert plan.unmatched == ["sem-correspondencia"]
    assert plan.actions == []


def test_profile_limits_keep_excess_as_skipped() -> None:
    slugs = [f"game-{index}" for index in range(7)]
    matches = [approved_match(slug, index + 1) for index, slug in enumerate(slugs)]
    plan = build_plan(matches, [], favorite_slugs=slugs)
    favorites = [action for action in plan.actions if action.kind == "favorite"]
    assert len(favorites) == 5
    assert len([item for item in plan.skipped if item["kind"] == "favorite"]) == 2


def test_profile_limit_ignores_unmatched_entries() -> None:
    slugs = [f"game-{index}" for index in range(5)]
    matches = [approved_match(slug, index + 1) for index, slug in enumerate(slugs)]
    plan = build_plan(matches, [], favorite_slugs=["nao-existe", *slugs])
    favorites = [action for action in plan.actions if action.kind == "favorite"]
    assert len(favorites) == 5
    assert plan.unmatched == ["nao-existe"]


def test_validator_blocks_duplicate_actions() -> None:
    plan = build_plan([approved_match("sky", 1)], [review("sky")])
    plan.actions.append(plan.actions[0].model_copy())
    codes = {issue.code for issue in validate_plan(plan, require_approval=False)}
    assert "duplicate_action" in codes


def test_validator_blocks_unapproved_and_unresolved_liked() -> None:
    plan = build_plan([approved_match("sky", 1)], [review("sky")])
    codes = {issue.code for issue in validate_plan(plan)}
    assert "plan_not_approved" in codes
    assert "action_not_approved" in codes
    assert "liked_unresolved" in codes

    approve_plan(plan)
    for action in plan.actions:
        action.after["liked"] = False  # type: ignore[index]
    codes = {issue.code for issue in validate_plan(plan)}
    assert "liked_unresolved" in codes

    for action in plan.actions:
        action.liked = LikedState(
            value=False,
            status=LikedStatus.DECIDED,
            source="human",
            source_ref="sky",
            game_id=1,
            source_hash="a" * 64,
        )
    assert not [issue for issue in validate_plan(plan) if issue.severity is Severity.ERROR]
