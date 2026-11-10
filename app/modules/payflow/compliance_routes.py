"""Compliance rules API (jurisdiction-based rules, calendar, testing, audit, client mapping).

Follows the PayFlow convention: GET reads, POST creates / updates / acts.
Authorization lives in the services: view_rules to read, modify_governance to
draft and submit, Operations Admin to approve, activate, deactivate and audit.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import DbSession
from app.core.exceptions import AppError
from app.modules.payflow.deps import RequirePayflowProduct
from app.modules.payflow.services.compliance import catalog
from app.modules.payflow.services.compliance.client_mapping import ClientComplianceService
from app.modules.payflow.services.compliance.evaluation_service import ComplianceEvaluationService
from app.modules.payflow.services.compliance.rule_service import ComplianceRuleService

router = APIRouter(prefix="/payflow", tags=["PayFlow · Compliance"])

_STATUS = {
    "unauthorized": status.HTTP_401_UNAUTHORIZED,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "not_found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
    "validation_error": status.HTTP_400_BAD_REQUEST,
}


def _http(exc: AppError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.code, 400), detail=exc.message)


class RuleBody(BaseModel):
    """A rule as sent by the editor. Unknown keys are ignored; missing keys keep their value."""

    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    rule_type: Optional[str] = None
    category: Optional[str] = None
    enforcement: Optional[str] = None
    regulatory_authority: Optional[str] = Field(default=None, max_length=255)
    legal_reference: Optional[str] = None
    source: Optional[str] = Field(default=None, max_length=255)
    jurisdiction_type: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    postal: Optional[str] = None
    collector_type: Optional[str] = None
    debt_type: Optional[str] = None
    channel: Optional[str] = None
    logic: Optional[str] = None
    conditions: Optional[list[dict[str, Any]]] = None
    settings: Optional[dict[str, Any]] = None
    action: Optional[str] = None
    override_policy: Optional[str] = None
    priority: Optional[int] = None
    effective_from: Optional[str] = None
    effective_until: Optional[str] = None
    client_id: Optional[int] = None
    reason: Optional[str] = Field(default=None, max_length=2000)


class ReasonBody(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=2000)
    # Approve only: False = approved but not yet in force (activate it later).
    activate: bool = True


class HolidayBody(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    date: Optional[str] = None
    recurring: Optional[bool] = None
    country: Optional[str] = None
    state: Optional[str] = None
    source: Optional[str] = Field(default=None, max_length=255)


class ClientRuleBody(BaseModel):
    rule_id: int
    enabled: bool
    reason: Optional[str] = Field(default=None, max_length=2000)


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timezone: Optional[str] = None
    flags: Optional[dict[str, bool]] = None
    sms_consent: Optional[bool] = None
    consent_source: Optional[str] = None
    consent_evidence: Optional[str] = None
    consent_recorded_at: Optional[str] = None
    written_notice_sent_at: Optional[str] = None
    last_conversation_at: Optional[str] = None
    attorney_contact: Optional[str] = None
    inconvenient_times: Optional[list[dict[str, Any]]] = None
    notes: Optional[str] = None
    reason: Optional[str] = Field(default=None, max_length=2000)


# --------------------------------------------------------------------------- reference data


@router.get("/compliance/catalog")
def compliance_catalog(user: RequirePayflowProduct, db: DbSession):
    """Rule types (JUR-01 … JUR-25), categories and every allowed value for the editor."""
    try:
        ComplianceRuleService(db)._assert_view(user)  # noqa: SLF001
    except AppError as exc:
        raise _http(exc) from exc
    return catalog.catalog_payload()


@router.get("/compliance/jurisdictions")
def compliance_jurisdictions(
    user: RequirePayflowProduct,
    db: DbSession,
    country: str | None = Query(default=None),
    state: str | None = Query(default=None),
):
    """Country → states / provinces → regions. No country: the supported countries."""
    try:
        ComplianceRuleService(db)._assert_view(user)  # noqa: SLF001
    except AppError as exc:
        raise _http(exc) from exc
    if not country:
        return {
            "countries": [
                {
                    "code": c["code"],
                    "name": c["name"],
                    "default_timezone": c["default_timezone"],
                    "subdivision_label": c["subdivision_label"],
                    "postal_label": c["postal_label"],
                }
                for c in catalog.COUNTRIES
            ]
        }
    found = catalog.find_country(country)
    if not found:
        raise HTTPException(status_code=404, detail="Country is not supported")
    out: dict[str, Any] = {
        "country": {"code": found["code"], "name": found["name"]},
        "states": found["states"],
        "regions": catalog.regions_for(found["code"], ""),
    }
    if state:
        region = catalog.find_state(found, state)
        if not region:
            raise HTTPException(status_code=404, detail="State / province not found for this country")
        out["state"] = region
        out["regions"] = catalog.regions_for(found["code"], region["code"])
    return out


# --------------------------------------------------------------------------- rules


@router.get("/compliance/rules")
def list_compliance_rules(
    user: RequirePayflowProduct,
    db: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
    category: str | None = Query(default=None),
    rule_type: str | None = Query(default=None),
    country: str | None = Query(default=None),
    state: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    enforcement: str | None = Query(default=None),
    client_id: int | None = Query(default=None),
    search: str | None = Query(default=None),
    sort: str = Query(default="updated_at"),
    order: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        return ComplianceRuleService(db).list_rules(
            user,
            status=status_filter,
            category=category,
            rule_type=rule_type,
            country=country,
            state=state,
            channel=channel,
            enforcement=enforcement,
            client_id=client_id,
            search=search,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/rules")
def create_compliance_rule(body: RuleBody, user: RequirePayflowProduct, db: DbSession):
    """Create a rule. It always starts as a Draft."""
    try:
        data = body.model_dump(exclude_unset=True)
        reason = data.pop("reason", None)
        return ComplianceRuleService(db).create_rule(user, data, reason=reason)
    except AppError as exc:
        raise _http(exc) from exc


@router.get("/compliance/rules/{rule_id}")
def get_compliance_rule(rule_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return ComplianceRuleService(db).get_rule(user, rule_id)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/rules/{rule_id}/update")
def update_compliance_rule(rule_id: int, body: RuleBody, user: RequirePayflowProduct, db: DbSession):
    """Change a rule. A reason is required; the previous version is kept."""
    try:
        data = body.model_dump(exclude_unset=True)
        reason = data.pop("reason", None)
        return ComplianceRuleService(db).update_rule(user, rule_id, data, reason=reason)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/rules/{rule_id}/duplicate")
def duplicate_compliance_rule(rule_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return ComplianceRuleService(db).duplicate_rule(user, rule_id)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/rules/{rule_id}/submit")
def submit_compliance_rule(
    rule_id: int, user: RequirePayflowProduct, db: DbSession, body: ReasonBody | None = None
):
    """Draft / Inactive → Pending Approval. Refused while the configuration is invalid."""
    try:
        return ComplianceRuleService(db).submit(user, rule_id, reason=body.reason if body else None)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/rules/{rule_id}/approve")
def approve_compliance_rule(
    rule_id: int, user: RequirePayflowProduct, db: DbSession, body: ReasonBody | None = None
):
    """Pending Approval → Active (or Inactive with ``activate: false``). Operations Admin."""
    try:
        return ComplianceRuleService(db).approve(
            user,
            rule_id,
            reason=body.reason if body else None,
            activate=body.activate if body else True,
        )
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/rules/{rule_id}/reject")
def reject_compliance_rule(rule_id: int, body: ReasonBody, user: RequirePayflowProduct, db: DbSession):
    try:
        return ComplianceRuleService(db).reject(user, rule_id, reason=body.reason)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/rules/{rule_id}/activate")
def activate_compliance_rule(
    rule_id: int, user: RequirePayflowProduct, db: DbSession, body: ReasonBody | None = None
):
    try:
        return ComplianceRuleService(db).activate(user, rule_id, reason=body.reason if body else None)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/rules/{rule_id}/deactivate")
def deactivate_compliance_rule(rule_id: int, body: ReasonBody, user: RequirePayflowProduct, db: DbSession):
    try:
        return ComplianceRuleService(db).deactivate(user, rule_id, reason=body.reason)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/rules/{rule_id}/archive")
def archive_compliance_rule(
    rule_id: int, user: RequirePayflowProduct, db: DbSession, body: ReasonBody | None = None
):
    try:
        return ComplianceRuleService(db).archive(user, rule_id, reason=body.reason if body else None)
    except AppError as exc:
        raise _http(exc) from exc


@router.get("/compliance/rules/{rule_id}/history")
def compliance_rule_history(rule_id: int, user: RequirePayflowProduct, db: DbSession):
    """Every version of the rule and its audit trail, newest first."""
    try:
        return ComplianceRuleService(db).history(user, rule_id)
    except AppError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- holidays


@router.get("/compliance/holidays")
def list_compliance_holidays(
    user: RequirePayflowProduct,
    db: DbSession,
    country: str | None = Query(default=None),
    state: str | None = Query(default=None),
):
    try:
        return ComplianceRuleService(db).list_holidays(user, country=country, state=state)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/holidays")
def create_compliance_holiday(body: HolidayBody, user: RequirePayflowProduct, db: DbSession):
    try:
        return ComplianceRuleService(db).save_holiday(user, body.model_dump(exclude_unset=True))
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/holidays/{holiday_id}/update")
def update_compliance_holiday(
    holiday_id: int, body: HolidayBody, user: RequirePayflowProduct, db: DbSession
):
    try:
        return ComplianceRuleService(db).save_holiday(
            user, body.model_dump(exclude_unset=True), holiday_id
        )
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/compliance/holidays/{holiday_id}/delete")
def delete_compliance_holiday(holiday_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return ComplianceRuleService(db).delete_holiday(user, holiday_id)
    except AppError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- evaluate / audit


@router.post("/compliance/evaluate")
def evaluate_compliance(body: dict[str, Any], user: RequirePayflowProduct, db: DbSession):
    """Would this communication be allowed?

    Send ``account_id`` to use what PayFlow knows about the account, or
    ``client_id`` with ``jurisdiction`` and facts to simulate one (rule testing).
    Returns the decision (ALLOW / BLOCK / RESCHEDULE / REVIEW_REQUIRED), the rules
    and versions that matched, reason codes and, when it can be worked out, the
    next permitted time. ``record: true`` writes the outcome to the audit log.
    """
    try:
        return ComplianceEvaluationService(db).evaluate_request(user, body)
    except AppError as exc:
        raise _http(exc) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid evaluation request: {exc}") from exc


@router.get("/compliance/audit")
def compliance_audit(
    user: RequirePayflowProduct,
    db: DbSession,
    entity_type: str | None = Query(default=None),
    rule_id: int | None = Query(default=None),
    client_id: int | None = Query(default=None),
    account_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Rule changes, calendar changes, client mapping changes and evaluation outcomes."""
    try:
        return ComplianceRuleService(db).list_audit(
            user,
            entity_type=entity_type,
            rule_id=rule_id,
            client_id=client_id,
            account_id=account_id,
            limit=limit,
            offset=offset,
        )
    except AppError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- client mapping


@router.get("/clients/{client_id}/compliance-rules")
def client_compliance_rules(client_id: int, user: RequirePayflowProduct, db: DbSession):
    """Rules that apply to this client, which are mandatory (locked) and which are on."""
    try:
        return ClientComplianceService(db).list_for_client(user, client_id)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/clients/{client_id}/compliance-rules/update")
def update_client_compliance_rule(
    client_id: int, body: ClientRuleBody, user: RequirePayflowProduct, db: DbSession
):
    """Switch an optional rule on or off for this client. Mandatory rules are refused."""
    try:
        return ClientComplianceService(db).set_enabled(
            user, client_id, body.rule_id, enabled=body.enabled, reason=body.reason
        )
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/clients/{client_id}/compliance-rules/sync")
def sync_client_compliance_rules(client_id: int, user: RequirePayflowProduct, db: DbSession):
    """Associate rules that became applicable since the client was last saved."""
    try:
        return ClientComplianceService(db).resync(user, client_id)
    except AppError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- account record


@router.get("/accounts/{account_id}/compliance-profile")
def account_compliance_profile(account_id: int, user: RequirePayflowProduct, db: DbSession):
    """Consent and suppression record for the customer on this account."""
    try:
        return ComplianceEvaluationService(db).get_profile(user, account_id)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/accounts/{account_id}/compliance-profile/update")
def update_account_compliance_profile(
    account_id: int, body: ProfileBody, user: RequirePayflowProduct, db: DbSession
):
    try:
        data = body.model_dump(exclude_unset=True)
        reason = data.pop("reason", None)
        return ComplianceEvaluationService(db).update_profile(user, account_id, data, reason=reason)
    except AppError as exc:
        raise _http(exc) from exc
