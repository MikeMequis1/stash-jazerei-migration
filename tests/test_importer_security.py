import src.migration.importer as importer
from src.config import Config
from src.jazerei.auth import Auth
from src.migration.importer import preflight
from src.migration.planner import build_plan
from src.models.normalized import GameMatch, MatchDecision
from src.models.source import ReviewRecord


def plan_fixture():
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
        text="Bom",
        rating=7,
        source_url="https://stash.games/games/sky/reviews/MikeMequis1",
    )
    return build_plan([match], [review])


def test_preflight_never_allows_execution_without_flag() -> None:
    config = Config(allow_writes=False)
    summary = preflight(plan_fixture(), config)
    assert summary.allowed is False
    assert any("read-only" in error for error in summary.errors)
    assert any("gateway" in error for error in summary.errors)


def test_allow_writes_true_does_not_authorize_execution() -> None:
    config = Config(allow_writes=True)
    summary = preflight(plan_fixture(), config)
    assert summary.allowed is False
    assert any("gateway" in error for error in summary.errors)
    assert not hasattr(importer, "execute")
    assert not hasattr(importer, "ImportRefused")


def test_auth_repr_and_headers_do_not_leak_token() -> None:
    auth = Auth(api_key="key", user_id="user", id_token="secret-token")
    assert "secret-token" not in repr(auth)
    assert "secret-token" not in str(auth)
    assert auth.headers()["Authorization"] == "Bearer secret-token"
