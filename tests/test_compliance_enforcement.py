"""Compliance is asked before every send, again at send time, and events count once."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationModel,
    PayflowComplianceAuditModel,
    PayflowComplianceEventModel,
    PayflowComplianceRuleModel,
    PayflowHumanReviewModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowStepRunModel,
    UserModel,
)
from app.modules.payflow.services.communication_service import PayflowCommunicationService
from app.modules.payflow.services.compliance import engine
from app.modules.payflow.services.compliance.evaluation_service import ComplianceEvaluationService
from app.modules.payflow.services.review_service import PayflowReviewService
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import PayflowWorkflowRunStatus

STEPS = [
    {
        "id": "s1",
        "kind": "Communication",
        "title": "Email reminder",
        "config": {"channel": "Email", "purpose": "Payment Reminder", "subject": "Reminder",
                   "body": "Hello {{first_name}}, please pay."},
        "next": "s2",
    },
    {"id": "s2", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
]
_SEQ = 0


def setup(db, *, province="Ontario", country="CA"):
    global _SEQ
    _SEQ += 1
    client = PayflowClientModel(code=f"enf-c-{_SEQ}", name="Enforce Client", country="Canada")
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Jane Doe",
        customer_first_name="Jane",
        account_reference=f"ENF-{_SEQ}",
        case_reference=f"CASE-ENF-{_SEQ}",
        original_balance=500.0,
        outstanding_balance=500.0,
        collection_status="Active",
        email="jane@example.com",
        phone_mobile="+14165550100",
        country_code=country,
        province_state=province,
    )
    strategy = PayflowStrategyModel(
        code=f"enf-wf-{_SEQ}", name="Enforce WF", client_id=client.id, status="Active",
        steps=STEPS, entry_node_id="s1",
    )
    db.add_all([account, strategy])
    db.flush()
    run = PayflowWorkflowRunModel(
        account_id=account.id, strategy_id=strategy.id, client_id=client.id,
        status=PayflowWorkflowRunStatus.ACTIVE.value, current_step_id="s1", strategy_version=1,
    )
    db.add(run)
    db.commit()
    return client, account, run


def active_rule(db, *, rule_type="JUR-09", category="Consent & Opt-Out", action="Suppress",
                field="Cease Communication", value="Yes", settings=None, state="", channel="All Channels"):
    global _SEQ
    _SEQ += 1
    row = PayflowComplianceRuleModel(
        code=f"CMP-E{_SEQ:03d}", name=f"Enforced rule {_SEQ}", category=category, rule_type=rule_type,
        enforcement="Mandatory", country="Canada", state=state, channel=channel, action=action,
        status="Active", version=2,
        conditions=[{"field": field, "operator": "Equals", "value": value, "period": 7}],
        settings=settings or {},
    )
    db.add(row)
    db.commit()
    return row


def closed_hours(db):
    """Permitted hours that exclude the present moment in Ontario."""
    return active_rule(
        db, rule_type="JUR-01", category="Permitted Hours", action="Reschedule",
        field="Outside Permitted Hours",
        settings={"start": "00:00", "end": "00:01", "days": [0, 1, 2, 3, 4, 5, 6]},
    )


def flag(db, account, name):
    account.compliance_profile = {"flags": {name: True}}
    db.commit()


def pending(db, run):
    return (
        db.query(PayflowWorkflowJobModel)
        .filter(PayflowWorkflowJobModel.run_id == run.id, PayflowWorkflowJobModel.status == "pending")
        .order_by(PayflowWorkflowJobModel.id.asc())
        .all()
    )


def sent(db, account):
    return (
        db.query(PayflowCommunicationModel)
        .filter(PayflowCommunicationModel.account_id == account.id,
                PayflowCommunicationModel.status.in_(["Sent", "Queued", "Delivered"]))
        .all()
    )


def no_link():
    return patch.object(WorkflowExecutionService, "_payment_link", return_value="")


# ------------------------------------------------------------------ baseline


def test_with_no_active_rules_messages_go_out_as_before(db_session):
    _client, account, run = setup(db_session)
    draft = active_rule(db_session)
    draft.status = "Draft"
    flag(db_session, account, "Cease Communication")

    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    assert len(sent(db_session, account)) == 1
    assert [j.step_id for j in pending(db_session, run)] == ["s2"]


# ------------------------------------------------------------------ BLOCK


def test_cease_communication_blocks_the_send_and_records_why(db_session):
    _client, account, run = setup(db_session)
    rule = active_rule(db_session)
    flag(db_session, account, "Cease Communication")

    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    assert sent(db_session, account) == []
    blocked = db_session.query(PayflowCommunicationModel).filter_by(account_id=account.id).one()
    assert blocked.status == "Suppressed"
    assert rule.code in (blocked.restriction_reason or "")
    assert blocked.rule_code == rule.code
    audit = db_session.query(PayflowComplianceAuditModel).filter_by(entity_type="evaluation").one()
    assert audit.event == "Execution check: BLOCK"
    assert audit.after["matches"][0]["version"] == 2  # the version that decided it


def test_send_gate_reports_the_compliance_decision(db_session):
    _client, account, _run = setup(db_session)
    active_rule(db_session)
    flag(db_session, account, "Cease Communication")

    gate = PayflowCommunicationService(db_session).evaluate_send_eligibility(account, channel="Email")

    assert gate["ok"] is False
    assert gate["eligibility_status"] == "Blocked"
    assert gate["compliance"]["decision"] == "BLOCK"


# ------------------------------------------------------------------ RESCHEDULE


def test_message_outside_permitted_hours_waits_for_the_next_permitted_time(db_session):
    _client, account, run = setup(db_session)
    closed_hours(db_session)

    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    assert sent(db_session, account) == []
    jobs = pending(db_session, run)
    assert [j.step_id for j in jobs] == ["s1"]  # the same message, later
    when = jobs[0].scheduled_at
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    assert when > datetime.now(timezone.utc)
    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.WAITING.value
    step_run = db_session.query(PayflowWorkflowStepRunModel).filter_by(run_id=run.id).one()
    assert step_run.status == "scheduled"


def test_scheduled_message_is_checked_again_when_its_time_comes(db_session):
    """Rules, flags and the clock can change while a message waits (AC17)."""
    _client, account, run = setup(db_session)
    hours = closed_hours(db_session)
    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    for job in pending(db_session, run):
        job.status = "done"
    # While it waited: the window was lifted, but the customer asked us to stop.
    hours.status = "Inactive"
    active_rule(db_session)
    flag(db_session, account, "Cease Communication")

    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1", after_comm_delay=True)

    assert sent(db_session, account) == []
    assert db_session.query(PayflowCommunicationModel).filter_by(
        account_id=account.id, status="Suppressed"
    ).count() == 1


# ------------------------------------------------------------------ REVIEW_REQUIRED


def test_review_required_pauses_the_run_for_a_supervisor(db_session):
    _client, account, run = setup(db_session)
    active_rule(db_session, rule_type="JUR-11", category="Contact Restrictions",
                action="Require Approval", field="Legal Representation")
    flag(db_session, account, "Legal Representation")

    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value
    assert sent(db_session, account) == []
    review = db_session.get(PayflowHumanReviewModel, run.human_review_id)
    assert review.reason == "Compliance review required"


def test_message_goes_out_once_the_supervisor_approves_the_review(db_session):
    _client, account, run = setup(db_session)
    active_rule(db_session, rule_type="JUR-11", category="Contact Restrictions",
                action="Require Approval", field="Legal Representation")
    flag(db_session, account, "Legal Representation")
    svc = WorkflowExecutionService(db_session)
    with no_link():
        svc.execute_step(run.id, "s1")
    db_session.refresh(run)
    admin = db_session.query(UserModel).filter_by(email="admin@payflow.ai").one()

    PayflowReviewService(db_session).approve(admin, run.human_review_id)
    resume = pending(db_session, run)[0]
    with no_link():
        svc.resume_run(run.id, from_step_id=resume.step_id, after_human_review=True)
        resume.status = "done"
        db_session.commit()
        svc.execute_step(run.id, "s1", after_human_review=True)

    assert len(sent(db_session, account)) == 1


def test_approval_does_not_lift_a_prohibition(db_session):
    """A supervisor can clear a review, not a mandatory block."""
    _client, account, run = setup(db_session)
    active_rule(db_session, rule_type="JUR-11", category="Contact Restrictions",
                action="Require Approval", field="Legal Representation")
    active_rule(db_session)
    account.compliance_profile = {"flags": {"Legal Representation": True, "Cease Communication": True}}
    db_session.commit()

    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1", after_human_review=True)

    assert sent(db_session, account) == []


def test_unknown_customer_location_needs_review_when_rules_are_in_force(db_session):
    _client, account, run = setup(db_session, province=None, country=None)
    active_rule(db_session)

    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value
    assert sent(db_session, account) == []


# ------------------------------------------------------------------ counters / duplicates


def test_a_sent_message_is_counted_once(db_session):
    _client, account, run = setup(db_session)

    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1")

    events = db_session.query(PayflowComplianceEventModel).filter_by(account_id=account.id).all()
    assert [(e.channel, e.kind) for e in events] == [("Email", "attempted")]


def test_the_same_event_received_twice_is_counted_once(db_session):
    _client, account, _run = setup(db_session)
    svc = ComplianceEvaluationService(db_session)

    first = svc.record_event(account, channel="SMS", kind="completed", key="twilio:SM123:delivered")
    again = svc.record_event(account, channel="SMS", kind="completed", key="twilio:SM123:delivered")
    db_session.commit()

    assert (first, again) == (True, False)
    assert db_session.query(PayflowComplianceEventModel).count() == 1


def test_repeated_delivery_webhook_does_not_inflate_the_counter(db_session):
    _client, account, run = setup(db_session)
    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    comm = sent(db_session, account)[0]
    service = PayflowCommunicationService(db_session)

    for _ in range(3):
        service.apply_provider_event(communication_id=comm.id, event_type="delivered", commit=True)

    completed = db_session.query(PayflowComplianceEventModel).filter_by(
        account_id=account.id, kind="completed"
    ).count()
    assert completed == 1


def test_frequency_limit_counts_real_sends_and_blocks_the_next_one(db_session):
    _client, account, run = setup(db_session)
    active_rule(
        db_session, rule_type="JUR-04", category="Frequency", action="Block",
        field="Contact Attempts", value="0", settings={"combined": True},
    )
    rule = db_session.query(PayflowComplianceRuleModel).order_by(PayflowComplianceRuleModel.id.desc()).first()
    rule.conditions = [{"field": "Contact Attempts", "operator": "Greater Than", "value": "0", "period": 7}]
    db_session.commit()
    svc = ComplianceEvaluationService(db_session)

    assert svc.evaluate_account(account, channel="Email", record=False).decision == "ALLOW"
    with no_link():
        WorkflowExecutionService(db_session).execute_step(run.id, "s1")  # one contact
    assert svc.evaluate_account(account, channel="Email", record=False).decision == "BLOCK"


def test_customer_time_zone_on_the_account_overrides_the_province_default(db_session):
    _client, account, _run = setup(db_session)
    account.timezone = "America/Vancouver"
    db_session.commit()

    where = ComplianceEvaluationService(db_session).jurisdiction_for(account)

    assert (where.country, where.state, where.timezone) == ("Canada", "Ontario", "America/Vancouver")
    assert isinstance(where, engine.Jurisdiction)
