"""Compliance rule catalogue: save, validate, approve, version, audit; holiday calendar.

Lifecycle
    Draft ──submit──► Pending Approval ──approve──► Active ◄──activate── Inactive
      ▲                     │                         │                      ▲
      └──────reject─────────┘                         └──────deactivate──────┘
    Anything that is not Active can be archived.

Rules are saved leniently (a draft may be incomplete) and checked strictly before
they can be submitted, approved or activated: an invalid rule is never in force.
Every save writes an immutable version row and an audit row with before / after.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationAppError,
)
from app.infrastructure.database.models import (
    PayflowClientModel,
    PayflowComplianceAuditModel,
    PayflowComplianceHolidayModel,
    PayflowComplianceRuleModel,
    PayflowComplianceRuleVersionModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.modules.payflow.services.compliance import catalog

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TIME_KEYS = (
    "start", "end", "weekend_start", "weekend_end",
    "saturday_start", "saturday_end", "sunday_start", "sunday_end",
)
_SORTABLE = {
    "name": PayflowComplianceRuleModel.name,
    "code": PayflowComplianceRuleModel.code,
    "status": PayflowComplianceRuleModel.status,
    "category": PayflowComplianceRuleModel.category,
    "rule_type": PayflowComplianceRuleModel.rule_type,
    "country": PayflowComplianceRuleModel.country,
    "state": PayflowComplianceRuleModel.state,
    "priority": PayflowComplianceRuleModel.priority,
    "effective_from": PayflowComplianceRuleModel.effective_from,
    "updated_at": PayflowComplianceRuleModel.updated_at,
    "created_at": PayflowComplianceRuleModel.created_at,
}
# Fields a caller may set. Status, version and approval fields move only through actions.
_EDITABLE = (
    "name", "description", "rule_type", "category", "enforcement", "regulatory_authority",
    "legal_reference", "source", "jurisdiction_type", "country", "state", "region", "city",
    "postal", "collector_type", "debt_type", "channel", "logic", "conditions", "settings",
    "action", "override_policy", "priority", "effective_from", "effective_until", "client_id",
)
_SCOPE_KEYS = (
    "rule_type", "country", "state", "region", "city", "postal", "channel",
    "collector_type", "debt_type", "client_id",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: Any, label: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationAppError(f"{label} must be a date (YYYY-MM-DD)") from exc


def _text(value: Any) -> str:
    return str(value or "").strip()


def serialize_rule(rule: PayflowComplianceRuleModel) -> dict[str, Any]:
    return {
        "id": rule.id,
        "code": rule.code,
        "name": rule.name,
        "description": rule.description,
        "category": rule.category,
        "rule_type": rule.rule_type,
        "rule_type_name": (catalog.RULE_TYPE_BY_CODE.get(rule.rule_type) or {}).get("name"),
        "enforcement": rule.enforcement,
        "regulatory_authority": rule.regulatory_authority,
        "legal_reference": rule.legal_reference,
        "source": rule.source,
        "jurisdiction_type": rule.jurisdiction_type,
        "country": rule.country,
        "state": rule.state,
        "region": rule.region,
        "city": rule.city,
        "postal": rule.postal,
        "timezone": catalog.timezone_for(rule.country, rule.state),
        "collector_type": rule.collector_type,
        "debt_type": rule.debt_type,
        "channel": rule.channel,
        "logic": rule.logic,
        "conditions": list(rule.conditions or []),
        "settings": dict(rule.settings or {}),
        "action": rule.action,
        "override_policy": rule.override_policy,
        "priority": rule.priority,
        "effective_from": rule.effective_from.isoformat() if rule.effective_from else None,
        "effective_until": rule.effective_until.isoformat() if rule.effective_until else None,
        "status": rule.status,
        "version": rule.version,
        "client_id": rule.client_id,
        "is_library": bool(rule.is_library),
        "created_by": rule.created_by,
        "updated_by": rule.updated_by,
        "submitted_by": rule.submitted_by,
        "approved_by": rule.approved_by,
        "approved_at": rule.approved_at.isoformat() if rule.approved_at else None,
        "rejection_reason": rule.rejection_reason,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }


def engine_rule(rule: PayflowComplianceRuleModel) -> dict[str, Any]:
    """The rule in the shape the evaluation engine reads."""
    return {
        "id": rule.id,
        "code": rule.code,
        "name": rule.name,
        "version": rule.version,
        "category": rule.category,
        "rule_type": rule.rule_type,
        "enforcement": rule.enforcement,
        "status": rule.status,
        "action": rule.action,
        "channel": rule.channel,
        "logic": rule.logic,
        "priority": rule.priority,
        "country": rule.country,
        "state": rule.state,
        "region": rule.region,
        "city": rule.city,
        "postal": rule.postal,
        "client_id": rule.client_id,
        "collector_type": rule.collector_type,
        "debt_type": rule.debt_type,
        "effective_from": rule.effective_from,
        "effective_until": rule.effective_until,
        "conditions": list(rule.conditions or []),
        "settings": dict(rule.settings or {}),
    }


# --------------------------------------------------------------------------- validation


def _clean_conditions(raw: Any) -> list[dict[str, Any]]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValidationAppError("conditions must be a list")
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationAppError("Each condition must be an object")
        name = _text(item.get("field"))
        operator = _text(item.get("operator")) or "Equals"
        if name not in catalog.CONDITION_FIELDS:
            raise ValidationAppError(f"Unknown condition field: {name or '(empty)'}")
        if operator not in catalog.OPERATORS:
            raise ValidationAppError(f"Unknown condition operator: {operator}")
        try:
            period = int(item.get("period") or 7)
        except (TypeError, ValueError) as exc:
            raise ValidationAppError("Condition period must be a number of days") from exc
        cleaned.append(
            {
                "id": _text(item.get("id")) or f"c{len(cleaned) + 1}",
                "field": name,
                "operator": operator,
                "value": _text(item.get("value")),
                "period": period,
            }
        )
    return cleaned


def _clean_settings(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValidationAppError("settings must be an object")
    settings = dict(raw)
    for key in _TIME_KEYS:
        value = _text(settings.get(key))
        if value and not _TIME_RE.match(value):
            raise ValidationAppError(f"settings.{key} must be a time (HH:MM, 24-hour)")
        if key in settings:
            settings[key] = value
    if "days" in settings:
        days = settings.get("days") or []
        if not isinstance(days, list) or any(
            not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6 for d in days
        ):
            raise ValidationAppError("settings.days must list days 0 (Sunday) to 6 (Saturday)")
        settings["days"] = sorted(set(days))
    for key in ("interval_hours", "daily", "weekly", "monthly", "consent_expiry_days", "retention_days"):
        if key in settings and settings[key] not in (None, ""):
            try:
                number = float(settings[key])
            except (TypeError, ValueError) as exc:
                raise ValidationAppError(f"settings.{key} must be a number") from exc
            if number < 0:
                raise ValidationAppError(f"settings.{key} cannot be negative")
            settings[key] = int(number) if number == int(number) else number
    events = settings.get("count_events")
    if events is not None:
        if not isinstance(events, list) or any(e not in catalog.COUNT_EVENTS for e in events):
            raise ValidationAppError(
                f"settings.count_events may contain: {', '.join(catalog.COUNT_EVENTS)}"
            )
    scope = settings.get("scope")
    if scope not in (None, "") and scope not in catalog.COUNT_SCOPES:
        raise ValidationAppError(f"settings.scope must be one of: {', '.join(catalog.COUNT_SCOPES)}")
    return settings


def _choice(value: Any, allowed: list[str], label: str, *, default: str | None = None) -> str:
    text = _text(value) or (default or "")
    if text not in allowed:
        raise ValidationAppError(f"{label} must be one of: {', '.join(allowed)}")
    return text


def activation_problems(data: dict[str, Any]) -> list[str]:
    """Everything that stops a rule from being in force. Empty list = ready."""
    problems: list[str] = []
    kind = catalog.RULE_TYPE_BY_CODE.get(data.get("rule_type") or "", {})
    settings = data.get("settings") or {}
    conditions = data.get("conditions") or []

    if not _text(data.get("legal_reference")):
        problems.append("Legal source / reference is required")
    if not data.get("effective_from"):
        problems.append("Effective date is required")
    start_on, end_on = data.get("effective_from"), data.get("effective_until")
    if start_on and end_on and end_on < start_on:
        problems.append("Expiry date cannot be before the effective date")
    if data.get("enforcement") == catalog.ENFORCEMENT_MANDATORY and data.get("action") == "Allow":
        problems.append(
            "Action 'Allow' is not a restriction: a Mandatory rule needs a restricting action"
        )
    if (
        data.get("enforcement") == catalog.ENFORCEMENT_MANDATORY
        and data.get("override_policy") == "Client Configurable"
    ):
        problems.append("A Mandatory rule cannot be overridden by the client")

    start, end = _text(settings.get("start")), _text(settings.get("end"))
    needs = kind.get("needs") or []
    if "hours" in needs:
        if not start or not end:
            problems.append("Permitted hours need a start time and an end time")
        elif start >= end:
            problems.append("Start time must be before the end time")
        if not settings.get("days"):
            problems.append("Select at least one permitted day")
    for prefix in ("weekend", "saturday", "sunday"):
        first, last = _text(settings.get(f"{prefix}_start")), _text(settings.get(f"{prefix}_end"))
        if bool(first) != bool(last):
            problems.append(f"{prefix.title()} hours need both a start time and an end time")
        elif first and first >= last:
            problems.append(f"{prefix.title()} start time must be before its end time")
    if "days" in needs and not settings.get("days"):
        problems.append("Select at least one permitted day")

    def _positive(value: Any) -> bool:
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False

    if "limit" in needs:
        threshold = any(
            c.get("field") in catalog.NUMERIC_FIELDS
            and _text(c.get("value")) != ""
            and _positive(c.get("period"))
            and _is_number(c.get("value"))
            for c in conditions
        )
        configured = any(_positive(settings.get(k)) for k in ("daily", "weekly", "monthly", "interval_hours"))
        if not threshold and not configured:
            problems.append("A frequency rule needs a threshold and a rolling period")
    if "period" in needs and not any(
        c.get("field") in catalog.NUMERIC_FIELDS and _positive(c.get("value")) for c in conditions
    ):
        problems.append("This rule type needs a period of at least one day")
    if "retention" in needs and not _positive(settings.get("retention_days")):
        problems.append("Record retention needs a number of days")

    for condition in conditions:
        if condition.get("field") in catalog.NUMERIC_FIELDS and condition.get("operator") != "Between":
            if not _is_number(condition.get("value")):
                problems.append(f"{condition.get('field')} needs a numeric value")
    triggers = bool(conditions) or any(
        _positive(settings.get(k))
        for k in ("daily", "weekly", "monthly", "interval_hours", "consent_expiry_days")
    ) or bool(_text(settings.get("required_text")))
    if not triggers:
        problems.append("Add at least one condition or limit: this rule can never apply")
    return problems


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


# --------------------------------------------------------------------------- service


class ComplianceRuleService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    # ----------------------------------------------------------------- authorization
    def _assert_view(self, user: UserModel) -> None:
        if not (self.access.is_operations_admin(user) or self.access.can(user, "view_rules")):
            raise ForbiddenError("Permission required: view_rules")

    def _assert_edit(self, user: UserModel) -> None:
        if not (self.access.is_operations_admin(user) or self.access.can(user, "modify_governance")):
            raise ForbiddenError("Permission required: modify_governance")

    def _assert_admin(self, user: UserModel) -> None:
        if not self.access.is_operations_admin(user):
            raise ForbiddenError("Operations Admin access required")

    def _visible_client_ids(self, user: UserModel) -> list[int] | None:
        if self.access.is_operations_admin(user):
            return None
        return list(self.access.build_context(user)["client_ids"])

    # ----------------------------------------------------------------- audit / versions
    def audit(
        self,
        *,
        entity_type: str,
        event: str,
        actor: UserModel | None = None,
        actor_name: str | None = None,
        entity_id: Any = None,
        rule: PayflowComplianceRuleModel | None = None,
        rule_name: str | None = None,
        client_id: int | None = None,
        account_id: int | None = None,
        reason: str | None = None,
        before: Any = None,
        after: Any = None,
        approval: str | None = None,
    ) -> PayflowComplianceAuditModel:
        row = PayflowComplianceAuditModel(
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else (str(rule.id) if rule else None),
            rule_id=rule.id if rule else None,
            rule_name=rule.name if rule else rule_name,
            client_id=client_id if client_id is not None else (rule.client_id if rule else None),
            account_id=account_id,
            event=event[:255],
            reason=reason,
            before=before,
            after=after,
            approval=approval,
            actor_user_id=actor.id if actor else None,
            actor_name=(actor.full_name if actor else None) or actor_name or "PayFlow",
        )
        self.db.add(row)
        return row

    def _snapshot(
        self, rule: PayflowComplianceRuleModel, *, event: str, by: str | None, reason: str | None
    ) -> None:
        self.db.add(
            PayflowComplianceRuleVersionModel(
                rule_id=rule.id,
                version=rule.version,
                status=rule.status,
                event=event,
                snapshot=serialize_rule(rule),
                change_reason=reason,
                changed_by=by,
            )
        )

    # ----------------------------------------------------------------- normalise
    def _normalized(self, payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        """Payload merged over the current values, cleaned and checked for save."""
        data = dict(current or {})
        for key in _EDITABLE:
            if key in payload:
                data[key] = payload[key]

        data["name"] = _text(data.get("name"))
        if not data["name"]:
            raise ValidationAppError("Rule name is required")
        rule_type = _text(data.get("rule_type"))
        kind = catalog.RULE_TYPE_BY_CODE.get(rule_type)
        if not kind:
            raise ValidationAppError(f"Unknown rule type: {rule_type or '(empty)'}")
        data["rule_type"] = rule_type
        data["category"] = _choice(data.get("category"), catalog.CATEGORIES, "Category", default=kind["category"])
        data["enforcement"] = _choice(
            data.get("enforcement"), catalog.ENFORCEMENTS, "Enforcement", default=catalog.ENFORCEMENT_MANDATORY
        )
        data["action"] = _choice(data.get("action"), catalog.ACTIONS, "Action", default=kind["action"])
        data["channel"] = _choice(data.get("channel"), catalog.CHANNELS, "Channel", default="All Channels")
        data["logic"] = _choice(_text(data.get("logic")).upper(), catalog.LOGICS, "Logic", default="AND")
        if _text(data.get("override_policy")):
            data["override_policy"] = _choice(
                data.get("override_policy"), catalog.OVERRIDE_POLICIES, "Override policy"
            )
        else:
            data["override_policy"] = None
        if _text(data.get("jurisdiction_type")):
            data["jurisdiction_type"] = _choice(
                data.get("jurisdiction_type"), catalog.JURISDICTION_TYPES, "Jurisdiction type"
            )
        else:
            data["jurisdiction_type"] = None

        country = catalog.find_country(data.get("country"))
        if not country:
            supported = ", ".join(c["name"] for c in catalog.COUNTRIES)
            raise ValidationAppError(f"Country is required and must be one of: {supported}")
        state_text = _text(data.get("state"))
        state = catalog.find_state(country, state_text)
        if state_text and not state:
            raise ValidationAppError(f"'{state_text}' is not a state / province of {country['name']}")
        data["country"] = country["name"]
        data["state"] = state["name"] if state else ""
        for key in ("region", "city", "postal", "collector_type", "debt_type"):
            data[key] = _text(data.get(key))
        data["postal"] = data["postal"].upper()
        if not data["jurisdiction_type"]:
            data["jurisdiction_type"] = (
                "Postal" if data["postal"] else "Municipal" if data["city"]
                else "Regional" if data["region"] else "State / Province" if data["state"] else "Federal"
            )

        data["conditions"] = _clean_conditions(data.get("conditions"))
        data["settings"] = _clean_settings(data.get("settings"))
        try:
            data["priority"] = int(data.get("priority") if data.get("priority") not in (None, "") else 100)
        except (TypeError, ValueError) as exc:
            raise ValidationAppError("Priority must be a number") from exc
        data["effective_from"] = _parse_date(data.get("effective_from"), "Effective date")
        data["effective_until"] = _parse_date(data.get("effective_until"), "Expiry date")
        for key in ("description", "regulatory_authority", "legal_reference", "source"):
            data[key] = _text(data.get(key)) or None

        client_id = data.get("client_id")
        if client_id in ("", 0):
            client_id = None
        if client_id is not None:
            if not self.db.get(PayflowClientModel, int(client_id)):
                raise ValidationAppError("Client not found")
            client_id = int(client_id)
        data["client_id"] = client_id
        return data

    def _apply(self, rule: PayflowComplianceRuleModel, data: dict[str, Any]) -> None:
        for key in _EDITABLE:
            setattr(rule, key, data[key])

    def _current(self, rule: PayflowComplianceRuleModel) -> dict[str, Any]:
        return {key: getattr(rule, key) for key in _EDITABLE}

    def _next_code(self) -> str:
        while True:
            code = f"CMP-{uuid.uuid4().hex[:8].upper()}"
            if not self.db.query(PayflowComplianceRuleModel.id).filter_by(code=code).first():
                return code

    def _get(self, rule_id: int) -> PayflowComplianceRuleModel:
        rule = self.db.get(PayflowComplianceRuleModel, rule_id)
        if not rule:
            raise NotFoundError("Compliance rule not found")
        return rule

    def _assert_ready(self, rule: PayflowComplianceRuleModel) -> None:
        problems = activation_problems(self._current(rule))
        if problems:
            raise ValidationAppError("This rule cannot be put in force: " + "; ".join(problems))

    def _assert_no_overlap(self, rule: PayflowComplianceRuleModel) -> None:
        """Two Active rules with the same scope and overlapping dates would be ambiguous."""
        query = self.db.query(PayflowComplianceRuleModel).filter(
            PayflowComplianceRuleModel.id != rule.id,
            PayflowComplianceRuleModel.status == catalog.STATUS_ACTIVE,
        )
        for key in _SCOPE_KEYS:
            column = getattr(PayflowComplianceRuleModel, key)
            value = getattr(rule, key)
            query = query.filter(column.is_(None) if value is None else column == value)
        far = date.max
        for other in query.all():
            starts_before_other_ends = (rule.effective_from or date.min) <= (other.effective_until or far)
            other_starts_before_ends = (other.effective_from or date.min) <= (rule.effective_until or far)
            if starts_before_other_ends and other_starts_before_ends:
                raise ConflictError(
                    f"Rule {other.code} ({other.name}) is already Active for the same scope and "
                    "dates. Give one of them an expiry date so the versions do not overlap."
                )

    def _transition(
        self,
        rule: PayflowComplianceRuleModel,
        new_status: str,
        *,
        actor: UserModel,
        reason: str | None,
        approval: str | None = None,
    ) -> dict[str, Any]:
        before = serialize_rule(rule)
        old = rule.status
        rule.status = new_status
        rule.updated_by = actor.full_name
        self.db.flush()
        event = f"Status: {old} → {new_status}"
        self._snapshot(rule, event=event, by=actor.full_name, reason=reason)
        self.audit(
            entity_type="rule", event=event, actor=actor, rule=rule, reason=reason,
            before=before, after=serialize_rule(rule), approval=approval,
        )
        self.db.commit()
        self.db.refresh(rule)
        return serialize_rule(rule)

    # ----------------------------------------------------------------- read
    def list_rules(
        self,
        user: UserModel,
        *,
        status: str | None = None,
        category: str | None = None,
        rule_type: str | None = None,
        country: str | None = None,
        state: str | None = None,
        channel: str | None = None,
        enforcement: str | None = None,
        client_id: int | None = None,
        search: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._assert_view(user)
        query = self.db.query(PayflowComplianceRuleModel)
        visible = self._visible_client_ids(user)
        if visible is not None:
            query = query.filter(
                or_(
                    PayflowComplianceRuleModel.client_id.is_(None),
                    PayflowComplianceRuleModel.client_id.in_(visible or [-1]),
                )
            )
        if status:
            query = query.filter(PayflowComplianceRuleModel.status == status)
        else:
            query = query.filter(PayflowComplianceRuleModel.status != catalog.STATUS_ARCHIVED)
        if category:
            query = query.filter(PayflowComplianceRuleModel.category == category)
        if rule_type:
            query = query.filter(PayflowComplianceRuleModel.rule_type == rule_type)
        if country:
            name, _ = catalog.normalize_jurisdiction(country, None)
            query = query.filter(PayflowComplianceRuleModel.country == name)
        if state:
            # "ON", "Ontario" or "ontario": resolve in the chosen country, else in any.
            where = [catalog.find_country(country)] if country else catalog.COUNTRIES
            names = [
                found["name"]
                for found in (catalog.find_state(c, state) for c in where if c)
                if found
            ] or [state.strip()]
            query = query.filter(PayflowComplianceRuleModel.state.in_(names))
        if channel:
            query = query.filter(PayflowComplianceRuleModel.channel == channel)
        if enforcement:
            query = query.filter(PayflowComplianceRuleModel.enforcement == enforcement)
        if client_id is not None:
            query = query.filter(PayflowComplianceRuleModel.client_id == client_id)
        if search and search.strip():
            like = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    PayflowComplianceRuleModel.name.ilike(like),
                    PayflowComplianceRuleModel.code.ilike(like),
                    PayflowComplianceRuleModel.description.ilike(like),
                    PayflowComplianceRuleModel.legal_reference.ilike(like),
                )
            )
        total = query.count()
        column = _SORTABLE.get(sort, PayflowComplianceRuleModel.updated_at)
        ordered = column.asc() if (order or "").lower() == "asc" else column.desc()
        rows = (
            query.order_by(ordered, PayflowComplianceRuleModel.id.asc())
            .offset(max(0, int(offset)))
            .limit(max(1, min(int(limit), 200)))
            .all()
        )
        return {
            "rules": [serialize_rule(r) for r in rows],
            "total": total,
            "limit": max(1, min(int(limit), 200)),
            "offset": max(0, int(offset)),
        }

    def get_rule(self, user: UserModel, rule_id: int) -> dict[str, Any]:
        self._assert_view(user)
        rule = self._get(rule_id)
        visible = self._visible_client_ids(user)
        if visible is not None and rule.client_id is not None and rule.client_id not in visible:
            raise NotFoundError("Compliance rule not found")
        out = serialize_rule(rule)
        out["activation_problems"] = activation_problems(self._current(rule))
        return out

    def history(self, user: UserModel, rule_id: int) -> dict[str, Any]:
        self._assert_view(user)
        rule = self._get(rule_id)
        versions = (
            self.db.query(PayflowComplianceRuleVersionModel)
            .filter(PayflowComplianceRuleVersionModel.rule_id == rule.id)
            .order_by(PayflowComplianceRuleVersionModel.id.desc())
            .all()
        )
        audit = (
            self.db.query(PayflowComplianceAuditModel)
            .filter(PayflowComplianceAuditModel.rule_id == rule.id)
            .order_by(PayflowComplianceAuditModel.id.desc())
            .all()
        )
        return {
            "rule_id": rule.id,
            "code": rule.code,
            "current_version": rule.version,
            "versions": [
                {
                    "id": v.id,
                    "version": v.version,
                    "status": v.status,
                    "event": v.event,
                    "snapshot": v.snapshot,
                    "change_reason": v.change_reason,
                    "changed_by": v.changed_by,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                }
                for v in versions
            ],
            "audit": [serialize_audit(a) for a in audit],
        }

    # ----------------------------------------------------------------- write
    def create_rule(
        self, user: UserModel, payload: dict[str, Any], *, reason: str | None = None
    ) -> dict[str, Any]:
        self._assert_edit(user)
        data = self._normalized(payload)
        if data["client_id"] is not None and not self.access.can_see_client(user, data["client_id"]):
            raise ForbiddenError("Client access denied")
        rule = PayflowComplianceRuleModel(
            code=self._next_code(),
            status=catalog.STATUS_DRAFT,
            version=1,
            created_by=user.full_name,
            updated_by=user.full_name,
        )
        self._apply(rule, data)
        self.db.add(rule)
        self.db.flush()
        self._snapshot(rule, event="Rule created", by=user.full_name, reason=reason)
        self.audit(
            entity_type="rule", event="Rule created", actor=user, rule=rule, reason=reason,
            before=None, after=serialize_rule(rule), approval="Not requested",
        )
        self.db.commit()
        self.db.refresh(rule)
        return serialize_rule(rule)

    def update_rule(
        self, user: UserModel, rule_id: int, payload: dict[str, Any], *, reason: str | None
    ) -> dict[str, Any]:
        self._assert_edit(user)
        if not _text(reason):
            raise ValidationAppError("A reason for the change is required")
        rule = self._get(rule_id)
        if rule.status == catalog.STATUS_ARCHIVED:
            raise ValidationAppError("An archived rule cannot be changed")
        if rule.status == catalog.STATUS_ACTIVE:
            # A rule in force changes what customers receive: only an administrator,
            # and only if the result is still a valid rule.
            self._assert_admin(user)
        current = self._current(rule)
        data = self._normalized(payload, current)
        if data["client_id"] != rule.client_id:
            # A client rule can never replace an inherited one (or the reverse).
            raise ValidationAppError("A rule cannot be moved to another client")
        before = serialize_rule(rule)
        self._apply(rule, data)
        if rule.status == catalog.STATUS_ACTIVE:
            self._assert_ready(rule)
            self._assert_no_overlap(rule)
        elif rule.status == catalog.STATUS_PENDING:
            # What was submitted is no longer what would be approved.
            rule.status = catalog.STATUS_DRAFT
        rule.version = int(rule.version or 1) + 1
        rule.updated_by = user.full_name
        self.db.flush()
        self._snapshot(rule, event="Rule modified", by=user.full_name, reason=reason)
        self.audit(
            entity_type="rule", event="Rule modified", actor=user, rule=rule, reason=reason,
            before=before, after=serialize_rule(rule),
            approval="Approved by Operations Admin" if rule.status == catalog.STATUS_ACTIVE else "Not requested",
        )
        self.db.commit()
        self.db.refresh(rule)
        return serialize_rule(rule)

    def duplicate_rule(self, user: UserModel, rule_id: int) -> dict[str, Any]:
        self._assert_edit(user)
        source = self._get(rule_id)
        payload = self._current(source)
        payload["name"] = f"{source.name} (copy)"
        return self.create_rule(user, payload, reason=f"Duplicated from {source.code} as a draft")

    def submit(self, user: UserModel, rule_id: int, *, reason: str | None = None) -> dict[str, Any]:
        self._assert_edit(user)
        rule = self._get(rule_id)
        if rule.status not in (catalog.STATUS_DRAFT, catalog.STATUS_INACTIVE):
            raise ValidationAppError(f"A rule that is {rule.status} cannot be submitted for approval")
        self._assert_ready(rule)
        rule.submitted_by = user.full_name
        rule.rejection_reason = None
        return self._transition(
            rule, catalog.STATUS_PENDING, actor=user, reason=reason, approval="Pending Approval"
        )

    def approve(
        self, user: UserModel, rule_id: int, *, reason: str | None = None, activate: bool = True
    ) -> dict[str, Any]:
        self._assert_admin(user)
        rule = self._get(rule_id)
        if rule.status != catalog.STATUS_PENDING:
            raise ValidationAppError("Only a rule that is Pending Approval can be approved")
        self._assert_ready(rule)
        if activate:
            self._assert_no_overlap(rule)
        rule.approved_by = user.full_name
        rule.approved_at = _utcnow()
        return self._transition(
            rule,
            catalog.STATUS_ACTIVE if activate else catalog.STATUS_INACTIVE,
            actor=user,
            reason=reason,
            approval="Approved by Operations Admin",
        )

    def reject(self, user: UserModel, rule_id: int, *, reason: str | None) -> dict[str, Any]:
        self._assert_admin(user)
        if not _text(reason):
            raise ValidationAppError("A reason for the rejection is required")
        rule = self._get(rule_id)
        if rule.status != catalog.STATUS_PENDING:
            raise ValidationAppError("Only a rule that is Pending Approval can be rejected")
        rule.rejection_reason = _text(reason)
        return self._transition(rule, catalog.STATUS_DRAFT, actor=user, reason=reason, approval="Rejected")

    def activate(self, user: UserModel, rule_id: int, *, reason: str | None = None) -> dict[str, Any]:
        self._assert_admin(user)
        rule = self._get(rule_id)
        if rule.status != catalog.STATUS_INACTIVE or not rule.approved_at:
            raise ValidationAppError(
                "Only an approved, Inactive rule can be activated. Submit it for approval first."
            )
        self._assert_ready(rule)
        self._assert_no_overlap(rule)
        return self._transition(
            rule, catalog.STATUS_ACTIVE, actor=user, reason=reason, approval="Approved by Operations Admin"
        )

    def deactivate(self, user: UserModel, rule_id: int, *, reason: str | None) -> dict[str, Any]:
        self._assert_admin(user)
        if not _text(reason):
            raise ValidationAppError("A reason for deactivating the rule is required")
        rule = self._get(rule_id)
        if rule.status != catalog.STATUS_ACTIVE:
            raise ValidationAppError("Only an Active rule can be deactivated")
        return self._transition(rule, catalog.STATUS_INACTIVE, actor=user, reason=reason)

    def archive(self, user: UserModel, rule_id: int, *, reason: str | None = None) -> dict[str, Any]:
        self._assert_admin(user)
        rule = self._get(rule_id)
        if rule.status == catalog.STATUS_ACTIVE:
            raise ValidationAppError("Deactivate the rule before archiving it")
        if rule.status == catalog.STATUS_ARCHIVED:
            return serialize_rule(rule)
        return self._transition(rule, catalog.STATUS_ARCHIVED, actor=user, reason=reason)

    # ----------------------------------------------------------------- holidays
    def list_holidays(
        self, user: UserModel, *, country: str | None = None, state: str | None = None
    ) -> dict[str, Any]:
        self._assert_view(user)
        query = self.db.query(PayflowComplianceHolidayModel)
        if country:
            name, _ = catalog.normalize_jurisdiction(country, None)
            query = query.filter(PayflowComplianceHolidayModel.country == name)
        if state:
            _, name = catalog.normalize_jurisdiction(country, state)
            query = query.filter(PayflowComplianceHolidayModel.state.in_(["", name]))
        rows = query.order_by(
            PayflowComplianceHolidayModel.holiday_date.asc(), PayflowComplianceHolidayModel.id.asc()
        ).all()
        return {"holidays": [serialize_holiday(h) for h in rows]}

    def _holiday_values(self, payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        data = dict(current or {})
        data.update({k: v for k, v in payload.items() if v is not None})
        name = _text(data.get("name"))
        if not name:
            raise ValidationAppError("Holiday name is required")
        when = _parse_date(data.get("date") or data.get("holiday_date"), "Holiday date")
        if not when:
            raise ValidationAppError("Holiday date is required")
        country = catalog.find_country(data.get("country"))
        if not country:
            raise ValidationAppError("Country is required and must be supported")
        state_text = _text(data.get("state"))
        state = catalog.find_state(country, state_text)
        if state_text and not state:
            raise ValidationAppError(f"'{state_text}' is not a state / province of {country['name']}")
        return {
            "name": name,
            "holiday_date": when,
            "recurring": bool(data.get("recurring", True)),
            "country": country["name"],
            "state": state["name"] if state else "",
            "source": _text(data.get("source")) or None,
        }

    def save_holiday(
        self, user: UserModel, payload: dict[str, Any], holiday_id: int | None = None
    ) -> dict[str, Any]:
        self._assert_admin(user)
        row = None
        before = None
        if holiday_id is not None:
            row = self.db.get(PayflowComplianceHolidayModel, holiday_id)
            if not row:
                raise NotFoundError("Holiday not found")
            before = serialize_holiday(row)
        values = self._holiday_values(
            payload,
            {**before, "holiday_date": before["date"]} if before else None,
        )
        if row is None:
            row = PayflowComplianceHolidayModel(**values)
            self.db.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        self.db.flush()
        self.audit(
            entity_type="holiday",
            entity_id=row.id,
            rule_name=row.name,
            event="Holiday modified" if before else "Holiday created",
            actor=user,
            reason=row.source,
            before=before,
            after=serialize_holiday(row),
            approval="Calendar maintained by Operations Admin",
        )
        self.db.commit()
        self.db.refresh(row)
        return serialize_holiday(row)

    def delete_holiday(self, user: UserModel, holiday_id: int) -> dict[str, Any]:
        self._assert_admin(user)
        row = self.db.get(PayflowComplianceHolidayModel, holiday_id)
        if not row:
            raise NotFoundError("Holiday not found")
        before = serialize_holiday(row)
        self.audit(
            entity_type="holiday", entity_id=row.id, rule_name=row.name, event="Holiday removed",
            actor=user, reason=row.source, before=before, after=None,
            approval="Calendar maintained by Operations Admin",
        )
        self.db.delete(row)
        self.db.commit()
        return {"message": "Holiday removed"}

    # ----------------------------------------------------------------- audit log
    def list_audit(
        self,
        user: UserModel,
        *,
        entity_type: str | None = None,
        rule_id: int | None = None,
        client_id: int | None = None,
        account_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._assert_admin(user)
        query = self.db.query(PayflowComplianceAuditModel)
        if entity_type:
            query = query.filter(PayflowComplianceAuditModel.entity_type == entity_type)
        if rule_id is not None:
            query = query.filter(PayflowComplianceAuditModel.rule_id == rule_id)
        if client_id is not None:
            query = query.filter(PayflowComplianceAuditModel.client_id == client_id)
        if account_id is not None:
            query = query.filter(PayflowComplianceAuditModel.account_id == account_id)
        total = query.count()
        rows = (
            query.order_by(PayflowComplianceAuditModel.id.desc())
            .offset(max(0, int(offset)))
            .limit(max(1, min(int(limit), 500)))
            .all()
        )
        return {"audit": [serialize_audit(a) for a in rows], "total": total}


def serialize_holiday(row: PayflowComplianceHolidayModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "date": row.holiday_date.isoformat() if row.holiday_date else None,
        "recurring": bool(row.recurring),
        "country": row.country,
        "state": row.state or "",
        "source": row.source,
    }


def serialize_audit(row: PayflowComplianceAuditModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "rule_id": row.rule_id,
        "rule_name": row.rule_name,
        "client_id": row.client_id,
        "account_id": row.account_id,
        "event": row.event,
        "reason": row.reason,
        "before": row.before,
        "after": row.after,
        "approval": row.approval,
        "by": row.actor_name,
        "at": row.created_at.isoformat() if row.created_at else None,
    }


# --------------------------------------------------------------------------- seed


def seed_compliance_library(db: Session) -> dict[str, int]:
    """Add the jurisdiction library (as Drafts) and sample holidays, once.

    Drafts are never evaluated: the library is a starting point an administrator
    reviews, corrects and activates. Existing rows are left exactly as they are.
    """
    service = ComplianceRuleService(db)
    existing = {
        code for (code,) in db.query(PayflowComplianceRuleModel.code).filter(
            PayflowComplianceRuleModel.is_library.is_(True)
        )
    }
    added = 0
    for entry in catalog.JURISDICTION_LIBRARY:
        if entry["code"] in existing:
            continue
        payload = catalog.library_rule_payload(entry)
        data = service._normalized(payload)  # noqa: SLF001 - same checks as the API
        rule = PayflowComplianceRuleModel(
            code=entry["code"],
            status=catalog.STATUS_DRAFT,
            version=1,
            is_library=True,
            created_by="PayFlow jurisdiction library",
            updated_by="PayFlow jurisdiction library",
        )
        service._apply(rule, data)  # noqa: SLF001
        db.add(rule)
        db.flush()
        service._snapshot(  # noqa: SLF001
            rule, event="Rule created", by="PayFlow jurisdiction library",
            reason="Illustrative configuration requiring legal validation",
        )
        added += 1
    holidays = 0
    if not db.query(PayflowComplianceHolidayModel.id).first():
        for entry in catalog.HOLIDAY_SEED:
            db.add(
                PayflowComplianceHolidayModel(
                    name=entry["name"],
                    holiday_date=date.fromisoformat(entry["date"]),
                    recurring=entry["recurring"],
                    country=entry["country"],
                    state=entry["state"],
                    source="Illustrative calendar; legal validation required",
                )
            )
            holidays += 1
    if added or holidays:
        db.commit()
    return {"rules": added, "holidays": holidays}
