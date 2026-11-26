"""Politica de rede do navegador: leitura permitida, escrita bloqueada.

A decisao considera metodo, host, endpoint e finalidade. GET nao e tratado
como inofensivo por definicao: URLs com aparencia de acao (logout, delete,
remove) sao bloqueadas. POST desconhecido tambem e bloqueado e registrado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

SAFE_RPCS = frozenset(
    {
        "get_my_profile",
        "get_profile_watchlist",
        "get_game_watchlist_count",
        "get_public_user_by_username",
        "get_user_external_accounts_public",
        "get_user_activity_heatmap",
        "check_veterano_badge",
        "list_user_external_library",
        "count_user_external_library",
        "get_profile_watchlist_facets",
    }
)

SAFE_POST_PATHS = frozenset({"/api/search/games", "/api/igdb/query"})

READ_RPC_PREFIXES = ("get_", "public_get_", "list_", "count_", "check_")
READ_RPC_SUFFIXES = ("_exists", "_count", "_status")


def is_read_rpc(name: str) -> bool:
    if name in SAFE_RPCS:
        return True
    if any(marker in f"/{name}" for marker in WRITE_PATH_MARKERS):
        return False
    return name.startswith(READ_RPC_PREFIXES) or name.endswith(READ_RPC_SUFFIXES)


DANGEROUS_PATH_MARKERS = (
    "/logout",
    "/signout",
    "/delete",
    "/remove",
    "/unwatch",
    "/unfollow",
    "/cancel",
    "/upsert",
    "/insert",
    "/update",
)

WRITE_PATH_MARKERS = (
    "/upsert",
    "/insert",
    "/update",
    "/delete",
    "/remove",
    "/user_watchlist_games",
    "/favorite",
    "/playing",
)

AUTH_HOSTS = frozenset({"securetoken.googleapis.com", "identitytoolkit.googleapis.com"})
GOOGLE_AUTH_HOSTS = frozenset(
    {"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com"}
)
SUPABASE_SUFFIXES = (".supabase.co",)


def _is_google_auth_post(host: str, path: str) -> bool:
    if host == "accounts.google.com":
        return True
    if host == "oauth2.googleapis.com":
        return path.startswith("/token") or path.startswith("/oauth2")
    if host == "www.googleapis.com":
        return path.startswith(("/oauth2/", "/gsi/", "/identitytoolkit/"))
    if host == "www.google.com":
        return path.startswith(("/recaptcha/", "/accounts/"))
    return False


class RequestCategory(StrEnum):
    READ = "read"
    AUTH = "auth"
    ASSET = "asset"
    CHALLENGE = "challenge"
    BLOCKED_WRITE = "blocked_write"
    BLOCKED_UNKNOWN = "blocked_unknown"
    BLOCKED_SIDE_EFFECT = "blocked_side_effect"


@dataclass(frozen=True)
class RequestDecision:
    allowed: bool
    category: RequestCategory
    reason: str


@dataclass
class RequestLogEntry:
    method: str
    url: str
    category: str
    allowed: bool
    reason: str


@dataclass
class RequestLog:
    entries: list[RequestLogEntry] = field(default_factory=list)
    max_entries: int = 200

    def add(self, method: str, url: str, decision: RequestDecision) -> None:
        self.entries.append(
            RequestLogEntry(
                method=method,
                url=sanitize_url(url),
                category=decision.category.value,
                allowed=decision.allowed,
                reason=decision.reason,
            )
        )
        if len(self.entries) > self.max_entries:
            del self.entries[: len(self.entries) - self.max_entries]

    @property
    def blocked(self) -> list[RequestLogEntry]:
        return [entry for entry in self.entries if not entry.allowed]

    def summary(self) -> dict[str, Any]:
        return {
            "observed": len(self.entries),
            "allowed": len(self.entries) - len(self.blocked),
            "blocked": len(self.blocked),
            "blocked_entries": [entry.__dict__ for entry in self.blocked[-20:]],
        }


def sanitize_url(url: str) -> str:
    """Remove query/fragment para nao registrar tokens ou parametros sensiveis."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else parts.path


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().split(":")[0]


def _path(url: str) -> str:
    return urlsplit(url).path


def classify(method: str, url: str, *, allow_login: bool = False) -> RequestDecision:
    method = method.upper()
    host = _host(url)
    path = _path(url)
    lowered = path.lower()

    if method in {"DELETE", "PATCH", "PUT"}:
        return RequestDecision(False, RequestCategory.BLOCKED_WRITE, f"{method} sempre bloqueado")

    if method in {"GET", "HEAD", "OPTIONS"}:
        if any(marker in lowered for marker in DANGEROUS_PATH_MARKERS):
            return RequestDecision(
                False,
                RequestCategory.BLOCKED_SIDE_EFFECT,
                "GET com aparencia de acao; bloqueado por seguranca",
            )
        if path.startswith("/rest/v1/") or path.startswith("/api/"):
            return RequestDecision(True, RequestCategory.READ, "leitura de API")
        if host.endswith(SUPABASE_SUFFIXES):
            return RequestDecision(True, RequestCategory.READ, "leitura Supabase")
        return RequestDecision(True, RequestCategory.ASSET, "leitura de pagina/recurso")

    if method != "POST":
        return RequestDecision(
            False, RequestCategory.BLOCKED_UNKNOWN, f"metodo {method} nao reconhecido"
        )

    if host in {
        "stash.games",
        "www.stash.games",
        "jazerei.com",
        "www.jazerei.com",
        "challenges.cloudflare.com",
    } and path.startswith("/cdn-cgi/challenge-platform/"):
        return RequestDecision(
            True, RequestCategory.CHALLENGE, "desafio anti-bot do provedor de borda"
        )

    if any(marker in lowered for marker in WRITE_PATH_MARKERS):
        return RequestDecision(
            False, RequestCategory.BLOCKED_WRITE, "endpoint de escrita bloqueado"
        )

    if host in AUTH_HOSTS:
        if host == "securetoken.googleapis.com" and path == "/v1/token":
            return RequestDecision(True, RequestCategory.AUTH, "refresh de token Firebase")
        if host == "identitytoolkit.googleapis.com" and "accounts:lookup" in lowered:
            return RequestDecision(True, RequestCategory.AUTH, "leitura de conta Firebase")
        if (
            host == "identitytoolkit.googleapis.com"
            and allow_login
            and "accounts:signin" in lowered
        ):
            return RequestDecision(True, RequestCategory.AUTH, "login Firebase manual")
        return RequestDecision(False, RequestCategory.BLOCKED_WRITE, "operacao de conta bloqueada")

    if allow_login and _is_google_auth_post(host, path):
        return RequestDecision(True, RequestCategory.AUTH, "autenticacao Google manual")

    if host.endswith(SUPABASE_SUFFIXES):
        if path.startswith("/rest/v1/rpc/"):
            rpc_name = path.rsplit("/", 1)[-1]
            if is_read_rpc(rpc_name):
                return RequestDecision(
                    True, RequestCategory.READ, f"RPC de leitura Supabase: {rpc_name}"
                )
        return RequestDecision(False, RequestCategory.BLOCKED_WRITE, "escrita Supabase bloqueada")

    if path in SAFE_POST_PATHS:
        return RequestDecision(True, RequestCategory.READ, "pesquisa de catalogo")

    if path.startswith("/rest/v1/rpc/"):
        rpc_name = path.rsplit("/", 1)[-1]
        if is_read_rpc(rpc_name):
            return RequestDecision(True, RequestCategory.READ, f"RPC de leitura: {rpc_name}")
        return RequestDecision(
            False, RequestCategory.BLOCKED_WRITE, f"RPC nao permitida: {rpc_name}"
        )

    return RequestDecision(
        False, RequestCategory.BLOCKED_UNKNOWN, "POST desconhecido; bloqueado por seguranca"
    )


def install_policy(context: Any, log: RequestLog, *, allow_login: bool = False) -> None:
    """Instala handler de rota que aborta qualquer requisicao nao classificada como leitura."""

    def handler(route: Any, request: Any) -> None:
        decision = classify(request.method, request.url, allow_login=allow_login)
        log.add(request.method, request.url, decision)
        if decision.allowed:
            route.continue_()
        else:
            route.abort("blockedbyclient")

    context.route("**/*", handler)
