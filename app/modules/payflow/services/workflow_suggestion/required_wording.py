"""The wording compliance rules require in a workflow's messages - code, no AI.

A compliance admin can give a "Content" rule a required text, e.g. "This is a communication from a debt collector."
When a message is sent, the compliance check (compliance/engine.py) blocks it, or holds it for review, if that text
is missing. So the AI is told the wording and must include it word for word.

The rules are picked exactly the way the send check picks them (compliance.engine.applicable_rules):
- active, for this client or for all clients, not switched off for this client, in effect today;
- matching the client's collector type and the accounts' debt type;
- for the place (country / province / region / city / postal code) of the customer;
- for the channel: a rule for Email, for SMS, or for all channels.

One text of a workflow goes to all of its customers, so it carries the wording of every rule that applies to any of
them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.infrastructure.database.models import PayflowClientModel
from app.modules.payflow.services.compliance.engine import ALLOW_ACTION, EvaluationInput, applicable_rules
from app.modules.payflow.services.compliance.evaluation_service import ComplianceEvaluationService

CHANNELS = ("Email", "SMS")
CONTENT = "Content"  # the compliance category whose rules can require a text
LABEL = "Required wording"  # the workflow's ai_context line that shows it to reviewers


def workflow_wording(db: Session, workflow: dict[str, Any]) -> dict[str, list[str]]:
    """required_wording for a saved workflow: its client and the accounts it is for."""
    from app.modules.payflow.services.workflow_suggestion.context import load_segment_members

    return required_wording(db, workflow["client_id"], load_segment_members(db, workflow))


def required_wording_line(wording: dict[str, list[str]] | None) -> list[dict[str, str]]:
    """The ai_context line that tells reviewers which wording the messages carry and why ([] when none)."""
    if not wording:
        return []
    parts = []
    for text in dict.fromkeys(text for channel in CHANNELS for text in wording.get(channel) or []):
        where = " and ".join("emails" if channel == "Email" else "SMS"
                             for channel in CHANNELS if text in (wording.get(channel) or []))
        parts.append(f'"{text}" ({where})')
    return [{"label": LABEL, "value": "The client's compliance rules require, word for word: " + "; ".join(parts)}]


def required_wording(
    db: Session, client_id: int, members: list, *, at: datetime | None = None
) -> dict[str, list[str]]:
    """{"Email": [...], "SMS": [...]}: the texts every email / SMS of a workflow must include, word for word, most
    important rule first. A channel with nothing required is left out; {} when no rule requires any text.

    `members`: the workflow's eligible accounts. `at`: when (default now) - a rule must be in effect then.
    """
    service = ComplianceEvaluationService(db)
    if not members or not service.has_active_rules(client_id):
        return {}
    rules = [rule for rule in service.rules_in_force(client_id) if _required_text(rule)]
    if not rules:
        return {}
    client = db.get(PayflowClientModel, client_id)
    collector_type = str(getattr(client, "client_type", None) or "").strip()
    disabled = service.disabled_rule_ids(client_id)
    when = at or datetime.now(timezone.utc)
    # The places and debt types of the customers, each once, in the accounts' order.
    places = dict.fromkeys(
        (service.jurisdiction_for(account), str(getattr(account, "account_category", None) or "").strip())
        for account in members
    )
    wording: dict[str, list[str]] = {}
    for channel in CHANNELS:
        texts: list[str] = []
        for jurisdiction, debt_type in places:
            data = EvaluationInput(
                client_id=client_id, jurisdiction=jurisdiction, channel=channel, at=when,
                collector_type=collector_type, debt_type=debt_type,
            )
            for rule in applicable_rules(rules, data, disabled_rule_ids=disabled):
                text = _required_text(rule)
                if text not in texts:
                    texts.append(text)
        if texts:
            wording[channel] = texts
    return wording


def _required_text(rule: dict[str, Any]) -> str:
    """The rule's required text when the send check asks for it in a collection message; "" otherwise (not a
    Content rule, an "Allow" rule, or a rule for promotional messages only)."""
    settings = rule.get("settings") or {}
    if rule.get("category") != CONTENT or rule.get("action") == ALLOW_ACTION or settings.get("promotional_only"):
        return ""
    return str(settings.get("required_text") or "").strip()
