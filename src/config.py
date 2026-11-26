"""Configuracao local carregada de ambiente/.env.

Nenhum segredo e impresso ou serializado. Tokens vivem apenas em memoria e
nunca entram em relatorios.

`allow_writes` e apenas uma condicao necessaria, nunca suficiente: nao cria
transporte, nao reativa helpers e nao substitui gateway/aprovacao/confirmacao.
`enable_real_write_transport` e o segundo gate, tambem falso por padrao; ambos
precisam ser verdadeiros apenas quando o gateway construir o transporte real.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv e opcional em tempo de import para testes isolados
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in TRUTHY


@dataclass(frozen=True)
class Config:
    stash_base_url: str = "https://stash.games"
    stash_username: str = "MikeMequis1"
    jazerei_base_url: str = "https://jazerei.com"
    user_agent: str = "stash-jazerei-migration/0.1 (+local, read-only)"
    timeout: float = 30.0
    min_interval: float = 1.0
    max_retries: int = 3
    allow_writes: bool = False
    enable_real_write_transport: bool = False
    data_dir: Path = field(default_factory=lambda: Path("data"))
    browser_profile_dir: Path | None = None
    browser_headless: bool = False
    jazerei_user_id: str = ""
    jazerei_user_id_source: str = ""
    jazerei_page_size: int = 50
    operator_id: str = "local"

    @property
    def browser_dir(self) -> Path:
        return self.data_dir / "browser"

    @property
    def resolved_browser_profile(self) -> Path:
        return self.browser_profile_dir or self.browser_dir / "profile"

    @property
    def stash_dir(self) -> Path:
        return self.data_dir / "stash"

    @property
    def jazerei_dir(self) -> Path:
        return self.data_dir / "jazerei"

    @property
    def migration_dir(self) -> Path:
        return self.data_dir / "migration"


def load_config(env_file: str | Path | None = ".env") -> Config:
    if load_dotenv is not None and env_file:
        load_dotenv(env_file, override=False)

    data_dir = Path(os.getenv("MIGRATION_DATA_DIR", "data"))
    configured_user_id = os.getenv("JAZEREI_USER_ID", "")
    browser_dir = data_dir / "browser"
    from src.jazerei.identity import identity_source, resolve_expected_user_id

    return Config(
        stash_base_url=os.getenv("STASH_BASE_URL", Config.stash_base_url).rstrip("/"),
        stash_username=os.getenv("STASH_USERNAME", Config.stash_username),
        jazerei_base_url=os.getenv("JAZEREI_BASE_URL", Config.jazerei_base_url).rstrip("/"),
        user_agent=os.getenv("MIGRATION_USER_AGENT", Config.user_agent),
        timeout=float(os.getenv("MIGRATION_TIMEOUT", "30")),
        min_interval=float(os.getenv("MIGRATION_MIN_INTERVAL", "1.0")),
        max_retries=int(os.getenv("MIGRATION_MAX_RETRIES", "3")),
        allow_writes=_flag("MIGRATION_ALLOW_WRITES", False),
        enable_real_write_transport=_flag("MIGRATION_ENABLE_REAL_WRITE_TRANSPORT", False),
        data_dir=data_dir,
        browser_profile_dir=(
            Path(os.environ["MIGRATION_BROWSER_PROFILE_DIR"])
            if os.getenv("MIGRATION_BROWSER_PROFILE_DIR")
            else None
        ),
        browser_headless=_flag("MIGRATION_BROWSER_HEADLESS", False),
        jazerei_user_id=resolve_expected_user_id(
            configured=configured_user_id,
            browser_dir=browser_dir,
        ),
        jazerei_user_id_source=(
            identity_source(configured=configured_user_id, browser_dir=browser_dir) or ""
        ),
        jazerei_page_size=int(os.getenv("JAZEREI_PAGE_SIZE", "50")),
        operator_id=(os.getenv("MIGRATION_OPERATOR", "local").strip() or "local"),
    )
