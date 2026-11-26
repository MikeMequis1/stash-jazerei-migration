from src.browser.policy import (
    RequestCategory,
    RequestLog,
    classify,
    install_policy,
    sanitize_url,
)


def test_get_reads_allowed() -> None:
    decision = classify("GET", "https://jazerei.com/rest/v1/reviews?select=*")
    assert decision.allowed
    assert decision.category is RequestCategory.READ
    assert "token" not in sanitize_url(
        "https://jazerei.com/rest/v1/reviews?access_token=secret&select=*"
    )


def test_catalog_search_post_allowed() -> None:
    assert classify("POST", "https://jazerei.com/api/search/games").allowed


def test_read_rpcs_allowed() -> None:
    for rpc in ("get_my_profile", "get_profile_watchlist", "get_game_watchlist_count"):
        assert classify("POST", f"https://jazerei.com/rest/v1/rpc/{rpc}").allowed
    assert classify("POST", "https://jazerei.com/api/igdb/query").allowed
    assert classify(
        "POST",
        "https://example.supabase.co/rest/v1/rpc/public_get_top_reviewers",
    ).allowed
    assert not classify(
        "POST",
        "https://example.supabase.co/rest/v1/rpc/public_delete_review",
    ).allowed
    assert classify(
        "POST",
        "https://example.supabase.co/rest/v1/rpc/public_user_id_exists",
    ).allowed


def test_write_endpoints_blocked() -> None:
    blocked = [
        ("POST", "https://jazerei.com/api/reviews/upsert"),
        ("POST", "https://jazerei.com/rest/v1/user_watchlist_games"),
        ("POST", "https://jazerei.com/rest/v1/rpc/update_my_profile"),
        ("PATCH", "https://jazerei.com/rest/v1/reviews?id=eq.1"),
        ("DELETE", "https://jazerei.com/rest/v1/reviews?id=eq.1"),
        ("PUT", "https://jazerei.com/api/reviews/upsert"),
    ]
    for method, url in blocked:
        decision = classify(method, url)
        assert not decision.allowed, (method, url)
        assert decision.category is RequestCategory.BLOCKED_WRITE


def test_unknown_post_and_dangerous_get_blocked() -> None:
    unknown = classify("POST", "https://jazerei.com/api/unknown")
    assert not unknown.allowed
    assert unknown.category is RequestCategory.BLOCKED_UNKNOWN

    logout = classify("GET", "https://jazerei.com/logout")
    assert not logout.allowed
    assert logout.category is RequestCategory.BLOCKED_SIDE_EFFECT


def test_cloudflare_challenge_post_allowed() -> None:
    decision = classify(
        "POST",
        "https://stash.games/cdn-cgi/challenge-platform/h/b/fo/123/managed",
    )
    assert decision.allowed
    assert decision.category is RequestCategory.CHALLENGE


def test_firebase_login_only_in_login_mode() -> None:
    url = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=REDACTED"
    assert not classify("POST", url).allowed
    assert classify("POST", url, allow_login=True).allowed
    assert classify(
        "POST",
        "https://identitytoolkit.googleapis.com/v1/accounts:lookup?key=REDACTED",
    ).allowed
    assert not classify(
        "POST", "https://identitytoolkit.googleapis.com/v1/accounts:signUp", allow_login=True
    ).allowed
    assert classify("POST", "https://securetoken.googleapis.com/v1/token").allowed


def test_google_auth_post_only_in_login_mode() -> None:
    url = "https://accounts.google.com/o/oauth2/auth"
    assert not classify("POST", url).allowed
    assert classify("POST", url, allow_login=True).allowed
    assert classify(
        "POST",
        "https://oauth2.googleapis.com/token",
        allow_login=True,
    ).allowed
    assert not classify(
        "POST",
        "https://www.googleapis.com/oauth2/v3/userinfo",
    ).allowed


def test_install_policy_aborts_writes() -> None:
    class FakeRoute:
        def __init__(self) -> None:
            self.continued = False
            self.aborted: str | None = None

        def continue_(self) -> None:
            self.continued = True

        def abort(self, reason: str) -> None:
            self.aborted = reason

    class FakeRequest:
        def __init__(self, method: str, url: str) -> None:
            self.method = method
            self.url = url

    class FakeContext:
        def __init__(self) -> None:
            self.handler = None

        def route(self, pattern: str, handler) -> None:
            self.handler = handler

    context = FakeContext()
    log = RequestLog()
    install_policy(context, log)
    assert context.handler is not None

    write_route = FakeRoute()
    context.handler(
        write_route,
        FakeRequest("POST", "https://jazerei.com/api/reviews/upsert"),
    )
    assert write_route.aborted == "blockedbyclient"
    assert not write_route.continued

    read_route = FakeRoute()
    context.handler(
        read_route,
        FakeRequest("GET", "https://jazerei.com/rest/v1/reviews?select=*&user_id=eq.1"),
    )
    assert read_route.continued
    assert len(log.blocked) == 1
