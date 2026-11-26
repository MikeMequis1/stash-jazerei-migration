from __future__ import annotations

from pathlib import Path

import pytest
from src.config import Config
from src.jazerei.snapshots import write_account_snapshot_run
from src.jazerei.state import IdentityError, build_account_review_snapshot
from src.migration.approval import destination_provenance
from src.models.destination import DestinationReview
from src.models.snapshot import AccountReviewsSnapshot
from src.storage import verify_manifest


def pagination(review: DestinationReview | None = None):
    from src.jazerei.state import PaginationResult

    return PaginationResult(
        reviews=[review] if review else [],
        pages_fetched=1,
        records_received=1 if review else 0,
        duplicates_discarded=0,
        termination_reason="short_page" if review else "empty_page",
        content_range_total=None,
    )


def test_account_snapshot_roundtrip_and_provenance(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path)
    review = DestinationReview(
        id="user_974",
        game_id=974,
        game_slug="resident-evil-4",
        user_id="user-1",
        rating=10,
        text="",
    )
    snapshot = build_account_review_snapshot(
        expected_user_id="user-1",
        pagination=pagination(review),
        page_size=50,
        max_pages=100,
        identity_sources=("firebase_indexeddb", "get_my_profile"),
    )
    assert isinstance(snapshot, AccountReviewsSnapshot)
    assert snapshot.capture.complete is True
    assert snapshot.pagination.complete is True
    artifact = write_account_snapshot_run(config, snapshot, run_id="account-1")
    assert artifact.snapshot_path and artifact.snapshot_path.is_file()
    assert verify_manifest(artifact.run_dir) == []
    data, loaded = destination_provenance(config, artifact.snapshot_path)
    assert data["complete"] is True
    assert data["kind"] == "account"
    assert data["review_count"] == 1
    assert data["snapshot_run_id"] == "account-1"
    assert isinstance(loaded, AccountReviewsSnapshot)


def test_account_snapshot_rejects_other_user() -> None:
    review = DestinationReview(
        id="user_974",
        game_id=974,
        game_slug="resident-evil-4",
        user_id="other",
        rating=10,
        text="",
    )
    with pytest.raises(IdentityError):
        build_account_review_snapshot(
            expected_user_id="user-1",
            pagination=pagination(review),
            page_size=50,
            max_pages=100,
        )


def test_account_snapshot_does_not_overwrite(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path)
    snapshot = build_account_review_snapshot(
        expected_user_id="user-1",
        pagination=pagination(),
        page_size=50,
        max_pages=100,
    )
    write_account_snapshot_run(config, snapshot, run_id="account-1")
    with pytest.raises(FileExistsError):
        write_account_snapshot_run(config, snapshot, run_id="account-1")
