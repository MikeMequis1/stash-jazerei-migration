"""Reviews no Ja Zerei: leitura e upsert guardado."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from src.jazerei.client import JazereiClient
from src.models.destination import DestinationReview

SENSITIVE_KEY_PARTS = (
    "token",
    "authorization",
    "cookie",
    "apikey",
    "api_key",
    "secret",
    "password",
)
SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "id_token",
    "refresh_token",
    "auth",
    "authorization",
    "apikey",
    "api_key",
    "key",
    "sig",
    "signature",
}
DROP_REVIEW_KEYS = {
    "user_avatar",
    "avatar_url",
    "email",
    "nome",
    "family_name",
    "given_name",
    "phone_number",
    "data_nascimento",
    "moderated_by",
}


def sanitize_url(value: str) -> str:
    """Remove parametros de credencial de URLs antes de persistir dados."""
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in SENSITIVE_QUERY_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: sanitize_value(item)
            for key, item in value.items()
            if not any(part in key.lower() for part in SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return sanitize_url(value)
    return value


def sanitize_review_payload(item: dict[str, Any]) -> dict[str, Any]:
    sanitized = sanitize_value(item)
    sanitized = {
        key: value for key, value in sanitized.items() if key.lower() not in DROP_REVIEW_KEYS
    }
    if sanitized.get("text") is None:
        sanitized["text"] = ""
    return sanitized


def list_reviews(client: JazereiClient, user_id: str | None = None) -> list[DestinationReview]:
    uid = user_id or client.auth.require_user_id()
    path = f"/rest/v1/reviews?select=*&user_id=eq.{quote(uid, safe='')}&order=created_at.desc"
    payload = client.get_json(path)
    return parse_reviews_payload(payload)


def parse_reviews_payload(payload: Any) -> list[DestinationReview]:
    """Normaliza GET /rest/v1/reviews para uso HTTP e Browser."""
    if not isinstance(payload, list):
        return []
    reviews: list[DestinationReview] = []
    for item in payload:
        if isinstance(item, dict) and "game_id" in item:
            reviews.append(DestinationReview.model_validate(sanitize_review_payload(item)))
    return reviews
