"""Validacao do plano antes de qualquer escrita."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.migration.planner import MAX_FAVORITES, MAX_PLAYING_NOW
from src.models.destination import DestinationReview
from src.models.normalized import LikedStatus, MigrationPlan


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    source_ref: str | None = None


class PlanValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        errors = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
        super().__init__(f"plano invalido: {errors}")


def validate_plan(
    plan: MigrationPlan,
    destination_reviews: list[DestinationReview] | None = None,
    *,
    require_approval: bool = True,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    existing_by_game = {review.game_id: review for review in destination_reviews or []}
    seen_reviews: set[int] = set()
    seen_actions: set[tuple[str, int | None, str]] = set()

    if require_approval and not plan.approved:
        issues.append(
            ValidationIssue(
                severity=Severity.ERROR,
                code="plan_not_approved",
                message="o plano inteiro precisa de aprovacao explicita",
            )
        )

    for action in plan.actions:
        label = f"{action.kind}:{action.source_ref}"
        action_key = (action.kind, action.game_id, action.source_ref)
        if action_key in seen_actions:
            issues.append(
                ValidationIssue(
                    severity=Severity.ERROR,
                    code="duplicate_action",
                    message="acao duplicada no plano",
                    source_ref=label,
                )
            )
        seen_actions.add(action_key)
        if require_approval and not action.approved:
            issues.append(
                ValidationIssue(
                    severity=Severity.ERROR,
                    code="action_not_approved",
                    message="acao sem aprovacao explicita",
                    source_ref=label,
                )
            )
        if action.game_id is None:
            issues.append(
                ValidationIssue(
                    severity=Severity.ERROR,
                    code="missing_game_id",
                    message="acao sem game_id de destino",
                    source_ref=label,
                )
            )

        if action.kind == "review":
            after = action.after or {}
            rating = after.get("rating")
            if rating is not None and not 0 <= float(rating) <= 10:
                issues.append(
                    ValidationIssue(
                        severity=Severity.ERROR,
                        code="rating_out_of_range",
                        message=f"nota fora de 0..10: {rating}",
                        source_ref=label,
                    )
                )
            if action.liked.status is LikedStatus.UNRESOLVED:
                issues.append(
                    ValidationIssue(
                        severity=Severity.ERROR,
                        code="liked_unresolved",
                        message="recomendacao liked sem decisao humana explicita",
                        source_ref=label,
                    )
                )
            elif action.liked.status is LikedStatus.STALE:
                issues.append(
                    ValidationIssue(
                        severity=Severity.ERROR,
                        code="liked_decision_stale",
                        message="decisao de liked nao corresponde a versao atual da review de origem",
                        source_ref=label,
                    )
                )
            elif action.liked.status is LikedStatus.REQUIRES_RECONFIRMATION:
                issues.append(
                    ValidationIssue(
                        severity=Severity.ERROR,
                        code="liked_requires_reconfirmation",
                        message="decisao automatica de liked exige reconfirmacao humana",
                        source_ref=label,
                    )
                )
            elif action.liked.status is LikedStatus.DECIDED and (
                not isinstance(action.liked.value, bool) or after.get("liked") != action.liked.value
            ):
                issues.append(
                    ValidationIssue(
                        severity=Severity.ERROR,
                        code="liked_value_mismatch",
                        message="valor de liked nao coincide com a decisao humana",
                        source_ref=label,
                    )
                )
            if action.game_id in seen_reviews:
                issues.append(
                    ValidationIssue(
                        severity=Severity.ERROR,
                        code="duplicate_review_action",
                        message="mais de uma review para o mesmo game_id",
                        source_ref=label,
                    )
                )
            seen_reviews.add(action.game_id)
            existing = existing_by_game.get(action.game_id) if action.game_id else None
            if existing is not None and action.before is None:
                issues.append(
                    ValidationIssue(
                        severity=Severity.ERROR,
                        code="undetected_existing_review",
                        message="review existente no destino nao foi capturada em before",
                        source_ref=label,
                    )
                )

    favorites = [action for action in plan.actions if action.kind == "favorite"]
    if len(favorites) > MAX_FAVORITES:
        issues.append(
            ValidationIssue(
                severity=Severity.ERROR,
                code="favorites_limit",
                message=f"favoritos excedem {MAX_FAVORITES}",
            )
        )
    playing_now = [action for action in plan.actions if action.kind == "playing_now"]
    if len(playing_now) > MAX_PLAYING_NOW:
        issues.append(
            ValidationIssue(
                severity=Severity.ERROR,
                code="playing_now_limit",
                message=f"Playing Now excede {MAX_PLAYING_NOW}",
            )
        )

    if plan.approved and not plan.actions:
        issues.append(
            ValidationIssue(
                severity=Severity.WARNING,
                code="empty_plan",
                message="plano aprovado sem acoes",
            )
        )

    return issues


def blocking_errors(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    return [issue for issue in issues if issue.severity is Severity.ERROR]


def assert_valid(
    plan: MigrationPlan, destination_reviews: list[DestinationReview] | None = None
) -> None:
    issues = blocking_errors(validate_plan(plan, destination_reviews))
    if issues:
        raise PlanValidationError(issues)
