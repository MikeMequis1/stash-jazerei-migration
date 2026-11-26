"""Perfil do Ja Zerei: somente leitura.

Atualizacao de perfil nao e suportada pelo gateway nesta fase e nao existe
helper mutavel neste modulo.
"""

from __future__ import annotations

from typing import Any

from src.jazerei.client import JazereiClient
from src.models.destination import DestinationProfile


def get_profile(client: JazereiClient) -> tuple[DestinationProfile, dict[str, Any]]:
    payload = client.post_read_json("/rest/v1/rpc/get_my_profile", {})
    return parse_profile_payload(payload)


def parse_profile_payload(payload: Any) -> tuple[DestinationProfile, dict[str, Any]]:
    raw = _unwrap(payload)
    return DestinationProfile.model_validate(raw), raw


def _unwrap(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if isinstance(payload, dict):
        inner = payload.get("profile")
        if isinstance(inner, dict):
            return inner
        return payload
    raise ValueError("get_my_profile retornou formato inesperado")
