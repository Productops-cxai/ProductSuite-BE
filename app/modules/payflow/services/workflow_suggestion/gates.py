"""Readiness checks before auto (or gated) workflow suggestion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.infrastructure.database.models import (
    PayflowClientModel,
    PayflowStrategyModel,
)
from app.modules.payflow.services.workflow_suggestion.context import load_suggest_scope
from app.modules.payflow.services.workflow_suggestion.segmenter import filter_eligible
from app.shared.enums import (
    PayflowClientStatus,
    PayflowPortfolioStatus,
    PayflowStrategySource,
)


@dataclass(frozen=True)
class SuggestGateResult:
    ok: bool
    reason: str | None = None
    eligible_accounts: int = 0
    total_accounts: int = 0


def evaluate_suggest_gates(
    db: Session, client_id: int, portfolio_id: int | None
) -> SuggestGateResult:
    """Client active + (portfolio active | parent scope) + at least one eligible account."""
    client = db.query(PayflowClientModel).filter(PayflowClientModel.id == client_id).first()
    if not client:
        return SuggestGateResult(ok=False, reason="Client not found")
    if (client.status or "").lower() != PayflowClientStatus.ACTIVE.value:
        return SuggestGateResult(ok=False, reason="Client is not active")

    try:
        scope, accounts = load_suggest_scope(db, client_id, portfolio_id)
    except NotFoundError as exc:
        return SuggestGateResult(ok=False, reason=getattr(exc, "message", None) or str(exc))

    if portfolio_id is not None:
        if (scope.status or "").lower() != PayflowPortfolioStatus.ACTIVE.value:
            return SuggestGateResult(ok=False, reason="Portfolio is not active")
    # Parent-level (portfolio_id None): only client Active is required.

    eligible, _excluded = filter_eligible(accounts)
    if not eligible:
        return SuggestGateResult(
            ok=False,
            reason="No eligible accounts that need a workflow",
            eligible_accounts=0,
            total_accounts=len(accounts),
        )
    return SuggestGateResult(
        ok=True,
        eligible_accounts=len(eligible),
        total_accounts=len(accounts),
    )


def portfolio_has_ai_workflows(
    db: Session, client_id: int, portfolio_id: int | None
) -> bool:
    """True if this scope already has any AI-generated strategy (idempotency).

    ``portfolio_id=None`` means client-level / parent-account strategies.
    """
    q = db.query(PayflowStrategyModel.id).filter(
        PayflowStrategyModel.client_id == client_id,
        PayflowStrategyModel.source == PayflowStrategySource.AI_GENERATED.value,
    )
    if portfolio_id is None:
        q = q.filter(PayflowStrategyModel.portfolio_id.is_(None))
    else:
        q = q.filter(PayflowStrategyModel.portfolio_id == portfolio_id)
    return q.first() is not None


def gate_summary(result: SuggestGateResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "reason": result.reason,
        "eligible_accounts": result.eligible_accounts,
        "total_accounts": result.total_accounts,
    }
