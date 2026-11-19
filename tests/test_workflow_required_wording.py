"""The wording compliance rules require in a workflow's messages (workflow_suggestion/required_wording.py): the rules
are picked the way the send check picks them, for the places of the workflow's customers - code only, no AI."""

from datetime import date, timedelta

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientComplianceRuleModel,
    PayflowClientModel,
    PayflowComplianceRuleModel,
)
from app.modules.payflow.services.compliance.evaluation_service import ComplianceEvaluationService
from app.modules.payflow.services.workflow_suggestion.required_wording import required_wording
from tests.test_workflow_audience_messages import add_seniors
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)

DEBT_COLLECTOR = "This is a communication from a debt collector."
_SEQ = [0]


def content_rule(db, text, *, priority=100, country="Canada", state="", channel="All Channels", **values):
    """An active, mandatory Content rule that requires `text` (as a compliance admin would set it up)."""
    _SEQ[0] += 1
    row = PayflowComplianceRuleModel(
        code=f"CMP-W{_SEQ[0]:03d}", name=f"Required wording {_SEQ[0]}", category="Content", rule_type="JUR-23",
        enforcement=values.pop("enforcement", "Mandatory"), status=values.pop("status", "Active"),
        action=values.pop("action", "Block"), country=country, state=state, channel=channel, priority=priority,
        version=1, conditions=[], settings={"required_text": text, **values.pop("settings", {})}, **values,
    )
    db.add(row)
    db.commit()
    return row


def accounts(db, portfolio_id):  # noqa: F811
    return db.query(PayflowAccountModel).filter(PayflowAccountModel.portfolio_id == portfolio_id).all()


def ontario(db, portfolio):  # noqa: F811
    """The portfolio's accounts, 25 of them in Ontario, Canada (the 30 of the fixture have no place)."""
    add_seniors(db, portfolio, country_code="CA", province_state="ON", timezone="America/Toronto")
    return [a for a in accounts(db, portfolio["portfolio_id"]) if a.province_state == "ON"]


def test_each_channel_gets_the_wording_of_the_rules_for_its_customers(db_session, portfolio):  # noqa: F811
    members = ontario(db_session, portfolio)
    content_rule(db_session, DEBT_COLLECTOR, priority=300)  # all of Canada, every channel
    content_rule(db_session, "Ontario email wording.", priority=200, state="Ontario", channel="Email")
    content_rule(db_session, "Quebec wording.", priority=250, state="Quebec")  # no customer in Quebec
    content_rule(db_session, "SMS wording.", priority=100, channel="SMS")

    assert required_wording(db_session, portfolio["client_id"], members) == {
        "Email": [DEBT_COLLECTOR, "Ontario email wording."],  # most important rule first
        "SMS": [DEBT_COLLECTOR, "SMS wording."],
    }


def test_rules_that_do_not_apply_are_left_out(db_session, portfolio):  # noqa: F811
    members = ontario(db_session, portfolio)
    client_id = portfolio["client_id"]
    db_session.get(PayflowClientModel, client_id).client_type = "first_party"
    content_rule(db_session, DEBT_COLLECTOR)
    content_rule(db_session, "Draft.", status="Draft")
    content_rule(db_session, "Expired.", effective_until=date.today() - timedelta(days=2))
    content_rule(db_session, "Not yet.", effective_from=date.today() + timedelta(days=2))
    content_rule(db_session, "Another client.", client_id=portfolio["other_id"])
    content_rule(db_session, "Agencies only.", collector_type="agency")
    content_rule(db_session, "Allowed anyway.", action="Allow")
    content_rule(db_session, "Promotional only.", settings={"promotional_only": True})
    off = content_rule(db_session, "Switched off.", enforcement="Client Configurable")
    db_session.add(PayflowClientComplianceRuleModel(client_id=client_id, rule_id=off.id, enabled=False))
    hours = content_rule(db_session, "Not a Content rule.")
    hours.category = "Permitted Hours"
    db_session.commit()

    assert required_wording(db_session, client_id, members) == {"Email": [DEBT_COLLECTOR], "SMS": [DEBT_COLLECTOR]}


def test_customers_without_a_known_place_only_get_rules_for_anywhere(db_session, portfolio):  # noqa: F811
    members = accounts(db_session, portfolio["portfolio_id"])  # no country or province
    content_rule(db_session, "Canada only.")
    assert required_wording(db_session, portfolio["client_id"], members) == {}
    content_rule(db_session, "Everywhere.", country="")
    assert required_wording(db_session, portfolio["client_id"], members) == {"Email": ["Everywhere."],
                                                                           "SMS": ["Everywhere."]}


def test_nothing_required_without_rules_or_customers(db_session, portfolio):  # noqa: F811
    members = ontario(db_session, portfolio)
    assert required_wording(db_session, portfolio["client_id"], members) == {}
    content_rule(db_session, DEBT_COLLECTOR)
    assert required_wording(db_session, portfolio["client_id"], []) == {}


def test_it_agrees_with_the_send_check(db_session, portfolio):  # noqa: F811
    members = ontario(db_session, portfolio)
    content_rule(db_session, DEBT_COLLECTOR, priority=300)
    content_rule(db_session, "Ontario email wording.", state="Ontario", channel="Email")
    wording = required_wording(db_session, portfolio["client_id"], members)
    check = ComplianceEvaluationService(db_session)
    for channel, texts in wording.items():
        with_it = check.evaluate_account(members[0], channel=channel, message_text="Hello. " + " ".join(texts),
                                         record=False)
        without = check.evaluate_account(members[0], channel=channel, message_text="Hello.", record=False)
        assert with_it.decision == "ALLOW", (channel, with_it.explanation)
        assert without.decision == "BLOCK", (channel, without.explanation)
