"""Autenticacao do Ja Zerei.

Tokens sao lidos do ambiente e mantidos apenas em memoria. Nunca logar,
serializar ou incluir em relatorios. O cabecalho exigido por endpoint ainda
nao foi confirmado; a configuracao padrao e Bearer + apikey.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace


class MissingAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class Auth:
    api_key: str = field(default="", repr=False)
    user_id: str = ""
    id_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> Auth:
        return cls(
            api_key=os.getenv("JAZEREI_FIREBASE_API_KEY", ""),
            user_id=os.getenv("JAZEREI_USER_ID", ""),
            id_token=os.getenv("JAZEREI_ID_TOKEN", ""),
            refresh_token=os.getenv("JAZEREI_REFRESH_TOKEN", ""),
        )

    def require_token(self) -> str:
        if not self.id_token:
            raise MissingAuthError(
                "JAZEREI_ID_TOKEN ausente. Configure .env local; nunca versione tokens."
            )
        return self.id_token

    def require_user_id(self) -> str:
        if not self.user_id:
            raise MissingAuthError("JAZEREI_USER_ID ausente.")
        return self.user_id

    def headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.require_token()}"}
        if self.api_key:
            headers["apikey"] = self.api_key
        return headers

    def with_id_token(self, token: str) -> Auth:
        return replace(self, id_token=token)


def refresh_id_token(auth: Auth, http) -> Auth:
    """Troca refresh_token por id_token via Firebase Secure Token.

    Nao implementado no scaffold: depende de contrato de autenticacao validado.
    """
    raise NotImplementedError(
        "Refresh de token ainda nao implementado; use sessao legitima e .env local."
    )
