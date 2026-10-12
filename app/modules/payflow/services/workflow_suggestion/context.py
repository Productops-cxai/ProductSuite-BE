"""Load what a workflow suggestion needs: client settings, rules, portfolio accounts."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowPortfolioModel,
    PayflowRuleModel,
)
from app.shared.enums import PayflowRuleStatus, PayflowRuleType


def load_client_context(db: Session, client_id: int) -> dict[str, Any]:
    client = db.query(PayflowClientModel).filter(PayflowClientModel.id == client_id).first()
    if not client:
        raise NotFoundError("Client not found")
    channel_flags = {"Email": client.channel_email, "SMS": client.channel_sms}
    return {
        "id": client.id,
        "code": client.code,
        "name": client.name,
        "client_type": client.client_type,
        "ai_mode": client.ai_mode,
        "language": client.correspondence_language,
        "currency": client.currency_code,
        "channels": [channel for channel, enabled in channel_flags.items() if enabled],
        "governance_rules": list(client.governance_rules or []),
    }


def load_rules(db: Session, client_id: int) -> dict[str, list[dict[str, Any]]]:
    """Active system rules (everyone) + active client rules (this client)."""
    rows = (
        db.query(PayflowRuleModel)
        .filter(
            PayflowRuleModel.status == PayflowRuleStatus.ACTIVE.value,
            or_(
                PayflowRuleModel.rule_type == PayflowRuleType.SYSTEM.value,
                PayflowRuleModel.client_id == client_id,
            ),
        )
        .order_by(PayflowRuleModel.id.asc())
        .all()
    )
    rules: dict[str, list[dict[str, Any]]] = {"system": [], "client": []}
    for row in rows:
        key = "system" if row.rule_type == PayflowRuleType.SYSTEM.value else "client"
        rules[key].append(
            {
                "name": row.name,
                "description": row.description,
                "category": row.category,
                "logic": row.logic,
                "conditions": list(row.conditions or []),
                "action": row.action,
            }
        )
    return rules


def load_portfolio_accounts(
    db: Session, client_id: int, portfolio_id: int
) -> tuple[PayflowPortfolioModel, list[PayflowAccountModel]]:
    portfolio = (
        db.query(PayflowPortfolioModel)
        .filter(
            PayflowPortfolioModel.id == portfolio_id,
            PayflowPortfolioModel.client_id == client_id,
        )
        .first()
    )
    if not portfolio:
        raise NotFoundError("Portfolio not found for this client")
    accounts = (
        db.query(PayflowAccountModel)
        .filter(PayflowAccountModel.portfolio_id == portfolio.id)
        .order_by(PayflowAccountModel.id.asc())
        .all()
    )
    return portfolio, accounts
