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
from app.modules.payflow.services.workflow_suggestion.context import load_portfolio_accounts
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


def evaluate_suggest_gates(db: Session, client_id: int, portfolio_id: int) -> SuggestGateResult:
    """Client active + portfolio active + at least one eligible account."""
    client = db.query(PayflowClientModel).filter(PayflowClientModel.id == client_id).first()
    if not client:
        return SuggestGateResult(ok=False, reason="Client not found")
    if (client.status or "").lower() != PayflowClientStatus.ACTIVE.value:
        return SuggestGateResult(ok=False, reason="Client is not active")

    try:
        portfolio, accounts = load_portfolio_accounts(db, client_id, portfolio_id)
    except NotFoundError:
        return SuggestGateResult(ok=False, reason="Portfolio not found for this client")

    if (portfolio.status or "").lower() != PayflowPortfolioStatus.ACTIVE.value:
        return SuggestGateResult(ok=False, reason="Portfolio is not active")

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


def portfolio_has_ai_workflows(db: Session, client_id: int, portfolio_id: int) -> bool:
    """True if this portfolio already has any AI-generated strategy (idempotency)."""
    row = (
        db.query(PayflowStrategyModel.id)
        .filter(
            PayflowStrategyModel.client_id == client_id,
            PayflowStrategyModel.portfolio_id == portfolio_id,
            PayflowStrategyModel.source == PayflowStrategySource.AI_GENERATED.value,
        )
        .first()
    )
    return row is not None


def gate_summary(result: SuggestGateResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "reason": result.reason,
        "eligible_accounts": result.eligible_accounts,
        "total_accounts": result.total_accounts,
    }
