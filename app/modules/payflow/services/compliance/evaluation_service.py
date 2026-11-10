"""Compliance evaluation against real data: one place every send asks before it goes.

Collects what PayFlow knows about the account (location, time zone, consent,
suppression flags, contact history), loads the rules in force and the holiday
calendar, runs the engine, and records the outcome in the audit log.

Also owns the two small records the engine depends on:
- counted contact events (deduplicated by key, so a repeated provider event is
  never counted twice toward a frequency limit);
- the account's compliance profile (cease-communication, attorney, dispute,
  workplace, SMS consent, inconvenient times, notices …).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientComplianceRuleModel,
    PayflowClientModel,
    PayflowComplianceEventModel,
    PayflowComplianceHolidayModel,
    PayflowComplianceRuleModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.modules.payflow.services.compliance import catalog
from app.modules.payflow.services.compliance.engine import (
    ContactEvent,
    EvaluationInput,
    EvaluationResult,
    Jurisdiction,
    evaluate,
)
from app.modules.payflow.services.compliance.rule_service import (
    ComplianceRuleService,
    engine_rule,
)

# How far back contact history is loaded (the longest rolling window is 30 days).
_HISTORY_DAYS = 31

_PROFILE_DATES = ("consent_recorded_at", "written_notice_sent_at", "last_conversation_at")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _valid_zone(name: str | None) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return False
    return True


class ComplianceEvaluationService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    # ----------------------------------------------------------------- what is in force
    def has_active_rules(self, client_id: int | None) -> bool:
        """Cheap gate: with nothing in force there is nothing to evaluate or record."""
        query = self.db.query(PayflowComplianceRuleModel.id).filter(
            PayflowComplianceRuleModel.status == catalog.STATUS_ACTIVE
        )
        query = query.filter(
            or_(
                PayflowComplianceRuleModel.client_id.is_(None),
                PayflowComplianceRuleModel.client_id == client_id,
            )
        )
        return query.first() is not None

    def rules_in_force(self, client_id: int | None) -> list[dict[str, Any]]:
        rows = (
            self.db.query(PayflowComplianceRuleModel)
            .filter(
                PayflowComplianceRuleModel.status == catalog.STATUS_ACTIVE,
                or_(
                    PayflowComplianceRuleModel.client_id.is_(None),
                    PayflowComplianceRuleModel.client_id == client_id,
                ),
            )
            .all()
        )
        return [engine_rule(r) for r in rows]

    def holidays(self) -> list[dict[str, Any]]:
        return [
            {
                "date": h.holiday_date.isoformat(),
                "recurring": bool(h.recurring),
                "country": h.country,
                "state": h.state or "",
            }
            for h in self.db.query(PayflowComplianceHolidayModel).all()
        ]

    def disabled_rule_ids(self, client_id: int | None) -> set[int]:
        if client_id is None:
            return set()
        return {
            rule_id
            for (rule_id,) in self.db.query(PayflowClientComplianceRuleModel.rule_id).filter(
                PayflowClientComplianceRuleModel.client_id == client_id,
                PayflowClientComplianceRuleModel.enabled.is_(False),
            )
        }

    # ----------------------------------------------------------------- account facts
    def jurisdiction_for(self, account: PayflowAccountModel) -> Jurisdiction:
        """Where the customer is: from the account only, never guessed from the client."""
        country = catalog.find_country(account.country_code)
        country_name = country["name"] if country else ""
        state = catalog.find_state(country, account.province_state)
        state_name = state["name"] if state else (account.province_state or "").strip()
        zone = (account.timezone or "").strip()
        if not _valid_zone(zone):
            zone = catalog.timezone_for(country_name, state_name) if country else ""
        return Jurisdiction(
            country=country_name,
            state=state_name,
            region=(account.region or "").strip() if hasattr(account, "region") else "",
            city=(account.city or "").strip(),
            postal=(account.postal_code or "").strip().upper(),
            timezone=zone,
        )

    def history_for(self, account: PayflowAccountModel, *, until: datetime) -> list[ContactEvent]:
        since = until - timedelta(days=_HISTORY_DAYS)
        same = PayflowComplianceEventModel.account_id == account.id
        if account.debtor_id:
            # The same person may have several accounts with this creditor.
            same = or_(
                same,
                (PayflowComplianceEventModel.client_id == account.client_id)
                & (PayflowComplianceEventModel.debtor_ref == account.debtor_id),
            )
        rows = (
            self.db.query(PayflowComplianceEventModel)
            .filter(same, PayflowComplianceEventModel.occurred_at >= since)
            .all()
        )
        return [
            ContactEvent(at=_aware(r.occurred_at), channel=r.channel, kind=r.kind, account_id=r.account_id)
            for r in rows
        ]

    def input_for_account(
        self,
        account: PayflowAccountModel,
        *,
        channel: str,
        at: datetime | None = None,
        activity: str = "Collection",
        message_text: str | None = None,
    ) -> EvaluationInput:
        when = _aware(at) or _utcnow()
        profile = dict(account.compliance_profile or {})
        client = account.client or self.db.get(PayflowClientModel, account.client_id)
        flags = {name for name in catalog.ACCOUNT_FLAGS if profile.get("flags", {}).get(name)}
        if account.provincial_hold:
            flags.add("Do Not Contact")
        is_sms = channel.strip().lower() == "sms"
        if is_sms:
            consent = profile.get("sms_consent")
            opted_out = bool(getattr(account, "sms_opt_out", False))
        else:
            consent = account.email_consent
            opted_out = account.email_consent is False
        history = self.history_for(account, until=when)
        last_contact = max((e.at for e in history if e.kind == "attempted"), default=None)
        conversations = [e.at for e in history if e.kind == "conversation"]
        last_conversation = max(conversations, default=None) or _parse_dt(
            profile.get("last_conversation_at")
        )
        return EvaluationInput(
            client_id=account.client_id,
            account_id=account.id,
            jurisdiction=self.jurisdiction_for(account),
            channel=channel.strip() or "Email",
            at=when,
            activity=activity,
            consent=consent if isinstance(consent, bool) else None,
            opted_out=opted_out,
            flags=flags,
            history=history,
            consent_date=_parse_dt(profile.get("consent_recorded_at")),
            last_contact_at=last_contact,
            last_conversation_at=last_conversation,
            notice_sent_at=_parse_dt(profile.get("written_notice_sent_at")),
            message_text=message_text,
            language=(account.language or "").strip(),
            balance=float(account.outstanding_balance or 0),
            days_past_due=int(account.days_past_due or 0),
            collector_type=(client.client_type or "").strip() if client else "",
            debt_type=(account.account_category or "").strip(),
            inconvenient_times=list(profile.get("inconvenient_times") or []),
        )

    # ----------------------------------------------------------------- evaluate
    def evaluate_account(
        self,
        account: PayflowAccountModel,
        *,
        channel: str,
        at: datetime | None = None,
        activity: str = "Collection",
        message_text: str | None = None,
        record: bool = True,
        actor_name: str = "PayFlow",
    ) -> EvaluationResult:
        """Ask before a covered activity. Always re-evaluates: rules, flags and the
        clock may all have changed since the activity was scheduled."""
        if not self.has_active_rules(account.client_id):
            return EvaluationResult(decision="ALLOW", explanation="No compliance rules are in force.")
        data = self.input_for_account(
            account, channel=channel, at=at, activity=activity, message_text=message_text
        )
        result = evaluate(
            self.rules_in_force(account.client_id),
            self.holidays(),
            data,
            disabled_rule_ids=self.disabled_rule_ids(account.client_id),
        )
        if record and (result.decision != "ALLOW" or result.warnings):
            self._record(result, data, actor_name=actor_name)
        return result

    def _record(
        self,
        result: EvaluationResult,
        data: EvaluationInput,
        *,
        actor_name: str = "PayFlow",
        actor: UserModel | None = None,
    ) -> None:
        ComplianceRuleService(self.db).audit(
            entity_type="evaluation",
            entity_id=data.account_id,
            rule_name=f"Communication · {data.channel}",
            client_id=data.client_id,
            account_id=data.account_id,
            event=f"Execution check: {result.decision}",
            reason=result.explanation,
            before=None,
            after={
                "channel": data.channel,
                "activity": data.activity,
                "at": data.at.isoformat(),
                "jurisdiction": {
                    "country": data.jurisdiction.country,
                    "state": data.jurisdiction.state,
                    "timezone": data.jurisdiction.timezone,
                },
                **result.as_dict(),
            },
            approval=result.decision,
            actor=actor,
            actor_name=actor_name,
        )

    def evaluate_request(self, user: UserModel, payload: dict[str, Any]) -> dict[str, Any]:
        """Rule testing / manual check: by account, or with facts supplied in the request."""
        if not (self.access.is_operations_admin(user) or self.access.can(user, "view_rules")):
            raise ForbiddenError("Permission required: view_rules")
        channel = str(payload.get("channel") or "Email").strip()
        if channel not in catalog.CHANNELS or channel == "All Channels":
            raise ValidationAppError("channel must be one of: Email, SMS, Voice, WhatsApp")
        at = _parse_dt(payload.get("at")) or _utcnow()
        record = bool(payload.get("record"))

        account_id = payload.get("account_id")
        if account_id is not None:
            account = self.db.get(PayflowAccountModel, int(account_id))
            if not account:
                raise NotFoundError("Account not found")
            if not self.access.can_see_client(user, account.client_id):
                raise ForbiddenError("Client access denied")
            data = self.input_for_account(
                account,
                channel=channel,
                at=at,
                activity=str(payload.get("activity") or "Collection"),
                message_text=payload.get("message_text"),
            )
        else:
            client_id = payload.get("client_id")
            if client_id is None:
                raise ValidationAppError("client_id or account_id is required")
            client_id = int(client_id)
            if not self.db.get(PayflowClientModel, client_id):
                raise NotFoundError("Client not found")
            if not self.access.can_see_client(user, client_id):
                raise ForbiddenError("Client access denied")
            where = payload.get("jurisdiction") or {}
            country, state = catalog.normalize_jurisdiction(where.get("country"), where.get("state"))
            if not catalog.find_country(country):
                country = ""
            zone = str(where.get("timezone") or "").strip()
            if not _valid_zone(zone):
                zone = catalog.timezone_for(country, state) if country else ""
            history = [
                ContactEvent(
                    at=_parse_dt(item.get("at")) or at,
                    channel=str(item.get("channel") or channel),
                    kind=str(item.get("kind") or "attempted"),
                )
                for item in (payload.get("contact_history") or [])
                if isinstance(item, dict)
            ]
            consent = payload.get("consent")
            data = EvaluationInput(
                client_id=client_id,
                jurisdiction=Jurisdiction(
                    country=country,
                    state=state if country else "",
                    region=str(where.get("region") or "").strip(),
                    city=str(where.get("city") or "").strip(),
                    postal=str(where.get("postal") or "").strip().upper(),
                    timezone=zone,
                ),
                channel=channel,
                at=at,
                activity=str(payload.get("activity") or "Collection"),
                consent=consent if isinstance(consent, bool) else None,
                opted_out=bool(payload.get("opted_out")),
                flags={str(f) for f in (payload.get("flags") or [])},
                history=history,
                consent_date=_parse_dt(payload.get("consent_date")),
                last_contact_at=_parse_dt(payload.get("last_contact_at")),
                last_conversation_at=_parse_dt(payload.get("last_conversation_at")),
                notice_sent_at=_parse_dt(payload.get("notice_sent_at")),
                message_text=payload.get("message_text"),
                language=str(payload.get("language") or ""),
                balance=float(payload.get("balance") or 0),
                days_past_due=int(payload.get("days_past_due") or 0),
                sender_identified=bool(payload.get("sender_identified", True)),
                unsubscribe_instructions=bool(payload.get("unsubscribe_instructions", True)),
                template_approved=bool(payload.get("template_approved", True)),
                reply_enabled=bool(payload.get("reply_enabled", True)),
                collector_type=str(payload.get("collector_type") or ""),
                debt_type=str(payload.get("debt_type") or ""),
                inconvenient_times=list(payload.get("inconvenient_times") or []),
                extra=dict(payload.get("extra") or {}),
            )
        result = evaluate(
            self.rules_in_force(data.client_id),
            self.holidays(),
            data,
            disabled_rule_ids=self.disabled_rule_ids(data.client_id),
        )
        if record:
            self._record(result, data, actor=user)
            self.db.commit()
        return result.as_dict()

    # ----------------------------------------------------------------- counted events
    def record_event(
        self,
        account: PayflowAccountModel,
        *,
        channel: str,
        kind: str,
        key: str,
        at: datetime | None = None,
        source: str | None = None,
    ) -> bool:
        """Count one contact. False when this key was already counted (duplicate event)."""
        if kind not in catalog.COUNT_EVENTS:
            raise ValidationAppError(f"kind must be one of: {', '.join(catalog.COUNT_EVENTS)}")
        exists = (
            self.db.query(PayflowComplianceEventModel.id)
            .filter(PayflowComplianceEventModel.event_key == key)
            .first()
        )
        if exists:
            return False
        row = PayflowComplianceEventModel(
            event_key=key[:255],
            client_id=account.client_id,
            account_id=account.id,
            debtor_ref=account.debtor_id,
            channel=channel.strip() or "Email",
            kind=kind,
            source=source,
            occurred_at=_aware(at) or _utcnow(),
        )
        try:
            # Savepoint: a concurrent duplicate must not undo the caller's work.
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError:
            return False
        return True

    # ----------------------------------------------------------------- account profile
    def get_profile(self, user: UserModel, account_id: int) -> dict[str, Any]:
        account = self._account_for(user, account_id)
        return self._serialize_profile(account)

    def _account_for(self, user: UserModel, account_id: int) -> PayflowAccountModel:
        account = self.db.get(PayflowAccountModel, account_id)
        if not account:
            raise NotFoundError("Account not found")
        if not self.access.can_see_client(user, account.client_id):
            raise ForbiddenError("Client access denied")
        return account

    def _serialize_profile(self, account: PayflowAccountModel) -> dict[str, Any]:
        profile = dict(account.compliance_profile or {})
        where = self.jurisdiction_for(account)
        return {
            "account_id": account.id,
            "timezone": account.timezone,
            "resolved_jurisdiction": {
                "country": where.country,
                "state": where.state,
                "city": where.city,
                "postal": where.postal,
                "timezone": where.timezone,
            },
            "flags": {name: bool(profile.get("flags", {}).get(name)) for name in catalog.ACCOUNT_FLAGS},
            "email_consent": account.email_consent,
            "sms_opt_out": bool(getattr(account, "sms_opt_out", False)),
            "sms_consent": profile.get("sms_consent"),
            "consent_source": profile.get("consent_source"),
            "consent_evidence": profile.get("consent_evidence"),
            "consent_recorded_at": profile.get("consent_recorded_at"),
            "written_notice_sent_at": profile.get("written_notice_sent_at"),
            "last_conversation_at": profile.get("last_conversation_at"),
            "attorney_contact": profile.get("attorney_contact"),
            "inconvenient_times": list(profile.get("inconvenient_times") or []),
            "notes": profile.get("notes"),
        }

    def update_profile(
        self, user: UserModel, account_id: int, payload: dict[str, Any], *, reason: str | None
    ) -> dict[str, Any]:
        account = self._account_for(user, account_id)
        if not (
            self.access.is_operations_admin(user)
            or self.access.can(user, "modify_governance", account.client_id)
        ):
            raise ForbiddenError("Permission required: modify_governance")
        if not (reason or "").strip():
            raise ValidationAppError("A reason for the change is required")
        before = self._serialize_profile(account)
        profile = dict(account.compliance_profile or {})

        if "timezone" in payload:
            zone = str(payload.get("timezone") or "").strip()
            if zone and not _valid_zone(zone):
                raise ValidationAppError("timezone must be an IANA time zone, e.g. America/Toronto")
            account.timezone = zone or None
        if "flags" in payload:
            incoming = payload.get("flags") or {}
            if not isinstance(incoming, dict):
                raise ValidationAppError("flags must be an object of name: true/false")
            unknown = [name for name in incoming if name not in catalog.ACCOUNT_FLAGS]
            if unknown:
                raise ValidationAppError(f"Unknown flag(s): {', '.join(unknown)}")
            flags = dict(profile.get("flags") or {})
            flags.update({name: bool(value) for name, value in incoming.items()})
            profile["flags"] = flags
        if "sms_consent" in payload:
            value = payload.get("sms_consent")
            if value is not None and not isinstance(value, bool):
                raise ValidationAppError("sms_consent must be true, false or null")
            profile["sms_consent"] = value
        for key in _PROFILE_DATES:
            if key in payload:
                value = payload.get(key)
                if value in (None, ""):
                    profile[key] = None
                else:
                    parsed = _parse_dt(value)
                    if parsed is None:
                        raise ValidationAppError(f"{key} must be a date / time")
                    profile[key] = parsed.isoformat()
        for key in ("consent_source", "consent_evidence", "attorney_contact", "notes"):
            if key in payload:
                profile[key] = (str(payload.get(key) or "").strip() or None)
        if "inconvenient_times" in payload:
            windows = payload.get("inconvenient_times") or []
            if not isinstance(windows, list) or any(not isinstance(w, dict) for w in windows):
                raise ValidationAppError("inconvenient_times must be a list of {days, start, end}")
            profile["inconvenient_times"] = windows

        account.compliance_profile = profile
        self.db.flush()
        ComplianceRuleService(self.db).audit(
            entity_type="account_profile",
            entity_id=account.id,
            rule_name=f"Account {account.account_reference}",
            client_id=account.client_id,
            account_id=account.id,
            event="Consent / suppression record changed",
            actor=user,
            reason=reason,
            before=before,
            after=self._serialize_profile(account),
        )
        self.db.commit()
        self.db.refresh(account)
        return self._serialize_profile(account)
