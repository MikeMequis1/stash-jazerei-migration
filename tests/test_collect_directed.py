from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from src.config import Config
from src.export_run import RunOutcome, classify_kind, should_write_records
from src.models.source import ExportReport
from src.stash.games import collect_games
from src.stash.reviews import collect_reviews
from src.storage import read_json, verify_manifest, write_json
from src.transport import HttpError

FIXTURES = Path(__file__).parent / "fixtures"


def games_html(slugs: list[str], last_page: int = 1) -> str:
    cards = "".join(
        f'<a class="games-list__item-link" href="/games/{slug}">'
        f'<span class="games-list__item-name">{slug}</span>'
        f'<span class="games-list__item-date">1 Jan 2020</span>'
        f'<span class="game__rating">8</span>'
        f'<svg><use href="#beaten"></use></svg></a>'
        for slug in slugs
    )
    nav = "".join(
        f'<a href="/users/user/games/?page={page}">{page}</a>' for page in range(1, last_page + 1)
    )
    return f"<main><section>{cards}</section><nav class='profile-pagination'>{nav}</nav></main>"


class FakeSource:
    def __init__(
        self,
        *,
        games: dict[int, str] | None = None,
        reviews: dict[int, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.games = games or {}
        self.reviews = reviews or {}
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return

    def profile_url(self, kind: str, page: int = 1) -> str:
        return f"https://stash.games/users/user/{kind}/?page={page}"

    def fetch_page(self, kind: str, page: int = 1) -> str:
        if self.error is not None:
            raise self.error
        source = self.games if kind == "games" else self.reviews
        return source[page]

    def fetch_url(self, url: str) -> str:
        if self.error is not None:
            raise self.error
        return '<p class="game__review-comment">Texto integral do detalhe.</p>'


def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path)


def test_directed_games_finds_slug_and_records_pages(tmp_path: Path) -> None:
    source = FakeSource(games={2: games_html(["outro", "demolition-girl"], last_page=3)})
    records, report = collect_games(source, config(tmp_path), pages=[2], slug="demolition-girl")
    assert [record.slug for record in records] == ["demolition-girl"]
    assert report.directed is True
    assert report.requested_pages == [2]
    assert report.pages_requested == 1
    assert report.pages_fetched == 1
    assert report.target_found is True
    assert report.requested_scope_complete is True
    assert report.complete is False
    assert classify_kind(report) is RunOutcome.DIRECTED
    assert should_write_records(report) is True


def test_directed_slug_not_found(tmp_path: Path) -> None:
    source = FakeSource(games={2: games_html(["outro"], last_page=3)})
    records, report = collect_games(source, config(tmp_path), pages=[2], slug="demolition-girl")
    assert records == []
    assert report.target_found is False
    assert classify_kind(report) is RunOutcome.TARGET_NOT_FOUND
    assert should_write_records(report) is False


def test_page_out_of_range_is_failure(tmp_path: Path) -> None:
    source = FakeSource(games={99: games_html([], last_page=3)})
    _, report = collect_games(source, config(tmp_path), pages=[99], slug="demolition-girl")
    assert report.pages_fetched == 0
    assert report.failures
    assert "fora do intervalo" in report.failures[0].error
    assert classify_kind(report) is RunOutcome.FAILED


def test_http_403_is_failure(tmp_path: Path) -> None:
    source = FakeSource(error=HttpError("403", status_code=403))
    _, report = collect_games(source, config(tmp_path), pages=[1], slug="demolition-girl")
    assert report.pages_fetched == 0
    assert classify_kind(report) is RunOutcome.FAILED
    assert should_write_records(report) is False


def test_parse_failure_is_partial(tmp_path: Path, monkeypatch) -> None:
    source = FakeSource(games={1: games_html(["demolition-girl"])})
    monkeypatch.setattr(
        "src.stash.games.parser.parse_games_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("html quebrado")),
    )
    _, report = collect_games(source, config(tmp_path), pages=[1], slug="demolition-girl")
    assert report.pages_fetched == 1
    assert report.failures
    assert classify_kind(report) is RunOutcome.PARTIAL


def test_empty_success_is_distinct_from_failure(tmp_path: Path) -> None:
    source = FakeSource(games={1: games_html([], last_page=1)})
    records, report = collect_games(source, config(tmp_path), max_pages=None)
    assert records == []
    assert report.complete is True
    assert report.pages_fetched == 1
    assert classify_kind(report) is RunOutcome.EMPTY
    assert should_write_records(report) is True


def test_full_limited_collection_is_partial(tmp_path: Path) -> None:
    source = FakeSource(games={1: games_html(["demolition-girl"], last_page=3)})
    _, report = collect_games(source, config(tmp_path), max_pages=1)
    assert report.pages_requested == 1
    assert report.pages_fetched == 1
    assert report.complete is False
    assert classify_kind(report) is RunOutcome.PARTIAL


def test_limited_search_for_missing_slug_is_partial_not_absent(tmp_path: Path) -> None:
    source = FakeSource(games={1: games_html(["outro"], last_page=3)})
    _, report = collect_games(source, config(tmp_path), max_pages=1, slug="demolition-girl")
    assert report.target_found is False
    assert report.complete is False
    assert classify_kind(report) is RunOutcome.PARTIAL
    assert should_write_records(report) is False


def test_collect_games_reports_pages_and_cancels_between_pages(tmp_path: Path) -> None:
    from src.cancellation import OperationCancelled

    source = FakeSource(
        games={
            1: games_html(["one", "two"], last_page=2),
            2: games_html(["three"], last_page=2),
        }
    )
    pages: list[dict] = []

    def on_page(payload: dict) -> None:
        pages.append(payload)

    with pytest.raises(OperationCancelled):
        collect_games(
            source,
            config(tmp_path),
            pages=[1, 2],
            on_page=on_page,
            should_cancel=lambda: len(pages) >= 1,
        )
    assert pages[0]["kind"] == "games"
    assert pages[0]["page"] == 1
    assert pages[0]["records"] == 2


def test_collect_reviews_cancels_during_details(tmp_path: Path) -> None:
    from src.cancellation import OperationCancelled

    html = (FIXTURES / "reviews_page.html").read_text(encoding="utf-8")
    source = FakeSource(reviews={1: html})
    progress: list[dict] = []
    with pytest.raises(OperationCancelled):
        collect_reviews(
            source,
            config(tmp_path),
            pages=[1],
            fetch_details=True,
            on_page=progress.append,
            should_cancel=lambda: any(item.get("phase") == "detail" for item in progress),
        )
    assert progress[0]["kind"] == "reviews"
    assert progress[0]["page"] == 1


def test_cmd_export_cancel_does_not_publish_partial_run(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    import src.cli as cli
    from src.cancellation import OperationCancelled

    class Log:
        entries: list = []

        def summary(self):
            return {"observed": 0, "blocked": 0, "blocked_entries": []}

    class Session:
        log = Log()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    source = FakeSource(
        games={
            1: games_html(["one"], last_page=2),
            2: games_html(["two"], last_page=2),
        },
        reviews={1: games_html(["unused"], last_page=1)},
    )
    monkeypatch.setattr(cli, "BrowserSession", lambda *args, **kwargs: Session())
    monkeypatch.setattr(cli, "BrowserStashClient", lambda *args, **kwargs: source)
    args = SimpleNamespace(
        kind="games",
        method="browser",
        browser_headless=False,
        all_pages=False,
        max_pages=2,
        slug=None,
        game_page=None,
        review_page=None,
        run_id="cancel-run",
        overwrite=False,
        save_html=False,
        no_details=True,
    )
    cancelled = {"value": False}

    def on_page(payload):
        if payload.get("kind") == "games" and payload.get("page") == 1:
            cancelled["value"] = True

    with pytest.raises(OperationCancelled):
        cli.cmd_export(
            config(tmp_path),
            args,
            on_page=on_page,
            should_cancel=lambda: cancelled["value"],
        )
    runs = tmp_path / "stash" / "runs"
    assert not (runs / "cancel-run").exists()
    assert not list(runs.glob(".cancel-run*"))


def test_directed_reviews_filters_and_fetches_detail(tmp_path: Path) -> None:
    html = (FIXTURES / "reviews_page.html").read_text(encoding="utf-8")
    source = FakeSource(reviews={2: html})
    records, report = collect_reviews(
        source,
        config(tmp_path),
        pages=[2],
        slug="sky-children-of-the-light",
        fetch_details=True,
    )
    assert len(records) == 1
    assert records[0].game_slug == "sky-children-of-the-light"
    assert records[0].text_source == "detail"
    assert report.details_checked == 1
    assert report.target_found is True
    assert classify_kind(report) is RunOutcome.DIRECTED


def test_invalid_pages_rejected(tmp_path: Path) -> None:
    source = FakeSource(games={1: games_html([])})
    with pytest.raises(ValueError):
        collect_games(source, config(tmp_path), pages=[0])
    with pytest.raises(ValueError):
        collect_games(source, config(tmp_path), pages=[])


def test_storage_does_not_overwrite_by_default(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    write_json(path, {"value": 1})
    with pytest.raises(FileExistsError):
        write_json(path, {"value": 2})
    assert read_json(path) == {"value": 1}
    write_json(path, {"value": 2}, overwrite=True)
    assert read_json(path) == {"value": 2}


def test_manifest_detects_tamper(tmp_path: Path) -> None:
    from src.storage import write_manifest

    path = tmp_path / "games.json"
    write_json(path, [{"slug": "a"}])
    write_manifest(tmp_path, {"schema_version": 1, "run_id": "r1"})
    assert verify_manifest(tmp_path) == []

    path.write_text('[{"slug":"b"}]', encoding="utf-8")
    errors = verify_manifest(tmp_path)
    assert any("hash divergente" in error for error in errors)


def test_cli_export_writes_versioned_run_and_manifest(tmp_path: Path, monkeypatch) -> None:
    from src import cli

    source = FakeSource(games={2: games_html(["demolition-girl"], last_page=3)})
    monkeypatch.setattr(cli, "StashClient", lambda config: source)
    args = SimpleNamespace(
        kind="games",
        max_pages=1,
        all_pages=False,
        save_html=False,
        no_details=True,
        method="http",
        browser_headless=False,
        slug="demolition-girl",
        game_page=[2],
        review_page=None,
        run_id="pilot-run",
        overwrite=False,
    )
    code = cli.cmd_export(config(tmp_path), args)
    assert code == 0
    run_dir = tmp_path / "stash" / "runs" / "pilot-run"
    assert (run_dir / "games.json").exists()
    assert (run_dir / "export-report.json").exists()
    assert (run_dir / "manifest.json").exists()
    report = read_json(run_dir / "export-report.json")
    assert report["outcome"] == "directed"
    assert report["exit_code"] == 0
    assert verify_manifest(run_dir) == []


def test_cli_export_failure_does_not_write_data_file(tmp_path: Path, monkeypatch) -> None:
    from src import cli

    source = FakeSource(error=HttpError("403", status_code=403))
    monkeypatch.setattr(cli, "StashClient", lambda config: source)
    args = SimpleNamespace(
        kind="games",
        max_pages=1,
        all_pages=False,
        save_html=False,
        no_details=True,
        method="http",
        browser_headless=False,
        slug="demolition-girl",
        game_page=[1],
        review_page=None,
        run_id="failed-run",
        overwrite=False,
    )
    code = cli.cmd_export(config(tmp_path), args)
    assert code == 2
    run_dir = tmp_path / "stash" / "runs" / "failed-run"
    assert not (run_dir / "games.json").exists()
    assert (run_dir / "export-report.json").exists()
    report = read_json(run_dir / "export-report.json")
    assert report["outcome"] == "failed"
    assert report["failures"]
    assert verify_manifest(run_dir) == []


def test_cli_export_refuses_existing_run_without_overwrite(tmp_path: Path, monkeypatch) -> None:
    from src import cli

    source = FakeSource(games={1: games_html([])})
    monkeypatch.setattr(cli, "StashClient", lambda config: source)
    args = SimpleNamespace(
        kind="games",
        max_pages=1,
        all_pages=None,
        save_html=False,
        no_details=True,
        method="http",
        browser_headless=False,
        slug=None,
        game_page=[1],
        review_page=None,
        run_id="same-run",
        overwrite=False,
    )
    assert cli.cmd_export(config(tmp_path), args) == 0
    with pytest.raises(FileExistsError):
        cli.cmd_export(config(tmp_path), args)


def test_failed_overwrite_preserves_previous_run(tmp_path: Path, monkeypatch) -> None:
    from src import cli

    monkeypatch.setattr(
        cli,
        "StashClient",
        lambda config: FakeSource(games={1: games_html(["demolition-girl"], last_page=1)}),
    )
    base_args = dict(
        kind="games",
        max_pages=1,
        all_pages=None,
        save_html=False,
        no_details=True,
        method="http",
        browser_headless=False,
        slug="demolition-girl",
        game_page=[1],
        review_page=None,
        run_id="stable-run",
    )
    assert cli.cmd_export(config(tmp_path), SimpleNamespace(**base_args, overwrite=False)) == 0
    run_dir = tmp_path / "stash" / "runs" / "stable-run"
    original = (run_dir / "games.json").read_text(encoding="utf-8")

    monkeypatch.setattr(
        cli,
        "StashClient",
        lambda config: FakeSource(error=HttpError("403", status_code=403)),
    )
    failing = SimpleNamespace(**base_args, overwrite=True)
    assert cli.cmd_export(config(tmp_path), failing) == 2
    assert (run_dir / "games.json").read_text(encoding="utf-8") == original
    failed_runs = list((tmp_path / "stash" / "runs").glob("stable-run-failed-*"))
    assert len(failed_runs) == 1
    assert (failed_runs[0] / "export-report.json").exists()
    assert not (failed_runs[0] / "games.json").exists()


def test_partial_collection_exit_code_and_data(tmp_path: Path, monkeypatch) -> None:
    from src import cli

    source = FakeSource(games={1: games_html(["demolition-girl"], last_page=3)})
    monkeypatch.setattr(cli, "StashClient", lambda config: source)
    args = SimpleNamespace(
        kind="games",
        max_pages=1,
        all_pages=False,
        save_html=False,
        no_details=True,
        method="http",
        browser_headless=False,
        slug=None,
        game_page=None,
        review_page=None,
        run_id="partial-run",
        overwrite=False,
    )
    assert cli.cmd_export(config(tmp_path), args) == 1
    run_dir = tmp_path / "stash" / "runs" / "partial-run"
    assert (run_dir / "games.json").exists()
    assert read_json(run_dir / "export-report.json")["outcome"] == "partial"


def test_empty_success_writes_empty_list(tmp_path: Path, monkeypatch) -> None:
    from src import cli

    source = FakeSource(games={1: games_html([], last_page=1)})
    monkeypatch.setattr(cli, "StashClient", lambda config: source)
    args = SimpleNamespace(
        kind="games",
        max_pages=None,
        all_pages=True,
        save_html=False,
        no_details=True,
        method="http",
        browser_headless=False,
        slug=None,
        game_page=None,
        review_page=None,
        run_id="empty-run",
        overwrite=False,
    )
    assert cli.cmd_export(config(tmp_path), args) == 0
    run_dir = tmp_path / "stash" / "runs" / "empty-run"
    assert read_json(run_dir / "games.json") == []
    report = read_json(run_dir / "export-report.json")
    assert report["outcome"] == "empty"
    assert report["data_files"]["games"] == "games.json"


def test_export_report_fields_are_typed() -> None:
    report = ExportReport(kind="games", started_at="now", base_url="https://example")
    assert report.directed is False
    assert report.requested_pages == []
    assert report.target_found is None
