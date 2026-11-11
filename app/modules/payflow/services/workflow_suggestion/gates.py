"""Readiness checks before auto (or gated) workflow suggestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.infrastructure.database.models import (
    PayflowClientModel,
    PayflowStrategyModel,
    PayflowStrategyVersionModel,
)
from app.modules.payflow.services.workflow_suggestion.context import load_suggest_scope
from app.modules.payflow.services.workflow_suggestion.segmenter import (
    filter_eligible,
    match_account_to_segment,
)
from app.shared.enums import (
    PayflowClientStatus,
    PayflowPortfolioStatus,
    PayflowStrategySource,
    PayflowStrategyStatus,
)

# A workflow in one of these statuses covers the accounts of its segment: it runs for them
# (Active), or it is on its way to (proposed / being reviewed / approved). Inactive ones do not.
COVERING_STATUSES = (
    PayflowStrategyStatus.AI_PROPOSED.value,
    PayflowStrategyStatus.DRAFT.value,
    PayflowStrategyStatus.UNDER_REVIEW.value,
    PayflowStrategyStatus.APPROVED.value,
    PayflowStrategyStatus.ACTIVE.value,
)
# Reject puts a workflow back to AI Proposed / Draft; its newest history row says it was rejected.
_BACK_AFTER_REJECT = (PayflowStrategyStatus.AI_PROPOSED.value, PayflowStrategyStatus.DRAFT.value)
ALL_COVERED_REASON = "Every eligible account is already covered by a workflow"


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

    eligible, excluded = filter_eligible(accounts)
    if not eligible:
        if not accounts:
            reason = (
                "No accounts found for this parent client"
                if portfolio_id is None
                else "No accounts found for this portfolio"
            )
        else:
            breakdown = ", ".join(
                f"{name}={count}" for name, count in excluded.items() if count
            )
            reason = (
                f"No eligible accounts that need a workflow "
                f"({len(accounts)} accounts; excluded: {breakdown or 'none'})"
            )
        return SuggestGateResult(
            ok=False,
            reason=reason,
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


def _was_rejected(db: Session, strategy: PayflowStrategyModel) -> bool:
    """True while the newest thing that happened to the workflow is a reviewer rejecting it.

    A rejected workflow that is edited or sent for review again gets a newer history row and
    counts as covering its segment again.
    """
    if strategy.status not in _BACK_AFTER_REJECT:
        return False
    latest = (
        db.query(PayflowStrategyVersionModel)
        .filter(PayflowStrategyVersionModel.strategy_id == strategy.id)
        .order_by(PayflowStrategyVersionModel.id.desc())
        .first()
    )
    if latest is None:
        return False
    return (latest.status or "") == "Rejected" or (latest.note or "").startswith("Rejected")


def uncovered_accounts(
    db: Session, client_id: int, portfolio_id: int | None, accounts: list
) -> list:
    """The eligible accounts of the scope that fit no existing workflow.

    These are the accounts that moved into (or arrived in) a segment nobody designed a workflow
    for. Rejected and Inactive workflows do not count as covering anything.
    ``portfolio_id=None`` means the client-level scope (parent accounts).
    """
    eligible, _ = filter_eligible(accounts)
    if not eligible:
        return []
    q = db.query(PayflowStrategyModel).filter(
        PayflowStrategyModel.client_id == client_id,
        PayflowStrategyModel.status.in_(COVERING_STATUSES),
    )
    if portfolio_id is None:
        q = q.filter(PayflowStrategyModel.portfolio_id.is_(None))
    else:
        q = q.filter(PayflowStrategyModel.portfolio_id == portfolio_id)
    candidates = [
        {"id": strategy.id, "segment": strategy.segment or {}}
        for strategy in q.all()
        if not _was_rejected(db, strategy)
    ]
    if not candidates:
        return eligible
    today = date.today()
    return [a for a in eligible if match_account_to_segment(a, candidates, today) is None]
