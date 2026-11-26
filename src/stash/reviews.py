"""Coleta paginada de reviews do perfil Stash e comparacao com o detalhe."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.cancellation import OperationCancelled
from src.config import Config
from src.models.source import ExportReport, PageFailure, ReviewRecord
from src.stash import parser
from src.stash.client import StashClient
from src.stash.pagination import parse_last_page, parse_page_links
from src.storage import save_html as persist_html
from src.storage import utc_now
from src.transport import HttpError

TRACKED_FIELDS = (
    "game_title",
    "text",
    "rating",
    "date",
    "status",
    "progress",
    "likes_count",
    "comments_count",
)
WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value).strip()


def collect_reviews(
    client: StashClient,
    config: Config,
    *,
    max_pages: int | None = None,
    pages: list[int] | None = None,
    slug: str | None = None,
    fetch_details: bool = True,
    save_html: bool = False,
    html_dir: Path | None = None,
    overwrite_html: bool = False,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[ReviewRecord], ExportReport]:
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages deve ser >= 1 ou None para todas as paginas")
    requested_pages = _normalize_pages(pages)

    report = ExportReport(
        kind="reviews",
        started_at=utc_now(),
        base_url=f"{config.stash_base_url}/users/{config.stash_username}/reviews/",
        directed=requested_pages is not None,
        requested_pages=requested_pages or [],
        target_slug=slug,
    )
    records: list[ReviewRecord] = []

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

    if fetch_details:
        _merge_details(
            client,
            config,
            records,
            report,
            save_html=save_html,
            html_dir=html_dir,
            overwrite_html=overwrite_html,
            on_page=on_page,
            should_cancel=should_cancel,
        )

    report.finished_at = utc_now()
    report.records = len(records)
    report.unique_records = len({(record.game_slug, record.date) for record in records})
    report.target_found = (
        any(record.game_slug == slug for record in records) if slug is not None else None
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

    key_counts = Counter((record.game_slug, record.date) for record in records)
    report.duplicates = sorted(
        f"{slug}@{date}" for (slug, date), count in key_counts.items() if count > 1
    )
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
    records: list[ReviewRecord],
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
        url = client.profile_url("reviews", page)
        try:
            html = client.fetch_page("reviews", page)
        except HttpError as exc:
            report.failures.append(_failure(page, url, exc))
            continue
        except Exception as exc:  # noqa: BLE001
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
            page_records = parser.parse_reviews_page(html, url)
        except Exception as exc:  # noqa: BLE001
            report.failures.append(_failure_text(page, url, f"parse: {type(exc).__name__}"))
            continue
        records.extend(_filter_records(page_records, slug))
        _report_page(report, page, records, on_page)


def _collect_paginated(
    client: StashClient,
    config: Config,
    report: ExportReport,
    records: list[ReviewRecord],
    max_pages: int | None,
    slug: str | None,
    save_html: bool,
    html_dir: Path | None,
    overwrite_html: bool,
    on_page: Callable[[dict[str, Any]], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> None:
    first_url = client.profile_url("reviews", 1)
    _check_cancel(should_cancel)
    try:
        first_html = client.fetch_page("reviews", 1)
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
        url = (
            first_url if page == 1 else page_links.get(page) or client.profile_url("reviews", page)
        )
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
            page_records = parser.parse_reviews_page(html, url)
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
    records: list[ReviewRecord],
    on_page: Callable[[dict[str, Any]], None] | None,
) -> None:
    if on_page is None:
        return
    on_page(
        {
            "kind": "reviews",
            "page": page,
            "pages": report.pages_requested,
            "records": len(records),
        }
    )


def _filter_records(records: list[ReviewRecord], slug: str | None) -> list[ReviewRecord]:
    if slug is None:
        return records
    return [record for record in records if record.game_slug == slug]


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
            (html_dir or config.stash_dir / "html") / f"reviews-{page}.html",
            html,
            overwrite=overwrite_html,
        )


def _merge_details(
    client: StashClient,
    config: Config,
    records: list[ReviewRecord],
    report: ExportReport,
    *,
    save_html: bool,
    html_dir: Path | None = None,
    overwrite_html: bool = False,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    for index, record in enumerate(records):
        _check_cancel(should_cancel)
        if not record.source_url:
            continue
        try:
            html = client.fetch_url(record.source_url)
        except HttpError as exc:
            report.failures.append(_failure(0, record.source_url, exc, prefix="detalhe: "))
            continue
        except Exception as exc:  # noqa: BLE001
            report.failures.append(
                _failure_text(0, record.source_url, f"detalhe: {type(exc).__name__}: {exc}")
            )
            continue

        if save_html:
            persist_html(
                (html_dir or config.stash_dir / "html") / f"review-detail-{index + 1}.html",
                html,
                overwrite=overwrite_html,
            )

        report.details_checked += 1
        if on_page is not None:
            on_page(
                {
                    "kind": "reviews",
                    "phase": "detail",
                    "current": index + 1,
                    "total": len(records),
                    "records": len(records),
                }
            )
        detail_text = parser.parse_review_detail(html)
        if not detail_text:
            continue
        record.raw["text_listing"] = record.text
        record.raw["text_detail"] = detail_text
        if record.text and _normalize_text(record.text) not in _normalize_text(detail_text):
            report.text_mismatches.append(f"{record.game_slug}@{record.date or 'sem-data'}")
        record.text = detail_text
        record.text_source = "detail"


def _failure(page: int, url: str, exc: HttpError, *, prefix: str = "") -> PageFailure:
    detail = f"HTTP {exc.status_code}" if exc.status_code else str(exc)
    return PageFailure(page=page, url=url, error=f"{prefix}{detail}")


def _failure_text(page: int, url: str, error: str) -> PageFailure:
    return PageFailure(page=page, url=url, error=error)
