"""Motor central de elegibilidade das avaliacoes para um futuro plano."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.models.normalized import GameMatch, LikedState, LikedStatus, MatchDecision

BATCH_MIN_CONFIDENCE = 0.90
BATCH_REASONS = frozenset(
    {
        "slug identico",
        "titulo normalizado identico",
        "ano identico",
    }
)


class BlockReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    label: str


class EligibilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eligible: bool = False
    reasons: list[BlockReason] = Field(default_factory=list)
    batch_eligible: bool = False
    batch_reasons: list[BlockReason] = Field(default_factory=list)


def _reason(code: str, label: str) -> BlockReason:
    return BlockReason(code=code, label=label)


def evaluate_eligibility(
    *,
    match: GameMatch | None,
    liked: LikedState | dict | None,
    special_edition: bool,
    duplicate_title: bool,
    edition_confirmed: bool,
    destination_present: bool,
    history_conflict: bool,
    evaluation_current: bool,
    source_hash_current: bool,
) -> EligibilityResult:
    """Aplica todos os bloqueios, sem depender da UI nem do planner."""
    reasons: list[BlockReason] = []
    if isinstance(liked, dict):
        liked_value = liked.get("value")
        try:
            liked_status = LikedStatus(str(liked.get("status") or "unresolved"))
        except ValueError:
            liked_status = LikedStatus.UNRESOLVED
        liked = LikedState(
            value=liked_value if isinstance(liked_value, bool) else None,
            status=liked_status,
        )
    if match is None or match.dest_game_id is None:
        reasons.append(_reason("missing_match", "Sem candidato de destino."))
        return EligibilityResult(eligible=False, reasons=reasons)

    if match.lookup_status in {"inconclusive", "error"}:
        reasons.append(_reason("inconclusive", "Consulta ao catálogo inconclusiva."))
    if match.lookup_status == "no_candidate":
        reasons.append(_reason("no_candidate", "Sem candidato no catálogo."))
    if match.decision is not MatchDecision.APPROVED:
        reasons.append(_reason("missing_approval", "Falta aprovar a correspondência."))
    if special_edition and not edition_confirmed:
        reasons.append(_reason("edition_unconfirmed", "Falta confirmar a edição especial."))
    if destination_present:
        reasons.append(_reason("destination_present", "Já existe no destino; não será reenviada."))
    if history_conflict:
        reasons.append(_reason("history_conflict", "Histórico inconsistente com o estado atual."))
    if not evaluation_current:
        reasons.append(_reason("run_mismatch", "Decisão pertence a outra versão das avaliações."))
    if not source_hash_current:
        reasons.append(_reason("source_changed", "A origem mudou desde a decisão."))

    if liked is None or liked.status is LikedStatus.UNRESOLVED:
        reasons.append(_reason("liked_unresolved", "Falta decidir liked."))
    elif liked.status is LikedStatus.REQUIRES_RECONFIRMATION:
        reasons.append(
            _reason(
                "liked_requires_reconfirmation", "Decisão automática exige reconfirmação humana."
            )
        )
    elif liked.status is LikedStatus.STALE:
        reasons.append(_reason("liked_stale", "Decisão de liked desatualizada."))
    elif liked.status is LikedStatus.DECIDED and type(liked.value) is not bool:
        reasons.append(_reason("liked_invalid", "Decisão de liked sem valor booleano."))

    batch_reasons: list[BlockReason] = []
    if match.decision is not MatchDecision.CANDIDATE:
        batch_reasons.append(_reason("not_candidate", "Não é um candidato pendente."))
    if special_edition:
        batch_reasons.append(
            _reason("special_edition", "Edição especial exige revisão individual.")
        )
    if duplicate_title:
        batch_reasons.append(_reason("duplicate_title", "Título duplicado na origem."))
    if destination_present:
        batch_reasons.append(_reason("destination_present", "Já existe no destino."))
    if history_conflict:
        batch_reasons.append(_reason("history_conflict", "Conflito histórico."))
    if match.lookup_status in {"inconclusive", "error", "no_candidate", "manual_review"}:
        batch_reasons.append(_reason("not_strong", "Consulta não conclusiva."))
    if match.confidence < BATCH_MIN_CONFIDENCE:
        batch_reasons.append(_reason("low_confidence", "Confiança abaixo do critério de lote."))
    missing = BATCH_REASONS - set(match.reasons)
    if missing:
        batch_reasons.append(
            _reason("weak_evidence", "Evidência insuficiente: " + ", ".join(sorted(missing)))
        )
    if not evaluation_current or not source_hash_current:
        batch_reasons.append(
            _reason("stale_source", "Versão da avaliação ou origem desatualizada.")
        )

    return EligibilityResult(
        eligible=not reasons,
        reasons=reasons,
        batch_eligible=not batch_reasons,
        batch_reasons=batch_reasons,
    )
