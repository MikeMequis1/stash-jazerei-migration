"""Coleta paginada de jogos do perfil Stash."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.cancellation import OperationCancelled
from src.config import Config
from src.models.source import ExportReport, GameRecord, PageFailure
from src.stash import parser
from src.stash.client import StashClient
from src.stash.pagination import parse_last_page, parse_page_links
from src.storage import save_html as persist_html
from src.storage import utc_now
from src.transport import HttpError

TRACKED_FIELDS = (
    "title",
    "cover_url",
    "year",
    "status",
    "date_displayed",
    "rating",
    "platform",
    "category",
)


def collect_games(
    client: StashClient,
    config: Config,
    *,
    max_pages: int | None = None,
    pages: list[int] | None = None,
    slug: str | None = None,
    save_html: bool = False,
    html_dir: Path | None = None,
    overwrite_html: bool = False,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[GameRecord], ExportReport]:
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages deve ser >= 1 ou None para todas as paginas")
    requested_pages = _normalize_pages(pages)

    report = ExportReport(
        kind="games",
        started_at=utc_now(),
        base_url=f"{config.stash_base_url}/users/{config.stash_username}/games/",
        directed=requested_pages is not None,
        requested_pages=requested_pages or [],
        target_slug=slug,
    )
    records: list[GameRecord] = []

    if requested_pages is not None:
        _collect_requested_pages(
            client,
            config,
            report,
            records,
            requested_pages,
            slug,
            save_html,
            html_dir,
            overwrite_html,
            on_page,
            should_cancel,
        )
    else:
        _collect_paginated(
            client,
            config,
            report,
            records,
            max_pages,
            slug,
            save_html,
            html_dir,
            overwrite_html,
            on_page,
            should_cancel,
        )

    report.finished_at = utc_now()
    report.records = len(records)
    report.unique_records = len({record.slug for record in records})
    report.target_found = (
        any(record.slug == slug for record in records) if slug is not None else None
    )
    report.complete = (
        not report.directed
        and not report.failures
        and report.last_page_detected > 0
        and report.pages_fetched >= report.last_page_detected
    )
    report.requested_scope_complete = (
        not report.failures
        and report.pages_requested > 0
        and report.pages_fetched >= report.pages_requested
    )

    counts: Counter[str] = Counter()
    for record in records:
        counts.update(parser.missing_fields(record, TRACKED_FIELDS))
    report.missing_fields = dict(counts)

    slug_counts = Counter(record.slug for record in records)
    report.duplicates = sorted(slug for slug, count in slug_counts.items() if count > 1)
    return records, report


def _normalize_pages(pages: list[int] | None) -> list[int] | None:
    if pages is None:
        return None
    if not pages:
        raise ValueError("pages nao pode ser vazio")
    if any(page < 1 for page in pages):
        raise ValueError("pages deve conter apenas inteiros >= 1")
    return sorted(set(pages))


def _collect_requested_pages(
    client: StashClient,
    config: Config,
    report: ExportReport,
    records: list[GameRecord],
    pages: list[int],
    slug: str | None,
    save_html: bool,
    html_dir: Path | None,
    overwrite_html: bool,
    on_page: Callable[[dict[str, Any]], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> None:
    report.pages_requested = len(pages)
    for page in pages:
        _check_cancel(should_cancel)
        url = client.profile_url("games", page)
        try:
            html = client.fetch_page("games", page)
        except HttpError as exc:
            report.failures.append(_failure(page, url, exc))
            continue
        except Exception as exc:  # noqa: BLE001 - browser/transporte viram falha de coleta
            report.failures.append(_failure_text(page, url, f"{type(exc).__name__}: {exc}"))
            continue

        if report.last_page_detected == 0:
            report.last_page_detected = parse_last_page(html)
        if report.last_page_detected and page > report.last_page_detected:
            report.failures.append(
                _failure_text(
                    page,
                    url,
                    f"pagina fora do intervalo detectado (1..{report.last_page_detected})",
                )
            )
            continue

        _save_page(config, page, html, save_html, html_dir, overwrite_html)
        report.pages_fetched += 1
        try:
            page_records = parser.parse_games_page(html, url)
        except Exception as exc:  # noqa: BLE001
            report.failures.append(_failure_text(page, url, f"parse: {type(exc).__name__}"))
            continue
        records.extend(_filter_records(page_records, slug))
        _report_page(report, page, records, on_page)


def _collect_paginated(
    client: StashClient,
    config: Config,
    report: ExportReport,
    records: list[GameRecord],
    max_pages: int | None,
    slug: str | None,
    save_html: bool,
    html_dir: Path | None,
    overwrite_html: bool,
    on_page: Callable[[dict[str, Any]], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> None:
    first_url = client.profile_url("games", 1)
    _check_cancel(should_cancel)
    try:
        first_html = client.fetch_page("games", 1)
    except HttpError as exc:
        report.failures.append(_failure(1, first_url, exc))
        return
    except Exception as exc:  # noqa: BLE001
        report.failures.append(_failure_text(1, first_url, f"{type(exc).__name__}: {exc}"))
        return

    report.last_page_detected = parse_last_page(first_html)
    page_links = parse_page_links(first_html, first_url)
    report.pages_requested = (
        min(max_pages, report.last_page_detected)
        if max_pages is not None
        else report.last_page_detected
    )

    for page in range(1, report.pages_requested + 1):
        _check_cancel(should_cancel)
        url = first_url if page == 1 else page_links.get(page) or client.profile_url("games", page)
        try:
            html = first_html if page == 1 else client.fetch_url(url)
        except HttpError as exc:
            report.failures.append(_failure(page, url, exc))
            break
        except Exception as exc:  # noqa: BLE001
            report.failures.append(_failure_text(page, url, f"{type(exc).__name__}: {exc}"))
            break

        _save_page(config, page, html, save_html, html_dir, overwrite_html)
        report.pages_fetched += 1
        try:
            page_records = parser.parse_games_page(html, url)
        except Exception as exc:  # noqa: BLE001
            report.failures.append(_failure_text(page, url, f"parse: {type(exc).__name__}"))
            continue
        records.extend(_filter_records(page_records, slug))
        _report_page(report, page, records, on_page)


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise OperationCancelled("coleta cancelada")


def _report_page(
    report: ExportReport,
    page: int,
    records: list[GameRecord],
    on_page: Callable[[dict[str, Any]], None] | None,
) -> None:
    if on_page is None:
        return
    on_page(
        {
            "kind": "games",
            "page": page,
            "pages": report.pages_requested,
            "records": len(records),
        }
    )


def _filter_records(records: list[GameRecord], slug: str | None) -> list[GameRecord]:
    if slug is None:
        return records
    return [record for record in records if record.slug == slug]


def _save_page(
    config: Config,
    page: int,
    html: str,
    save_html: bool,
    html_dir: Path | None,
    overwrite_html: bool,
) -> None:
    if save_html:
        persist_html(
            (html_dir or config.stash_dir / "html") / f"games-{page}.html",
            html,
            overwrite=overwrite_html,
        )


def _failure(page: int, url: str, exc: HttpError) -> PageFailure:
    detail = f"HTTP {exc.status_code}" if exc.status_code else str(exc)
    return PageFailure(page=page, url=url, error=detail)


def _failure_text(page: int, url: str, error: str) -> PageFailure:
    return PageFailure(page=page, url=url, error=error)
