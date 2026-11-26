"""CLI do projeto. Leituras seguras por padrao; escritas nunca automaticas."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.browser.jazerei import (
    BrowserApiError,
    capture_reviews,
    fetch_game_detail,
    login_evidence,
    wait_for_login,
)
from src.browser.jazerei import (
    search_games as browser_search_games,
)
from src.browser.session import (
    BrowserSession,
    BrowserUnavailable,
    mark_closed,
    profile_status,
)
from src.browser.stash import BrowserStashClient
from src.cancellation import OperationCancelled
from src.config import Config, load_config
from src.export_run import should_write_records, summarize
from src.jazerei.auth import Auth, MissingAuthError
from src.jazerei.catalog import search_games
from src.jazerei.client import JazereiClient
from src.jazerei.identity import (
    SessionIdentity,
    SessionIdentityError,
    discover_session_user_id,
    save_session_identity,
)
from src.jazerei.snapshots import (
    allocate_run_id,
    safe_error_message,
    write_account_snapshot_run,
    write_failure_run,
    write_snapshot_run,
)
from src.jazerei.state import (
    CaptureError,
    IdentityError,
    authenticate,
    capture_account_reviews,
    capture_review_state,
)
from src.migration.approval import (
    ApprovalError,
    action_hash,
    approval_status,
    build_bundle,
    decisions_provenance,
    destination_provenance,
    legacy_destination_provenance,
    load_approvals,
    load_bundle,
    matching_provenance,
    source_provenance,
    verify_bundle,
)
from src.migration.approval import (
    approve_plan as approve_manifest,
)
from src.migration.evaluation_matching import match_evaluations
from src.migration.evaluation_store import (
    create_run as create_evaluation_run,
)
from src.migration.evaluation_store import (
    latest_evaluation_run,
    load_evaluation_run,
    populate_run,
)
from src.migration.evaluations import build_evaluation_set
from src.migration.execution import (
    ExecutionError,
    build_browser_write_transport,
    confirm_action,
    execute_action,
    prepare_execution,
    resolve_ambiguous_action,
    validate_execution,
)
from src.migration.executions import (
    ExecutionStorageError,
    allocate_execution_id,
    create_execution,
    execution_manifest_hash,
    load_confirmations,
    load_execution,
    reconcile_execution,
    verify_execution,
)
from src.migration.importer import preflight
from src.migration.liked import (
    DecisionError,
    append_decision,
    load_decisions,
    resolve_liked,
    review_version_hash,
)
from src.migration.matcher import match_all
from src.migration.planner import build_plan
from src.migration.preflight import (
    build_result,
    capture_snapshots_for_actions,
    compare_actions,
    prepare_preflight,
    summarize_checks,
)
from src.migration.preflights import (
    allocate_preflight_id,
    load_preflight,
    verify_preflight,
    write_preflight_run,
)
from src.migration.ratification import load_ratifications, validation_summary
from src.migration.validator import Severity, validate_plan
from src.migration.verifier import verify_plan
from src.models.approval import ApprovalStatus
from src.models.destination import DestinationReview
from src.models.normalized import GameMatch, MigrationPlan
from src.models.preflight import (
    ActionCheck,
    ActionStatus,
    PreflightStatus,
    PreflightSummary,
)
from src.models.snapshot import AccountReviewsSnapshot
from src.models.source import GameRecord, ReviewRecord
from src.stash.client import StashClient
from src.stash.games import collect_games
from src.stash.reviews import collect_reviews
from src.storage import read_json, sha256_file, utc_now, verify_manifest, write_json, write_manifest


def _load_models(path: Path, model: type) -> list[Any]:
    payload = read_json(path)
    if isinstance(payload, dict):
        payload = payload.get("records", payload.get("items", []))
    return [model.model_validate(item) for item in payload]


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_status(config: Config) -> int:
    auth = Auth.from_env()
    _print_json(
        {
            "stash_username": config.stash_username,
            "stash_base_url": config.stash_base_url,
            "jazerei_base_url": config.jazerei_base_url,
            "allow_writes": config.allow_writes,
            "enable_real_write_transport": config.enable_real_write_transport,
            "jazerei_page_size": config.jazerei_page_size,
            "expected_user_id": config.jazerei_user_id or None,
            "expected_user_id_source": config.jazerei_user_id_source or None,
            "auth": {
                "has_api_key": bool(auth.api_key),
                "has_user_id": bool(auth.user_id),
                "has_expected_user_id": bool(config.jazerei_user_id),
                "has_id_token": bool(auth.id_token),
                "has_refresh_token": bool(auth.refresh_token),
            },
            "data_dir": str(config.data_dir),
            "browser": profile_status(config).as_dict(),
        }
    )
    return 0


def cmd_export(
    config: Config,
    args: argparse.Namespace,
    *,
    on_page: Any = None,
    should_cancel: Any = None,
) -> int:
    if not args.all_pages and args.max_pages < 1:
        raise ValueError("--max-pages deve ser >= 1; use --all-pages para tudo")
    kinds = ["games", "reviews"] if args.kind == "both" else [args.kind]
    game_pages = _normalize_page_args(args.game_page, "--game-page")
    review_pages = _normalize_page_args(args.review_page, "--review-page")
    if game_pages and "games" not in kinds:
        raise ValueError("--game-page exige --kind games ou both")
    if review_pages and "reviews" not in kinds:
        raise ValueError("--review-page exige --kind reviews ou both")

    run_id = _resolve_run_id(config, args)
    target_dir = config.stash_dir / "runs" / run_id
    if target_dir.exists() and not args.overwrite:
        raise FileExistsError(
            f"run ja existe: {target_dir}; use --run-id <id> --overwrite para substituir"
        )
    work_dir = config.stash_dir / "runs" / f".{run_id}.staging"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    if target_dir.exists() and args.overwrite:
        print(f"atencao: run existente sera substituido somente em caso de sucesso: {target_dir}")

    max_pages = None if args.all_pages else args.max_pages
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": utc_now(),
        "stash_user": config.stash_username,
        "method": args.method,
        "slug": args.slug,
        "options": {
            "max_pages": max_pages,
            "game_pages": game_pages,
            "review_pages": review_pages,
            "save_html": args.save_html,
            "overwrite": args.overwrite,
        },
        "requested": {},
        "complete": False,
    }
    kind_reports: list[Any] = []
    data_files: dict[str, str | None] = {}

    with ExitStack() as stack:
        if args.method == "browser":
            session = stack.enter_context(BrowserSession(config, headless=args.browser_headless))
            source: Any = BrowserStashClient(session, config)
        else:
            source = stack.enter_context(StashClient(config))

        if "games" in kinds:
            try:
                records, games_report = collect_games(
                    source,
                    config,
                    max_pages=max_pages,
                    pages=game_pages or None,
                    slug=args.slug,
                    save_html=args.save_html,
                    html_dir=work_dir / "html",
                    overwrite_html=args.overwrite,
                    on_page=on_page,
                    should_cancel=should_cancel,
                )
            except OperationCancelled:
                shutil.rmtree(work_dir, ignore_errors=True)
                print("coleta cancelada; nenhum run foi publicado", file=sys.stderr)
                raise
            kind_reports.append(games_report)
            data_files["games"] = _write_kind_data(
                work_dir, "games", records, games_report, overwrite=args.overwrite
            )
            report["requested"]["games"] = games_report.model_dump(mode="json")
            print(
                f"jogos: {len(records)} registros "
                f"({games_report.pages_fetched}/{games_report.pages_requested} paginas, "
                f"{'dirigida' if games_report.directed else 'paginada'})"
            )
        if "reviews" in kinds:
            try:
                records, reviews_report = collect_reviews(
                    source,
                    config,
                    max_pages=max_pages,
                    pages=review_pages or None,
                    slug=args.slug,
                    fetch_details=not args.no_details,
                    save_html=args.save_html,
                    html_dir=work_dir / "html",
                    overwrite_html=args.overwrite,
                    on_page=on_page,
                    should_cancel=should_cancel,
                )
            except OperationCancelled:
                shutil.rmtree(work_dir, ignore_errors=True)
                print("coleta cancelada; nenhum run foi publicado", file=sys.stderr)
                raise
            kind_reports.append(reviews_report)
            data_files["reviews"] = _write_kind_data(
                work_dir, "reviews", records, reviews_report, overwrite=args.overwrite
            )
            report["requested"]["reviews"] = reviews_report.model_dump(mode="json")
            print(
                f"reviews: {len(records)} registros "
                f"({reviews_report.pages_fetched}/{reviews_report.pages_requested} paginas, "
                f"{reviews_report.details_checked} detalhes)"
            )

        if args.method == "browser" and session.log.entries:
            summary = session.log.summary()
            report["browser_requests"] = summary
            print(
                f"navegador: {summary['observed']} requisicoes observadas, "
                f"{summary['blocked']} bloqueadas"
            )

    outcome, exit_code = summarize(kind_reports)
    report["outcome"] = outcome.value
    report["exit_code"] = exit_code
    report["complete"] = outcome.value == "complete"
    report["data_files"] = data_files
    report["failures"] = [
        {"kind": item.kind, "failures": [failure.model_dump() for failure in item.failures]}
        for item in kind_reports
        if item.failures
    ]
    write_json(work_dir / "export-report.json", report)
    write_manifest(
        work_dir,
        {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": report["generated_at"],
            "method": args.method,
            "stash_user": config.stash_username,
            "slug": args.slug,
            "outcome": outcome.value,
            "exit_code": exit_code,
            "requested": report["requested"],
        },
    )

    successful = outcome.value in {"complete", "directed", "empty"}
    if target_dir.exists() and args.overwrite and not successful:
        failed_dir = _unique_sibling(target_dir, "failed")
        work_dir.rename(failed_dir)
        print(f"resultado: {outcome.value} (exit {exit_code})")
        print(f"run anterior preservado: {target_dir}")
        print(f"evidencia da falha: {failed_dir}")
        return exit_code

    if target_dir.exists():
        _reset_run_dir(target_dir)
        for child in work_dir.iterdir():
            shutil.move(str(child), target_dir / child.name)
        work_dir.rmdir()
    else:
        work_dir.rename(target_dir)

    print(f"resultado: {outcome.value} (exit {exit_code})")
    print(f"relatorio: {target_dir / 'export-report.json'}")
    print(f"manifesto: {target_dir / 'manifest.json'}")
    return exit_code


def _unique_sibling(path: Path, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}-{label}-{stamp}")
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}-{label}-{stamp}-{index}")
        index += 1
    return candidate


def _normalize_page_args(values: list[int] | None, label: str) -> list[int]:
    if not values:
        return []
    if any(value < 1 for value in values):
        raise ValueError(f"{label} deve conter paginas >= 1")
    return sorted(set(values))


def _resolve_run_id(config: Config, args: argparse.Namespace) -> str:
    if args.overwrite and not args.run_id:
        raise ValueError("--overwrite exige --run-id explicito")
    if args.slug and (
        args.slug in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.slug)
    ):
        raise ValueError("--slug invalido")
    if args.run_id:
        if args.run_id in {".", ".."} or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", args.run_id
        ):
            raise ValueError(
                "--run-id deve iniciar por letra/numero e usar apenas letras, numeros, ponto, underscore e hifen"
            )
        return args.run_id

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = args.slug or "stash"
    base = f"{stamp}-{suffix}-{args.method}"
    candidate = base
    index = 2
    while (config.stash_dir / "runs" / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _reset_run_dir(run_dir: Path) -> None:
    known = {"games.json", "reviews.json", "export-report.json", "manifest.json"}
    for child in run_dir.iterdir():
        if child.name in known or child.name == "html":
            continue
        raise ValueError(f"run contem arquivo desconhecido, overwrite recusado: {child.name}")
    for name in known:
        path = run_dir / name
        if path.exists():
            path.unlink()
    html_dir = run_dir / "html"
    if html_dir.exists():
        shutil.rmtree(html_dir)


def _write_kind_data(
    run_dir: Path,
    kind: str,
    records: list[Any],
    report: Any,
    *,
    overwrite: bool,
) -> str | None:
    if not should_write_records(report):
        return None
    path = write_json(run_dir / f"{kind}.json", records, overwrite=overwrite)
    return str(path.relative_to(run_dir))


def cmd_search(config: Config, args: argparse.Namespace) -> int:
    if args.method == "browser":
        with BrowserSession(config, headless=args.browser_headless) as session:
            outcome = browser_search_games(
                session, args.query, limit=args.limit, prefer_ui=args.prefer_ui
            )
            summary = session.log.summary()
        for note in outcome.notes:
            print(f"aviso: {note}", file=sys.stderr)
        print(f"metodo de busca: {outcome.method}", file=sys.stderr)
        print(
            f"navegador: {summary['observed']} requisicoes, {summary['blocked']} bloqueadas",
            file=sys.stderr,
        )
        _print_json([game.model_dump(mode="json") for game in outcome.games])
        return 0

    with JazereiClient(config) as client:
        games = search_games(client, args.query, limit=args.limit)
    _print_json([game.model_dump(mode="json") for game in games])
    return 0


def cmd_match(config: Config, args: argparse.Namespace) -> int:
    games = _load_models(Path(args.games), GameRecord)
    if args.method == "browser":
        with BrowserSession(config, headless=args.browser_headless) as session:
            matches = match_all(
                games,
                lambda title: browser_search_games(session, title, limit=args.limit).games,
                limit=args.limit,
            )
            summary = session.log.summary()
        print(
            f"navegador: {summary['observed']} requisicoes, {summary['blocked']} bloqueadas",
            file=sys.stderr,
        )
    else:
        with JazereiClient(config) as client:
            matches = match_all(
                games,
                lambda title: search_games(client, title, limit=args.limit),
                limit=args.limit,
            )
    path = write_json(config.migration_dir / "matches.json", matches, overwrite=True)
    candidates = sum(1 for match in matches if match.decision.value == "candidate")
    manual = sum(1 for match in matches if match.decision.value == "manual_review")
    print(f"correspondencias: {len(matches)} (candidatos={candidates}, revisao manual={manual})")
    print(f"arquivo: {path}")
    return 0


def _evaluation_inputs(
    source_run: Path,
) -> tuple[list[GameRecord], list[ReviewRecord], Any, str]:
    errors = verify_manifest(source_run)
    if errors:
        raise ValueError(f"manifesto invalido em {source_run}: {'; '.join(errors)}")
    games = _load_models(source_run / "games.json", GameRecord)
    reviews = _load_models(source_run / "reviews.json", ReviewRecord)
    evaluation_set = build_evaluation_set(games, reviews, source_run_id=source_run.name)
    return games, reviews, evaluation_set, sha256_file(source_run / "manifest.json")


def _refresh_slugs(evaluation_set: Any, historical_matches: list[GameMatch]) -> set[str]:
    by_slug = {match.source_slug: match for match in historical_matches}
    refresh: set[str] = set()
    for evaluation in evaluation_set.evaluations:
        if evaluation.evaluation_kind != "written_review":
            continue
        match = by_slug.get(evaluation.game_slug)
        if match is None or (match.dest_game_id is None and match.decision.value != "approved"):
            refresh.add(evaluation.game_slug)
    return refresh


def cmd_evaluations_build(config: Config, args: argparse.Namespace) -> int:
    games, _, evaluation_set, _ = _evaluation_inputs(Path(args.source_run))
    counts = evaluation_set.counts
    print(
        "avaliacoes: "
        f"total={counts.total} written={counts.written_review} "
        f"rating_only={counts.rating_only} no_rating={counts.no_rating} "
        f"inconsistencias={counts.inconsistencies}"
    )
    print(
        f"edicoes especiais={len(evaluation_set.special_editions)} "
        f"titulos duplicados={len(evaluation_set.duplicate_titles)} "
        f"divergencias de texto={len(evaluation_set.text_divergences)}"
    )
    if evaluation_set.inconsistencies:
        for issue in evaluation_set.inconsistencies:
            print(f"inconsistencia: {issue.kind} {issue.slug}: {issue.message}")
    if args.json:
        _print_json(evaluation_set)
    return 0


def cmd_evaluations_match(config: Config, args: argparse.Namespace) -> int:
    source_run = Path(args.source_run)
    games, _, evaluation_set, source_manifest_sha256 = _evaluation_inputs(source_run)
    historical = _load_models(Path(args.matches), GameMatch) if Path(args.matches).is_file() else []
    refresh = _refresh_slugs(evaluation_set, historical)
    targets = [
        evaluation
        for evaluation in evaluation_set.evaluations
        if evaluation.evaluation_kind == "rating_only" or evaluation.game_slug in refresh
    ]
    existing = [match for match in historical if match.source_slug in refresh]
    run = create_evaluation_run(config, args.run_id)
    print(
        f"matching: {len(targets)} avaliacoes "
        f"(rating_only={sum(1 for item in targets if item.evaluation_kind == 'rating_only')}, "
        f"written_refresh={sum(1 for item in targets if item.evaluation_kind == 'written_review')})",
        flush=True,
    )

    def progress(payload: dict[str, Any]) -> None:
        current = payload.get("current")
        total = payload.get("total")
        if isinstance(current, int) and isinstance(total, int) and current % 25 == 0:
            print(f"matching {current}/{total} {payload.get('slug')}", flush=True)

    if args.method == "browser":
        with BrowserSession(config, headless=args.browser_headless) as session:
            result = match_evaluations(
                targets,
                games,
                lambda title: browser_search_games(session, title, limit=args.limit).games,
                existing_matches=existing,
                refresh_slugs=refresh,
                cache_path=run.cache_path,
                limit=args.limit,
                max_attempts=args.max_attempts,
                retry_delay=args.retry_delay,
                on_progress=progress,
            )
            summary = session.log.summary()
        print(
            f"navegador: {summary['observed']} requisicoes, {summary['blocked']} bloqueadas",
            file=sys.stderr,
        )
    else:
        with JazereiClient(config) as client:
            result = match_evaluations(
                targets,
                games,
                lambda title: search_games(client, title, limit=args.limit),
                existing_matches=existing,
                refresh_slugs=refresh,
                cache_path=run.cache_path,
                limit=args.limit,
                max_attempts=args.max_attempts,
                retry_delay=args.retry_delay,
                on_progress=progress,
            )
    populate_run(
        run,
        evaluation_set=evaluation_set,
        match_result=result,
        source_manifest_sha256=source_manifest_sha256,
    )
    print(f"run: {run.run_dir}")
    _print_json(result.report.counts)
    return 0


def cmd_evaluations_report(config: Config, args: argparse.Namespace) -> int:
    run = latest_evaluation_run(config)
    if run is None:
        print("nenhum run de avaliacoes encontrado", file=sys.stderr)
        return 2
    evaluation_set, matches, report = load_evaluation_run(run)
    payload = {
        "run": run.run_id,
        "counts": evaluation_set.counts.model_dump(mode="json"),
        "matches": len(matches),
        "match_counts": report.counts,
        "by_kind": report.by_kind,
        "special_editions": len(evaluation_set.special_editions),
        "text_divergences": len(evaluation_set.text_divergences),
        "inconsistencies": len(evaluation_set.inconsistencies),
    }
    _print_json(payload)
    return 0


def cmd_plan(config: Config, args: argparse.Namespace) -> int:
    matches_path = Path(args.matches)
    reviews_path = Path(args.reviews)
    games_path = Path(args.games)
    matches = _load_models(matches_path, GameMatch)
    reviews = _load_models(reviews_path, ReviewRecord) if args.reviews else []
    games = _load_models(games_path, GameRecord) if args.games else []
    liked_path = Path(args.liked_decisions)
    liked_decisions = load_decisions(liked_path)

    destination_snapshot = None
    if args.destination_snapshot:
        destination_info, snapshot = destination_provenance(config, Path(args.destination_snapshot))
        destination_snapshot = Path(args.destination_snapshot)
        if isinstance(snapshot, AccountReviewsSnapshot):
            destination_reviews = list(snapshot.reviews)
        else:
            destination_reviews = (
                [snapshot.result.review] if snapshot and snapshot.result.review else []
            )
    else:
        destination_info = legacy_destination_provenance(
            Path(args.destination_reviews) if args.destination_reviews else None
        )
        destination_reviews = (
            _load_models(Path(args.destination_reviews), DestinationReview)
            if args.destination_reviews
            else []
        )

    plan = build_plan(
        matches,
        reviews,
        games=games,
        destination_reviews=destination_reviews,
        liked_decisions=liked_decisions,
        watchlist_slugs=args.watchlist or [],
        favorite_slugs=args.favorite or [],
        playing_now_slugs=args.playing_now or [],
    )
    plan_id = _resolve_plan_id(config, args)
    source = source_provenance(config, games_path, reviews_path)
    matching = matching_provenance(matches_path)
    decisions = decisions_provenance(liked_path, plan)
    provenance_complete = (
        source.complete
        and bool(destination_info.get("complete"))
        and bool(matching.get("complete"))
        and bool(decisions.get("complete"))
    )
    inputs = {
        "games": str(games_path),
        "reviews": str(reviews_path),
        "matches": str(matches_path),
        "decisions": str(liked_path),
        "snapshot": str(destination_snapshot) if destination_snapshot else "",
        "destination_reviews": str(args.destination_reviews or ""),
    }
    plan_dir, manifest = build_bundle(
        config,
        plan=plan,
        plan_id=plan_id,
        source=source.data,
        destination=destination_info,
        matching=matching,
        decisions=decisions,
        inputs=inputs,
        provenance_complete=provenance_complete,
    )
    print(
        f"plano: {len(plan.actions)} acoes, {len(plan.unmatched)} sem correspondencia, "
        f"{len(plan.skipped)} ignoradas"
    )
    print(f"plan_id: {plan_id}")
    print(f"manifesto: {plan_dir / 'manifest.json'}")
    print(f"proveniencia completa: {provenance_complete}")
    if not provenance_complete:
        print("aviso: aprovacao sera recusada enquanto a proveniencia estiver incompleta")
    print("aprovacao: pendente (nenhuma acao sera escrita sem aprovacao explicita)")
    return 0


def _resolve_plan_id(config: Config, args: argparse.Namespace) -> str:
    root = config.migration_dir / "plans"
    if args.plan_id:
        if args.plan_id in {".", ".."} or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", args.plan_id
        ):
            raise ValueError("--plan-id invalido")
        if (root / args.plan_id).exists():
            raise FileExistsError(f"plano ja existe: {root / args.plan_id}")
        return args.plan_id
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = f"plan-{stamp}"
    index = 2
    while (root / candidate).exists():
        candidate = f"plan-{stamp}-{index}"
        index += 1
    return candidate


def cmd_migration_show(config: Config, args: argparse.Namespace) -> int:
    bundle = load_bundle(config, args.plan_id)
    _print_json(
        {
            "plan": bundle.plan.model_dump(mode="json"),
            "manifest": bundle.manifest.model_dump(mode="json"),
            "plan_dir": str(bundle.plan_dir),
        }
    )
    return 0


def _decision_validation(config: Config, bundle) -> dict[str, Any]:
    manifest = bundle.manifest
    try:
        matches = _load_models(config.migration_dir / "matches.json", GameMatch)
        decisions = load_decisions(config.migration_dir / "liked-decisions.jsonl")
        ratifications = load_ratifications(config.migration_dir / "stage9" / "ratifications.jsonl")
        history_path = config.migration_dir / "stage9" / "match-decisions.jsonl"
        history = (
            [
                json.loads(line)
                for line in history_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if history_path.exists()
            else []
        )
        return validation_summary(
            matches=matches,
            history=history,
            decisions=decisions,
            ratifications=ratifications,
            source_run_id=str(manifest.source.get("run_id")),
            source_manifest_sha256=str(manifest.source.get("manifest_sha256")),
            destination_run_id=str(manifest.destination.get("snapshot_run_id")),
            destination_manifest_sha256=str(manifest.destination.get("manifest_sha256")),
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def cmd_migration_verify_plan(config: Config, args: argparse.Namespace) -> int:
    bundle = load_bundle(config, args.plan_id)
    verification = verify_bundle(config, bundle)
    issues = [
        {
            "severity": "error",
            "code": issue.code,
            "message": issue.message,
            "source_ref": issue.source_ref,
        }
        for issue in validate_plan(bundle.plan, bundle.destination_reviews, require_approval=False)
        if issue.severity is Severity.ERROR
    ]
    _print_json(
        {
            "plan_id": args.plan_id,
            "invalid": verification.invalid,
            "stale": verification.stale,
            "blocked": issues,
            "validation": _decision_validation(config, bundle),
        }
    )
    return 0 if not verification.invalid and not verification.stale else 2


def cmd_migration_approve(config: Config, args: argparse.Namespace) -> int:
    bundle = load_bundle(config, args.plan_id)
    verification = verify_bundle(config, bundle)
    issues = [
        issue
        for issue in validate_plan(bundle.plan, bundle.destination_reviews, require_approval=False)
        if issue.severity is Severity.ERROR
    ]
    if verification.invalid or verification.stale or issues:
        _print_json(
            {
                "plan_id": args.plan_id,
                "status": "blocked",
                "invalid": verification.invalid,
                "stale": verification.stale,
                "issues": [f"{issue.code}: {issue.message}" for issue in issues],
            }
        )
        return 2
    _print_json(
        {
            "plan_id": args.plan_id,
            "manifest_sha256": bundle.manifest.manifest_sha256,
            "plan_sha256": bundle.manifest.plan_sha256,
            "actions": bundle.manifest.plan.get("actions", []),
            "actions_detail": [
                {
                    "action_id": action_hash(action),
                    "kind": action.kind,
                    "source_ref": action.source_ref,
                    "game_id": action.game_id,
                    "before": action.before,
                    "after": action.after,
                    "liked": action.liked.model_dump(mode="json"),
                }
                for action in bundle.plan.actions
            ],
            "source": bundle.manifest.source,
            "destination": bundle.manifest.destination,
            "decisions": bundle.manifest.decisions,
            "confirmation": f"digite exatamente {args.plan_id} para confirmar",
        }
    )
    confirmation = args.confirm
    if not confirmation:
        if not sys.stdin.isatty():
            print("confirmacao explicita obrigatoria: use --confirm <plan-id>", file=sys.stderr)
            return 1
        confirmation = input(f"Digite {args.plan_id} para aprovar: ").strip()
    if confirmation != args.plan_id:
        print("aprovacao cancelada: confirmacao nao coincide", file=sys.stderr)
        return 1
    record = approve_manifest(config, args.plan_id, Path(args.approvals))
    _print_json(record.model_dump(mode="json"))
    return 0


def cmd_migration_approval_status(config: Config, args: argparse.Namespace) -> int:
    check = approval_status(config, args.plan_id, Path(args.approvals))
    payload = check.model_dump(mode="json")
    try:
        bundle = load_bundle(config, args.plan_id)
        payload["validation"] = _decision_validation(config, bundle)
    except Exception as exc:  # noqa: BLE001
        payload["validation"] = {"error": str(exc)}
    _print_json(payload)
    if check.status is ApprovalStatus.APPROVED:
        return 0
    if check.status is ApprovalStatus.UNAPPROVED:
        return 1
    return 2


def cmd_approvals_list(config: Config, args: argparse.Namespace) -> int:
    records = load_approvals(Path(args.approvals))
    _print_json([record.model_dump(mode="json") for record in records])
    return 0


def _unsupported_checks(plan, action_ids: dict[str, str]) -> list[ActionCheck]:
    checks: list[ActionCheck] = []
    for action in plan.actions:
        if action.kind == "review":
            continue
        checks.append(
            ActionCheck(
                action_id=action_hash(action),
                kind=action.kind,
                source_ref=action.source_ref,
                game_id=action.game_id,
                status=ActionStatus.UNSUPPORTED,
                reason="action_kind_not_supported_in_preflight",
            )
        )
    return checks


def cmd_migration_preflight(
    config: Config,
    args: argparse.Namespace,
    *,
    on_page: Any = None,
    should_cancel: Any = None,
) -> int:
    preflight_id = allocate_preflight_id(
        config, plan_id=args.plan_id, preflight_id=args.preflight_id
    )
    preparation = prepare_preflight(config, args.plan_id, Path(args.approvals))
    if not preparation.ready:
        result = build_result(
            preflight_id=preflight_id,
            preparation=preparation,
            checks=[],
            summary=PreflightSummary(status=preparation.status),
            snapshots=[],
            reasons=preparation.reasons,
            checks_status=preparation.checks,
        )
        result.plan_id = args.plan_id
        artifact = write_preflight_run(config, result, {}, preflight_id=preflight_id)
        _print_json(
            {
                "preflight_id": preflight_id,
                "status": preparation.status.value,
                "reasons": preparation.reasons,
                "result": str(artifact.result_path),
            }
        )
        return 2

    bundle = preparation.bundle
    assert bundle is not None
    action_ids = {action.source_ref: action_hash(action) for action in bundle.plan.actions}
    unsupported = _unsupported_checks(bundle.plan, action_ids)
    if unsupported:
        summary = summarize_checks(unsupported)
        result = build_result(
            preflight_id=preflight_id,
            preparation=preparation,
            checks=unsupported,
            summary=summary,
            snapshots=[],
            reasons=["plano contem acoes nao suportadas pelo preflight"],
            checks_status={**preparation.checks, "destination_capture": "not_run"},
        )
        artifact = write_preflight_run(config, result, {}, preflight_id=preflight_id)
        _print_json(
            {
                "preflight_id": preflight_id,
                "status": summary.status.value,
                "result": str(artifact.result_path),
            }
        )
        return 2

    page_size = args.page_size or config.jazerei_page_size
    snapshots = {}
    identity_user = preparation.expected_user_id
    try:
        with BrowserSession(config, headless=args.headless) as session:
            snapshots, _sources = capture_snapshots_for_actions(
                session,
                bundle.plan.actions,
                expected_user_id=preparation.expected_user_id or "",
                page_size=page_size,
                max_pages=args.max_pages,
                on_page=on_page,
                should_cancel=should_cancel,
            )
    except Exception as exc:  # noqa: BLE001 - captura inconclusiva nao pode virar ausencia
        status = (
            PreflightStatus.BLOCKED
            if isinstance(exc, (IdentityError, ApprovalError))
            else PreflightStatus.INCONCLUSIVE
        )
        checks = [
            ActionCheck(
                action_id=action_hash(action),
                kind=action.kind,
                source_ref=action.source_ref,
                game_id=action.game_id,
                status=ActionStatus.INCONCLUSIVE,
                reason="capture_failed",
            )
            for action in bundle.plan.actions
        ]
        result = build_result(
            preflight_id=preflight_id,
            preparation=preparation,
            checks=checks,
            summary=PreflightSummary(status=status, inconclusive=len(checks)),
            snapshots=[],
            reasons=[f"captura inconclusiva: {type(exc).__name__}"],
            identity_verified=False,
            identity_user_id=None,
            checks_status={
                **preparation.checks,
                "destination_capture": "failed",
                "destination_identity": "unknown",
            },
        )
        artifact = write_preflight_run(config, result, {}, preflight_id=preflight_id)
        _print_json(
            {
                "preflight_id": preflight_id,
                "status": status.value,
                "reasons": result.reasons,
                "result": str(artifact.result_path),
            }
        )
        if isinstance(exc, OperationCancelled):
            raise
        return 2

    checks, summary = compare_actions(
        bundle.plan.actions,
        snapshots,
        expected_user_id=preparation.expected_user_id,
    )
    # Preserve unsupported actions if a review-only plan somehow gained one.
    result = build_result(
        preflight_id=preflight_id,
        preparation=preparation,
        checks=checks,
        summary=summary,
        snapshots=[],
        identity_verified=True,
        identity_user_id=identity_user,
        checks_status={
            **preparation.checks,
            "destination_capture": "complete",
            "destination_identity": "valid",
        },
    )
    artifact = write_preflight_run(config, result, snapshots, preflight_id=preflight_id)
    _print_json(
        {
            "preflight_id": preflight_id,
            "status": summary.status.value,
            "compatible": summary.compatible_actions,
            "conflicts": summary.conflicts,
            "unsupported": summary.unsupported,
            "inconclusive": summary.inconclusive,
            "result": str(artifact.result_path),
        }
    )
    if summary.status.value == "compatible":
        return 0
    if summary.status.value == "conflict":
        return 1
    return 2


def cmd_migration_preflight_show(config: Config, args: argparse.Namespace) -> int:
    result = load_preflight(config, args.preflight_id)
    _print_json(result.model_dump(mode="json"))
    return 0


def cmd_migration_verify_preflight(config: Config, args: argparse.Namespace) -> int:
    errors = verify_preflight(config, args.preflight_id)
    _print_json({"preflight_id": args.preflight_id, "valid": not errors, "errors": errors})
    return 0 if not errors else 2


def cmd_execution_prepare(config: Config, args: argparse.Namespace) -> int:
    execution_id = allocate_execution_id(
        config, plan_id=args.plan_id, execution_id=args.execution_id
    )
    execution = prepare_execution(
        config,
        plan_id=args.plan_id,
        preflight_id=args.preflight_id,
        execution_id=execution_id,
        approvals_path=Path(args.approvals),
    )
    artifact = create_execution(config, execution)
    _print_json(
        {
            "execution_id": execution_id,
            "status": execution.status.value,
            "plan_id": execution.plan_id,
            "preflight_id": execution.preflight_id,
            "actions": [
                {
                    "action_id": action.action_id,
                    "game_id": action.game_id,
                    "status": action.status.value,
                    "payload_sha256": action.payload_sha256,
                }
                for action in execution.actions
            ],
            "execution": str(artifact.execution_path),
            "manifest": str(artifact.manifest_path),
        }
    )
    return 0


def cmd_execution_show(config: Config, args: argparse.Namespace) -> int:
    execution = load_execution(config, args.execution_id)
    confirmations = load_confirmations(config, args.execution_id)
    _print_json(
        {
            "execution": execution.model_dump(mode="json"),
            "confirmations": [record.model_dump(mode="json") for record in confirmations],
        }
    )
    return 0


def cmd_execution_validate(config: Config, args: argparse.Namespace) -> int:
    errors = validate_execution(config, args.execution_id, Path(args.approvals))
    _print_json({"execution_id": args.execution_id, "valid": not errors, "errors": errors})
    return 0 if not errors else 2


def cmd_execution_confirm(config: Config, args: argparse.Namespace) -> int:
    execution = load_execution(config, args.execution_id)
    action = next((item for item in execution.actions if item.action_id == args.action_id), None)
    if action is None:
        print("acao inexistente", file=sys.stderr)
        return 2
    if action.status.value not in {"prepared", "awaiting_confirmation"}:
        print(
            f"acao nao esta aguardando confirmacao: {action.status.value}",
            file=sys.stderr,
        )
        return 2
    _print_json(
        {
            "execution_id": execution.execution_id,
            "plan_id": execution.plan_id,
            "preflight_id": execution.preflight_id,
            "action": {
                "action_id": action.action_id,
                "kind": action.kind,
                "game_id": action.game_id,
                "source_ref": action.source_ref,
                "before": action.planned_before,
                "after": action.planned_after,
                "payload": action.payload,
                "payload_sha256": action.payload_sha256,
                "status": action.status.value,
            },
            "identity": execution.user_id,
            "confirmation": f"digite exatamente {execution.execution_id}",
            "note": "confirmacao documental; nenhuma escrita sera executada nesta etapa",
        }
    )
    if not args.confirm:
        print("confirmacao explicita obrigatoria: use --confirm <execution-id>", file=sys.stderr)
        return 1
    record = confirm_action(
        config,
        execution_id=args.execution_id,
        action_id=args.action_id,
        confirmation=args.confirm,
        approvals_path=Path(args.approvals),
    )
    _print_json(record.model_dump(mode="json"))
    return 0


def cmd_execution_status(config: Config, args: argparse.Namespace) -> int:
    execution = load_execution(config, args.execution_id)
    _print_json(
        {
            "execution_id": execution.execution_id,
            "status": execution.status.value,
            "summary": execution.summary.model_dump(mode="json"),
            "actions": [
                {"action_id": action.action_id, "status": action.status.value}
                for action in execution.actions
            ],
            "manifest_sha256": execution_manifest_hash(config, args.execution_id),
        }
    )
    return 0


def cmd_execution_verify(config: Config, args: argparse.Namespace) -> int:
    errors = verify_execution(config, args.execution_id)
    _print_json({"execution_id": args.execution_id, "valid": not errors, "errors": errors})
    return 0 if not errors else 2


def cmd_execution_reconcile(config: Config, args: argparse.Namespace) -> int:
    execution = reconcile_execution(config, args.execution_id)
    _print_json(
        {
            "execution_id": execution.execution_id,
            "status": execution.status.value,
            "summary": execution.summary.model_dump(mode="json"),
            "actions": [
                {
                    "action_id": action.action_id,
                    "status": action.status.value,
                    "reason": action.reason,
                }
                for action in execution.actions
            ],
        }
    )
    return 0


def cmd_execution_resolve(config: Config, args: argparse.Namespace) -> int:
    from src.models.snapshot import ReviewStateSnapshot

    snapshot = ReviewStateSnapshot.model_validate(read_json(Path(args.snapshot)))
    execution = resolve_ambiguous_action(
        config,
        execution_id=args.execution_id,
        action_id=args.action_id,
        observed=snapshot,
    )
    _print_json(
        {
            "execution_id": execution.execution_id,
            "status": execution.status.value,
            "summary": execution.summary.model_dump(mode="json"),
            "action": next(
                item.model_dump(mode="json")
                for item in execution.actions
                if item.action_id == args.action_id
            ),
        }
    )
    return 0


def cmd_execution_run(config: Config, args: argparse.Namespace) -> int:
    """Execucao real futura, bloqueada por dois gates independentes.

    Nao executar nesta etapa. Sem os dois flags e a confirmacao exata, retorna
    antes de abrir navegador ou tocar em qualquer artefato.
    """
    if not (config.allow_writes and config.enable_real_write_transport):
        print(
            "escrita real desabilitada: exige MIGRATION_ALLOW_WRITES=true e "
            "MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=true",
            file=sys.stderr,
        )
        return 2
    if args.confirm != args.execution_id:
        print("confirmacao deve ser exatamente o execution_id", file=sys.stderr)
        return 2
    execution = load_execution(config, args.execution_id)
    action = next((item for item in execution.actions if item.action_id == args.action_id), None)
    if action is None:
        print("acao inexistente", file=sys.stderr)
        return 2
    if action.game_id is None:
        print("acao sem game_id", file=sys.stderr)
        return 2
    slug = action.destination_slug or action.source_ref
    with BrowserSession(config, headless=args.headless) as session:
        auth_context = authenticate(session, config.jazerei_user_id)
        transport = build_browser_write_transport(
            config, session, auth_headers=auth_context.headers
        )

        def read_state(game_id: int):
            return capture_review_state(
                session,
                game_id=game_id,
                slug=slug,
                expected_user_id=config.jazerei_user_id,
                page_size=config.jazerei_page_size,
            )

        result = execute_action(
            config,
            execution_id=args.execution_id,
            action_id=args.action_id,
            transport=transport,
            read_state=read_state,
            approvals_path=Path(args.approvals),
        )
    _print_json(
        {
            "execution_id": result.execution_id,
            "status": result.status.value,
            "summary": result.summary.model_dump(mode="json"),
            "action": next(
                {
                    "action_id": item.action_id,
                    "status": item.status.value,
                    "reason": item.reason,
                    "verification": item.verification,
                }
                for item in result.actions
                if item.action_id == args.action_id
            ),
        }
    )
    return 0 if result.status.value in {"completed", "completed_with_errors"} else 2


def cmd_validate(config: Config, args: argparse.Namespace) -> int:
    plan = MigrationPlan.model_validate(read_json(Path(args.plan)))
    destination_reviews = (
        _load_models(Path(args.destination_reviews), DestinationReview)
        if args.destination_reviews
        else []
    )
    issues = validate_plan(plan, destination_reviews)
    _print_json(
        [
            {
                "severity": issue.severity.value,
                "code": issue.code,
                "message": issue.message,
                "source_ref": issue.source_ref,
            }
            for issue in issues
        ]
    )
    summary = preflight(plan, config)
    print(f"preflight: permitido={summary.allowed}")
    return 1 if any(issue.severity is Severity.ERROR for issue in issues) else 0


def cmd_verify(config: Config, args: argparse.Namespace) -> int:
    plan = MigrationPlan.model_validate(read_json(Path(args.plan)))
    destination_reviews = _load_models(Path(args.destination_reviews), DestinationReview)
    report = verify_plan(plan, destination_reviews)
    path = write_json(config.migration_dir / "verification-report.json", report, overwrite=True)
    print(
        f"verificacao: {report.verified}/{report.checked} confirmadas, "
        f"{len(report.missing)} ausentes, {len(report.mismatched)} divergentes"
    )
    print(f"arquivo: {path}")
    return 0 if not report.missing and not report.mismatched else 1


def _load_review_for_decision(path: Path, source_ref: str) -> ReviewRecord:
    reviews = [
        review for review in _load_models(path, ReviewRecord) if review.game_slug == source_ref
    ]
    if not reviews:
        raise ValueError(f"review de origem nao encontrada para slug {source_ref}")
    if len(reviews) > 1:
        raise ValueError(f"mais de uma review de origem para slug {source_ref}")
    return reviews[0]


def _parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise argparse.ArgumentTypeError("use true ou false")


def cmd_liked_decide(config: Config, args: argparse.Namespace) -> int:
    review = _load_review_for_decision(Path(args.review_file), args.source_ref)
    source_hash = review_version_hash(review)
    decision = append_decision(
        Path(args.decisions),
        source_ref=args.source_ref,
        game_id=args.game_id,
        source_hash=source_hash,
        value=args.value,
        replace=args.replace,
    )
    _print_json(decision.model_dump(mode="json"))
    return 0


def cmd_liked_list(config: Config, args: argparse.Namespace) -> int:
    decisions = load_decisions(Path(args.decisions))
    _print_json([decision.model_dump(mode="json") for decision in decisions])
    return 0


def cmd_liked_show(config: Config, args: argparse.Namespace) -> int:
    review = _load_review_for_decision(Path(args.review_file), args.source_ref)
    source_hash = review_version_hash(review)
    state = resolve_liked(
        load_decisions(Path(args.decisions)),
        source_ref=args.source_ref,
        game_id=args.game_id,
        source_hash=source_hash,
    )
    _print_json(state.model_dump(mode="json"))
    return 0


def cmd_browser_open(config: Config, args: argparse.Namespace) -> int:
    url = (
        f"{config.stash_base_url}/users/{config.stash_username}/"
        if args.site == "stash"
        else config.jazerei_base_url
    )
    with BrowserSession(config, headless=args.headless) as session:
        session.goto(url)
        session.wait(500)
        _print_json(
            {
                "site": args.site,
                "url": session.current_url_sanitized(),
                "title": session.title(),
                "profile_dir": str(config.resolved_browser_profile),
                "seconds_open": args.seconds,
            }
        )
        if args.seconds > 0:
            session.wait(int(args.seconds * 1000))
        summary = session.log.summary()
        print(
            f"navegador: {summary['observed']} requisicoes, {summary['blocked']} bloqueadas",
            file=sys.stderr,
        )
    return 0


def _persist_session_identity(config: Config, session: BrowserSession) -> SessionIdentity | None:
    """Descobre e guarda apenas o UID da sessão logada, sem credenciais."""
    try:
        identity = discover_session_user_id(session)
    except SessionIdentityError:
        return None
    except Exception:  # noqa: BLE001 - login continua válido mesmo sem UID local
        return None
    if identity is None:
        return None
    try:
        save_session_identity(config.browser_dir, identity)
    except (OSError, SessionIdentityError):
        return None
    return identity


def cmd_browser_login(config: Config, args: argparse.Namespace) -> int:
    with BrowserSession(config, headless=False, allow_login=True) as session:
        session.goto(config.jazerei_base_url)
        print(
            "Faca login manualmente na janela do Chromium. "
            "Nenhuma senha e solicitada, preenchida ou armazenada pelo programa.",
            file=sys.stderr,
        )
        try:
            evidence = wait_for_login(session.page, args.timeout)
        except BrowserUnavailable as exc:
            summary = session.log.summary()
            _print_json(
                {
                    "logged_in": False,
                    "error": str(exc),
                    "requests": {
                        "observed": summary["observed"],
                        "blocked": summary["blocked"],
                        "blocked_entries": summary["blocked_entries"],
                    },
                }
            )
            print(
                "A janela foi fechada antes da confirmacao. Rode "
                "`browser status --verify` para checar se a sessao foi salva.",
                file=sys.stderr,
            )
            return 2

        identity = _persist_session_identity(config, session) if evidence.logged_in else None
        summary = session.log.summary()
        _print_json(
            {
                "logged_in": evidence.logged_in,
                "firebase_auth_marker": evidence.firebase_auth,
                "supabase_auth_marker": evidence.supabase_auth,
                "indexed_db_marker": evidence.indexed_db,
                "logout_visible": evidence.logout_visible,
                "login_visible": evidence.login_visible,
                "path": evidence.path,
                "user_id": identity.user_id if identity else None,
                "user_id_source": identity.source if identity else None,
                "identity_saved": identity is not None,
                "identity_file": str(config.browser_dir / "session-identity.json"),
                "requests": {
                    "observed": summary["observed"],
                    "blocked": summary["blocked"],
                    "blocked_entries": summary["blocked_entries"],
                },
            }
        )
        if not evidence.logged_in:
            print(
                "Login nao confirmado dentro do tempo limite. A sessao pode estar "
                "expirada, o login nao foi concluido ou a janela foi fechada. "
                "Rode `browser status --verify` para checar o perfil persistido.",
                file=sys.stderr,
            )
            return 1
        if identity is None:
            print(
                "Login confirmado, mas o UID da conta nao foi encontrado. "
                "Rode `browser status --verify` ou defina JAZEREI_USER_ID no ambiente "
                "antes do preflight.",
                file=sys.stderr,
            )
    return 0


def cmd_browser_status(config: Config, args: argparse.Namespace) -> int:
    payload = profile_status(config).as_dict()
    if args.verify:
        with BrowserSession(config, headless=True) as session:
            session.goto(config.jazerei_base_url)
            session.wait(1_000)
            evidence = login_evidence(session.page)
            identity = _persist_session_identity(config, session) if evidence.logged_in else None
            payload["online_check"] = {
                "logged_in": evidence.logged_in,
                "firebase_auth_marker": evidence.firebase_auth,
                "supabase_auth_marker": evidence.supabase_auth,
                "indexed_db_marker": evidence.indexed_db,
                "logout_visible": evidence.logout_visible,
                "login_visible": evidence.login_visible,
                "path": evidence.path,
                "user_id": identity.user_id if identity else None,
                "user_id_source": identity.source if identity else None,
                "identity_saved": identity is not None,
            }
            if identity is not None:
                payload["expected_user_id"] = identity.user_id
                payload["expected_user_id_source"] = "session_file"
            payload["requests"] = session.log.summary()
    _print_json(payload)
    return 0


def cmd_browser_close(config: Config, args: argparse.Namespace) -> int:
    mark_closed(config)
    _print_json(
        {
            "closed": True,
            "note": "cada comando fecha o navegador; cookies e storage foram preservados",
            "profile": profile_status(config).as_dict(),
        }
    )
    return 0


def cmd_browser_reviews(config: Config, args: argparse.Namespace) -> int:
    with BrowserSession(config, headless=args.headless) as session:
        outcome = capture_reviews(
            session,
            user_id=args.user_id or None,
            timeout_seconds=args.timeout,
        )
        summary = session.log.summary()
    for note in outcome.notes:
        print(f"aviso: {note}", file=sys.stderr)
    print(f"metodo: {outcome.method}", file=sys.stderr)
    print(
        f"navegador: {summary['observed']} requisicoes, {summary['blocked']} bloqueadas",
        file=sys.stderr,
    )
    if not outcome.reviews:
        print("nenhuma review encontrada; nada foi gravado", file=sys.stderr)
        return 1
    path = write_json(Path(args.out), outcome.reviews, overwrite=True)
    print(f"reviews: {len(outcome.reviews)} ({path})")
    return 0


def cmd_browser_account_reviews(config: Config, args: argparse.Namespace) -> int:
    expected_user_id = config.jazerei_user_id
    if not expected_user_id:
        print(
            "identidade esperada ausente: faca login em jazerei ou defina JAZEREI_USER_ID",
            file=sys.stderr,
        )
        return 2
    run_id = args.run_id or (
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-account-review-state-{expected_user_id}"
    )
    with BrowserSession(config, headless=args.headless) as session:
        snapshot = capture_account_reviews(
            session,
            expected_user_id=expected_user_id,
            page_size=args.page_size or config.jazerei_page_size,
            max_pages=args.max_pages,
        )
    artifact = write_account_snapshot_run(config, snapshot, run_id=run_id)
    _print_json(
        {
            "run_id": artifact.run_id,
            "reviews": len(snapshot.reviews),
            "complete": snapshot.capture.complete,
            "pagination_complete": snapshot.pagination.complete,
            "response_hash": snapshot.capture.response_hash,
            "snapshot": str(artifact.snapshot_path),
            "manifest": str(artifact.manifest_path),
        }
    )
    return 0


def cmd_browser_review_state(config: Config, args: argparse.Namespace) -> int:
    expected_user_id = config.jazerei_user_id
    if not expected_user_id:
        print(
            "identidade esperada ausente: faca login em jazerei ou defina JAZEREI_USER_ID "
            "no ambiente local",
            file=sys.stderr,
        )
        return 2
    page_size = args.page_size or config.jazerei_page_size
    if page_size < 1 or page_size > 1000:
        print("page-size deve estar entre 1 e 1000", file=sys.stderr)
        return 2
    if args.max_pages < 1:
        print("max-pages deve ser >= 1", file=sys.stderr)
        return 2

    run_id = allocate_run_id(config, slug=args.slug, game_id=args.game_id, run_id=args.run_id)
    try:
        with BrowserSession(config, headless=args.headless) as session:
            snapshot = capture_review_state(
                session,
                game_id=args.game_id,
                slug=args.slug,
                expected_user_id=expected_user_id,
                page_size=page_size,
                max_pages=args.max_pages,
            )
        artifact = write_snapshot_run(config, snapshot, run_id=run_id)
    except FileExistsError:
        raise
    except Exception as exc:  # noqa: BLE001 - falha vira evidencia inconclusiva
        error_code = _capture_error_code(exc)
        try:
            artifact = write_failure_run(
                config,
                run_id=run_id,
                game_id=args.game_id,
                slug=args.slug,
                expected_user_id=expected_user_id,
                error_code=error_code,
                error_message=str(exc),
            )
        except FileExistsError:
            raise
        except Exception as storage_exc:  # noqa: BLE001
            print(f"captura falhou: {exc}", file=sys.stderr)
            print(f"armazenamento da falha tambem falhou: {storage_exc}", file=sys.stderr)
            return 2
        print(f"captura inconclusiva: {safe_error_message(str(exc))}", file=sys.stderr)
        print(f"evidencia de falha: {artifact.run_dir}", file=sys.stderr)
        return 2

    _print_json(
        {
            "run_id": artifact.run_id,
            "outcome": artifact.outcome,
            "game_id": snapshot.query.game_id,
            "slug": snapshot.query.slug,
            "user_id": snapshot.identity.user_id,
            "identity_verified": snapshot.identity.verified,
            "exists": snapshot.result.exists,
            "pages_fetched": snapshot.pagination.pages_fetched,
            "records_received": snapshot.pagination.records_received,
            "unique_records": snapshot.pagination.unique_records,
            "duplicates_discarded": snapshot.pagination.duplicates_discarded,
            "termination_reason": snapshot.pagination.termination_reason,
            "complete": snapshot.capture.complete,
            "response_hash": snapshot.capture.response_hash,
            "snapshot": str(artifact.snapshot_path) if artifact.snapshot_path else None,
            "manifest": str(artifact.manifest_path),
        }
    )
    return 0


def _capture_error_code(exc: Exception) -> str:
    if isinstance(exc, IdentityError):
        return "identity_invalid"
    if isinstance(exc, CaptureError):
        return "capture_incomplete"
    if isinstance(exc, BrowserUnavailable):
        return "browser_unavailable"
    return "capture_error"


def cmd_browser_detail(config: Config, args: argparse.Namespace) -> int:
    with BrowserSession(config, headless=args.headless) as session:
        detail = fetch_game_detail(session, args.slug)
        summary = session.log.summary()
    _print_json(detail)
    print(
        f"navegador: {summary['observed']} requisicoes, {summary['blocked']} bloqueadas",
        file=sys.stderr,
    )
    return 0


def cmd_review_ui(config: Config, args: argparse.Namespace) -> int:
    from src.review_ui import run_review_ui

    run_review_ui(
        config,
        host=args.host,
        port=args.port,
        open_browser=args.open,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stash-jazerei-migration")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="mostra configuracao segura (sem segredos)")

    export = sub.add_parser("export", help="exporta dados publicos do Stash")
    export.add_argument("--kind", choices=["games", "reviews", "both"], default="both")
    export.add_argument("--max-pages", type=int, default=1)
    export.add_argument("--all-pages", action="store_true")
    export.add_argument("--save-html", action="store_true")
    export.add_argument("--no-details", action="store_true", help="nao visitar reviews individuais")
    export.add_argument("--method", choices=["http", "browser"], default="http")
    export.add_argument("--browser-headless", action="store_true")
    export.add_argument("--slug", default=None, help="filtra pelo slug exato do jogo")
    export.add_argument(
        "--game-page",
        action="append",
        type=int,
        default=None,
        help="pagina explicita de jogos (repetivel)",
    )
    export.add_argument(
        "--review-page",
        action="append",
        type=int,
        default=None,
        help="pagina explicita de reviews (repetivel)",
    )
    export.add_argument("--run-id", default=None, help="identificador da execucao")
    export.add_argument(
        "--overwrite",
        action="store_true",
        help="substitui um run existente; exige --run-id explicito",
    )

    search = sub.add_parser("search", help="pesquisa o catalogo do Ja Zerei")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--method", choices=["http", "browser"], default="http")
    search.add_argument("--browser-headless", action="store_true")
    search.add_argument(
        "--prefer-ui", action="store_true", help="tentar busca pela interface primeiro"
    )

    match = sub.add_parser("match", help="gera correspondencias (nunca aprova)")
    match.add_argument("--games", default="data/stash/games.json")
    match.add_argument("--limit", type=int, default=8)
    match.add_argument("--method", choices=["http", "browser"], default="http")
    match.add_argument("--browser-headless", action="store_true")

    evaluations = sub.add_parser(
        "evaluations", help="constroi e casa avaliacoes escritas e somente nota"
    )
    evaluations_sub = evaluations.add_subparsers(dest="evaluations_command", required=True)
    evaluations_build = evaluations_sub.add_parser(
        "build", help="valida o universo de avaliacoes (76 escritas + 622 somente nota)"
    )
    evaluations_build.add_argument(
        "--source-run", default="data/stash/runs/20261126T183458Z-stash-browser"
    )
    evaluations_build.add_argument("--json", action="store_true")
    evaluations_match = evaluations_sub.add_parser(
        "match", help="executa matching somente leitura das avaliacoes pendentes"
    )
    evaluations_match.add_argument(
        "--source-run", default="data/stash/runs/20261126T183458Z-stash-browser"
    )
    evaluations_match.add_argument("--matches", default="data/migration/matches.json")
    evaluations_match.add_argument("--run-id", default=None)
    evaluations_match.add_argument("--limit", type=int, default=8)
    evaluations_match.add_argument("--max-attempts", type=int, default=3)
    evaluations_match.add_argument("--retry-delay", type=float, default=2.0)
    evaluations_match.add_argument("--method", choices=["http", "browser"], default="http")
    evaluations_match.add_argument("--browser-headless", action="store_true")
    evaluations_sub.add_parser("report", help="mostra o ultimo run de avaliacoes")

    browser = sub.add_parser("browser", help="sessao Chromium persistente e coleta via navegador")
    browser_sub = browser.add_subparsers(dest="browser_command", required=True)

    browser_open = browser_sub.add_parser("open", help="abre um site no Chromium")
    browser_open.add_argument("site", choices=["stash", "jazerei"])
    browser_open.add_argument("--seconds", type=float, default=30)
    browser_open.add_argument("--headless", action="store_true")

    browser_login = browser_sub.add_parser("login", help="login manual no Ja Zerei")
    browser_login.add_argument("site", choices=["jazerei"], nargs="?", default="jazerei")
    browser_login.add_argument("--timeout", type=float, default=300)

    browser_status = browser_sub.add_parser("status", help="status nao sensivel da sessao")
    browser_status.add_argument("--verify", action="store_true", help="checar login online")

    browser_sub.add_parser("close", help="marca a sessao como encerrada")

    browser_reviews = browser_sub.add_parser("reviews", help="le reviews da conta via navegador")
    browser_reviews.add_argument("--user-id", default="")
    browser_reviews.add_argument("--timeout", type=float, default=8)
    browser_reviews.add_argument("--headless", action="store_true")
    browser_reviews.add_argument("--out", default="data/jazerei/reviews.json")

    review_state = browser_sub.add_parser(
        "review-state", help="captura snapshot autenticado de um jogo"
    )
    review_state.add_argument("--game-id", type=int, required=True)
    review_state.add_argument("--slug", required=True)
    review_state.add_argument("--page-size", type=int, default=0)
    review_state.add_argument("--max-pages", type=int, default=100)
    review_state.add_argument("--run-id", default=None)
    review_state.add_argument("--headless", action="store_true")

    account_reviews = browser_sub.add_parser(
        "account-reviews", help="captura snapshot versionado de todas as reviews da conta"
    )
    account_reviews.add_argument("--page-size", type=int, default=0)
    account_reviews.add_argument("--max-pages", type=int, default=100)
    account_reviews.add_argument("--run-id", default=None)
    account_reviews.add_argument("--headless", action="store_true")

    browser_detail = browser_sub.add_parser("detail", help="le detalhes publicos de um jogo")
    browser_detail.add_argument("slug")
    browser_detail.add_argument("--headless", action="store_true")

    plan = sub.add_parser("plan", help="monta plano de migracao nao aprovado")
    plan.add_argument("--matches", default="data/migration/matches.json")
    plan.add_argument("--reviews", default="data/stash/reviews.json")
    plan.add_argument("--games", default="data/stash/games.json")
    plan.add_argument("--destination-reviews", default="")
    plan.add_argument("--destination-snapshot", default="")
    plan.add_argument("--liked-decisions", default="data/migration/liked-decisions.jsonl")
    plan.add_argument("--plan-id", default=None)
    plan.add_argument("--favorite", action="append")
    plan.add_argument("--playing-now", action="append")
    plan.add_argument("--watchlist", action="append")

    migration = sub.add_parser("migration", help="planos e aprovacoes")
    migration_sub = migration.add_subparsers(dest="migration_command", required=True)

    migration_show = migration_sub.add_parser("show", help="mostra plano e manifesto")
    migration_show.add_argument("--plan-id", required=True)

    migration_verify = migration_sub.add_parser("verify-plan", help="verifica hashes do plano")
    migration_verify.add_argument("--plan-id", required=True)

    migration_approve = migration_sub.add_parser("approve", help="aprova um manifesto exato")
    migration_approve.add_argument("--plan-id", required=True)
    migration_approve.add_argument("--confirm", default="")
    migration_approve.add_argument("--approvals", default="data/migration/approvals.jsonl")

    migration_status = migration_sub.add_parser(
        "approval-status", help="consulta validade da aprovacao"
    )
    migration_status.add_argument("--plan-id", required=True)
    migration_status.add_argument("--approvals", default="data/migration/approvals.jsonl")

    approvals = migration_sub.add_parser("approvals", help="lista aprovacoes")
    approvals_sub = approvals.add_subparsers(dest="approvals_command", required=True)
    approvals_list = approvals_sub.add_parser("list", help="lista registros append-only")
    approvals_list.add_argument("--approvals", default="data/migration/approvals.jsonl")

    preflight = migration_sub.add_parser("preflight", help="rele o destino e compara com o plano")
    preflight.add_argument("--plan-id", required=True)
    preflight.add_argument("--preflight-id", default=None)
    preflight.add_argument("--approvals", default="data/migration/approvals.jsonl")
    preflight.add_argument("--page-size", type=int, default=0)
    preflight.add_argument("--max-pages", type=int, default=100)
    preflight.add_argument("--headless", action="store_true")

    preflight_show = migration_sub.add_parser("preflight-show", help="mostra preflight anterior")
    preflight_show.add_argument("--preflight-id", required=True)

    verify_preflight = migration_sub.add_parser(
        "verify-preflight", help="verifica integridade de um preflight"
    )
    verify_preflight.add_argument("--preflight-id", required=True)

    execution_prepare = migration_sub.add_parser(
        "execution-prepare", help="prepara execucao sem escrever no destino"
    )
    execution_prepare.add_argument("--plan-id", required=True)
    execution_prepare.add_argument("--preflight-id", required=True)
    execution_prepare.add_argument("--execution-id", default=None)
    execution_prepare.add_argument("--approvals", default="data/migration/approvals.jsonl")

    execution_show = migration_sub.add_parser("execution-show", help="mostra execucao")
    execution_show.add_argument("--execution-id", required=True)

    execution_validate = migration_sub.add_parser(
        "execution-validate", help="revalida vinculos/hashes da execucao"
    )
    execution_validate.add_argument("--execution-id", required=True)
    execution_validate.add_argument("--approvals", default="data/migration/approvals.jsonl")

    execution_confirm = migration_sub.add_parser(
        "execution-confirm", help="confirma documentalmente uma acao (nao executa)"
    )
    execution_confirm.add_argument("--execution-id", required=True)
    execution_confirm.add_argument("--action-id", required=True)
    execution_confirm.add_argument("--confirm", required=True)
    execution_confirm.add_argument("--approvals", default="data/migration/approvals.jsonl")

    execution_status = migration_sub.add_parser("execution-status", help="estado da execucao")
    execution_status.add_argument("--execution-id", required=True)

    execution_verify = migration_sub.add_parser(
        "execution-verify", help="verifica integridade dos artefatos"
    )
    execution_verify.add_argument("--execution-id", required=True)

    execution_reconcile = migration_sub.add_parser(
        "execution-reconcile", help="classifica escritas interrompidas como ambiguas (sem reenvio)"
    )
    execution_reconcile.add_argument("--execution-id", required=True)

    execution_resolve = migration_sub.add_parser(
        "execution-resolve", help="resolve ambiguidade com snapshot autenticado (sem escrever)"
    )
    execution_resolve.add_argument("--execution-id", required=True)
    execution_resolve.add_argument("--action-id", required=True)
    execution_resolve.add_argument("--snapshot", required=True)

    execution_run = migration_sub.add_parser(
        "execution-run",
        help="executa UMA acao real; exige dois gates e confirmacao exata",
    )
    execution_run.add_argument("--execution-id", required=True)
    execution_run.add_argument("--action-id", required=True)
    execution_run.add_argument("--confirm", required=True)
    execution_run.add_argument("--approvals", default="data/migration/approvals.jsonl")
    execution_run.add_argument("--headless", action="store_true")

    liked = migration_sub.add_parser("liked", help="decisoes do campo liked")
    liked_sub = liked.add_subparsers(dest="liked_command", required=True)

    liked_decide = liked_sub.add_parser("decide", help="registra decisao humana true/false")
    liked_decide.add_argument("--source-ref", required=True)
    liked_decide.add_argument("--game-id", required=True, type=int)
    liked_decide.add_argument("--value", required=True, type=_parse_bool)
    liked_decide.add_argument("--review-file", default="data/stash/reviews.json")
    liked_decide.add_argument("--decisions", default="data/migration/liked-decisions.jsonl")
    liked_decide.add_argument("--replace", action="store_true")

    liked_list = liked_sub.add_parser("list", help="lista decisoes registradas")
    liked_list.add_argument("--decisions", default="data/migration/liked-decisions.jsonl")

    liked_show = liked_sub.add_parser("show", help="mostra o estado resolvido de uma review")
    liked_show.add_argument("--source-ref", required=True)
    liked_show.add_argument("--game-id", required=True, type=int)
    liked_show.add_argument("--review-file", default="data/stash/reviews.json")
    liked_show.add_argument("--decisions", default="data/migration/liked-decisions.jsonl")

    validate = sub.add_parser("validate", help="valida plano e pre-condicoes")
    validate.add_argument("--plan", default="data/migration/plan.json")
    validate.add_argument("--destination-reviews", default="")

    review_ui = sub.add_parser(
        "review-ui", help="interface local para revisar correspondências dos jogos"
    )
    review_ui.add_argument("--host", default="127.0.0.1")
    review_ui.add_argument("--port", type=int, default=8765)
    review_ui.add_argument("--open", action="store_true", help="abre o navegador")

    verify = sub.add_parser("verify", help="compara estado final com o plano")
    verify.add_argument("--plan", default="data/migration/plan.json")
    verify.add_argument("--destination-reviews", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()
    handlers = {
        "status": lambda: cmd_status(config),
        "export": lambda: cmd_export(config, args),
        "search": lambda: cmd_search(config, args),
        "match": lambda: cmd_match(config, args),
        "evaluations": lambda: {
            "build": cmd_evaluations_build,
            "match": cmd_evaluations_match,
            "report": cmd_evaluations_report,
        }[args.evaluations_command](config, args),
        "plan": lambda: cmd_plan(config, args),
        "validate": lambda: cmd_validate(config, args),
        "verify": lambda: cmd_verify(config, args),
        "review-ui": lambda: cmd_review_ui(config, args),
        "migration": lambda: {
            "show": cmd_migration_show,
            "verify-plan": cmd_migration_verify_plan,
            "approve": cmd_migration_approve,
            "approval-status": cmd_migration_approval_status,
            "approvals": lambda config, args: {"list": cmd_approvals_list}[args.approvals_command](
                config, args
            ),
            "preflight": cmd_migration_preflight,
            "preflight-show": cmd_migration_preflight_show,
            "verify-preflight": cmd_migration_verify_preflight,
            "execution-prepare": cmd_execution_prepare,
            "execution-show": cmd_execution_show,
            "execution-validate": cmd_execution_validate,
            "execution-confirm": cmd_execution_confirm,
            "execution-status": cmd_execution_status,
            "execution-verify": cmd_execution_verify,
            "execution-reconcile": cmd_execution_reconcile,
            "execution-resolve": cmd_execution_resolve,
            "execution-run": cmd_execution_run,
            "liked": lambda config, args: {
                "decide": cmd_liked_decide,
                "list": cmd_liked_list,
                "show": cmd_liked_show,
            }[args.liked_command](config, args),
        }[args.migration_command](config, args),
        "browser": lambda: {
            "open": cmd_browser_open,
            "login": cmd_browser_login,
            "status": cmd_browser_status,
            "close": cmd_browser_close,
            "reviews": cmd_browser_reviews,
            "review-state": cmd_browser_review_state,
            "account-reviews": cmd_browser_account_reviews,
            "detail": cmd_browser_detail,
        }[args.browser_command](config, args),
    }
    try:
        return handlers[args.command]()
    except (BrowserUnavailable, BrowserApiError) as exc:
        print(f"navegador: {exc}", file=sys.stderr)
        return 2
    except MissingAuthError as exc:
        print(f"autenticacao: {exc}", file=sys.stderr)
        return 2
    except DecisionError as exc:
        print(f"decisao: {exc}", file=sys.stderr)
        return 2
    except ApprovalError as exc:
        print(f"aprovacao: {exc}", file=sys.stderr)
        return 2
    except (ExecutionError, ExecutionStorageError) as exc:
        print(f"execucao: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - falha do Playwright nao deve virar traceback
        if type(exc).__module__.startswith("playwright."):
            print(
                "navegador: a janela foi fechada ou a operacao foi interrompida; "
                "a sessao persistida foi preservada",
                file=sys.stderr,
            )
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
