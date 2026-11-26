from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from src.config import Config
from src.migration.wizard import MigrationError, MigrationService
from src.models.destination import DestinationReview
from src.models.normalized import GameMatch, MatchDecision
from src.models.source import GameRecord, ReviewRecord
from src.review_ui import (
    ReviewPaths,
    ReviewService,
    ReviewUIError,
    ReviewUIHandler,
    liked_value_for_rating,
)
from src.storage import read_json, write_json, write_manifest


def review(slug: str, title: str, text: str = "texto") -> ReviewRecord:
    return ReviewRecord(
        game_slug=slug,
        game_title=title,
        text=text,
        rating=7.0,
        date="2025-01-01T00:00:00Z",
        status="beaten",
        source_url=f"https://stash.games/games/{slug}/reviews/MikeMequis1",
        raw={"text_listing": text, "text_detail": text},
    )


def match(
    slug: str,
    title: str,
    *,
    dest_id: int | None,
    decision: MatchDecision = MatchDecision.CANDIDATE,
    alternatives: list[dict] | None = None,
    dest_slug: str | None = None,
) -> GameMatch:
    return GameMatch(
        source_slug=slug,
        source_title=title,
        source_year=2020,
        dest_game_id=dest_id,
        dest_slug=dest_slug if dest_slug is not None else (f"{slug}-dest" if dest_id else None),
        dest_name=f"{title} (destino)" if dest_id else None,
        dest_year=2020,
        confidence=0.9 if decision is MatchDecision.CANDIDATE else 0.2,
        reasons=["slug identico"],
        decision=decision,
        alternatives=alternatives
        if alternatives is not None
        else (
            [
                {
                    "game_id": dest_id,
                    "slug": f"{slug}-dest",
                    "name": title,
                    "release_year": 2020,
                    "confidence": 0.9,
                }
            ]
            if dest_id
            else []
        ),
    )


def workspace(tmp_path: Path) -> ReviewPaths:
    source = tmp_path / "stash" / "run"
    destination = tmp_path / "jazerei" / "run"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    reviews = [
        review("strong", "Strong"),
        review("ambiguous", "Ambiguous"),
        review("none", "None"),
        review("existing", "Existing"),
    ]
    games = [
        GameRecord(
            slug=item.game_slug,
            title=item.game_title,
            year=2020,
            rating=item.rating,
            source_url=item.source_url,
        )
        for item in reviews
    ]
    write_json(source / "reviews.json", reviews)
    write_json(source / "games.json", games)
    write_manifest(source, {"schema_version": 1, "run_id": "source"})
    dest_reviews = [
        DestinationReview(
            id="user_300",
            game_id=300,
            game_slug="existing",
            user_id="user-1",
            rating=9.0,
            text="já existe",
        )
    ]
    write_json(destination / "reviews.json", dest_reviews)
    write_manifest(destination, {"schema_version": 1, "run_id": "destination"})
    matches = [
        match(
            "strong",
            "Strong",
            dest_id=100,
            alternatives=[
                {
                    "game_id": 100,
                    "slug": "strong-dest",
                    "name": "Strong destino",
                    "release_year": 2020,
                    "confidence": 0.9,
                },
                {
                    "game_id": 101,
                    "slug": "strong-alt",
                    "name": "Strong alt",
                    "release_year": 2020,
                    "confidence": 0.1,
                },
            ],
        ),
        match("ambiguous", "Ambiguous", dest_id=200, decision=MatchDecision.MANUAL_REVIEW),
        match("none", "None", dest_id=None, decision=MatchDecision.MANUAL_REVIEW),
        match("existing", "Existing", dest_id=300, dest_slug="existing"),
    ]
    matches_path = tmp_path / "migration" / "matches.json"
    write_json(matches_path, matches)
    return ReviewPaths(
        source_run=source,
        destination_run=destination,
        matches=matches_path,
        liked=tmp_path / "migration" / "liked.jsonl",
        history=tmp_path / "migration" / "history.jsonl",
        plans=tmp_path / "migration" / "plans",
        categories=tmp_path / "migration" / "categories.json",
        overrides=tmp_path / "migration" / "overrides.jsonl",
    )


def service(tmp_path: Path) -> ReviewService:
    return ReviewService(Config(data_dir=tmp_path), workspace(tmp_path))


def add_rating_only(paths: ReviewPaths) -> None:
    games = read_json(paths.source_run / "games.json")
    games.append(
        {
            "source": "stash",
            "source_url": "https://stash.games/games/rating-only",
            "raw": {},
            "slug": "rating-only",
            "title": "Rating Only",
            "year": 2020,
            "rating": 6.0,
            "status": "beaten",
            "platform": None,
            "category": None,
        }
    )
    write_json(paths.source_run / "games.json", games, overwrite=True)
    write_manifest(paths.source_run, {"schema_version": 1, "run_id": "source"}, overwrite=True)


def test_state_distinguishes_rating_only_and_blocks_automatic_policy(tmp_path: Path) -> None:
    paths = workspace(tmp_path)
    add_rating_only(paths)
    svc = ReviewService(Config(data_dir=tmp_path), paths)
    state = svc.state()
    assert state["summary"]["total"] == 5
    assert state["summary"]["written_review"] == 4
    assert state["summary"]["rating_only"] == 1
    item = next(item for item in state["items"] if item["slug"] == "rating-only")
    assert item["evaluation_kind"] == "rating_only"
    assert item["has_written_text"] is False
    assert item["text"] == ""
    assert item["match_status"] == "not_run"
    assert item["liked"]["status"] == "unresolved"
    with pytest.raises(ReviewUIError):
        svc.apply_automatic_policy(dry_run=True)
    with pytest.raises(ReviewUIError):
        svc.decide_match({"slug": "rating-only", "action": "no_match"})


def test_state_exclusive_classification_and_counts(tmp_path: Path) -> None:
    state = service(tmp_path).state()
    assert state["summary"]["total"] == 4
    assert state["summary"]["approved"] == 0
    assert state["summary"]["manual"] == 0
    assert {item["slug"]: item["initial_classification"] for item in state["items"]} == {
        "strong": "strong_candidate",
        "ambiguous": "ambiguous",
        "none": "no_candidate",
        "existing": "already_exists",
    }
    assert state["summary"]["liked_pending"] == 4


def test_liked_rating_policy() -> None:
    assert liked_value_for_rating(10) is True
    assert liked_value_for_rating(7) is True
    assert liked_value_for_rating(6.5) is None
    assert liked_value_for_rating(6) is None
    assert liked_value_for_rating(5.5) is None
    assert liked_value_for_rating(5) is False
    assert liked_value_for_rating(2) is False
    assert liked_value_for_rating(None) is None


def test_auto_policy_approves_strong_and_sets_liked(tmp_path: Path) -> None:
    svc = service(tmp_path)
    result = svc.apply_automatic_policy()
    assert result["auto_approved"] == 1
    state = svc.state()
    strong = next(item for item in state["items"] if item["slug"] == "strong")
    assert strong["review_status"] == "approved"
    assert strong["liked"]["status"] == "requires_reconfirmation"
    assert strong["liked"]["value"] is None
    assert strong["initial_classification"] == "strong_candidate"
    assert state["summary"]["eligible"] == 0
    # Idempotente: não duplica aprovação nem liked.
    again = svc.apply_automatic_policy()
    assert again["auto_approved"] == 0
    assert again["auto_reverted_category"] == 0
    assert again["auto_liked"] == 0
    assert len((svc.paths.history).read_text(encoding="utf-8").splitlines()) == 1


def test_special_category_blocks_auto_approval_and_suggests_no_match(tmp_path: Path) -> None:
    svc = service(tmp_path)
    write_json(
        svc.paths.categories,
        {"source_run_id": "test", "categories": {"strong": "Mod", "none": "Remaster"}},
    )
    result = svc.apply_automatic_policy()
    assert result["auto_approved"] == 0
    state = svc.state()
    strong = next(item for item in state["items"] if item["slug"] == "strong")
    none = next(item for item in state["items"] if item["slug"] == "none")
    assert strong["special_category"] is True
    assert strong["category"] == "Mod"
    assert strong["review_status"] == "pending"
    assert strong["suggestion"] == "manual_review"
    assert none["suggestion"] == "no_match"
    assert none["review_status"] == "pending"


def test_review_edit_preserves_original_and_is_used_in_plan(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.decide_match({"slug": "strong", "action": "approve", "candidate_id": 100})
    svc.decide_liked({"slug": "strong", "value": True})
    svc.decide_review(
        {
            "slug": "strong",
            "fields": {
                "rating": 6.0,
                "text": "texto editado",
                "status": "completed",
                "progress": "100%",
            },
        }
    )
    item = next(item for item in svc.state()["items"] if item["slug"] == "strong")
    assert item["rating"] == 6.0
    assert item["text"] == "texto editado"
    assert item["edited"] is True
    assert item["original_rating"] == 7.0
    assert item["original_text"] == "texto"
    lines = svc.paths.overrides.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    svc.decide_review({"slug": "strong", "fields": {"rating": 6.0}})
    assert len(svc.paths.overrides.read_text(encoding="utf-8").splitlines()) == 1
    result = svc.generate_plan()
    plan = read_json(Path(result["path"]) / "plan.json")
    assert plan["actions"][0]["after"]["rating"] == 6.0
    assert plan["actions"][0]["after"]["text"] == "texto editado"


def test_approve_persists_and_detects_duplicate(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.decide_match({"slug": "strong", "action": "approve", "candidate_id": 100})
    state = svc.state()
    item = next(item for item in state["items"] if item["slug"] == "strong")
    assert item["review_status"] == "approved"
    assert item["selected_candidate_id"] == 100
    lines = (svc.paths.history).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    svc.decide_match({"slug": "strong", "action": "approve", "candidate_id": 100})
    assert len((svc.paths.history).read_text(encoding="utf-8").splitlines()) == 1
    assert ReviewService(Config(data_dir=tmp_path), svc.paths).state()["summary"]["approved"] == 1


def test_reject_specific_candidate_does_not_reject_others(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.decide_match({"slug": "strong", "action": "reject", "candidate_id": 101})
    item = next(item for item in svc.state()["items"] if item["slug"] == "strong")
    assert 101 in item["rejected_candidate_ids"]
    assert item["review_status"] == "pending"
    svc.decide_match({"slug": "ambiguous", "action": "reject", "candidate_id": 200})
    item = next(item for item in svc.state()["items"] if item["slug"] == "ambiguous")
    assert item["review_status"] == "rejected"


def test_no_match_and_manual_selection(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.decide_match({"slug": "none", "action": "no_match"})
    assert (
        next(item for item in svc.state()["items"] if item["slug"] == "none")["review_status"]
        == "no_match"
    )
    svc.search_catalog = lambda query: [  # type: ignore[method-assign]
        {
            "game_id": 999,
            "slug": "manual-dest",
            "name": "Manual destino",
            "year": 2021,
            "platforms": ["PC"],
            "developer": "Dev",
        }
    ]
    svc.decide_match(
        {"slug": "ambiguous", "action": "manual", "candidate_id": 999, "query": "Manual"}
    )
    item = next(item for item in svc.state()["items"] if item["slug"] == "ambiguous")
    assert item["review_status"] == "manual"
    assert item["selected_candidate_id"] == 999


def test_liked_is_independent_and_requires_destination(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.decide_match({"slug": "strong", "action": "approve", "candidate_id": 100})
    assert svc.state()["summary"]["eligible"] == 0
    assert svc.state()["summary"]["blocked_liked"] == 1
    svc.decide_liked({"slug": "strong", "value": True})
    item = next(item for item in svc.state()["items"] if item["slug"] == "strong")
    assert item["liked"]["status"] == "decided"
    assert item["liked"]["value"] is True
    assert svc.state()["summary"]["eligible"] == 1
    svc.decide_liked({"slug": "strong", "value": True})
    assert len(svc.paths.liked.read_text(encoding="utf-8").splitlines()) == 1
    svc.decide_liked({"slug": "none", "value": False})
    no_identity = next(item for item in svc.state()["items"] if item["slug"] == "none")
    assert no_identity["liked"]["status"] == "decided"
    assert no_identity["liked"]["binding_status"] == "pending_identity"


def test_stale_source_marks_decision_stale(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.decide_match({"slug": "strong", "action": "approve", "candidate_id": 100})
    reviews = read_json(svc.paths.source_run / "reviews.json")
    reviews[0]["text"] = "texto alterado"
    write_json(svc.paths.source_run / "reviews.json", reviews, overwrite=True)
    write_manifest(
        svc.paths.source_run,
        {"schema_version": 1, "run_id": "source"},
        overwrite=True,
    )
    item = next(item for item in svc.state()["items"] if item["slug"] == "strong")
    assert item["stale"] is True
    assert (
        item["source_hash"]
        != json.loads((svc.paths.history).read_text(encoding="utf-8").splitlines()[0])[
            "source_hash"
        ]
    )


def test_plan_generation_uses_existing_planner(tmp_path: Path) -> None:
    svc = service(tmp_path)
    result = svc.generate_plan()
    assert result["plan_id"].startswith("stage9-review-")
    plan_path = Path(result["path"]) / "plan.json"
    assert plan_path.is_file()
    assert result["actions"] == 0
    assert "proveniência" in result["warning"]


def test_http_state_and_decision_endpoint(tmp_path: Path) -> None:
    svc = service(tmp_path)

    class Handler(ReviewUIHandler):
        pass

    Handler.service = svc
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/", timeout=5) as response:
            assert response.status == 200
            assert "Avançado" in response.read().decode("utf-8")
        with urlopen(f"{base}/review.js", timeout=5) as response:
            assert response.status == 200
        with urlopen(f"{base}/api/state", timeout=5) as response:
            state = json.loads(response.read().decode("utf-8"))
            assert state["summary"]["total"] == 4
        request = Request(
            f"{base}/api/match",
            data=json.dumps({"slug": "strong", "action": "approve", "candidate_id": 100}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
            assert body["summary"]["approved"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_migration_service_status_and_gate(tmp_path: Path) -> None:
    if not (Config().migration_dir / "plans" / "stage9-review-20261125T201911Z").is_dir():
        pytest.skip("plano histórico removido pelo reset absoluto")
    service = MigrationService(
        Config(),
        approvals=tmp_path / "approvals.jsonl",
    )
    status = service.status("stage9-review-20261125T201911Z")
    assert status["exists"] is True
    assert status["actions"] == 70
    assert status["approved"] is False
    assert status["write_gates"]["allow_writes"] is False
    assert status["write_gates"]["enable_real_write_transport"] is False
    with pytest.raises(MigrationError):
        service.approve(plan_id="stage9-review-20261125T201911Z", confirm="wrong")
    with pytest.raises(MigrationError):
        service.execute(
            execution_id="missing",
            plan_id="stage9-review-20261125T201911Z",
            confirm="stage9-review-20261125T201911Z",
        )


def test_real_dataset_has_698_evaluations() -> None:
    paths = ReviewPaths()
    if not paths.matches.is_file() or not paths.source_run.is_dir():
        pytest.skip("dados stage9 ausentes")
    state = ReviewService(Config()).state()
    assert state["summary"]["total"] == 698
    assert state["summary"]["written_review"] == 76
    assert state["summary"]["rating_only"] == 622
    assert state["summary"]["no_rating_excluded"] == 416
    assert state["summary"]["destination_present"] == 35
    assert state["summary"]["identity_conflicts"] == 1
    evidence = {
        item["destination_evidence"] for item in state["items"] if item["destination_present"]
    }
    assert evidence == {"execution_verified", "content_reconciled"}
    assert (
        sum(item["destination_evidence"] == "execution_verified" for item in state["items"]) == 17
    )
    assert (
        sum(item["destination_evidence"] == "content_reconciled" for item in state["items"]) == 18
    )
