"""The prototype's operational rules are added once, as System rules, and never as an Active rule
that PayFlow cannot check or carry out."""

from datetime import date, timedelta

import pytest

from app.core.exceptions import ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentLinkModel,
    PayflowRuleModel,
    UserModel,
)
from app.modules.payflow.services.operational_rule_seed import OPERATIONAL_RULES, seed_operational_rules
from app.modules.payflow.services.rule_runtime_service import (
    FIELD_GETTERS,
    RuleRuntimeService,
    unenforced_parts,
)
from app.modules.payflow.services.rule_service import RULE_ACTIONS, RULE_CATEGORIES, PayflowRuleService


@pytest.fixture()
def admin(db_session):
    return db_session.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()


def rules(db):
    return {r.code: r for r in db.query(PayflowRuleModel).all()}


def test_seed_adds_the_seventeen_rules_once(db_session):
    result = seed_operational_rules(db_session)
    stored = rules(db_session)
    assert result["rules"] == len(OPERATIONAL_RULES) == 17 == len(stored)
    assert all(r.rule_type == "System Rule" and r.client_id is None for r in stored.values())
    assert all(r.created_by == "PayFlow" and r.applied_to == [] for r in stored.values())
    assert all(r.category in RULE_CATEGORIES and r.action in RULE_ACTIONS for r in stored.values())

    # Nothing is changed on a second run, and an edit made in the app is kept.
    stored["quiet-period-guard"].conditions = [{"id": "x", "field": "Messages Sent (7 days)", "operator": "Greater Than", "value": "9"}]
    db_session.commit()
    assert seed_operational_rules(db_session)["rules"] == 0
    assert rules(db_session)["quiet-period-guard"].conditions[0]["value"] == "9"


def test_no_seeded_rule_is_active_without_working(db_session):
    seed_operational_rules(db_session)
    stored = rules(db_session)
    active = {code for code, r in stored.items() if r.status == "Active"}
    # The prototype's Active rules PayFlow can check stay Active; the others become Drafts.
    assert active == {"high-balance-review", "repeated-attempts-escalation", "quiet-period-guard"}
    for code in ("low-confidence-review", "dispute-detected-review", "northstar-hardship-review"):
        assert stored[code].status == "Draft"
        assert "cannot check" in stored[code].history[0]["change"]
    assert all(not unenforced_parts(r.conditions, r.action) for r in stored.values() if r.status == "Active")
    # Every essential rule is a Draft for review, as in the prototype.
    essentials = [e["code"] for e in OPERATIONAL_RULES if e.get("note")]
    assert len(essentials) == 10 and all(stored[code].status == "Draft" for code in essentials)


def test_seeded_rules_do_not_apply_until_a_client_selects_them(db_session):
    seed_operational_rules(db_session)
    client = PayflowClientModel(code="opr-c", name="Rule Client")
    db_session.add(client)
    db_session.flush()
    account = PayflowAccountModel(
        client_id=client.id, customer_name="Sam Sample", account_reference="OPR-1", case_reference="CASE-OPR-1",
        original_balance=20000, outstanding_balance=20000, recovered_balance=0, collection_status="Active",
    )
    db_session.add(account)
    db_session.commit()
    runtime = RuleRuntimeService(db_session)
    assert runtime.active_rules_for_account(account) == []

    client.governance_rules = ["High Balance Review"]
    db_session.commit()
    assert [r.code for r in runtime.active_rules_for_account(account)] == ["high-balance-review"]
    assert runtime.first_blocking_action(account, for_communication=True)["action"] == "Require Human Review"


def test_a_rule_that_cannot_work_cannot_be_activated(db_session, admin):
    seed_operational_rules(db_session)
    service = PayflowRuleService(db_session)
    draft = rules(db_session)["missed-installment-follow-up"]  # action "Send Payment Reminder"
    with pytest.raises(ValidationAppError) as error:
        service.activate(admin, draft.id)
    assert 'action "Send Payment Reminder"' in error.value.message
    assert service.get_rule(admin, draft.id)["unenforced"] == ['action "Send Payment Reminder"']

    with pytest.raises(ValidationAppError):
        service.create_rule(
            admin,
            {
                "name": "Needs dispute data", "rule_type": "System Rule", "category": "Customer Risk",
                "action": "Hold Action", "status": "Active",
                "conditions": [{"field": "Dispute Flag", "operator": "Is", "value": "Yes"}],
            },
        )
    # The same rule as a Draft is fine; a working essential can be activated.
    service.create_rule(
        admin,
        {
            "name": "Needs dispute data", "rule_type": "System Rule", "category": "Customer Risk",
            "action": "Hold Action", "status": "Draft",
            "conditions": [{"field": "Dispute Flag", "operator": "Is", "value": "Yes"}],
        },
    )
    opted_out = rules(db_session)["respect-channel-unsubscribe"]
    assert service.activate(admin, opted_out.id)["status"] == "Active"


def test_new_rule_fields_are_checked(db_session):
    client = PayflowClientModel(code="opr-f", name="Field Client")
    db_session.add(client)
    db_session.flush()
    account = PayflowAccountModel(
        client_id=client.id, customer_name="Sam Sample", account_reference="OPR-2", case_reference="CASE-OPR-2",
        original_balance=500, outstanding_balance=300, recovered_balance=200, collection_status="Active",
        days_past_due=45, last_payment_amount=200.0, last_contact_date=date.today() - timedelta(days=4),
        email_consent=False, sms_opt_out=False,
    )
    db_session.add(account)
    db_session.commit()
    ctx = {"db": db_session, "channel": "Email"}

    def value(field, **extra):
        return FIELD_GETTERS[field](account, {**ctx, **extra})

    assert (value("Days Past Due"), value("Last Payment Amount"), value("Days Since Last Contact")) == (45, 200.0, 4)
    assert value("Customer Opted Out") == "Yes" and value("Customer Opted Out", channel="SMS") == "No"
    assert (value("Payment Status"), value("Payment Processing Status"), value("Total Contact Attempts")) == (
        "Partially Paid", "None", 0,
    )

    link = PayflowPaymentLinkModel(token="opr-token-0000001", client_id=client.id, account_id=account.id,
                                   expires_at=date.today() + timedelta(days=3))
    db_session.add(link)
    db_session.flush()
    attempt = PayflowPaymentAttemptModel(
        public_id="opr-att-1", payment_link_id=link.id, client_id=client.id, account_id=account.id,
        option_type="partial", amount=50, status="Processing", provider="mock",
    )
    db_session.add(attempt)
    db_session.commit()
    assert value("Payment Processing Status") == "Pending"
    attempt.status = "Failed"
    db_session.commit()
    assert (value("Payment Processing Status"), value("Payment Status")) == ("Failed", "Failed")
    account.outstanding_balance = 0
    db_session.commit()
    assert value("Payment Status") == "Paid"
