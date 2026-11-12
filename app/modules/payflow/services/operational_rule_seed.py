"""Operational (governance) rules of the PayFlow prototype, added once so the Rules screens are not empty.

All are System rules. A System rule only applies to a client after that client selects it
(client governance settings), so adding them changes nothing that is already running.

- The ten "essential" rules are Drafts, as in the prototype: offered for review, never enforced
  until someone activates them and a client selects them.
- The prototype's other rules keep their status when PayFlow can check and carry them out;
  a rule that uses something PayFlow cannot check yet is stored as a Draft instead.
- The prototype tied four of them to named clients: here they are System rules, so any client can
  select them; no client is named.

A rule that already exists (same code) is never changed, so edits made in the app are kept.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.infrastructure.database.models import PayflowRuleModel
from app.modules.payflow.services.rule_runtime_service import unenforced_parts
from app.shared.enums import PayflowRuleStatus, PayflowRuleType

SEED_ACTOR = "PayFlow"


def _cond(code: str, field: str, operator: str, value: str) -> dict[str, str]:
    return {"id": code, "field": field, "operator": operator, "value": value}


ESSENTIAL_NOTE = "Essential rule draft created for client review"

OPERATIONAL_RULES: list[dict[str, Any]] = [
    # ------------------------------------------------------------- essentials (Draft)
    {
        "code": "allow-unsubscribe-option",
        "name": "Allow Unsubscribe Option",
        "category": "Communication",
        "logic": "ANY",
        "description": (
            "Include a working unsubscribe option in email or STOP instructions in SMS. An unsubscribe "
            "request must still suppress future contact on that channel; this rule never overrides "
            "mandatory compliance."
        ),
        "conditions": [_cond("unsub-email", "Channel", "Is", "Email"), _cond("unsub-sms", "Channel", "Is", "SMS")],
        "action": "Include Unsubscribe Option",
        "status": "Draft",
        "note": ESSENTIAL_NOTE,
    },
    {
        "code": "failed-payment-alternate-method",
        "name": "Failed Payment · Offer Alternate Method",
        "category": "Payment Activity",
        "logic": "ALL",
        "description": (
            "When a payment fails, offer another enabled payment method if one is available. Never "
            "charge automatically, retry the same payment, or offer a disabled method."
        ),
        "conditions": [
            _cond("payment-failed", "Payment Status", "Is", "Failed"),
            _cond("method-available", "Alternate Payment Method Available", "Is", "Yes"),
        ],
        "action": "Offer Alternate Payment Method",
        "status": "Draft",
        "note": ESSENTIAL_NOTE,
    },
    {
        "code": "failed-delivery-alternate-channel",
        "name": "Failed Delivery · Alternate Channel",
        "category": "Communication",
        "logic": "ALL",
        "description": (
            "After email or SMS delivery fails, propose the other enabled channel only when its address "
            "is valid, consent is recorded and it is not opted out. Recheck all compliance restrictions "
            "on that channel; do not switch channels to bypass an unsubscribe or a legal restriction."
        ),
        "conditions": [
            _cond("delivery-failed", "Delivery Status", "Is", "Failed"),
            _cond("channel-eligible", "Alternate Channel Eligible", "Is", "Yes"),
        ],
        "action": "Send via Alternate Channel",
        "status": "Draft",
        "note": ESSENTIAL_NOTE,
    },
    {
        "code": "respect-channel-unsubscribe",
        "name": "Stop Contact After Unsubscribe",
        "category": "Communication",
        "logic": "ALL",
        "description": (
            "Prevent further messages on the channel the customer unsubscribed from. Mandatory opt-out "
            "protections still apply even when this operational rule is not selected."
        ),
        "conditions": [_cond("opted-out", "Customer Opted Out", "Is", "Yes")],
        "action": "Prevent Communication",
        "status": "Draft",
        "note": ESSENTIAL_NOTE,
    },
    {
        "code": "paid-account-stop-reminders",
        "name": "Stop Collection Reminders After Payment",
        "category": "Payment Status",
        "logic": "ANY",
        "description": (
            "Stop collection reminders when the balance is fully paid or is zero. A separate payment "
            "receipt can still be reviewed; this rule is for collection messages, not receipts."
        ),
        "conditions": [
            _cond("paid", "Payment Status", "Is", "Paid"),
            _cond("zero-balance", "Outstanding Balance", "Less Than or Equal To", "0"),
        ],
        "action": "Prevent Communication",
        "status": "Draft",
        "note": ESSENTIAL_NOTE,
    },
    {
        "code": "payment-processing-hold",
        "name": "Hold Reminders While Payment Is Processing",
        "category": "Payment Activity",
        "logic": "ALL",
        "description": (
            "Hold further collection reminders while a payment is pending. Resume only after the payment "
            "outcome and remaining balance are confirmed."
        ),
        "conditions": [_cond("pending", "Payment Processing Status", "Is", "Pending")],
        "action": "Hold Action",
        "status": "Draft",
        "note": ESSENTIAL_NOTE,
    },
    {
        "code": "prevent-duplicate-messages",
        "name": "Prevent Duplicate Communications",
        "category": "Communication",
        "logic": "ALL",
        "description": (
            "Prevent a message already identified as a duplicate for the same customer, case and workflow "
            "event. Re-importing a file should not produce another copy."
        ),
        "conditions": [_cond("duplicate", "Duplicate Communication", "Is", "Yes")],
        "action": "Prevent Communication",
        "status": "Draft",
        "note": ESSENTIAL_NOTE,
    },
    {
        "code": "invalid-address-hold",
        "name": "Hold Undeliverable Contact Channel",
        "category": "Communication",
        "logic": "ALL",
        "description": (
            "Stop sending to a bounced address until its contact details are corrected. An eligible "
            "alternate channel is handled separately and must pass compliance checks."
        ),
        "conditions": [_cond("bounced", "Delivery Status", "Is", "Bounced")],
        "action": "Prevent Communication",
        "status": "Draft",
        "note": ESSENTIAL_NOTE,
    },
    {
        "code": "missed-installment-follow-up",
        "name": "Missed Instalment Follow-Up",
        "category": "Payment Plan",
        "logic": "ALL",
        "description": (
            "Propose a reminder when a plan has a missed instalment and a balance remains. Do not cancel "
            "the plan or charge the customer automatically; review contact eligibility first."
        ),
        "conditions": [
            _cond("missed", "Missed Installments", "Greater Than", "0"),
            _cond("remaining", "Outstanding Balance", "Greater Than", "0"),
        ],
        "action": "Send Payment Reminder",
        "status": "Draft",
        "note": ESSENTIAL_NOTE,
    },
    {
        "code": "successful-payment-confirmation",
        "name": "Successful Payment Confirmation",
        "category": "Payment Activity",
        "logic": "ALL",
        "description": (
            "Propose one receipt after a successful payment when no confirmation has been sent. Use the "
            "recorded payment outcome and never send a success receipt for a pending or failed payment."
        ),
        "conditions": [
            _cond("succeeded", "Payment Processing Status", "Is", "Succeeded"),
            _cond("not-confirmed", "Payment Confirmation Sent", "Is", "No"),
        ],
        "action": "Send Payment Confirmation",
        "status": "Draft",
        "note": ESSENTIAL_NOTE,
    },
    # ------------------------------------------------------------- the prototype's other rules
    {
        "code": "high-balance-review",
        "name": "High Balance Review",
        "category": "Amount",
        "logic": "ALL",
        "description": "Routes high-value accounts to a supervisor before any autonomous collection action is executed.",
        "conditions": [_cond("hb-1", "Outstanding Balance", "Greater Than", "10000")],
        "action": "Require Human Review",
        "status": "Active",
    },
    {
        "code": "repeated-attempts-escalation",
        "name": "Repeated Attempts Escalation",
        "category": "Collection Attempts",
        "logic": "ALL",
        "description": "Escalates accounts where repeated outreach has not produced a response or payment commitment.",
        "conditions": [_cond("ra-1", "Unsuccessful Attempts", "Greater Than or Equal To", "3")],
        "action": "Require Human Review",
        "status": "Active",
    },
    {
        "code": "low-confidence-review",
        "name": "Low Confidence Review",
        "category": "AI Confidence",
        "logic": "ALL",
        "description": "Requires supervisor judgement when the AI recommendation confidence falls below the configured threshold.",
        "conditions": [_cond("lc-1", "AI Confidence", "Less Than", "70")],
        "action": "Require Human Review",
        "status": "Active",
    },
    {
        "code": "dispute-detected-review",
        "name": "Dispute Detected Review",
        "category": "Customer Risk",
        "logic": "ANY",
        "description": "Holds collection activity when a customer disputes the balance.",
        "conditions": [
            _cond("dd-1", "Dispute Flag", "Is", "Yes"),
            _cond("dd-2", "Customer Reply Content", "Contains", "dispute"),
        ],
        "action": "Hold Action",
        "status": "Active",
    },
    {
        "code": "broken-promise-escalation",
        "name": "Broken Promise Escalation",
        "category": "Promise-to-Pay",
        "logic": "ALL",
        "description": "Escalates cases where a promise-to-pay was not honoured.",
        "conditions": [
            _cond("bp-1", "Promise-to-Pay Status", "Is", "Broken"),
            _cond("bp-2", "Outstanding Balance", "Greater Than", "2500"),
        ],
        "action": "Escalate Case",
        "status": "Draft",
    },
    {
        "code": "quiet-period-guard",
        "name": "Quiet Period Guard",
        "category": "Communication",
        "logic": "ALL",
        "description": "Prevents further outreach when the account has already received the configured weekly message volume.",
        "conditions": [_cond("qp-1", "Messages Sent (7 days)", "Greater Than or Equal To", "4")],
        "action": "Prevent Communication",
        "status": "Active",
    },
    {
        "code": "northstar-hardship-review",
        "name": "Hardship Signal Review",
        "category": "Customer Risk",
        "logic": "ALL",
        "description": "Review for customers signalling financial hardship.",
        "conditions": [_cond("nh-1", "Customer Risk Level", "Is", "High")],
        "action": "Require Human Review",
        "status": "Active",
    },
]


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%d %b %Y")


def seed_operational_rules(db: Session) -> dict[str, int]:
    """Add the prototype's operational rules that are missing (by code). Returns how many were added."""
    existing = {code for (code,) in db.query(PayflowRuleModel.code)}
    added = drafted = 0
    for entry in OPERATIONAL_RULES:
        if entry["code"] in existing:
            continue
        status = entry["status"]
        problems = unenforced_parts(entry["conditions"], entry["action"])
        note = entry.get("note") or "Rule created"
        if status == PayflowRuleStatus.ACTIVE.value and problems:
            # Never an Active rule that silently does nothing.
            status = PayflowRuleStatus.DRAFT.value
            note = "Sample rule stored as a Draft: PayFlow cannot check " + ", ".join(problems) + " yet"
            drafted += 1
        history = [{"at": _stamp(), "change": note, "by": SEED_ACTOR}]
        if status == PayflowRuleStatus.ACTIVE.value:
            history.append({"at": _stamp(), "change": "Rule activated", "by": SEED_ACTOR})
        db.add(
            PayflowRuleModel(
                code=entry["code"],
                name=entry["name"],
                description=entry["description"],
                rule_type=PayflowRuleType.SYSTEM.value,
                client_id=None,
                category=entry["category"],
                logic=entry["logic"],
                conditions=[dict(c) for c in entry["conditions"]],
                action=entry["action"],
                status=status,
                created_by=SEED_ACTOR,
                triggers_7d=0,
                applied_to=[],
                history=history,
            )
        )
        added += 1
    if added:
        db.commit()
    return {"rules": added, "stored_as_draft": drafted}
