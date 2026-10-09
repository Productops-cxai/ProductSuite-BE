from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowClientModel,
    PayflowPortfolioModel,
    PayflowStrategyModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.deletion import record_deletion, snapshot_model
from app.shared.enums import (
    PayflowStrategyOrigin,
    PayflowStrategySource,
    PayflowStrategyStatus,
)

STRATEGY_STEP_KINDS = [
    "Trigger",
    "Communication",
    "Wait",
    "Condition",
    "Payment Action",
    "Case Action",
    "Human Review",
    "AI Reassessment",
    "Outcome",
]

CHANNELS = ["Email", "SMS"]
MESSAGE_PURPOSES = [
    "Payment Reminder",
    "Firm Reminder",
    "Payment Link",
    "Promise-to-Pay Reminder",
    "Payment Plan Reminder",
    "Contact Details Update Request",
]
REFERENCE_EVENTS = [
    "Case Received",
    "Due Date",
    "Previous Action",
    "Previous Email",
    "Previous SMS",
    "Payment Link Sent",
    "Promise-to-Pay Date",
    "Broken Promise-to-Pay",
    "Last Customer Response",
]
TIME_UNITS = ["Hours", "Days", "Weeks"]
TIME_DIRECTIONS = ["After", "Before"]
CONDITION_ATTRIBUTES = [
    "Payment Status",
    "Email Delivery",
    "SMS Delivery",
    "Email Address",
    "Mobile Number",
    "Payment Link",
    "Promise-to-Pay",
    "Customer Response",
    "Outstanding Balance",
]
CONDITION_OPERATORS = ["Equals", "Not Equals", "Greater Than", "Less Than"]
CONDITION_VALUES: dict[str, list[str]] = {
    "Payment Status": ["Unpaid", "Paid In Full", "Partial Payment", "Payment Plan Active"],
    "Email Delivery": ["Delivered", "Failed", "Bounced"],
    "SMS Delivery": ["Delivered", "Failed"],
    "Email Address": ["Valid", "Invalid", "Missing"],
    "Mobile Number": ["Valid", "Invalid", "Missing"],
    "Payment Link": ["Clicked", "Not Clicked"],
    "Promise-to-Pay": ["Created", "Kept", "Broken", "None"],
    "Customer Response": ["Received", "None", "Dispute Raised"],
    "Outstanding Balance": ["1,000", "5,000", "10,000"],
}
CASE_ACTIONS = [
    "Move Case To Escalated Treatment",
    "Hold Automated Contact",
    "Request Contact Details Update",
    "Close Case",
]
PAYMENT_ACTIONS = [
    "Send Secure Payment Link",
    "Offer Payment Plan",
    "Record Promise To Pay",
]
OUTCOMES = [
    "Paid In Full",
    "Payment Plan Active",
    "Promise To Pay Recorded",
    "No Contactable Channel",
    "Case Closed",
]
AGE_BANDS = ["All ages", "18 – 24", "25 – 34", "35 – 49", "50 – 64", "65 and over"]
POSTAL_REGIONS = [
    "All regions",
    "Ontario (M, L, K, N, P)",
    "Quebec (H, J, G)",
    "British Columbia (V)",
    "Alberta (T)",
    "Atlantic (A, B, C, E)",
    "Prairies (R, S)",
]
BALANCE_BANDS = [
    "All balances",
    "Under $500",
    "$500 – $1,500",
    "$1,500 – $5,000",
    "$5,000 and over",
]
DELINQUENCY_BANDS = [
    "All stages",
    "1 – 29 days past due",
    "30 – 59 days past due",
    "60 – 89 days past due",
    "90+ days past due",
]
LANGUAGE_PREFERENCES = ["All languages", "English", "French", "English + French"]
TENURE_BANDS = [
    "All customers",
    "New customer (under 6 months)",
    "Established (6 – 24 months)",
    "Long-standing (2 years+)",
]
EXCLUDED_TARGETING = ["Ethnicity", "Religion", "Gender", "Health status", "Marital status"]

MESSAGE_TEMPLATES = [
    {
        "id": "em-reminder",
        "name": "Email — Friendly Payment Reminder",
        "channel": "Email",
        "purpose": "Payment Reminder",
        "subject": "A friendly reminder about your balance",
        "body": (
            "Hi {{first_name}},\n\nOur records show an outstanding balance of "
            "{{outstanding_balance}} on account {{account_reference}}. You can settle it "
            "securely online at any time.\n\n{{payment_link}}\n\nIf you have already paid, "
            "please ignore this message.\n\n{{brand_name}}"
        ),
    },
    {
        "id": "em-firm",
        "name": "Email — Firm Reminder",
        "channel": "Email",
        "purpose": "Firm Reminder",
        "subject": "Action required on account {{account_reference}}",
        "body": (
            "Hi {{first_name}},\n\nYour balance of {{outstanding_balance}} remains unpaid. "
            "Please arrange payment or set up a payment plan using the secure link below.\n\n"
            "{{payment_link}}\n\nIf you need support with affordability, reply to this email "
            "and we will help.\n\n{{brand_name}}"
        ),
    },
    {
        "id": "em-plan",
        "name": "Email — Payment Plan Option",
        "channel": "Email",
        "purpose": "Payment Plan Reminder",
        "subject": "Spread your balance over time",
        "body": (
            "Hi {{first_name}},\n\nYou can pay {{outstanding_balance}} in daily, weekly or "
            "monthly instalments that suit you.\n\nSet up your plan: {{payment_link}}\n\n"
            "{{brand_name}}"
        ),
    },
    {
        "id": "em-link",
        "name": "Email — Secure Payment Link",
        "channel": "Email",
        "purpose": "Payment Link",
        "subject": "Your secure payment link",
        "body": (
            "Hi {{first_name}},\n\nHere is your secure link to settle {{outstanding_balance}}:\n\n"
            "{{payment_link}}\n\nThe link is unique to account {{account_reference}}.\n\n"
            "{{brand_name}}"
        ),
    },
    {
        "id": "sms-link",
        "name": "SMS — Payment Link",
        "channel": "SMS",
        "purpose": "Payment Link",
        "body": (
            "{{brand_name}}: balance {{outstanding_balance}} on {{account_reference}}. "
            "Pay securely: {{payment_link}}. Reply STOP to opt out."
        ),
    },
    {
        "id": "sms-reminder",
        "name": "SMS — Payment Reminder",
        "channel": "SMS",
        "purpose": "Payment Reminder",
        "body": (
            "{{brand_name}}: a reminder that {{outstanding_balance}} is outstanding. "
            "Pay or set up a plan: {{payment_link}}. Reply STOP to opt out."
        ),
    },
    {
        "id": "sms-promise",
        "name": "SMS — Promise-to-Pay Reminder",
        "channel": "SMS",
        "purpose": "Promise-to-Pay Reminder",
        "body": (
            "{{brand_name}}: your promised payment of {{promise_amount}} is due {{promise_date}}. "
            "Pay now: {{payment_link}}. Reply STOP to opt out."
        ),
    },
    {
        "id": "sms-plan",
        "name": "SMS — Payment Plan Option",
        "channel": "SMS",
        "purpose": "Payment Plan Reminder",
        "body": (
            "{{brand_name}}: you can spread {{outstanding_balance}} into instalments. "
            "Choose a plan: {{payment_link}}. Reply STOP to opt out."
        ),
    },
    {
        "id": "sms-contact",
        "name": "SMS — Contact Details Update",
        "channel": "SMS",
        "purpose": "Contact Details Update Request",
        "body": (
            "{{brand_name}}: we could not reach you by email regarding {{account_reference}}. "
            "Update your details: {{payment_link}}. Reply STOP to opt out."
        ),
    },
    {
        "id": "em-contact",
        "name": "Email — Contact Details Update",
        "channel": "Email",
        "purpose": "Contact Details Update Request",
        "subject": "Please confirm your contact details",
        "body": (
            "Hi {{first_name}},\n\nWe were unable to reach you about account "
            "{{account_reference}}. Please confirm your email and mobile number so we can "
            "keep you updated.\n\n{{payment_link}}\n\n{{brand_name}}"
        ),
    },
]

AWAITING_STATUSES = {
    PayflowStrategyStatus.AI_PROPOSED.value,
    PayflowStrategyStatus.UNDER_REVIEW.value,
}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "strategy"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%d %b %Y")


def _stamp_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cfg(step: dict[str, Any]) -> dict[str, Any]:
    cfg = step.get("config")
    return dict(cfg) if isinstance(cfg, dict) else {}


def _timing_label(step: dict[str, Any]) -> str | None:
    if step.get("timing"):
        return str(step["timing"])
    cfg = _cfg(step)
    reference = cfg.get("reference_event") or step.get("reference_event")
    if not reference:
        return None
    amount = cfg.get("amount", step.get("amount"))
    unit = cfg.get("unit") or step.get("unit") or "Days"
    direction = cfg.get("direction") or step.get("direction") or "After"
    try:
        amount_num = float(amount) if amount is not None else 0.0
    except (TypeError, ValueError):
        amount_num = 0.0
    if amount_num == 0:
        return f"Immediately when {str(reference).lower()} occurs"
    unit_label = unit
    if amount_num == 1 and isinstance(unit, str) and unit.endswith("s"):
        unit_label = unit[:-1]
    # Explicit event-based timing (never "Day 3")
    return f"{int(amount_num) if amount_num == int(amount_num) else amount_num} {unit_label} {direction} {reference}"


def _normalize_step(raw: dict[str, Any], *, default_origin: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationAppError("Each workflow step must be an object")
    kind = (raw.get("kind") or "").strip()
    if kind not in STRATEGY_STEP_KINDS:
        raise ValidationAppError(f"Unsupported step kind: {kind or '(empty)'}")
    title = (raw.get("title") or "").strip() or kind
    step_id = (raw.get("id") or "").strip() or None
    cfg = _cfg(raw)
    # Lift legacy flat fields into config
    for key in (
        "channel",
        "purpose",
        "reference_event",
        "amount",
        "unit",
        "direction",
        "attribute",
        "operator",
        "value",
        "action",
        "outcome",
        "note",
        "template_id",
    ):
        if key in raw and raw[key] is not None and key not in cfg:
            cfg[key] = raw[key]
    # Accept camelCase from Lovable-shaped clients
    aliases = {
        "referenceEvent": "reference_event",
        "templateId": "template_id",
    }
    for camel, snake in aliases.items():
        if camel in raw and raw[camel] is not None and snake not in cfg:
            cfg[snake] = raw[camel]
        if isinstance(raw.get("config"), dict) and camel in raw["config"] and snake not in cfg:
            cfg[snake] = raw["config"][camel]

    amount = cfg.get("amount")
    unit = cfg.get("unit")
    if amount not in (None, "", 0, 0.0) or unit:
        if not cfg.get("reference_event"):
            raise ValidationAppError(
                f"Step '{title}' timing must use an explicit reference event "
                "(e.g. 3 Days After Due Date), not Day N wording"
            )

    origin = (raw.get("origin") or default_origin or PayflowStrategyOrigin.HUMAN_CREATED.value).strip()
    step: dict[str, Any] = {
        "id": step_id,
        "kind": kind,
        "title": title,
        "origin": origin,
        "disabled": bool(raw.get("disabled") or False),
        "config": cfg,
        "next": raw.get("next"),
        "yes": raw.get("yes"),
        "no": raw.get("no"),
        "channel": cfg.get("channel") or raw.get("channel"),
        "purpose": cfg.get("purpose") or raw.get("purpose"),
        "detail": raw.get("detail"),
    }
    step["timing"] = _timing_label(step)
    if kind == "Condition" and not (step.get("yes") or step.get("no")):
        # Conditions should branch; allow incomplete drafts but prefer yes/no when provided
        pass
    return step


def _normalize_steps(
    steps: list[Any], *, default_origin: str | None = None
) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not steps:
        raise ValidationAppError("At least one step is required")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, raw in enumerate(steps):
        step = _normalize_step(raw if isinstance(raw, dict) else {}, default_origin=default_origin)
        if not step["id"]:
            step["id"] = f"s{idx + 1}"
        if step["id"] in seen:
            raise ValidationAppError(f"Duplicate step id: {step['id']}")
        seen.add(step["id"])
        normalized.append(step)
    ids = {s["id"] for s in normalized}
    for step in normalized:
        for link in ("next", "yes", "no"):
            target = step.get(link)
            if target and target not in ids:
                raise ValidationAppError(
                    f"Step '{step['id']}' {link} points to unknown step '{target}'"
                )
    return normalized


def _step_stats(steps: list[dict]) -> dict[str, int]:
    return {
        "steps": len(steps),
        "branches": sum(1 for s in steps if s.get("kind") == "Condition"),
        "emails": sum(
            1
            for s in steps
            if s.get("kind") == "Communication"
            and ((_cfg(s).get("channel") or s.get("channel")) == "Email")
        ),
        "sms": sum(
            1
            for s in steps
            if s.get("kind") == "Communication"
            and ((_cfg(s).get("channel") or s.get("channel")) == "SMS")
        ),
        "payment_actions": sum(1 for s in steps if s.get("kind") == "Payment Action"),
        "case_actions": sum(1 for s in steps if s.get("kind") == "Case Action"),
    }


def _cases_covered(coverage: str | None, cases_covered: int | None = None) -> int | None:
    if cases_covered is not None:
        return int(cases_covered)
    if coverage is None:
        return None
    text = str(coverage).strip().replace(",", "")
    if text.isdigit():
        return int(text)
    return None


def _coverage_value(payload: dict[str, Any], existing: str | None = None) -> str | None:
    if payload.get("cases_covered") is not None:
        return str(int(payload["cases_covered"]))
    if "coverage" in payload:
        cov = payload.get("coverage")
        return None if cov is None else str(cov)
    return existing


def _is_human_modified(row: PayflowStrategyModel, steps: list[dict]) -> bool:
    if row.origin == PayflowStrategyOrigin.HUMAN_MODIFIED.value:
        return True
    return any(s.get("origin") == PayflowStrategyOrigin.HUMAN_MODIFIED.value for s in steps)


def _row_source(row: PayflowStrategyModel) -> str:
    if row.source:
        return row.source
    if row.origin == PayflowStrategyOrigin.AI_PROPOSED.value:
        return PayflowStrategySource.AI_GENERATED.value
    return PayflowStrategySource.HUMAN_CREATED.value


def _awaiting_review(status: str) -> bool:
    return status in AWAITING_STATUSES


def _catalog() -> dict[str, Any]:
    return {
        "channels": CHANNELS,
        "message_purposes": MESSAGE_PURPOSES,
        "reference_events": REFERENCE_EVENTS,
        "time_units": TIME_UNITS,
        "time_directions": TIME_DIRECTIONS,
        "condition_attributes": CONDITION_ATTRIBUTES,
        "condition_operators": CONDITION_OPERATORS,
        "condition_values": CONDITION_VALUES,
        "case_actions": CASE_ACTIONS,
        "payment_actions": PAYMENT_ACTIONS,
        "outcomes": OUTCOMES,
        "age_bands": AGE_BANDS,
        "postal_regions": POSTAL_REGIONS,
        "balance_bands": BALANCE_BANDS,
        "delinquency_bands": DELINQUENCY_BANDS,
        "language_preferences": LANGUAGE_PREFERENCES,
        "tenure_bands": TENURE_BANDS,
        "excluded_targeting_attributes": EXCLUDED_TARGETING,
        "message_templates": MESSAGE_TEMPLATES,
    }


def _step_fingerprint(step: dict[str, Any]) -> str:
    cfg = _cfg(step)
    parts = [
        step.get("kind"),
        step.get("title"),
        cfg.get("channel") or step.get("channel"),
        cfg.get("purpose") or step.get("purpose"),
        _timing_label(step),
        cfg.get("attribute"),
        cfg.get("operator"),
        cfg.get("value"),
        cfg.get("action"),
        cfg.get("outcome"),
        "disabled" if step.get("disabled") else "enabled",
        f"next={step.get('next')}",
        f"yes={step.get('yes')}",
        f"no={step.get('no')}",
    ]
    return "|".join("" if p is None else str(p) for p in parts)


def _diff_steps(before: list[dict], after: list[dict]) -> list[str]:
    before_by_id = {s.get("id"): s for s in before if s.get("id")}
    after_by_id = {s.get("id"): s for s in after if s.get("id")}
    changes: list[str] = []
    for sid, old in before_by_id.items():
        if sid not in after_by_id:
            changes.append(f"Removed step '{old.get('title') or sid}'")
            continue
        new = after_by_id[sid]
        old_timing = _timing_label(old)
        new_timing = _timing_label(new)
        if old_timing != new_timing and (old_timing or new_timing):
            changes.append(
                f"{new.get('title') or sid}: timing changed from "
                f"{old_timing or 'unset'} to {new_timing or 'unset'}"
            )
        old_ch = _cfg(old).get("channel") or old.get("channel")
        new_ch = _cfg(new).get("channel") or new.get("channel")
        if old_ch != new_ch and (old_ch or new_ch):
            changes.append(
                f"{new.get('title') or sid}: channel changed from {old_ch or 'unset'} to {new_ch or 'unset'}"
            )
        old_cond = (
            f"{_cfg(old).get('attribute')} {_cfg(old).get('operator')} {_cfg(old).get('value')}"
        )
        new_cond = (
            f"{_cfg(new).get('attribute')} {_cfg(new).get('operator')} {_cfg(new).get('value')}"
        )
        if old.get("kind") == "Condition" and old_cond != new_cond:
            changes.append(f"{new.get('title') or sid}: condition changed to {new_cond.strip()}")
        if bool(old.get("disabled")) != bool(new.get("disabled")):
            changes.append(
                f"{new.get('title') or sid}: "
                f"{'disabled' if new.get('disabled') else 're-enabled'}"
            )
        if (
            old.get("next") != new.get("next")
            or old.get("yes") != new.get("yes")
            or old.get("no") != new.get("no")
        ):
            changes.append(f"{new.get('title') or sid}: branch/sequence connection updated")
        if _step_fingerprint(old) != _step_fingerprint(new) and not any(
            sid in c or (new.get("title") or "") in c for c in changes[-5:]
        ):
            changes.append(f"{new.get('title') or sid}: configuration updated")
    for sid, new in after_by_id.items():
        if sid not in before_by_id:
            changes.append(f"Added step '{new.get('title') or sid}' ({new.get('kind')})")
    # de-dupe while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for c in changes:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


class PayflowStrategyService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def _visible_client_ids(self, user: UserModel) -> list[int] | None:
        if self.access.is_operations_admin(user):
            return None
        ctx = self.access.build_context(user)
        return list(ctx.get("client_ids") or [])

    def _assert_view(self, user: UserModel, client_id: int) -> None:
        if self.access.is_operations_admin(user):
            return
        if not self.access.can_see_client(user, client_id):
            raise ForbiddenError("Strategy access denied")
        if not self.access.can(user, "view_workflows", client_id):
            raise ForbiddenError("View Workflows permission required")

    def _assert_manage(self, user: UserModel, client_id: int) -> None:
        self._assert_view(user, client_id)
        if self.access.is_operations_admin(user):
            return
        if not self.access.can(user, "create_edit_workflows", client_id):
            raise ForbiddenError("Create / Edit Workflows permission required")

    def _version_entry(
        self,
        *,
        version: int,
        note: str,
        row: PayflowStrategyModel | None = None,
        steps: list[dict] | None = None,
        segment: dict | None = None,
        changes: list[str] | None = None,
        status: str | None = None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "version": version,
            "date": _stamp(),
            "note": note,
            "changes": list(changes or []),
        }
        if row is not None:
            entry["status"] = status or row.status
            entry["origin"] = origin or row.origin
            entry["approved_by"] = row.approved_by
            entry["approval_date"] = row.approval_date
            entry["steps"] = copy.deepcopy(steps if steps is not None else list(row.steps or []))
            entry["segment"] = copy.deepcopy(
                segment if segment is not None else (row.segment or {})
            )
        elif steps is not None:
            entry["steps"] = copy.deepcopy(steps)
            entry["segment"] = copy.deepcopy(segment or {})
            if status:
                entry["status"] = status
            if origin:
                entry["origin"] = origin
        return entry

    def _prepend_version(self, row: PayflowStrategyModel, entry: dict[str, Any]) -> None:
        versions = list(row.versions or [])
        versions.insert(0, entry)
        row.versions = versions

    def _archive_current(self, row: PayflowStrategyModel, *, note: str, changes: list[str] | None = None) -> None:
        self._prepend_version(
            row,
            self._version_entry(
                version=int(row.version or 1),
                note=note,
                row=row,
                changes=changes,
            ),
        )

    def _serialize(self, row: PayflowStrategyModel) -> dict[str, Any]:
        steps = list(row.steps or [])
        # Ensure timing labels present for older rows
        for step in steps:
            if isinstance(step, dict) and not step.get("timing"):
                step["timing"] = _timing_label(step)
        stats = _step_stats(steps)
        return {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "client_id": row.client_id,
            "client_code": row.client.code if row.client else None,
            "client_name": row.client.name if row.client else None,
            "portfolio_id": row.portfolio_id,
            "portfolio_name": row.portfolio.name if row.portfolio else None,
            "status": row.status,
            "origin": row.origin,
            "source": _row_source(row),
            "version": row.version,
            "summary": row.summary,
            "coverage": row.coverage,
            "cases_covered": _cases_covered(row.coverage),
            "segment": row.segment or {},
            "entry_node_id": row.entry_node_id,
            "steps": steps,
            "stats": stats,
            "ai_context": list(row.ai_context or []),
            "ai_proposal_snapshot": row.ai_proposal_snapshot,
            "versions": list(row.versions or []),
            "approved_by": row.approved_by,
            "approval_date": row.approval_date,
            "reviewed_by": row.reviewed_by,
            "created_by": row.created_by,
            "human_modified": _is_human_modified(row, steps),
            "awaiting_review": _awaiting_review(row.status),
            "last_updated_label": row.updated_at.strftime("%d %b %Y") if row.updated_at else None,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def list_strategies(
        self,
        user: UserModel,
        *,
        client_id: int | None = None,
        portfolio_id: int | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        allowed = self._visible_client_ids(user)
        q = self.db.query(PayflowStrategyModel).options(
            joinedload(PayflowStrategyModel.client),
            joinedload(PayflowStrategyModel.portfolio),
        )
        if allowed is not None:
            if not allowed:
                return self._empty()
            q = q.filter(PayflowStrategyModel.client_id.in_(allowed))
        if client_id is not None:
            self._assert_view(user, client_id)
            q = q.filter(PayflowStrategyModel.client_id == client_id)
        if portfolio_id is not None:
            q = q.filter(PayflowStrategyModel.portfolio_id == portfolio_id)
        if status:
            q = q.filter(PayflowStrategyModel.status == status)

        rows = q.order_by(PayflowStrategyModel.name.asc()).all()
        if not self.access.is_operations_admin(user):
            rows = [r for r in rows if self.access.can(user, "view_workflows", r.client_id)]
        if search:
            term = search.strip().lower()
            rows = [r for r in rows if term in (r.name or "").lower()]

        def count(status_value: str) -> int:
            return sum(1 for r in rows if r.status == status_value)

        awaiting = sum(1 for r in rows if _awaiting_review(r.status))
        return {
            "strategies": [self._serialize(r) for r in rows],
            "summary": {
                "total": len(rows),
                "ai_proposed": count(PayflowStrategyStatus.AI_PROPOSED.value),
                "draft": count(PayflowStrategyStatus.DRAFT.value),
                "under_review": count(PayflowStrategyStatus.UNDER_REVIEW.value),
                "approved": count(PayflowStrategyStatus.APPROVED.value),
                "active": count(PayflowStrategyStatus.ACTIVE.value),
                "inactive": count(PayflowStrategyStatus.INACTIVE.value),
                "awaiting_review": awaiting,
            },
            "statuses": [s.value for s in PayflowStrategyStatus],
            "step_kinds": STRATEGY_STEP_KINDS,
            "origins": [o.value for o in PayflowStrategyOrigin],
            "sources": [s.value for s in PayflowStrategySource],
            "catalog": _catalog(),
        }

    def get_strategy(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_view(user, row.client_id)
        return self._serialize(row)

    def create_strategy(self, user: UserModel, payload: dict[str, Any]) -> dict[str, Any]:
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValidationAppError("Strategy name is required")
        client_id = payload.get("client_id")
        if client_id is None:
            raise ValidationAppError("Client is required")
        client_id = int(client_id)
        self._assert_manage(user, client_id)
        client = (
            self.db.query(PayflowClientModel).filter(PayflowClientModel.id == client_id).first()
        )
        if not client:
            raise NotFoundError("Client not found")

        portfolio_id = payload.get("portfolio_id")
        if portfolio_id is None:
            raise ValidationAppError("Portfolio is required")
        portfolio = (
            self.db.query(PayflowPortfolioModel)
            .filter(
                PayflowPortfolioModel.id == int(portfolio_id),
                PayflowPortfolioModel.client_id == client_id,
            )
            .first()
        )
        if not portfolio:
            raise ValidationAppError("Portfolio not found for this client")
        portfolio_id = portfolio.id

        source_raw = (payload.get("source") or "").strip()
        status_raw = (payload.get("status") or "").strip()
        ai_generated = (
            source_raw == PayflowStrategySource.AI_GENERATED.value
            or status_raw == PayflowStrategyStatus.AI_PROPOSED.value
        )
        if ai_generated:
            source = PayflowStrategySource.AI_GENERATED.value
            origin = PayflowStrategyOrigin.AI_PROPOSED.value
            status = PayflowStrategyStatus.AI_PROPOSED.value
        else:
            source = PayflowStrategySource.HUMAN_CREATED.value
            origin = PayflowStrategyOrigin.HUMAN_CREATED.value
            status = status_raw or PayflowStrategyStatus.DRAFT.value
            if status == PayflowStrategyStatus.AI_PROPOSED.value:
                raise ValidationAppError("Human-created workflows cannot start as AI Proposed")
            if status not in {s.value for s in PayflowStrategyStatus}:
                raise ValidationAppError("Invalid status")
            # Manual create must not skip governance into Active
            if status in {
                PayflowStrategyStatus.ACTIVE.value,
                PayflowStrategyStatus.APPROVED.value,
            }:
                raise ValidationAppError(
                    "Newly created workflows must begin as Draft (or Under Review after submit)"
                )

        steps = _normalize_steps(
            payload.get("steps") or [],
            default_origin=origin,
        )
        entry_node_id = payload.get("entry_node_id") or (
            steps[0]["id"] if steps and steps[0].get("kind") == "Trigger" else steps[0]["id"]
        )
        coverage = _coverage_value(payload)
        segment = payload.get("segment") or {}
        summary = (payload.get("summary") or "").strip() or None
        ai_context = payload.get("ai_context") or []

        ai_snapshot = None
        if ai_generated:
            ai_snapshot = {
                "captured_at": _stamp_iso(),
                "summary": summary,
                "segment": copy.deepcopy(segment),
                "steps": copy.deepcopy(steps),
                "entry_node_id": entry_node_id,
                "ai_context": copy.deepcopy(ai_context),
            }

        code = self._unique_code(_slugify(name))
        row = PayflowStrategyModel(
            code=code,
            name=name,
            client_id=client_id,
            portfolio_id=portfolio_id,
            status=status,
            origin=origin,
            source=source,
            version=1,
            summary=summary,
            coverage=coverage,
            segment=segment,
            steps=steps,
            entry_node_id=entry_node_id,
            ai_context=ai_context,
            ai_proposal_snapshot=ai_snapshot,
            versions=[
                self._version_entry(
                    version=1,
                    note=(
                        "Proposed by PayFlow AI"
                        if ai_generated
                        else f"Created by {user.full_name}"
                    ),
                    steps=steps,
                    segment=segment,
                    status=status,
                    origin=origin,
                )
            ],
            reviewed_by=None,
            created_by=user.full_name,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        if row.status in AWAITING_STATUSES:
            self._notify_awaiting(row, user)
        return self._serialize(self._get_row(row.id))

    def begin_review(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_manage(user, row.client_id)
        if row.status != PayflowStrategyStatus.AI_PROPOSED.value:
            raise ValidationAppError("Only AI Proposed workflows can begin review this way")
        row.status = PayflowStrategyStatus.UNDER_REVIEW.value
        row.reviewed_by = user.full_name
        self._prepend_version(
            row,
            {
                "version": int(row.version or 1),
                "date": _stamp(),
                "note": f"Review started by {user.full_name}",
                "changes": [],
            },
        )
        self.db.commit()
        self.db.refresh(row)
        self._notify_awaiting(row, user)
        return self._serialize(self._get_row(row.id))

    def update_strategy(
        self, user: UserModel, strategy_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_manage(user, row.client_id)

        before_steps = copy.deepcopy(list(row.steps or []))
        was_ai_proposed = row.status == PayflowStrategyStatus.AI_PROPOSED.value
        was_approved_or_active = row.status in {
            PayflowStrategyStatus.APPROVED.value,
            PayflowStrategyStatus.ACTIVE.value,
        }

        if was_approved_or_active:
            self._archive_current(
                row,
                note=f"Archived approved/active configuration before edit by {user.full_name}",
            )
            row.version = int(row.version or 1) + 1
            row.status = PayflowStrategyStatus.UNDER_REVIEW.value

        if "name" in payload and payload["name"] is not None:
            name = str(payload["name"]).strip()
            if not name:
                raise ValidationAppError("Strategy name is required")
            row.name = name
        if "summary" in payload:
            row.summary = (payload.get("summary") or "").strip() or None
        if "segment" in payload and payload["segment"] is not None:
            row.segment = payload["segment"]
        if "entry_node_id" in payload:
            row.entry_node_id = payload.get("entry_node_id")
        if "coverage" in payload or "cases_covered" in payload:
            row.coverage = _coverage_value(payload, row.coverage)

        source = _row_source(row)
        if "steps" in payload and payload["steps"] is not None:
            default_origin = (
                PayflowStrategyOrigin.HUMAN_MODIFIED.value
                if source == PayflowStrategySource.AI_GENERATED.value
                else PayflowStrategyOrigin.HUMAN_CREATED.value
            )
            row.steps = _normalize_steps(payload["steps"], default_origin=default_origin)
            if not row.entry_node_id and row.steps:
                row.entry_node_id = row.steps[0].get("id")

        changes = _diff_steps(before_steps, list(row.steps or []))

        # Preserve original AI proposal on first human edit
        if (
            source == PayflowStrategySource.AI_GENERATED.value
            and not row.ai_proposal_snapshot
            and before_steps
        ):
            row.ai_proposal_snapshot = {
                "captured_at": _stamp_iso(),
                "summary": row.summary,
                "segment": copy.deepcopy(row.segment or {}),
                "steps": before_steps,
                "entry_node_id": row.entry_node_id,
                "ai_context": copy.deepcopy(list(row.ai_context or [])),
            }

        if source == PayflowStrategySource.AI_GENERATED.value:
            row.origin = PayflowStrategyOrigin.HUMAN_MODIFIED.value
            row.source = PayflowStrategySource.AI_GENERATED.value
        elif row.origin == PayflowStrategyOrigin.AI_PROPOSED.value:
            row.origin = PayflowStrategyOrigin.HUMAN_MODIFIED.value

        if was_ai_proposed or row.status == PayflowStrategyStatus.AI_PROPOSED.value:
            row.status = PayflowStrategyStatus.UNDER_REVIEW.value
            row.reviewed_by = row.reviewed_by or user.full_name

        if changes:
            self._prepend_version(
                row,
                {
                    "version": int(row.version or 1),
                    "date": _stamp(),
                    "note": f"Updated by {user.full_name}",
                    "changes": changes,
                    "status": row.status,
                    "origin": row.origin,
                },
            )

        # Editing never activates
        if row.status == PayflowStrategyStatus.ACTIVE.value:
            row.status = PayflowStrategyStatus.UNDER_REVIEW.value

        self.db.commit()
        self.db.refresh(row)
        return self._serialize(self._get_row(row.id))

    def save_draft(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_manage(user, row.client_id)

        if row.status in {
            PayflowStrategyStatus.APPROVED.value,
            PayflowStrategyStatus.ACTIVE.value,
        }:
            self._archive_current(
                row,
                note=f"Archived before draft save by {user.full_name}",
            )
            row.version = int(row.version or 1) + 1
            row.status = PayflowStrategyStatus.UNDER_REVIEW.value
        elif row.status == PayflowStrategyStatus.AI_PROPOSED.value:
            if (
                _row_source(row) == PayflowStrategySource.AI_GENERATED.value
                and not row.ai_proposal_snapshot
            ):
                row.ai_proposal_snapshot = {
                    "captured_at": _stamp_iso(),
                    "summary": row.summary,
                    "segment": copy.deepcopy(row.segment or {}),
                    "steps": copy.deepcopy(list(row.steps or [])),
                    "entry_node_id": row.entry_node_id,
                    "ai_context": copy.deepcopy(list(row.ai_context or [])),
                }
            row.status = PayflowStrategyStatus.UNDER_REVIEW.value
            row.origin = PayflowStrategyOrigin.HUMAN_MODIFIED.value
            row.source = PayflowStrategySource.AI_GENERATED.value
            row.reviewed_by = row.reviewed_by or user.full_name
        elif row.status == PayflowStrategyStatus.DRAFT.value:
            pass  # remain Draft
        elif row.status == PayflowStrategyStatus.UNDER_REVIEW.value:
            if _row_source(row) == PayflowStrategySource.AI_GENERATED.value:
                row.origin = PayflowStrategyOrigin.HUMAN_MODIFIED.value

        self._prepend_version(
            row,
            {
                "version": int(row.version or 1),
                "date": _stamp(),
                "note": f"Draft saved by {user.full_name}",
                "changes": [],
                "status": row.status,
                "origin": row.origin,
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(self._get_row(row.id))

    def submit_review(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_manage(user, row.client_id)
        if row.status != PayflowStrategyStatus.DRAFT.value:
            raise ValidationAppError("Only Draft workflows can be submitted for review")
        if not row.steps:
            raise ValidationAppError("Cannot submit an empty workflow for review")
        row.status = PayflowStrategyStatus.UNDER_REVIEW.value
        row.reviewed_by = user.full_name
        self._prepend_version(
            row,
            {
                "version": int(row.version or 1),
                "date": _stamp(),
                "note": f"Submitted for review by {user.full_name}",
                "changes": [],
                "status": row.status,
                "origin": row.origin,
            },
        )
        self.db.commit()
        self.db.refresh(row)
        self._notify_awaiting(row, user)
        return self._serialize(self._get_row(row.id))

    def approve(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_manage(user, row.client_id)
        if row.status != PayflowStrategyStatus.UNDER_REVIEW.value:
            raise ValidationAppError(
                "Only workflows Under Review can be approved "
                "(AI Proposed workflows cannot bypass human review into Active)"
            )

        # Keep Human Modified if already modified; do not revert to AI Proposed
        human_modified = _is_human_modified(row, list(row.steps or []))
        source = _row_source(row)
        if human_modified:
            row.origin = PayflowStrategyOrigin.HUMAN_MODIFIED.value
        elif source == PayflowStrategySource.HUMAN_CREATED.value:
            row.origin = PayflowStrategyOrigin.HUMAN_CREATED.value
        row.source = source

        row.status = PayflowStrategyStatus.APPROVED.value
        row.approved_by = user.full_name
        row.approval_date = _stamp()
        row.reviewed_by = row.reviewed_by or user.full_name
        row.version = int(row.version or 1) + 1
        self._prepend_version(
            row,
            self._version_entry(
                version=row.version,
                note=f"Approved by {user.full_name}",
                row=row,
            ),
        )
        self.db.commit()
        self.db.refresh(row)
        self._mark_notifications_read(row)
        return self._serialize(self._get_row(row.id))

    def reject(self, user: UserModel, strategy_id: int, *, note: str | None = None) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_manage(user, row.client_id)
        if row.status not in {
            PayflowStrategyStatus.UNDER_REVIEW.value,
            PayflowStrategyStatus.AI_PROPOSED.value,
            PayflowStrategyStatus.APPROVED.value,
        }:
            raise ValidationAppError("This workflow cannot be rejected in its current status")

        source = _row_source(row)
        if source == PayflowStrategySource.AI_GENERATED.value:
            row.status = PayflowStrategyStatus.AI_PROPOSED.value
            row.source = PayflowStrategySource.AI_GENERATED.value
            # Keep Human Modified marker if steps were changed; otherwise AI Proposed
            if _is_human_modified(row, list(row.steps or [])):
                row.origin = PayflowStrategyOrigin.HUMAN_MODIFIED.value
            else:
                row.origin = PayflowStrategyOrigin.AI_PROPOSED.value
        else:
            row.status = PayflowStrategyStatus.DRAFT.value
            row.origin = PayflowStrategyOrigin.HUMAN_CREATED.value
            row.source = PayflowStrategySource.HUMAN_CREATED.value

        self._prepend_version(
            row,
            {
                "version": int(row.version or 1),
                "date": _stamp(),
                "note": note or f"Rejected / regeneration requested by {user.full_name}",
                "changes": [],
                "status": row.status,
                "origin": row.origin,
            },
        )
        self.db.commit()
        self.db.refresh(row)
        if row.status in AWAITING_STATUSES:
            self._notify_awaiting(row, user)
        return self._serialize(self._get_row(row.id))

    def activate(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_manage(user, row.client_id)
        if row.status in {
            PayflowStrategyStatus.AI_PROPOSED.value,
            PayflowStrategyStatus.DRAFT.value,
            PayflowStrategyStatus.UNDER_REVIEW.value,
        }:
            raise ValidationAppError(
                "AI Proposed, Draft or Under Review workflows cannot be activated"
            )
        if row.status == PayflowStrategyStatus.ACTIVE.value:
            return self._serialize(row)
        if row.status == PayflowStrategyStatus.APPROVED.value:
            pass
        elif row.status == PayflowStrategyStatus.INACTIVE.value and row.approved_by:
            pass
        else:
            raise ValidationAppError("Only an Approved workflow can become Active")

        row.status = PayflowStrategyStatus.ACTIVE.value
        self._prepend_version(
            row,
            {
                "version": int(row.version or 1),
                "date": _stamp(),
                "note": f"Activated by {user.full_name}",
                "changes": [],
                "status": row.status,
                "origin": row.origin,
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(self._get_row(row.id))

    def deactivate(self, user: UserModel, strategy_id: int) -> dict[str, Any]:
        row = self._get_row(strategy_id)
        self._assert_manage(user, row.client_id)
        if row.status != PayflowStrategyStatus.ACTIVE.value:
            raise ValidationAppError("Only Active workflows can be deactivated")
        row.status = PayflowStrategyStatus.INACTIVE.value
        self._prepend_version(
            row,
            {
                "version": int(row.version or 1),
                "date": _stamp(),
                "note": f"Deactivated by {user.full_name}",
                "changes": [],
                "status": row.status,
                "origin": row.origin,
            },
        )
        self.db.commit()
        self.db.refresh(row)
        return self._serialize(self._get_row(row.id))

    def delete_strategy(
        self, user: UserModel, strategy_id: int, *, source: str | None = None
    ) -> dict[str, Any]:
        if not self.access.is_operations_admin(user):
            raise ForbiddenError("Operations Admin access required")
        row = self._get_row(strategy_id)
        snapshot = snapshot_model(row)
        record_deletion(
            self.db,
            actor=user,
            module="payflow",
            entity_type="workflow",
            entity_id=row.id,
            entity_label=row.name,
            source=source,
            record_snapshot=snapshot,
            related_deleted=[],
        )
        self.db.delete(row)
        self.db.commit()
        return {"message": "Workflow deleted"}

    def _notify_awaiting(self, row: PayflowStrategyModel, user: UserModel) -> None:
        try:
            from app.modules.payflow.services.notification_service import (
                PayflowNotificationService,
            )

            PayflowNotificationService(self.db).notify_workflow_awaiting(
                strategy_id=row.id,
                strategy_name=row.name,
                client_id=row.client_id,
                actor_user_id=user.id,
                send_mail=True,
            )
        except Exception:
            pass

    def _mark_notifications_read(self, row: PayflowStrategyModel) -> None:
        try:
            from app.modules.payflow.services.notification_service import (
                PayflowNotificationService,
            )

            PayflowNotificationService(self.db).mark_entity_read(
                entity_type="workflow", entity_id=row.id
            )
        except Exception:
            pass

    def _get_row(self, strategy_id: int) -> PayflowStrategyModel:
        row = (
            self.db.query(PayflowStrategyModel)
            .options(
                joinedload(PayflowStrategyModel.client),
                joinedload(PayflowStrategyModel.portfolio),
            )
            .filter(PayflowStrategyModel.id == strategy_id)
            .first()
        )
        if not row:
            raise NotFoundError("Strategy not found")
        return row

    def _unique_code(self, base: str) -> str:
        code = base
        n = 2
        while self.db.query(PayflowStrategyModel).filter(PayflowStrategyModel.code == code).first():
            code = f"{base}-{n}"
            n += 1
        return code

    def _empty(self) -> dict[str, Any]:
        return {
            "strategies": [],
            "summary": {
                "total": 0,
                "ai_proposed": 0,
                "draft": 0,
                "under_review": 0,
                "approved": 0,
                "active": 0,
                "inactive": 0,
                "awaiting_review": 0,
            },
            "statuses": [s.value for s in PayflowStrategyStatus],
            "step_kinds": STRATEGY_STEP_KINDS,
            "origins": [o.value for o in PayflowStrategyOrigin],
            "sources": [s.value for s in PayflowStrategySource],
            "catalog": _catalog(),
        }
