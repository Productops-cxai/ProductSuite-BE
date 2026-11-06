"""Require Human Review / Escalate only block Communication steps, not Wait/Trigger."""

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowRuleModel,
)
from app.modules.payflow.services.rule_runtime_service import RuleRuntimeService


def _seed(db, *, action: str = "Require Human Review"):
    client = PayflowClientModel(
        code="rhr-gate",
        name="RHR Gate Client",
        governance_rules=["High Balance Review"],
    )
    db.add(client)
    db.flush()
    rule = PayflowRuleModel(
        code="high-bal",
        name="High Balance Review",
        rule_type="System Rule",
        status="Active",
        category="Amount",
        action=action,
        conditions=[{"field": "Outstanding Balance", "operator": ">", "value": "100"}],
    )
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Johny Bravo",
        account_reference="100266045",
        case_reference="CASE-100266045-01",
        outstanding_balance=1750.0,
        collection_status="Active",
    )
    db.add_all([rule, account])
    db.commit()
    return account


def test_rhr_blocks_only_on_communication_steps(db_session):
    account = _seed(db_session)
    runtime = RuleRuntimeService(db_session)

    assert runtime.first_blocking_action(account, for_communication=False) is None
    blocked = runtime.first_blocking_action(account, for_communication=True, channel="Email")
    assert blocked is not None
    assert blocked["action"] == "Require Human Review"
    assert blocked["name"] == "High Balance Review"


def test_hold_action_still_applies_outside_communication(db_session):
    account = _seed(db_session, action="Hold Action")
    runtime = RuleRuntimeService(db_session)

    blocked = runtime.first_blocking_action(account, for_communication=False)
    assert blocked is not None
    assert blocked["action"] == "Hold Action"
