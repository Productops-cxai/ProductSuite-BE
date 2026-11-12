"""FR-13: authorized, time-bounded compliance overrides do not bypass Not Permitted rules."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import ForbiddenError
from app.infrastructure.database.models import (
    PayflowComplianceOverrideModel,
    PayflowComplianceRuleModel,
    UserModel,
)
from app.modules.payflow.services.compliance.evaluation_service import ComplianceEvaluationService
from app.modules.payflow.services.compliance.override_service import ComplianceOverrideService
from tests.test_compliance_enforcement import active_rule, flag, setup
from tests.test_security_gaps import _auth  # noqa: F401


@pytest.fixture()
def admin(db_session):
    return db_session.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()


def _ops_policy_rule(db, **over):
    rule = active_rule(
        db,
        rule_type="JUR-11",
        category="Contact Restrictions",
        action="Require Approval",
        field="Legal Representation",
        **over,
    )
    row = db.get(PayflowComplianceRuleModel, rule.id)
    row.override_policy = "Operations Admin With Reason"
    db.commit()
    return row


def test_not_permitted_override_is_refused(db_session, admin):
    _client, account, _run = setup(db_session)
    rule = active_rule(db_session)
    row = db_session.get(PayflowComplianceRuleModel, rule.id)
    row.override_policy = "Not Permitted"
    db_session.commit()

    with pytest.raises(ForbiddenError):
        ComplianceOverrideService(db_session).request(
            admin,
            account_id=account.id,
            rule_id=rule.id,
            reason="customer asked",
            evidence="call note #1",
        )


def test_approved_override_clears_restriction_on_evaluate(db_session, admin):
    _client, account, _run = setup(db_session)
    rule = _ops_policy_rule(db_session)
    flag(db_session, account, "Legal Representation")

    blocked = ComplianceEvaluationService(db_session).evaluate_account(
        account, channel="Email", record=False
    )
    assert blocked.decision == "REVIEW_REQUIRED"

    service = ComplianceOverrideService(db_session)
    pending = service.request(
        admin,
        account_id=account.id,
        rule_id=rule.id,
        reason="Written attorney consent to contact customer",
        evidence="Letter dated 2026-11-10 on file",
        channel="Email",
    )
    assert pending["status"] == "Pending"
    approved = service.approve(admin, pending["id"], review_note="Verified letter")
    assert approved["status"] == "Approved"
    assert approved["valid_until"]

    allowed = ComplianceEvaluationService(db_session).evaluate_account(
        account, channel="Email", record=False
    )
    assert allowed.decision == "ALLOW"
    assert allowed.overrides_applied
    assert allowed.overrides_applied[0]["rule_id"] == rule.id


def test_expired_override_does_not_clear_restriction(db_session, admin):
    _client, account, _run = setup(db_session)
    rule = _ops_policy_rule(db_session)
    flag(db_session, account, "Legal Representation")
    service = ComplianceOverrideService(db_session)
    pending = service.request(
        admin,
        account_id=account.id,
        rule_id=rule.id,
        reason="short window",
        evidence="ticket-9",
    )
    approved = service.approve(admin, pending["id"])
    row = db_session.get(PayflowComplianceOverrideModel, approved["id"])
    row.valid_from = datetime.now(timezone.utc) - timedelta(days=2)
    row.valid_until = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    again = ComplianceEvaluationService(db_session).evaluate_account(
        account, channel="Email", record=False
    )
    assert again.decision == "REVIEW_REQUIRED"


def test_override_apis_round_trip(client, admin_token, db_session):
    _client, account, _run = setup(db_session)
    rule = _ops_policy_rule(db_session)
    headers = _auth(admin_token)

    created = client.post(
        "/payflow/compliance/overrides",
        json={
            "account_id": account.id,
            "rule_id": rule.id,
            "reason": "Customer confirmed attorney allows email",
            "evidence": "Email thread 2026-11-11",
            "channel": "Email",
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    override_id = created.json()["id"]

    listed = client.get(
        f"/payflow/accounts/{account.id}/compliance-overrides",
        headers=headers,
    )
    assert listed.status_code == 200
    assert any(o["id"] == override_id for o in listed.json()["overrides"])

    approved = client.post(
        f"/payflow/compliance/overrides/{override_id}/approve",
        json={"review_note": "OK"},
        headers=headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "Approved"
