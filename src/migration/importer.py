"""Resumo de pré-condições do plano. Somente leitura.

Este modulo nao executa escritas e nao possui cliente mutavel. A unica
fronteira autorizada para uma futura escrita e o gateway de execucao.
`allowed` permanece sempre falso por construcao.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.config import Config
from src.migration.validator import Severity, validate_plan
from src.models.normalized import MigrationPlan


class ImportSummary(BaseModel):
    allowed: bool = False
    executed: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def preflight(plan: MigrationPlan, config: Config) -> ImportSummary:
    """Resume bloqueios; nunca autoriza execucao, mesmo com allow_writes=True."""
    summary = ImportSummary(allowed=False)
    if not config.allow_writes:
        summary.errors.append(
            "MIGRATION_ALLOW_WRITES nao esta habilitado; modo read-only permanece ativo"
        )
    summary.errors.append(
        "escrita indisponivel: operacoes mutaveis passam exclusivamente pelo gateway"
    )
    if not plan.approved:
        summary.errors.append("plano nao aprovado")
    issues = validate_plan(plan)
    summary.errors.extend(
        f"{issue.code}: {issue.message}" for issue in issues if issue.severity is Severity.ERROR
    )
    summary.warnings.extend(
        f"{issue.code}: {issue.message}" for issue in issues if issue.severity is Severity.WARNING
    )
    summary.skipped = len(plan.skipped) + len(plan.unmatched)
    summary.allowed = False
    return summary
