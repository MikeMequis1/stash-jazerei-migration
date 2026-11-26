"""Verificacao pos-escrita: compara o estado final com o plano aprovado."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.models.destination import DestinationReview
from src.models.normalized import MigrationPlan


class VerificationReport(BaseModel):
    checked: int = 0
    verified: int = 0
    missing: list[str] = Field(default_factory=list)
    mismatched: list[str] = Field(default_factory=list)
    details: list[dict[str, object]] = Field(default_factory=list)


def verify_plan(
    plan: MigrationPlan, destination_reviews: list[DestinationReview]
) -> VerificationReport:
    report = VerificationReport()
    by_game = {review.game_id: review for review in destination_reviews}

    for action in plan.actions:
        if action.kind != "review" or not action.approved:
            continue
        report.checked += 1
        label = f"review:{action.source_ref}"
        actual = by_game.get(action.game_id or -1)
        expected = action.after or {}

        if actual is None:
            report.missing.append(label)
            report.details.append({"action": label, "status": "missing"})
            continue

        differences: list[str] = []
        expected_text = str(expected.get("text") or "")
        if actual.text != expected_text:
            differences.append("text")
        expected_rating = expected.get("rating")
        if expected_rating is not None and float(actual.rating or 0) != float(expected_rating):
            differences.append("rating")

        if differences:
            report.mismatched.append(label)
            report.details.append({"action": label, "status": "mismatch", "fields": differences})
        else:
            report.verified += 1
            report.details.append({"action": label, "status": "verified"})

    return report
