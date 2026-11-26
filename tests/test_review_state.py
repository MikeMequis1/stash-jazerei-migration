from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from src.config import Config
from src.jazerei.snapshots import (
    allocate_run_id,
    write_failure_run,
    write_snapshot_run,
)
from src.jazerei.state import (
    CaptureError,
    IdentityError,
    IdentityEvidence,
    PageResponse,
    PaginationError,
    PaginationResult,
    build_review_snapshot,
    paginate_reviews,
    verify_identity,
)
from src.storage import read_json, verify_manifest


def item(
    review_id: str,
    *,
    game_id: int = 1,
    user_id: str = "user-1",
    **extra,
) -> dict:
    data = {
        "id": review_id,
        "game_id": game_id,
        "game_slug": "game",
        "user_id": user_id,
        "rating": 7.0,
        "text": "texto",
        "liked": True,
        "likes_count": 2,
        "comments_count": 1,
    }
    data.update(extra)
    return data


def page(
    records: list[dict],
    *,
    status: int = 200,
    content_range: str | None = None,
) -> PageResponse:
    return PageResponse(status=status, body=records, content_range=content_range)


def pagination(records: list[dict], **kwargs) -> PaginationResult:
    return paginate_reviews(
        lambda offset, end: page(records),
        expected_user_id="user-1",
        page_size=10,
        **kwargs,
    )


def test_identity_requires_all_sources_to_agree() -> None:
    evidence = IdentityEvidence(
        expected_user_id="user-1",
        session_user_id="user-1",
        api_user_id="user-1",
        review_query_user_id="user-1",
        sources=("firebase_indexeddb", "get_my_profile", "reviews_query"),
    )
    assert evidence.verified
    verify_identity(evidence)


@pytest.mark.parametrize(
    "evidence",
    [
        IdentityEvidence("", "user-1", "user-1", None, ()),
        IdentityEvidence("user-1", None, "user-1", None, ()),
        IdentityEvidence("user-1", "user-1", None, None, ()),
        IdentityEvidence("user-1", "user-2", "user-1", None, ()),
        IdentityEvidence("user-1", "user-1", "user-2", None, ()),
        IdentityEvidence("user-1", "user-1", "user-1", "user-2", ()),
    ],
)
def test_identity_invalid_variants(evidence: IdentityEvidence) -> None:
    assert not evidence.verified
    with pytest.raises(IdentityError):
        verify_identity(evidence)


def test_pagination_one_page() -> None:
    result = pagination([item("r1"), item("r2")])
    assert result.pages_fetched == 1
    assert result.records_received == 2
    assert result.duplicates_discarded == 0
    assert result.termination_reason == "short_page"


def make_fetcher(pages: list[PageResponse]):
    calls: list[int] = []

    def fetch(offset: int, end: int) -> PageResponse:
        calls.append(offset)
        return pages[len(calls) - 1]

    return fetch, calls


def test_pagination_multiple_pages() -> None:
    pages = [
        page([item("r1"), item("r2")], content_range="0-1/3"),
        page([item("r3")], content_range="2-2/3"),
    ]
    fetch, _ = make_fetcher(pages)
    result = paginate_reviews(fetch, expected_user_id="user-1", page_size=2)
    assert len(result.reviews) == 3
    assert result.pages_fetched == 2
    assert result.records_received == 3
    assert result.termination_reason == "content_range_total"


def test_pagination_content_range_and_empty_page() -> None:
    pages = [
        page([item("r1"), item("r2")], content_range="0-1/3"),
        page([item("r3")], content_range="2-2/3"),
    ]
    calls = []

    def fetch(offset: int, end: int) -> PageResponse:
        calls.append((offset, end))
        return pages[len(calls) - 1]

    result = paginate_reviews(fetch, expected_user_id="user-1", page_size=2)
    assert calls == [(0, 1), (2, 3)]
    assert result.content_range_total == 3
    assert result.termination_reason == "content_range_total"


def test_pagination_empty_first_page_is_valid() -> None:
    result = paginate_reviews(
        lambda offset, end: page([], content_range="*/0"),
        expected_user_id="user-1",
        page_size=50,
    )
    assert result.reviews == []
    assert result.pages_fetched == 1
    assert result.termination_reason == "content_range_total"


def test_pagination_empty_page_after_full_page_is_valid_terminal() -> None:
    pages = [
        page([item("r1"), item("r2")], content_range=None),
        page([], content_range=None),
    ]
    fetch, _ = make_fetcher(pages)
    result = paginate_reviews(fetch, expected_user_id="user-1", page_size=2)
    assert len(result.reviews) == 2
    assert result.pages_fetched == 2
    assert result.termination_reason == "empty_page"


def test_pagination_http_error_on_second_page_fails() -> None:
    pages = [
        page([item("r1"), item("r2")], content_range=None),
        page([], status=500),
    ]
    fetch, _ = make_fetcher(pages)
    with pytest.raises(PaginationError, match="HTTP"):
        paginate_reviews(fetch, expected_user_id="user-1", page_size=2)


def test_pagination_deduplicates_records() -> None:
    pages = [
        page([item("r1"), item("r2")], content_range="0-1/3"),
        page([item("r2"), item("r3")], content_range="2-3/3"),
    ]
    fetch, _ = make_fetcher(pages)
    result = paginate_reviews(fetch, expected_user_id="user-1", page_size=2)
    assert [review.id for review in result.reviews] == ["r1", "r2", "r3"]
    assert result.records_received == 4
    assert result.duplicates_discarded == 1


def test_pagination_repeated_page_fails() -> None:
    pages = [
        page([item("r1"), item("r2")], content_range=None),
        page([item("r1"), item("r2")], content_range=None),
    ]
    fetch, _ = make_fetcher(pages)
    with pytest.raises(PaginationError, match="pagina repetida"):
        paginate_reviews(fetch, expected_user_id="user-1", page_size=2)


def test_pagination_http_and_malformed_failures() -> None:
    for response in (
        page([], status=401),
        page([], status=403),
        page([], status=416),
        page([], status=500),
        PageResponse(status=200, body={"not": "list"}),
        page([{"id": "r1"}]),
        page([item("r1", user_id="other")]),
    ):
        with pytest.raises((IdentityError, PaginationError)):
            paginate_reviews(
                lambda offset, end, response=response: response,
                expected_user_id="user-1",
                page_size=2,
            )


def test_pagination_short_before_known_total_fails() -> None:
    with pytest.raises(PaginationError, match="antes do total"):
        paginate_reviews(
            lambda offset, end: page([item("r1")], content_range="0-0/5"),
            expected_user_id="user-1",
            page_size=2,
        )


def test_pagination_max_pages_fails() -> None:
    with pytest.raises(PaginationError, match="max_pages"):
        paginate_reviews(
            lambda offset, end: page([item(f"r{offset}"), item(f"s{offset}")]),
            expected_user_id="user-1",
            page_size=2,
            max_pages=1,
        )


def test_build_snapshot_exists_preserves_fields_and_hash() -> None:
    records = pagination(
        [
            item(
                "r1",
                game_id=26570,
                empty_marker="preservar",
            )
        ]
    )
    snapshot = build_review_snapshot(
        game_id=26570,
        slug="demolition-girl",
        expected_user_id="user-1",
        pagination=records,
        page_size=10,
        max_pages=100,
        captured_at="2026-01-01T00:00:00+00:00",
    )
    assert snapshot.result.exists is True
    assert snapshot.result.review is not None
    assert snapshot.result.review.liked is True
    assert snapshot.result.review.model_dump()["empty_marker"] == "preservar"
    assert snapshot.capture.complete is True
    assert snapshot.capture.response_hash
    assert snapshot.identity.verified is True


def test_build_snapshot_absent_and_hash_deterministic() -> None:
    records = pagination([item("r1", game_id=974)])
    first = build_review_snapshot(
        game_id=26570,
        slug="demolition-girl",
        expected_user_id="user-1",
        pagination=records,
        page_size=10,
        max_pages=100,
        captured_at="2026-01-01T00:00:00+00:00",
    )
    second = build_review_snapshot(
        game_id=26570,
        slug="demolition-girl",
        expected_user_id="user-1",
        pagination=records,
        page_size=10,
        max_pages=100,
        captured_at="2027-02-02T00:00:00+00:00",
    )
    assert first.result.exists is False
    assert first.result.review is None
    assert first.capture.response_hash == second.capture.response_hash


def test_build_snapshot_duplicate_game_is_ambiguous() -> None:
    records = pagination([item("r1", game_id=26570), item("r2", game_id=26570)])
    with pytest.raises(CaptureError, match="ambigua"):
        build_review_snapshot(
            game_id=26570,
            slug="demolition-girl",
            expected_user_id="user-1",
            pagination=records,
            page_size=10,
            max_pages=100,
        )


def test_build_snapshot_sanitizes_credentials_in_urls() -> None:
    records = pagination(
        [
            item(
                "r1",
                game_id=26570,
                user_avatar="https://firebasestorage.example/avatar?alt=media&token=secret-value",
            )
        ]
    )
    snapshot = build_review_snapshot(
        game_id=26570,
        slug="demolition-girl",
        expected_user_id="user-1",
        pagination=records,
        page_size=10,
        max_pages=100,
    )
    dumped = snapshot.model_dump(mode="json")
    assert "secret-value" not in str(dumped)
    assert "token=" not in str(dumped)
    assert snapshot.result.review is not None
    assert "user_avatar" not in snapshot.result.review.model_dump()


def test_snapshot_storage_manifest_and_no_overwrite(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path)
    snapshot = build_review_snapshot(
        game_id=26570,
        slug="demolition-girl",
        expected_user_id="user-1",
        pagination=pagination([item("r1", game_id=26570)]),
        page_size=10,
        max_pages=100,
    )
    run_id = allocate_run_id(config, slug="demolition-girl", game_id=26570, run_id="run-1")
    artifact = write_snapshot_run(config, snapshot, run_id=run_id)
    assert artifact.snapshot_path is not None
    assert artifact.snapshot_path.exists()
    assert artifact.manifest_path.exists()
    assert verify_manifest(artifact.run_dir) == []
    with pytest.raises(FileExistsError):
        allocate_run_id(config, slug="demolition-girl", game_id=26570, run_id="run-1")
    with pytest.raises(FileExistsError):
        write_snapshot_run(config, snapshot, run_id="run-1")


def test_failure_artifact_is_not_a_valid_snapshot(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path)
    artifact = write_failure_run(
        config,
        run_id="failed-1",
        game_id=26570,
        slug="demolition-girl",
        expected_user_id="user-1",
        error_code="identity_invalid",
        error_message="identidade invalida token=secret-value",
    )
    assert artifact.snapshot_path is None
    assert artifact.manifest_path.exists()
    assert (artifact.run_dir / "capture-failure.json").exists()
    assert not (artifact.run_dir / "review-state").exists()
    failure = read_json(artifact.run_dir / "capture-failure.json")
    assert "secret-value" not in failure["error"]
    manifest = read_json(artifact.manifest_path)
    assert manifest["valid_snapshot"] is False
    assert verify_manifest(artifact.run_dir) == []


def test_cli_review_state_absent_returns_success(tmp_path: Path, monkeypatch) -> None:
    from src import cli

    config = Config(data_dir=tmp_path, jazerei_user_id="user-1")
    snapshot = build_review_snapshot(
        game_id=26570,
        slug="demolition-girl",
        expected_user_id="user-1",
        pagination=pagination([item("r1", game_id=974)]),
        page_size=10,
        max_pages=100,
    )

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(cli, "BrowserSession", lambda *args, **kwargs: Session())
    monkeypatch.setattr(cli, "capture_review_state", lambda *args, **kwargs: snapshot)
    args = SimpleNamespace(
        game_id=26570,
        slug="demolition-girl",
        page_size=10,
        max_pages=100,
        run_id="cli-absent",
        headless=True,
    )
    assert cli.cmd_browser_review_state(config, args) == 0
    run_root = tmp_path / "jazerei" / "runs" / "cli-absent"
    snapshot_path = run_root / "review-state" / "demolition-girl-26570.json"
    assert snapshot_path.exists()
    assert verify_manifest(run_root) == []
    saved = read_json(snapshot_path)
    assert saved["result"]["exists"] is False


def test_cli_review_state_identity_failure_writes_failure_only(tmp_path: Path, monkeypatch) -> None:
    from src import cli

    config = Config(data_dir=tmp_path, jazerei_user_id="user-1")

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(cli, "BrowserSession", lambda *args, **kwargs: Session())

    def fail(*args, **kwargs):
        raise IdentityError("conta divergente")

    monkeypatch.setattr(cli, "capture_review_state", fail)
    args = SimpleNamespace(
        game_id=26570,
        slug="demolition-girl",
        page_size=10,
        max_pages=100,
        run_id="cli-identity-failed",
        headless=True,
    )
    assert cli.cmd_browser_review_state(config, args) == 2
    run_root = tmp_path / "jazerei" / "runs" / "cli-identity-failed"
    assert (run_root / "capture-failure.json").exists()
    assert not (run_root / "review-state").exists()
    assert verify_manifest(run_root) == []
