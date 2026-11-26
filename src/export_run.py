"""Classificacao de resultado de uma execucao de coleta.

Separa coleta completa, dirigida, vazia, parcial e malsucedida. Uma lista vazia
so e considerada evidencia quando todas as paginas do escopo foram consultadas
com sucesso.
"""

from __future__ import annotations

from enum import StrEnum

from src.models.source import ExportReport


class RunOutcome(StrEnum):
    COMPLETE = "complete"
    DIRECTED = "directed"
    EMPTY = "empty"
    PARTIAL = "partial"
    TARGET_NOT_FOUND = "target_not_found"
    FAILED = "failed"


EXIT_CODES = {
    RunOutcome.COMPLETE: 0,
    RunOutcome.DIRECTED: 0,
    RunOutcome.EMPTY: 0,
    RunOutcome.PARTIAL: 1,
    RunOutcome.TARGET_NOT_FOUND: 2,
    RunOutcome.FAILED: 2,
}


def classify_kind(report: ExportReport) -> RunOutcome:
    """Classifica o resultado de um tipo de coleta (games ou reviews)."""
    if report.pages_fetched == 0:
        return RunOutcome.FAILED
    if not report.requested_scope_complete:
        return RunOutcome.PARTIAL
    if report.target_slug and report.target_found is False:
        if report.directed or report.complete:
            return RunOutcome.TARGET_NOT_FOUND
        return RunOutcome.PARTIAL
    if report.directed:
        if report.records == 0 and report.target_slug is None:
            return RunOutcome.EMPTY
        return RunOutcome.DIRECTED
    if report.complete:
        return RunOutcome.COMPLETE if report.records > 0 else RunOutcome.EMPTY
    return RunOutcome.PARTIAL


def summarize(reports: list[ExportReport]) -> tuple[RunOutcome, int]:
    """Combina os resultados dos tipos coletados em um codigo de saida."""
    if not reports:
        return RunOutcome.FAILED, EXIT_CODES[RunOutcome.FAILED]

    outcomes = [classify_kind(report) for report in reports]
    if all(outcome is RunOutcome.FAILED for outcome in outcomes):
        final = RunOutcome.FAILED
    elif any(outcome is RunOutcome.FAILED for outcome in outcomes):
        final = RunOutcome.PARTIAL
    elif any(outcome is RunOutcome.TARGET_NOT_FOUND for outcome in outcomes):
        final = RunOutcome.TARGET_NOT_FOUND
    elif any(outcome is RunOutcome.PARTIAL for outcome in outcomes):
        final = RunOutcome.PARTIAL
    elif any(outcome is RunOutcome.DIRECTED for outcome in outcomes):
        final = RunOutcome.DIRECTED
    elif all(outcome is RunOutcome.EMPTY for outcome in outcomes):
        final = RunOutcome.EMPTY
    else:
        final = RunOutcome.COMPLETE
    return final, EXIT_CODES[final]


def should_write_records(report: ExportReport) -> bool:
    """Evita gravar lista vazia quando nao ha evidencia de consulta valida."""
    outcome = classify_kind(report)
    if outcome is RunOutcome.FAILED:
        return False
    if outcome is RunOutcome.TARGET_NOT_FOUND:
        return False
    if report.records > 0:
        return True
    return outcome is RunOutcome.EMPTY
