"""Captura autenticada e verificável do estado de reviews do Já Zerei.

Somente leitura: a sessão do Playwright é usada para descobrir a identidade e
os cabeçalhos da própria aplicação; a paginação é feita com GET e `Range`.
Nenhum token/cookie é impresso, persistido ou incluído no snapshot.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from src.browser.session import BrowserSession
from src.cancellation import OperationCancelled
from src.jazerei.reviews import sanitize_review_payload
from src.models.destination import DestinationReview
from src.models.snapshot import (
    AccountReviewsSnapshot,
    AccountSnapshotQuery,
    ReviewStateSnapshot,
    SnapshotCapture,
    SnapshotIdentity,
    SnapshotPagination,
    SnapshotQuery,
    SnapshotResult,
    canonical_account_hash,
    canonical_hash,
)

PROFILE_PATH = "/perfil"
REVIEWS_PATH = "/rest/v1/reviews"
PROFILE_RPC_PATH = "/rest/v1/rpc/get_my_profile"
CONTENT_RANGE_RE = re.compile(r"^(\d+)-(\d+)/(\d+|\*)$")
DEFAULT_MAX_PAGES = 100


class CaptureError(RuntimeError):
    """Falha de captura que nao pode virar snapshot valido."""


class IdentityError(CaptureError):
    pass


class PaginationError(CaptureError):
    pass


@dataclass(frozen=True)
class PageResponse:
    status: int
    body: Any
    content_range: str | None = None


@dataclass
class AuthenticatedContext:
    expected_user_id: str
    api_user_id: str
    session_user_id: str
    api_base: str
    headers: dict[str, str] = field(repr=False)
    sources: tuple[str, ...] = ()


@dataclass
class PaginationResult:
    reviews: list[DestinationReview]
    pages_fetched: int
    records_received: int
    duplicates_discarded: int
    termination_reason: str
    content_range_total: int | None


@dataclass
class SnapshotValidation:
    kind: str
    status: str
    complete: bool
    identity_ok: bool
    hash_status: str
    errors: list[str]


def validate_snapshot_semantics(
    snapshot: ReviewStateSnapshot | AccountReviewsSnapshot,
    *,
    expected_user_id: str | None = None,
    expected_game_id: int | None = None,
    expected_slug: str | None = None,
) -> SnapshotValidation:
    """Valida estrutura, completude, identidade e hash semântico do snapshot.

    O hash é o mesmo usado na captura; a validação nunca cria um algoritmo novo.
    """
    errors: list[str] = []
    completeness_errors: list[str] = []
    is_account = isinstance(snapshot, AccountReviewsSnapshot)
    kind = "account" if is_account else "game"
    complete = bool(snapshot.capture.complete and snapshot.pagination.complete)
    identity_ok = bool(
        snapshot.identity.verified
        and snapshot.query.user_id == snapshot.identity.user_id
        and (expected_user_id is None or snapshot.identity.user_id == expected_user_id)
    )
    if not snapshot.capture.complete:
        completeness_errors.append("capture.complete=false")
    if not snapshot.pagination.complete:
        completeness_errors.append("pagination.complete=false")
    if not snapshot.identity.verified:
        errors.append("identity.verified=false")
    if snapshot.query.user_id != snapshot.identity.user_id:
        errors.append("query.user_id diverge da identidade")
    if expected_user_id is not None and snapshot.identity.user_id != expected_user_id:
        errors.append("identidade do snapshot diverge do UID esperado")
    if snapshot.pagination.pages_fetched < 1:
        errors.append("pagination.pages_fetched < 1")
    if snapshot.pagination.records_received < 0:
        errors.append("pagination.records_received negativo")
    if snapshot.pagination.unique_records < 0:
        errors.append("pagination.unique_records negativo")
    if snapshot.pagination.records_received < snapshot.pagination.unique_records:
        errors.append("pagination.records_received menor que unique_records")

    if is_account:
        if snapshot.pagination.unique_records != len(snapshot.reviews):
            errors.append("unique_records diverge da quantidade de reviews")
        for review in snapshot.reviews:
            if review.user_id != snapshot.identity.user_id:
                errors.append("review de outro usuario no snapshot")
                break
        recomputed = canonical_account_hash(snapshot)
    else:
        if expected_game_id is not None and snapshot.query.game_id != expected_game_id:
            errors.append("game_id consultado diverge da ação")
        if expected_slug is not None and snapshot.query.slug != expected_slug:
            errors.append("slug consultado diverge da ação")
        review = snapshot.result.review
        if snapshot.result.exists != (review is not None):
            errors.append("result.exists incompatível com result.review")
        if review is not None:
            if review.game_id != snapshot.query.game_id:
                errors.append("review.game_id diverge da consulta")
            if review.user_id != snapshot.identity.user_id:
                errors.append("review.user_id diverge da identidade")
        recomputed = canonical_hash(snapshot)

    recorded = snapshot.capture.response_hash
    if not recorded or recorded == "pending":
        hash_status = "unavailable"
    elif recorded == recomputed:
        hash_status = "valid"
    else:
        hash_status = "divergent"
        errors.append("response_hash semantico divergente")

    if errors:
        status = "invalid"
    elif not complete:
        status = "incomplete"
    elif hash_status != "valid":
        status = "unverifiable"
    else:
        status = "valid"
    return SnapshotValidation(
        kind=kind,
        status=status,
        complete=complete,
        identity_ok=identity_ok,
        hash_status=hash_status,
        errors=completeness_errors + errors,
    )


@dataclass
class IdentityEvidence:
    expected_user_id: str
    session_user_id: str | None
    api_user_id: str | None
    review_query_user_id: str | None
    sources: tuple[str, ...]

    @property
    def verified(self) -> bool:
        identifiers = [
            value
            for value in (
                self.expected_user_id,
                self.session_user_id,
                self.api_user_id,
                self.review_query_user_id,
            )
            if value
        ]
        return bool(
            self.expected_user_id
            and self.session_user_id
            and self.api_user_id
            and len(set(identifiers)) == 1
        )


def verify_identity(evidence: IdentityEvidence) -> None:
    if not evidence.expected_user_id:
        raise IdentityError("JAZEREI_USER_ID ausente; configure o identificador esperado")
    if not evidence.session_user_id:
        raise IdentityError("sessao autenticada ausente ou identidade local indisponivel")
    if not evidence.api_user_id:
        raise IdentityError("a API nao retornou o identificador da conta autenticada")
    if evidence.session_user_id != evidence.api_user_id:
        raise IdentityError("identidade da sessao diverge da identidade retornada pela API")
    if evidence.api_user_id != evidence.expected_user_id:
        raise IdentityError("a conta autenticada nao corresponde ao JAZEREI_USER_ID esperado")
    if evidence.review_query_user_id and evidence.review_query_user_id != evidence.expected_user_id:
        raise IdentityError("a consulta de reviews foi feita para outro usuario")


def _read_json_body(body: Any) -> dict[str, Any] | None:
    if isinstance(body, list):
        body = body[0] if body else None
    return body if isinstance(body, dict) else None


def read_session_user_id(page: Any) -> str | None:
    """Le apenas o uid do registro de auth do Firebase no IndexedDB."""
    return page.evaluate(
        """
        async () => {
          const dbs = await indexedDB.databases();
          if (!dbs.some((db) => db.name === 'firebaseLocalStorageDb')) return null;
          return await new Promise((resolve) => {
            const request = indexedDB.open('firebaseLocalStorageDb');
            request.onerror = () => resolve(null);
            request.onsuccess = () => {
              const db = request.result;
              if (!db.objectStoreNames.contains('firebaseLocalStorage')) {
                resolve(null);
                return;
              }
              const store = db.transaction('firebaseLocalStorage', 'readonly')
                .objectStore('firebaseLocalStorage');
              const valuesRequest = store.getAll();
              valuesRequest.onsuccess = () => {
                const uidOf = (item) => {
                  if (!item) return null;
                  if (typeof item.uid === 'string') return item.uid;
                  if (item.value && typeof item.value === 'object'
                      && typeof item.value.uid === 'string') {
                    return item.value.uid;
                  }
                  if (item.value && typeof item.value === 'string') {
                    try {
                      const parsed = JSON.parse(item.value);
                      if (parsed && typeof parsed.uid === 'string') return parsed.uid;
                      if (parsed && parsed.user && typeof parsed.user.uid === 'string') {
                        return parsed.user.uid;
                      }
                    } catch (error) { /* ignora valor invalido */ }
                  }
                  return null;
                };
                for (const value of valuesRequest.result || []) {
                  const uid = uidOf(value);
                  if (uid) { resolve(uid); return; }
                }
                resolve(null);
              };
              valuesRequest.onerror = () => resolve(null);
            };
            setTimeout(() => resolve(null), 1500);
          });
        }
        """
    )


def read_profile_user_id(session: BrowserSession, *, timeout_seconds: float = 10.0) -> str | None:
    """Lê o id da conta na resposta autenticada de `get_my_profile`.

    A navegação é feita no próprio navegador para reutilizar os cabeçalhos da
    aplicação; nenhum token é extraído ou persistido.
    """
    page = session.page
    captured: dict[str, str] = {}

    def on_response(response: Any) -> None:
        try:
            if urlsplit(response.url).path != PROFILE_RPC_PATH or response.status >= 400:
                return
            body = _read_json_body(response.json())
            if body is not None and body.get("id"):
                captured["user_id"] = str(body["id"])
        except Exception:  # noqa: BLE001 - listener nao pode derrubar navegacao
            return

    page.on("response", on_response)
    try:
        session.goto(f"{session.config.jazerei_base_url}{PROFILE_PATH}")
        elapsed = 0.0
        while "user_id" not in captured and elapsed < timeout_seconds:
            session.wait(250)
            elapsed += 0.25
    finally:
        page.remove_listener("response", on_response)
    return captured.get("user_id")


def authenticate(session: BrowserSession, expected_user_id: str) -> AuthenticatedContext:
    """Confirma sessao + conta esperada + usuario retornado por get_my_profile."""
    if not expected_user_id:
        raise IdentityError("JAZEREI_USER_ID ausente; configure o identificador esperado")

    page = session.page
    profile_body: dict[str, Any] | None = None
    request_headers: dict[str, dict[str, str]] = {}
    api_base: str | None = None
    review_query_user_ids: set[str] = set()

    def on_request(request: Any) -> None:
        nonlocal api_base
        try:
            parts = urlsplit(request.url)
            if parts.path == REVIEWS_PATH and request.method == "GET":
                request_headers["reviews"] = dict(request.headers)
                api_base = f"{parts.scheme}://{parts.netloc}"
                values = parse_qs(parts.query).get("user_id", [])
                if values and values[0].startswith("eq."):
                    review_query_user_ids.add(values[0][3:])
            elif parts.path == PROFILE_RPC_PATH and request.method == "POST":
                request_headers["profile"] = dict(request.headers)
                api_base = api_base or f"{parts.scheme}://{parts.netloc}"
        except Exception:  # noqa: BLE001 - listener nao pode derrubar navegacao
            return

    def on_response(response: Any) -> None:
        nonlocal profile_body
        try:
            if urlsplit(response.url).path != PROFILE_RPC_PATH or response.status >= 400:
                return
            body = _read_json_body(response.json())
            if body is not None:
                profile_body = body
        except Exception:  # noqa: BLE001
            return

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        session.goto(f"{session.config.jazerei_base_url}{PROFILE_PATH}")
        deadline = 15.0
        elapsed = 0.0
        while profile_body is None and elapsed < deadline:
            session.wait(250)
            elapsed += 0.25
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)

    session_uid = read_session_user_id(page)
    api_uid = str(profile_body.get("id")) if profile_body and profile_body.get("id") else None
    if any(uid != expected_user_id for uid in review_query_user_ids):
        raise IdentityError("a navegacao consultou reviews de outro usuario")
    review_query_user_id = next(iter(review_query_user_ids), None)
    evidence = IdentityEvidence(
        expected_user_id=expected_user_id,
        session_user_id=session_uid,
        api_user_id=api_uid,
        review_query_user_id=review_query_user_id,
        sources=tuple(
            source
            for source, present in (
                ("firebase_indexeddb", bool(session_uid)),
                ("get_my_profile", bool(api_uid)),
                ("reviews_query", bool(review_query_user_id)),
            )
            if present
        ),
    )
    verify_identity(evidence)

    headers = request_headers.get("reviews") or request_headers.get("profile")
    headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    if not headers.get("authorization") or not headers.get("apikey"):
        raise IdentityError("nao foi possivel obter os cabecalhos da sessao autenticada")
    if not api_base:
        raise IdentityError("nao foi possivel determinar a origem da API autenticada")

    return AuthenticatedContext(
        expected_user_id=expected_user_id,
        api_user_id=api_uid or "",
        session_user_id=session_uid or "",
        api_base=api_base,
        headers=headers,
        sources=evidence.sources,
    )


def fetch_reviews_page(
    session: BrowserSession,
    context: AuthenticatedContext,
    offset: int,
    end: int,
) -> PageResponse:
    params = urlencode(
        {
            "select": "*",
            "user_id": f"eq.{context.expected_user_id}",
            "order": "created_at.desc",
        }
    )
    url = f"{context.api_base}{REVIEWS_PATH}?{params}"
    headers = dict(context.headers)
    headers["Range"] = f"{offset}-{end}"
    headers["Range-Unit"] = "items"
    response = session.context.request.get(
        url, headers=headers, timeout=int(session.config.timeout * 1000)
    )
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - corpo nao-JSON vira resposta malformada
        body = None
    return PageResponse(
        status=response.status,
        body=body,
        content_range=response.headers.get("content-range"),
    )


def paginate_reviews(
    fetch_page: Callable[[int, int], PageResponse],
    *,
    expected_user_id: str,
    page_size: int,
    max_pages: int = DEFAULT_MAX_PAGES,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PaginationResult:
    if page_size < 1 or page_size > 1000:
        raise PaginationError("page_size deve estar entre 1 e 1000")
    if max_pages < 1:
        raise PaginationError("max_pages deve ser >= 1")

    reviews: list[DestinationReview] = []
    seen_ids: set[str] = set()
    pages_fetched = 0
    records_received = 0
    duplicates = 0
    offset = 0
    previous_page_ids: list[str] | None = None
    content_range_total: int | None = None
    termination: str | None = None

    while pages_fetched < max_pages:
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("captura cancelada")
        response = fetch_page(offset, offset + page_size - 1)
        if response.status in (401, 403):
            raise IdentityError("sessao expirada ou sem autorizacao para ler reviews")
        if response.status == 416:
            raise PaginationError("servidor recusou o intervalo solicitado (HTTP 416)")
        if response.status not in (200, 206):
            raise PaginationError(f"resposta HTTP inesperada na paginacao: {response.status}")
        if not isinstance(response.body, list):
            raise PaginationError("resposta de reviews nao e uma lista JSON")
        if len(response.body) > page_size:
            raise PaginationError("servidor ignorou o tamanho de pagina solicitado")

        page_ids: list[str] = []
        for item in response.body:
            if not isinstance(item, dict):
                raise PaginationError("registro de review nao e um objeto")
            if "id" not in item or "game_id" not in item or "user_id" not in item:
                raise PaginationError("registro de review sem id, game_id ou user_id")
            page_ids.append(str(item["id"]))
        if page_ids and page_ids == previous_page_ids:
            raise PaginationError("pagina repetida durante a paginacao")

        pages_fetched += 1
        records_received += len(response.body)
        for item, review_id in zip(response.body, page_ids, strict=False):
            if str(item["user_id"]) != expected_user_id:
                raise IdentityError("registro retornado pertence a outro usuario")
            try:
                review = DestinationReview.model_validate(sanitize_review_payload(item))
            except Exception as exc:  # noqa: BLE001
                raise PaginationError("registro de review malformado") from exc
            if review_id in seen_ids:
                duplicates += 1
                continue
            seen_ids.add(review_id)
            reviews.append(review)

        total = _parse_content_range_total(response.content_range)
        if total is not None:
            content_range_total = total
        if on_page is not None:
            on_page(
                {
                    "kind": "destination",
                    "page": pages_fetched,
                    "records": len(reviews),
                    "total": total,
                }
            )

        if total is not None and offset + len(response.body) >= total:
            termination = "content_range_total"
            break
        if (
            total is not None
            and offset + len(response.body) < total
            and (not response.body or len(response.body) < page_size)
        ):
            raise PaginationError(
                "pagina vazia/curta antes do total informado; completude nao comprovada"
            )
        if len(response.body) < page_size:
            termination = "empty_page" if not response.body else "short_page"
            break
        offset += page_size
        previous_page_ids = page_ids
    else:
        raise PaginationError(
            f"paginacao nao comprovada apos {pages_fetched} paginas (max_pages={max_pages})"
        )

    if termination is None:
        raise PaginationError("paginacao terminou sem criterio comprovado")
    return PaginationResult(
        reviews=reviews,
        pages_fetched=pages_fetched,
        records_received=records_received,
        duplicates_discarded=duplicates,
        termination_reason=termination,
        content_range_total=content_range_total,
    )


def _parse_content_range_total(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("*/"):
        suffix = value[2:]
        return int(suffix) if suffix.isdigit() else None
    match = CONTENT_RANGE_RE.match(value)
    if not match or match.group(3) == "*":
        return None
    return int(match.group(3))


def build_review_snapshot(
    *,
    game_id: int,
    slug: str,
    expected_user_id: str,
    pagination: PaginationResult,
    page_size: int,
    max_pages: int,
    captured_at: str | None = None,
    identity_sources: tuple[str, ...] = (),
) -> ReviewStateSnapshot:
    matches = [review for review in pagination.reviews if review.game_id == game_id]
    if len(matches) > 1:
        raise CaptureError("mais de uma review para o mesmo game_id; captura ambigua")
    review = matches[0] if matches else None
    if review is not None and review.user_id != expected_user_id:
        raise IdentityError("review encontrada pertence a outro usuario")

    snapshot = ReviewStateSnapshot(
        query=SnapshotQuery(
            game_id=game_id,
            slug=slug,
            user_id=expected_user_id,
            endpoint=REVIEWS_PATH,
            page_size=page_size,
        ),
        identity=SnapshotIdentity(
            verified=True,
            user_id=expected_user_id,
            sources=list(identity_sources),
        ),
        result=SnapshotResult(exists=review is not None, review=review),
        pagination=SnapshotPagination(
            complete=True,
            pages_fetched=pagination.pages_fetched,
            records_received=pagination.records_received,
            unique_records=len(pagination.reviews),
            duplicates_discarded=pagination.duplicates_discarded,
            termination_reason=pagination.termination_reason,
            page_size=page_size,
            max_pages=max_pages,
            content_range_total=pagination.content_range_total,
        ),
        capture=SnapshotCapture(
            captured_at=captured_at or datetime.now(UTC).isoformat(timespec="seconds"),
            response_hash="pending",
            complete=True,
        ),
    )
    snapshot.capture.response_hash = canonical_hash(snapshot)
    return snapshot


def build_account_review_snapshot(
    *,
    expected_user_id: str,
    pagination: PaginationResult,
    page_size: int,
    max_pages: int,
    captured_at: str | None = None,
    identity_sources: tuple[str, ...] = (),
) -> AccountReviewsSnapshot:
    for review in pagination.reviews:
        if review.user_id != expected_user_id:
            raise IdentityError("review encontrada pertence a outro usuario")
    snapshot = AccountReviewsSnapshot(
        query=AccountSnapshotQuery(
            user_id=expected_user_id,
            endpoint=REVIEWS_PATH,
            page_size=page_size,
        ),
        identity=SnapshotIdentity(
            verified=True,
            user_id=expected_user_id,
            sources=list(identity_sources),
        ),
        reviews=pagination.reviews,
        pagination=SnapshotPagination(
            complete=True,
            pages_fetched=pagination.pages_fetched,
            records_received=pagination.records_received,
            unique_records=len(pagination.reviews),
            duplicates_discarded=pagination.duplicates_discarded,
            termination_reason=pagination.termination_reason,
            page_size=page_size,
            max_pages=max_pages,
            content_range_total=pagination.content_range_total,
        ),
        capture=SnapshotCapture(
            captured_at=captured_at or datetime.now(UTC).isoformat(timespec="seconds"),
            response_hash="pending",
            complete=True,
        ),
    )
    snapshot.capture.response_hash = canonical_account_hash(snapshot)
    return snapshot


def capture_reviews_window(
    session: BrowserSession,
    *,
    expected_user_id: str,
    page_size: int = 50,
    max_pages: int = DEFAULT_MAX_PAGES,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[AuthenticatedContext, PaginationResult]:
    """Autentica uma vez e pagina todas as reviews da conta."""
    context = authenticate(session, expected_user_id)
    pagination = paginate_reviews(
        lambda offset, end: fetch_reviews_page(session, context, offset, end),
        expected_user_id=expected_user_id,
        page_size=page_size,
        max_pages=max_pages,
        on_page=on_page,
        should_cancel=should_cancel,
    )
    return context, pagination


def capture_account_reviews(
    session: BrowserSession,
    *,
    expected_user_id: str,
    page_size: int = 50,
    max_pages: int = DEFAULT_MAX_PAGES,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> AccountReviewsSnapshot:
    """Captura única e paginada de todas as reviews da conta."""
    context, pagination = capture_reviews_window(
        session,
        expected_user_id=expected_user_id,
        page_size=page_size,
        max_pages=max_pages,
        on_page=on_page,
        should_cancel=should_cancel,
    )
    return build_account_review_snapshot(
        expected_user_id=expected_user_id,
        pagination=pagination,
        page_size=page_size,
        max_pages=max_pages,
        identity_sources=context.sources,
    )


def capture_review_state(
    session: BrowserSession,
    *,
    game_id: int,
    slug: str,
    expected_user_id: str,
    page_size: int = 50,
    max_pages: int = DEFAULT_MAX_PAGES,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ReviewStateSnapshot:
    if game_id < 1:
        raise CaptureError("game_id deve ser positivo")
    if not slug:
        raise CaptureError("slug obrigatorio")
    context, pagination = capture_reviews_window(
        session,
        expected_user_id=expected_user_id,
        page_size=page_size,
        max_pages=max_pages,
        on_page=on_page,
        should_cancel=should_cancel,
    )
    return build_review_snapshot(
        game_id=game_id,
        slug=slug,
        expected_user_id=expected_user_id,
        pagination=pagination,
        page_size=page_size,
        max_pages=max_pages,
        identity_sources=context.sources,
    )
