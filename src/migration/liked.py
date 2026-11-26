"""Decisoes humanas para `liked` e resolucao vinculada a versao da origem.

O registro e append-only: uma nova decisao nunca apaga a anterior. A decisao
mais recente de um par (source_ref, game_id) e usada apenas se o hash da review
de origem ainda corresponder ao hash registrado.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.models.normalized import LikedDecision, LikedState, LikedStatus
from src.models.source import ReviewRecord
from src.storage import utc_now


class DecisionError(RuntimeError):
    pass


class DecisionExistsError(DecisionError):
    pass


def review_version_hash(review: ReviewRecord) -> str:
    """Hash canonico do registro de origem usado na decisao.

    `evaluation_kind` fica fora do hash para que os hashes historicos das
    reviews escritas continuem validos apos a introducao do campo.
    """
    payload = review.model_dump(mode="json", exclude={"evaluation_kind"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_decision_id(
    *,
    source_ref: str,
    game_id: int | None,
    source_hash: str,
    value: bool | None,
    decided_at: str,
    supersedes: str | None,
    source: str = "human",
    event_type: str = "decision",
    binding_status: str = "bound",
    operator_id: str | None = None,
    schema_version: int = 1,
) -> str:
    payload = {
        "schema_version": schema_version,
        "source_ref": source_ref,
        "game_id": game_id,
        "source_hash": source_hash,
        "value": value,
        "source": source,
        "decided_at": decided_at,
        "supersedes": supersedes,
        "event_type": event_type,
        "binding_status": binding_status,
        "operator_id": operator_id,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_decisions(path: Path) -> list[LikedDecision]:
    if not path.exists():
        return []
    decisions: list[LikedDecision] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            decisions.append(LikedDecision.model_validate(payload))
        except Exception as exc:  # noqa: BLE001
            raise DecisionError(f"decisao invalida na linha {line_number}") from exc
    return decisions


def append_decision(
    path: Path,
    *,
    source_ref: str,
    game_id: int | None,
    source_hash: str,
    value: bool,
    replace: bool = False,
    source: str = "human",
    decided_at: str | None = None,
    event_type: str = "decision",
    evaluation_kind: str | None = None,
    evaluation_run_id: str | None = None,
    evaluations_sha256: str | None = None,
    operator_id: str = "local",
    destination_user_id: str | None = None,
    previous_value: bool | None = None,
    previous_status: str | None = None,
    reason: str = "",
) -> LikedDecision:
    if not source_ref:
        raise DecisionError("source_ref obrigatorio")
    if game_id is not None and game_id < 1:
        raise DecisionError("game_id deve ser positivo quando informado")
    if not _valid_hash(source_hash):
        raise DecisionError("source_hash deve ser SHA-256 hexadecimal")
    if not isinstance(value, bool):
        raise DecisionError("value deve ser booleano")
    if source not in {"human", "automatic"}:
        raise DecisionError("source deve ser human ou automatic")
    if event_type not in {"decision", "reconfirmation", "binding"}:
        raise DecisionError("event_type invalido")

    decisions = load_decisions(path)
    matching = [
        decision
        for decision in decisions
        if decision.source_ref == source_ref
        and (decision.game_id == game_id or decision.game_id is None or game_id is None)
    ]
    latest = matching[-1] if matching else None
    if latest is not None and latest.source_hash == source_hash and not replace:
        raise DecisionExistsError(
            "ja existe decisao para esta review e versao de origem; use --replace para registrar revisao"
        )

    decided_at = decided_at or utc_now()
    supersedes = latest.decision_id if latest else None
    binding_status = "bound" if game_id is not None else "pending_identity"
    decision_id = compute_decision_id(
        source_ref=source_ref,
        game_id=game_id,
        source_hash=source_hash,
        value=value,
        decided_at=decided_at,
        supersedes=supersedes,
        source=source,
        event_type=event_type,
        binding_status=binding_status,
        operator_id=operator_id or "local",
    )
    decision = LikedDecision(
        decision_id=decision_id,
        source_ref=source_ref,
        game_id=game_id,
        source_hash=source_hash,
        value=value,
        source=source,
        decided_at=decided_at,
        supersedes=supersedes,
        event_type=event_type,  # type: ignore[arg-type]
        binding_status=binding_status,  # type: ignore[arg-type]
        evaluation_kind=evaluation_kind,  # type: ignore[arg-type]
        evaluation_run_id=evaluation_run_id,
        evaluations_sha256=evaluations_sha256,
        operator_id=operator_id or "local",
        destination_user_id=destination_user_id,
        previous_value=previous_value
        if previous_value is not None
        else (latest.value if latest else None),
        previous_status=previous_status or (latest.event_type if latest else None),
        reason=reason.strip(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(decision.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return decision


def append_deferral(
    path: Path,
    *,
    source_ref: str,
    source_hash: str,
    decided_at: str | None = None,
    evaluation_kind: str | None = None,
    evaluation_run_id: str | None = None,
    evaluations_sha256: str | None = None,
    operator_id: str = "local",
    destination_user_id: str | None = None,
    reason: str = "",
) -> LikedDecision | None:
    decisions = load_decisions(path)
    matching = [decision for decision in decisions if decision.source_ref == source_ref]
    latest = matching[-1] if matching else None
    if latest is None:
        return None
    decided_at = decided_at or utc_now()
    binding_status = latest.binding_status
    decision_id = compute_decision_id(
        source_ref=source_ref,
        game_id=latest.game_id,
        source_hash=source_hash,
        value=None,
        decided_at=decided_at,
        supersedes=latest.decision_id,
        source="human",
        event_type="deferral",
        binding_status=binding_status,
        operator_id=operator_id or "local",
    )
    decision = LikedDecision(
        decision_id=decision_id,
        source_ref=source_ref,
        game_id=latest.game_id,
        source_hash=source_hash,
        value=None,
        source="human",
        decided_at=decided_at,
        supersedes=latest.decision_id,
        event_type="deferral",
        binding_status=binding_status,  # type: ignore[arg-type]
        evaluation_kind=evaluation_kind,  # type: ignore[arg-type]
        evaluation_run_id=evaluation_run_id,
        evaluations_sha256=evaluations_sha256,
        operator_id=operator_id or "local",
        destination_user_id=destination_user_id,
        previous_value=latest.value,
        previous_status=latest.event_type,
        reason=reason.strip(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(decision.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return decision


def resolve_liked(
    decisions: list[LikedDecision],
    *,
    source_ref: str,
    game_id: int | None,
    source_hash: str,
) -> LikedState:
    """Resolve o estado sem reutilizar decisao de outra versao/jogo.

    Decisoes source-only (`game_id=None`) continuam validas enquanto o
    `source_hash` nao mudar. Decisoes vinculadas a outro `game_id` ficam stale.
    """
    matching = [decision for decision in decisions if decision.source_ref == source_ref]
    base = {
        "source_ref": source_ref,
        "game_id": game_id,
        "source_hash": source_hash,
    }
    if not matching:
        return LikedState(status=LikedStatus.UNRESOLVED, **base)
    latest = matching[-1]
    if latest.source_hash != source_hash:
        return LikedState(
            status=LikedStatus.STALE,
            source=latest.source,
            decision_id=latest.decision_id,
            decided_at=latest.decided_at,
            event_type=latest.event_type,
            binding_status=latest.binding_status,
            **base,
        )
    if latest.event_type == "deferral" or latest.value is None:
        return LikedState(
            status=LikedStatus.UNRESOLVED,
            source=latest.source,
            decision_id=latest.decision_id,
            decided_at=latest.decided_at,
            event_type=latest.event_type,
            binding_status=latest.binding_status,
            **base,
        )
    if latest.game_id is not None and game_id is not None and latest.game_id != game_id:
        return LikedState(
            status=LikedStatus.UNRESOLVED,
            source=latest.source,
            decision_id=latest.decision_id,
            decided_at=latest.decided_at,
            event_type=latest.event_type,
            binding_status="stale",
            **base,
        )
    if latest.source == "automatic" and latest.event_type != "reconfirmation":
        return LikedState(
            status=LikedStatus.REQUIRES_RECONFIRMATION,
            source="automatic",
            decision_id=latest.decision_id,
            decided_at=latest.decided_at,
            event_type=latest.event_type,
            binding_status=latest.binding_status,
            requires_reconfirmation=True,
            **base,
        )
    return LikedState(
        value=latest.value,
        status=LikedStatus.DECIDED,
        source=latest.source,
        decision_id=latest.decision_id,
        decided_at=latest.decided_at,
        event_type=latest.event_type,
        binding_status=latest.binding_status,
        **base,
    )


def _valid_hash(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
