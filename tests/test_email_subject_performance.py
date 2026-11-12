"""Email subject performance: what is tracked on each email, which email a payment is credited to,
the Dashboard numbers, and the AI pieces (insights, suggestions, proposals) - never the real AI."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.exceptions import ForbiddenError, ServiceUnavailableError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowCommunicationEventModel,
    PayflowCommunicationModel,
    PayflowMessageProposalModel,
    PayflowMessageVersionModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentLinkModel,
    PayflowPaymentPlanModel,
    PayflowPaymentTransactionModel,
    PayflowPortfolioModel,
    PayflowStrategyModel,
    UserModel,
)
from app.modules.payflow.services import communication_service, email_subject_ai
from app.infrastructure.database.models import PayflowNotificationModel
from app.modules.payflow.services.communication_service import PayflowCommunicationService
from app.modules.payflow.services.email_subject_ai import PayflowEmailSubjectAiService
from app.modules.payflow.services.email_subject_service import PayflowEmailSubjectService
from app.modules.payflow.services.email_tracking import (
    reminder_stage,
    stamp_content_origin,
    subject_key,
)
from app.modules.payflow.services.payment_link_service import (
    PayflowPaymentLinkService,
    split_tracked_token,
    tracked_token,
)
from app.modules.payflow.services.payment_outcome_service import PayflowPaymentOutcomeService
from app.modules.payflow.services.payment_portal_service import PayflowPaymentPortalService
from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion import message_writer
from app.modules.payflow.services.workflow_suggestion.message_prompt import build_message_prompt

NOW = datetime.now(timezone.utc)
FRIENDLY = "A friendly reminder about your balance"
OPTIONS = "Your payment options are ready"
FIRM = "Action required on account {{account_reference}}"
_SEQ = 0


def _n():
    global _SEQ
    _SEQ += 1
    return _SEQ


@pytest.fixture()
def admin(db_session):
    return db_session.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()


@pytest.fixture(autouse=True)
def small_minimum(monkeypatch):
    """Three delivered emails are enough to rank in these tests (100 in production)."""
    monkeypatch.setattr(settings, "EMAIL_SUBJECT_MIN_DELIVERIES", 3)
    email_subject_ai._INSIGHT_CACHE.clear()  # no AI answer carried over from another test


def make_client(db, name=None):
    n = _n()
    client = PayflowClientModel(code=f"esp-c-{n}", name=name or f"Subject Client {n}")
    db.add(client)
    db.flush()
    return client


def make_account(db, client, *, portfolio_id=None):
    n = _n()
    account = PayflowAccountModel(
        client_id=client.id,
        portfolio_id=portfolio_id,
        customer_name="Pat Private",
        customer_first_name="Pat",
        account_reference=f"ESP-{n}",
        case_reference=f"CASE-ESP-{n}",
        email=f"pat{n}@example.com",
        email_consent=True,
        original_balance=500.0,
        outstanding_balance=500.0,
        recovered_balance=0.0,
        collection_status="Active",
        currency_code="CAD",
    )
    db.add(account)
    db.flush()
    return account


def email_step(step_id, purpose="Payment Reminder", **config):
    return {
        "id": step_id,
        "kind": "Communication",
        "title": f"Email {step_id}",
        "origin": "Human Created",
        "config": {"channel": "Email", "purpose": purpose, **config},
        "next": None,
    }


def make_strategy(db, client, steps=None, status="Active"):
    n = _n()
    steps = steps or [email_step("s1", subject=FRIENDLY, body="Hi {{first_name}}, pay here: {{payment_link}}")]
    row = PayflowStrategyModel(
        code=f"esp-wf-{n}",
        name=f"Subject Workflow {n}",
        client_id=client.id,
        status=status,
        origin="Human Created",
        source="Human Created",
        version=1,
        steps=steps,
        entry_node_id=steps[0]["id"],
    )
    db.add(row)
    db.flush()
    return row


def make_email(
    db,
    account,
    subject,
    *,
    strategy=None,
    stage="First reminder",
    purpose="Payment Reminder",
    delivered=True,
    opens=0,
    clicked=False,
    initiated=False,
    sent_at=None,
    version_id=None,
    step_id="s1",
    manual=False,
):
    sent_at = sent_at or NOW - timedelta(hours=2)
    comm = PayflowCommunicationModel(
        code=f"COM-T{_n():07d}",
        client_id=account.client_id,
        account_id=account.id,
        channel="Email",
        purpose=purpose,
        status="Delivered" if delivered else "Sent",
        strategy_id=strategy.id if strategy else None,
        workflow_name=strategy.name if strategy else "Manual Email",
        subject=subject.replace("{{account_reference}}", account.account_reference),
        subject_template=subject,
        subject_key=subject_key(subject),
        payment_link=True,
        sent_at=sent_at,
        delivered_at=sent_at + timedelta(minutes=1) if delivered else None,
        opens_count=opens,
        first_opened_at=sent_at + timedelta(minutes=5) if opens else None,
        clicks_count=2 if clicked else 0,  # a customer who came back to the portal twice
        last_clicked_at=sent_at + timedelta(minutes=10) if clicked else None,
        payment_initiated=initiated,
        reminder_stage=None if manual else stage,
        portfolio_id=account.portfolio_id,
        step_id=None if manual else step_id,
        message_version_id=version_id,
        is_manual=manual,
        engagement_tracking="available",
    )
    db.add(comm)
    db.flush()
    if clicked:
        db.add(
            PayflowCommunicationEventModel(
                communication_id=comm.id, event_type="click", occurred_at=comm.last_clicked_at
            )
        )
        db.flush()
    return comm


def make_payment(db, account, comm=None, *, status="Succeeded", method=None, completed_at=None):
    txn = PayflowPaymentTransactionModel(
        public_id=f"txn_t{_n()}",
        client_id=account.client_id,
        account_id=account.id,
        direction="payment",
        status=status,
        amount=50.0,
        currency_code="CAD",
        attributed_communication_id=comm.id if comm else None,
        attribution_method=method or ("last_click" if comm else "none"),
        completed_at=completed_at or NOW - timedelta(hours=1),
    )
    db.add(txn)
    db.flush()
    return txn


def make_link(db, account, comm=None):
    link = PayflowPaymentLinkModel(
        token=f"tok{_n():013d}abcdefgh",
        client_id=account.client_id,
        account_id=account.id,
        communication_id=comm.id if comm else None,
        expires_at=NOW + timedelta(days=7),
    )
    db.add(link)
    db.flush()
    return link


def make_attempt(db, account, link, *, comm=None, option="partial", **extra):
    attempt = PayflowPaymentAttemptModel(
        public_id=f"att-t{_n()}",
        payment_link_id=link.id,
        client_id=account.client_id,
        account_id=account.id,
        option_type=option,
        amount=50.0,
        currency_code="CAD",
        status="Processing",
        provider="mock",
        communication_id=comm.id if comm else None,
        **extra,
    )
    db.add(attempt)
    db.flush()
    return attempt


def confirm(db, account, attempt):
    return PayflowPaymentOutcomeService(db).record_confirmed_payment(
        attempt, account, amount=50.0, balance_before=500.0, balance_after=450.0, provider_ref=None
    )


def section(db, user, **filters):
    service = PayflowEmailSubjectService(db)
    return service.get_section(user, service.build_filters(user, **filters))


def by_subject(result):
    return {row["subject"]: row for row in result["rows"]}


# --------------------------------------------------------------------------- tracking (FR-06)


def send(db, account, strategy, step, *, subject, body, provider_status="sent"):
    """A real send_workflow_step with the email provider and the eligibility gate stubbed."""
    log = SimpleNamespace(status=provider_status, provider_message_id=f"m{_n()}", provider_name="sendgrid", error=None)
    with (
        patch.object(PayflowCommunicationService, "evaluate_send_eligibility", return_value={"ok": True}),
        patch.object(communication_service, "send_collection_email", return_value=log) as mail,
    ):
        result = PayflowCommunicationService(db).send_workflow_step(
            account=account,
            strategy=strategy,
            channel="Email",
            purpose="Payment Reminder",
            subject=subject.replace("{{account_reference}}", account.account_reference),
            body=body.replace("{{first_name}}", "Pat"),
            payment_link="1",
            client_id=account.client_id,
            tracking={
                "subject_template": subject,
                "body_template": body,
                "step_id": step["id"],
                "strategy_version": 1,
                "reminder_stage": "Final reminder",
                "origin": "AI Generated",
            },
        )
    return db.get(PayflowCommunicationModel, result["communication_id"]), mail


def test_email_keeps_the_subject_as_written_and_its_text_version(db_session):
    client = make_client(db_session)
    strategy = make_strategy(db_session, client)
    step = strategy.steps[0]
    first, _ = send(db_session, make_account(db_session, client), strategy, step, subject=FIRM, body="Hi {{first_name}}")
    second, _ = send(db_session, make_account(db_session, client), strategy, step, subject=FIRM, body="Hi {{first_name}}")

    # Each customer got their own account number, but it is one subject.
    assert first.subject != second.subject and "{{" not in first.subject
    assert first.subject_template == second.subject_template == FIRM
    assert first.subject_key == second.subject_key == FIRM
    assert (first.step_id, first.strategy_version, first.reminder_stage) == ("s1", 1, "Final reminder")
    # The same text is one stored version, with placeholders and no customer values.
    assert first.message_version_id == second.message_version_id
    version = db_session.get(PayflowMessageVersionModel, first.message_version_id)
    assert version.body_template == "Hi {{first_name}}" and "Pat" not in version.body_template
    assert version.origin == "AI Generated" and version.strategy_id == strategy.id

    changed, _ = send(db_session, make_account(db_session, client), strategy, step, subject=FIRM, body="Hello {{first_name}}")
    assert changed.message_version_id != first.message_version_id  # a new body is a new version
    assert db_session.get(PayflowMessageVersionModel, first.message_version_id) is not None  # the old one stays


def test_worker_passes_the_unfilled_text_and_stage(db_session):
    from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService

    client = make_client(db_session)
    steps = [
        email_step("s1", subject=FIRM, body="Hi {{first_name}}", content_origin="AI Generated"),
        email_step("s2"),  # no text of its own: its template is sent
    ]
    strategy = make_strategy(db_session, client, steps)
    account = make_account(db_session, client)
    run = SimpleNamespace(id=7, portfolio_id=None)
    worker = WorkflowExecutionService(db_session)

    own = worker._message_tracking(run, account, strategy, steps[0], channel="Email", purpose="Payment Reminder")
    assert (own["subject_template"], own["body_template"]) == (FIRM, "Hi {{first_name}}")
    assert (own["step_id"], own["run_id"], own["reminder_stage"]) == ("s1", 7, "First reminder")
    assert (own["origin"], own["template_id"]) == ("AI Generated", None)

    template = worker._message_tracking(run, account, strategy, steps[1], channel="Email", purpose="Payment Reminder")
    assert template["origin"] == "System Template" and template["reminder_stage"] == "Second reminder"
    assert "{{first_name}}" in template["body_template"] and "Pat" not in template["body_template"]


def test_each_email_carries_its_own_payment_link(db_session):
    client = make_client(db_session)
    strategy = make_strategy(db_session, client)
    account = make_account(db_session, client)
    first, mail = send(db_session, account, strategy, strategy.steps[0], subject=FRIENDLY, body="Hi")
    first_url = mail.call_args.kwargs["action_link"]
    second, mail = send(db_session, account, strategy, strategy.steps[0], subject=OPTIONS, body="Hi again")
    second_url = mail.call_args.kwargs["action_link"]

    links = db_session.query(PayflowPaymentLinkModel).filter_by(account_id=account.id).all()
    assert len(links) == 1  # still one Active link per account
    token = links[0].token
    assert first_url != second_url
    assert split_tracked_token(first_url.rsplit("/", 1)[-1]) == (token, first.id)
    assert split_tracked_token(second_url.rsplit("/", 1)[-1]) == (token, second.id)

    # The portal credits the visit to the email whose link was used - not the link's first email.
    portal = PayflowPaymentPortalService(db_session)
    assert portal._strip_preview_token(tracked_token(token, second.id)) == (token, False)
    assert portal._source_communication_id(tracked_token(token, second.id), links[0]) == second.id
    assert portal._source_communication_id(token, links[0]) == first.id  # an old link without a reference
    other = make_email(db_session, make_account(db_session, client), FRIENDLY)
    assert portal._source_communication_id(tracked_token(token, other.id), links[0]) == first.id  # not this account's

    portal.resolve_portal(tracked_token(token, second.id))
    db_session.refresh(first)
    db_session.refresh(second)
    assert (first.clicks_count, second.clicks_count) == (0, 1)


def test_manual_email_is_tracked_as_manual(db_session, admin):
    client = make_client(db_session)
    account = make_account(db_session, client)
    log = SimpleNamespace(status="sent", provider_message_id="m-manual", provider_name="sendgrid", error=None)
    with (
        patch.object(PayflowCommunicationService, "evaluate_send_eligibility", return_value={"ok": True}),
        patch.object(communication_service, "send_collection_email", return_value=log),
        patch.object(PayflowCommunicationService, "_notify_manual_comm_sent"),
    ):
        result = PayflowCommunicationService(db_session).send_manual_email(
            admin, account, subject="  About   your account ", body="Hello Pat"
        )
    comm = db_session.get(PayflowCommunicationModel, result["communication_id"])
    assert comm.is_manual is True and comm.subject_key == "About your account"
    assert comm.message_version_id is None and comm.reminder_stage is None

    row = by_subject(section(db_session, admin))["About your account"]
    assert (row["sent"], row["manual_sent"], row["variations"]) == (1, 1, 1)


def test_reminder_stage_comes_from_the_step_its_purpose_or_its_place():
    steps = [email_step("a"), email_step("b"), email_step("c")]
    assert [reminder_stage(steps, s, "Payment Reminder") for s in steps] == [
        "First reminder",
        "Second reminder",
        "Final reminder",
    ]
    assert reminder_stage(steps[:2], steps[1], "Payment Reminder") == "Second reminder"
    assert reminder_stage(steps, steps[0], "Payment Plan Reminder") == "Payment plan"
    assert reminder_stage(steps, steps[0], "Promise-to-Pay Reminder") == "Promise to pay"
    assert reminder_stage(steps, steps[0], "Firm Reminder") == "Final reminder"
    chosen = email_step("a", reminder_stage="Second reminder")
    assert reminder_stage([chosen], chosen, "Firm Reminder") == "Second reminder"


def test_content_origin_follows_who_wrote_the_text():
    ai = email_step("a", subject="S", body="B", content_origin="AI Generated")
    same = email_step("a", subject="S", body="B")
    edited = email_step("a", subject="S", body="B changed")
    assert stamp_content_origin([ai], [same])[0]["config"]["content_origin"] == "AI Generated"
    assert stamp_content_origin([ai], [edited])[0]["config"]["content_origin"] == "AI Generated, Human Edited"
    assert stamp_content_origin([email_step("a")], [edited])[0]["config"]["content_origin"] == "Human Created"
    assert stamp_content_origin([ai], [edited], ai_step_ids={"a"})[0]["config"]["content_origin"] == "AI Generated"
    assert "content_origin" not in stamp_content_origin([ai], [email_step("a")])[0]["config"]  # back to the template


# --------------------------------------------------------------------------- attribution (FR-11)


def test_payment_is_credited_to_the_last_clicked_email_once(db_session):
    client = make_client(db_session)
    account = make_account(db_session, client)
    early = make_email(db_session, account, FRIENDLY, clicked=True, sent_at=NOW - timedelta(days=3))
    late = make_email(db_session, account, OPTIONS, clicked=True, sent_at=NOW - timedelta(days=1))
    link = make_link(db_session, account, early)

    attempt = make_attempt(db_session, account, link)  # started from an old link: no reference
    txn = confirm(db_session, account, attempt)
    assert (txn.attributed_communication_id, txn.attribution_method) == (late.id, "last_click")
    assert confirm(db_session, account, attempt).id == txn.id  # the provider repeats: still one payment

    # The checkout's own email wins when the customer came in through it.
    direct = confirm(db_session, account, make_attempt(db_session, account, link, comm=early))
    assert direct.attributed_communication_id == early.id

    counted = (
        db_session.query(PayflowPaymentTransactionModel)
        .filter(PayflowPaymentTransactionModel.attributed_communication_id.in_([early.id, late.id]))
        .count()
    )
    assert counted == 2  # two payments, each credited to exactly one email


def test_click_older_than_the_window_is_not_credited(db_session):
    client = make_client(db_session)
    account = make_account(db_session, client)
    old = make_email(db_session, account, FRIENDLY, clicked=True, sent_at=NOW - timedelta(days=9))
    link = make_link(db_session, account, old)
    txn = confirm(db_session, account, make_attempt(db_session, account, link, comm=old))
    assert (txn.attributed_communication_id, txn.attribution_method) == (None, "none")

    never_clicked = make_account(db_session, client)
    make_email(db_session, never_clicked, FRIENDLY, opens=3)
    txn = confirm(db_session, never_clicked, make_attempt(db_session, never_clicked, make_link(db_session, never_clicked)))
    assert txn.attribution_method == "none"


def test_auto_charged_instalments_follow_the_plans_email(db_session):
    client = make_client(db_session)
    account = make_account(db_session, client)
    email = make_email(db_session, account, OPTIONS, clicked=True)
    link = make_link(db_session, account, email)
    first = confirm(db_session, account, make_attempt(db_session, account, link, comm=email, option="payment_plan"))
    plan = PayflowPaymentPlanModel(
        public_id=f"plan-{_n()}",
        client_id=client.id,
        account_id=account.id,
        payment_link_id=link.id,
        installments_total=3,
        installment_amount=50.0,
        total_payable=150.0,
        start_date=NOW.date(),
    )
    db_session.add(plan)
    db_session.flush()
    first.payment_plan_id = plan.id
    # A later email was clicked, but an automatic instalment is the plan's doing.
    make_email(db_session, account, FRIENDLY, clicked=True, sent_at=NOW - timedelta(minutes=30))
    auto = make_attempt(
        db_session, account, link, option="pay_next_instalment", plan_id=plan.public_id,
        provider_payload={"auto_charge": True},
    )
    txn = confirm(db_session, account, auto)
    assert (txn.attributed_communication_id, txn.attribution_method) == (email.id, "plan")


# --------------------------------------------------------------------------- dashboard (FR-02/03/11)


@pytest.fixture()
def history(db_session):
    """One client, one workflow: a strong subject, a weak one, and one with too few emails."""
    client = make_client(db_session, "Client A")
    strategy = make_strategy(db_session, client)
    accounts = [make_account(db_session, client) for _ in range(6)]
    # OPTIONS: 4 delivered, 3 opened, 2 clicked, 3 payments (two instalments from one email).
    strong = [
        make_email(db_session, accounts[0], OPTIONS, strategy=strategy, stage="Second reminder", opens=5, clicked=True, initiated=True),
        make_email(db_session, accounts[1], OPTIONS, strategy=strategy, stage="Second reminder", opens=1, clicked=True, initiated=True),
        make_email(db_session, accounts[2], OPTIONS, strategy=strategy, stage="Second reminder", opens=2),
        make_email(db_session, accounts[3], OPTIONS, strategy=strategy, stage="Second reminder"),
    ]
    make_payment(db_session, accounts[0], strong[0])
    make_payment(db_session, accounts[0], strong[0], method="plan")
    make_payment(db_session, accounts[1], strong[1])
    make_payment(db_session, accounts[1], strong[1], status="Refunded")  # taken back: not a completed payment
    # FRIENDLY: 5 sent, 4 delivered, all 4 opened, 0 clicks, 0 payments. Whitespace differs.
    for index, account in enumerate(accounts[:5]):
        make_email(
            db_session, account, f"  {FRIENDLY.replace(' ', '  ', 1)} " if index % 2 else FRIENDLY,
            strategy=strategy, stage="Second reminder", delivered=index != 4, opens=0 if index == 4 else 1,
        )
    # FIRM: 1 email, paid - a 100% conversion that must not rank.
    lucky = make_email(db_session, accounts[5], FIRM, strategy=strategy, stage="Final reminder", purpose="Firm Reminder", opens=1, clicked=True, initiated=True)
    make_payment(db_session, accounts[5], lucky)
    make_payment(db_session, accounts[5])  # paid with no qualifying click
    db_session.commit()
    return SimpleNamespace(client=client, strategy=strategy, accounts=accounts)


def test_table_counts_unique_events_and_payment_transactions(db_session, admin, history):
    result = section(db_session, admin)
    rows = by_subject(result)
    assert [row["subject"] for row in result["rows"]] == [OPTIONS, FIRM, FRIENDLY]  # completed payments, desc

    strong = rows[OPTIONS]
    assert (strong["sent"], strong["delivered"], strong["opens"], strong["clicks"]) == (4, 4, 3, 2)
    assert (strong["open_rate"], strong["click_rate"]) == (0.75, 0.5)
    assert (strong["payment_link_clicks"], strong["initiated"]) == (2, 2)
    assert (strong["completed"], strong["emails_with_payment"], strong["conversion"]) == (3, 2, 0.75)

    weak = rows[FRIENDLY]  # one subject, however its spaces were typed
    assert (weak["sent"], weak["delivered"], weak["opens"], weak["completed"], weak["conversion"]) == (5, 4, 4, 0, 0.0)
    assert weak["contexts"] == ["Client A · Second reminder"]

    assert rows[FIRM]["low_sample"] is True and rows[FIRM]["conversion"] == 1.0
    assert result["unattributed_payments"] == 1
    assert result["measurement"]["min_deliveries"] == 3 and result["measurement"]["attribution_window_days"] == 7


def test_kpi_cards_and_minimum_volume(db_session, admin, history):
    leaders = section(db_session, admin)["leaders"]
    assert leaders["opened"]["subject"] == FRIENDLY  # 4 unique opens
    assert leaders["clicked"]["subject"] == OPTIONS
    assert leaders["payments"]["subject"] == OPTIONS
    assert leaders["conversion"]["subject"] == OPTIONS  # FIRM's 100% is one email: stays in the table only

    only_firm = section(db_session, admin, reminder_stage="Final reminder")
    assert [row["subject"] for row in only_firm["rows"]] == [FIRM]
    assert only_firm["leaders"]["conversion"] is None and only_firm["leaders"]["payments"]["subject"] == FIRM


def test_filters_search_and_sort(db_session, admin, history):
    other_client = make_client(db_session, "Client B")
    portfolio = PayflowPortfolioModel(client_id=other_client.id, code="p1", name="Portfolio One")
    db_session.add(portfolio)
    db_session.flush()
    account = make_account(db_session, other_client, portfolio_id=portfolio.id)
    make_email(db_session, account, "Client B subject", sent_at=NOW - timedelta(days=10))
    make_email(db_session, make_account(db_session, other_client), "Client B other portfolio")
    db_session.commit()

    assert "Client B subject" in by_subject(section(db_session, admin))
    assert "Client B subject" not in by_subject(section(db_session, admin, date_range="7d"))
    assert set(by_subject(section(db_session, admin, client_id=history.client.id))) == {OPTIONS, FRIENDLY, FIRM}
    assert set(by_subject(section(db_session, admin, client_id=other_client.id, portfolio_ids=[portfolio.id]))) == {
        "Client B subject"
    }
    assert set(by_subject(section(db_session, admin, strategy_id=history.strategy.id))) == {OPTIONS, FRIENDLY, FIRM}
    assert set(by_subject(section(db_session, admin, search="payment OPTIONS"))) == {OPTIONS}
    custom = section(
        db_session, admin, date_range="custom",
        date_from=(NOW - timedelta(days=11)).date(), date_to=(NOW - timedelta(days=9)).date(),
    )
    assert set(by_subject(custom)) == {"Client B subject"}

    service = PayflowEmailSubjectService(db_session)
    ascending = service.get_section(admin, service.build_filters(admin), sort="open_rate", order="asc")
    rates = [row["open_rate"] for row in ascending["rows"]]
    assert rates == sorted(rates)
    with pytest.raises(ValidationAppError):
        service.build_filters(admin, date_range="custom", date_from=NOW.date(), date_to=(NOW - timedelta(days=1)).date())
    with pytest.raises(ValidationAppError):
        service.get_section(admin, service.build_filters(admin), sort="customer_name")


def test_user_only_sees_their_clients(db_session, admin, history):
    other = make_client(db_session, "Client B")
    make_email(db_session, make_account(db_session, other), "Client B subject")
    db_session.commit()
    with patch.object(PayflowEmailSubjectService, "_visible_client_ids", return_value=[history.client.id]):
        result = section(db_session, admin)
        assert "Client B subject" not in by_subject(result)
        assert [c["name"] for c in result["filter_options"]["clients"]] == ["Client A"]
        with pytest.raises(ForbiddenError):
            section(db_session, admin, client_id=other.id)
    with patch.object(PayflowEmailSubjectService, "_visible_client_ids", return_value=[]):
        assert section(db_session, admin)["rows"] == []


# --------------------------------------------------------------------------- details (FR-04)


def test_subject_details(db_session, admin, history):
    version = PayflowMessageVersionModel(
        client_id=history.client.id, content_hash="h1", channel="Email", subject_template=OPTIONS,
        subject_key=OPTIONS, body_template="Hi {{first_name}}, your options: {{payment_link}}", origin="AI Generated",
        first_used_at=NOW,
    )
    db_session.add(version)
    db_session.flush()
    emails = db_session.query(PayflowCommunicationModel).filter_by(subject_key=OPTIONS).all()
    for comm in emails[:3]:
        comm.message_version_id = version.id
    emails[3].sent_at = NOW - timedelta(days=8)
    db_session.commit()

    service = PayflowEmailSubjectService(db_session)
    details = service.get_details(admin, service.build_filters(admin, search="ignored"), f"  {OPTIONS} ")
    assert details["summary"]["completed"] == 3 and details["reminder_stages"] == ["Second reminder"]
    assert [(s["label"], s["value"]) for s in details["funnel"]] == [
        ("Sent", 4), ("Delivered", 4), ("Opened", 3), ("Clicked", 2), ("Payment Initiated", 2), ("Completed Payment", 3),
    ]
    assert details["funnel"][2]["pct_of_sent"] == 0.75
    assert len(details["trend"]) == 2 and sum(p["payments"] for p in details["trend"]) == 3
    assert details["breakdown"]["by_client"] == [
        {"id": history.client.id, "label": "Client A", "delivered": 4, "completed": 3, "conversion": 0.75}
    ]
    assert details["breakdown"]["by_workflow"][0]["label"] == history.strategy.name
    assert details["breakdown"]["by_stage"][0]["label"] == "Second reminder"
    # Two bodies under one subject: the stored version (with placeholders) and the rest.
    labels = {v["label"]: v for v in details["variations"]}
    assert labels["Body version 1"]["body"].startswith("Hi {{first_name}}") and labels["Body version 1"]["sent"] == 3
    assert labels["Body version 1"]["origin"] == "AI Generated"
    assert [row["subject"] for row in details["similar"]] == [FRIENDLY]  # same stage; FIRM is another stage

    weekly = service.get_details(admin, service.build_filters(admin), OPTIONS, period="weekly")
    assert all(datetime.fromisoformat(p["date"]).weekday() == 0 for p in weekly["trend"])


# --------------------------------------------------------------------------- API shape


def test_api_section_details_and_insight(client, admin_token, db_session, history):
    headers = {"Authorization": f"Bearer {admin_token}"}
    response = client.get("/payflow/dashboard/email-subjects", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows"][0]["subject"] == OPTIONS and body["leaders"]["conversion"]["subject"] == OPTIONS
    assert set(body["rows"][0]) >= {
        "subject", "sent", "delivered", "opens", "open_rate", "clicks", "click_rate", "payment_link_clicks",
        "initiated", "completed", "conversion", "variations",
    }
    assert body["filter_options"]["reminder_stages"][0] == "First reminder"

    response = client.get(
        "/payflow/dashboard/email-subjects/details",
        params={"subject_key": OPTIONS, "period": "weekly", "client_id": history.client.id},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["completed"] == 3
    missing = client.get("/payflow/dashboard/email-subjects/details", params={"subject_key": "nope"}, headers=headers)
    assert missing.status_code == 404
    assert client.get("/payflow/dashboard/email-subjects", params={"date_range": "year"}, headers=headers).status_code == 400

    response = client.post(
        "/payflow/dashboard/email-subjects/insight", json={"scope": "subject", "subject_key": FIRM}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["insufficient_data"] is True and response.json()["generated_by"] == "rules"
    client.cookies.clear()  # signed out
    assert client.get("/payflow/dashboard/email-subjects").status_code in (401, 403)


# --------------------------------------------------------------------------- AI insights (FR-05)


def fake_ai(reply):
    prompts = []

    def call(messages, schema, **kwargs):
        prompts.append(messages[1]["content"])
        return reply

    return call, prompts


def test_insight_is_explained_from_tracked_numbers(db_session, admin, history):
    service = PayflowEmailSubjectAiService(db_session)
    filters = service.subjects.build_filters(admin)
    call, prompts = fake_ai({"insight": "High opens, no clicks.", "observations": ["a", "b", "c", "d"]})
    with patch.object(message_writer, "call_llm", side_effect=call):
        result = service.insight(admin, filters, scope="subject", subject=FRIENDLY)
    assert result["generated_by"] == "ai" and result["insight"] == "High opens, no clicks."
    assert len(result["observations"]) == 3 and result["insufficient_data"] is False
    assert result["evidence"]["metrics"]["opens"] == 4 and result["evidence"]["comparable_subjects"]["subjects"] == 1
    facts = json.loads(prompts[0])
    assert facts["metrics"]["delivered"] == 4 and facts["comparable_subjects"][0]["subject"] == OPTIONS
    # Numbers and subject text only: nothing about a customer or the client.
    assert "Pat" not in prompts[0] and "example.com" not in prompts[0] and "Client A" not in prompts[0]


def test_insight_without_the_ai_and_with_too_little_data(db_session, admin, history):
    service = PayflowEmailSubjectAiService(db_session)
    filters = service.subjects.build_filters(admin)

    weak = service.insight(admin, filters, scope="subject", subject=FRIENDLY)  # the AI is off in tests
    assert weak["generated_by"] == "rules" and "clicks" in weak["insight"]

    call, prompts = fake_ai({"insight": "should not be asked"})
    with patch.object(message_writer, "call_llm", side_effect=call):
        thin = service.insight(admin, filters, scope="subject", subject=FIRM)
        nothing = service.insight(admin, service.subjects.build_filters(admin, reminder_stage="Final reminder"), scope="kpi", metric="conversion")
    assert thin["insufficient_data"] is True and "1 delivered" in thin["insight"]
    assert nothing["insufficient_data"] is True and nothing["subject"] is None
    assert prompts == []  # "not enough data" is decided without the AI

    kpi = service.insight(admin, filters, scope="kpi", metric="completed")
    assert kpi["subject"] == OPTIONS
    whole = service.insight(admin, filters, scope="section")
    assert whole["evidence"]["best"]["subject"] == OPTIONS and whole["evidence"]["weakest"]["subject"] == FRIENDLY
    with pytest.raises(ValidationAppError):
        service.insight(admin, filters, scope="kpi", metric="revenue")


# --------------------------------------------------------------------------- AI generation (FR-08)


def test_subject_suggestions_use_the_clients_own_history(db_session, admin, history):
    other = make_client(db_session, "Client B")
    for _ in range(4):
        make_email(db_session, make_account(db_session, other), "Client B best subject", stage="Second reminder", opens=1)
    db_session.commit()
    service = PayflowEmailSubjectAiService(db_session)
    reply = {
        "subjects": [
            {"subject": "Your options, ready when you are", "explanation": "Keeps the options angle.", "source_subject": OPTIONS},
            {"subject": OPTIONS, "explanation": "a copy", "source_subject": OPTIONS},  # not new: dropped
            {"subject": "Made up source", "explanation": "x", "source_subject": "never sent"},  # dropped
        ]
    }
    call, prompts = fake_ai(reply)
    with patch.object(message_writer, "call_llm", side_effect=call):
        result = service.suggestions(
            admin, client_id=history.client.id, strategy_id=history.strategy.id,
            reminder_stage="Second reminder", purpose="Payment Reminder",
        )
    assert result["scope"] == "Same client, workflow, stage and purpose" and result["ai_generated"] is True
    first, second, third = result["rows"]
    assert (first["subject"], first["measured"], first["metrics"]["completed"]) == (OPTIONS, True, 3)
    assert (second["subject"], second["measured"], second["source_subject"]) == ("Your options, ready when you are", False, OPTIONS)
    assert "has not been measured" in second["explanation"]
    assert third["subject"] == FRIENDLY
    assert "Client B best subject" not in prompts[0] and "Client B best subject" not in json.dumps(result)

    # A workflow that is not saved yet falls back to the client's stage and purpose.
    fallback = service.suggestions(admin, client_id=history.client.id, strategy_id=999999, reminder_stage="Second reminder")
    assert fallback["scope"].endswith("no exact workflow history") and fallback["ai_generated"] is False
    assert [row["subject"] for row in fallback["rows"]] == [OPTIONS, FRIENDLY]
    empty = service.suggestions(admin, client_id=make_client(db_session).id, reminder_stage="First reminder")
    assert empty["rows"] == [] and empty["scope"].startswith("No relevant historical data")


def test_message_writer_is_given_the_measured_history(db_session, admin, history):
    performance = PayflowEmailSubjectAiService(db_session).performance_context(history.client.id)
    assert [item["subject"] for item in performance["best"]] == [OPTIONS, FRIENDLY]  # FIRM has too few emails
    assert performance["best"][0]["conversion"] == "75.0%" and performance["best"][0]["delivered"] == 4
    assert PayflowEmailSubjectAiService(db_session).performance_context(make_client(db_session).id) is None

    client = {"name": "Client A", "client_type": "first_party", "email_performance": performance}
    workflow = {"name": "W", "summary": "", "entry_node_id": "s1", "steps": []}
    prompt = build_message_prompt(client, workflow, {"segment": {}}, [], [])[1]["content"]
    assert "Measured results of this client's past emails" in prompt and OPTIONS in prompt
    without = build_message_prompt({**client, "email_performance": None}, workflow, {"segment": {}}, [], [])[1]["content"]
    assert "Measured results" not in without


# --------------------------------------------------------------------------- low performers (FR-09/10)


@pytest.fixture()
def weak_workflow(db_session, history):
    """An Active workflow whose email uses the weak subject, at the stage where OPTIONS does well."""
    steps = [email_step("m1", subject="First hello", body="Hello"), email_step("m2", subject=FRIENDLY, body="Old body {{payment_link}}")]
    strategy = make_strategy(db_session, history.client, steps)
    for comm in db_session.query(PayflowCommunicationModel).filter_by(subject_key=FRIENDLY).all():
        comm.strategy_id, comm.step_id = strategy.id, "m2"
    db_session.commit()
    return strategy


def test_weak_email_gets_a_proposal_and_the_workflow_is_untouched(db_session, admin, weak_workflow):
    before = json.dumps(weak_workflow.steps, sort_keys=True)
    service = PayflowEmailSubjectAiService(db_session)
    result = service.analyze_workflow(admin, weak_workflow.id)  # the AI writer is off: measured fallback
    proposals = {p["step_id"]: p for p in result["proposals"]}

    assert proposals["m1"]["recommendation"] == "Insufficient data" and proposals["m1"]["proposed"] is None
    weak = proposals["m2"]
    assert weak["recommendation"] == "Replace" and weak["status"] == "Pending"
    assert weak["existing"] == {"subject": FRIENDLY, "body": "Old body {{payment_link}}", "origin": "Human Created"}
    assert weak["proposed"]["subject"] == OPTIONS and weak["proposed"]["origin"] == "Measured history"
    assert weak["evidence"]["step"]["conversion"] == 0.0 and weak["evidence"]["comparable_templates"]["conversion"] == 0.75
    assert weak["alternative_subjects"][0]["subject"] == OPTIONS and "less than half" in weak["explanation"]

    db_session.refresh(weak_workflow)
    assert json.dumps(weak_workflow.steps, sort_keys=True) == before and weak_workflow.status == "Active"

    again = service.analyze_workflow(admin, weak_workflow.id)  # a new analysis replaces the open proposals
    listed = service.list_proposals(admin, strategy_id=weak_workflow.id)
    assert {p["id"] for p in listed["items"]} == {p["id"] for p in again["proposals"]}
    with pytest.raises(ValidationAppError):
        service.decide(admin, proposals["m2"]["id"], action="accept")  # superseded


def test_accepting_a_proposal_goes_through_the_workflow_review(db_session, admin, weak_workflow):
    service = PayflowEmailSubjectAiService(db_session)
    written = {"subject": "Options that fit your budget", "body": "New body {{payment_link}}", "tone": "Clear"}
    with patch.object(PayflowEmailSubjectAiService, "_rewrite", return_value=written):
        proposals = {p["step_id"]: p for p in service.analyze_workflow(admin, weak_workflow.id)["proposals"]}
    assert proposals["m2"]["proposed"]["body"] == "New body {{payment_link}}"

    with pytest.raises(ValidationAppError):
        service.decide(admin, proposals["m1"]["id"], action="accept")  # nothing proposed for it
    done = service.decide(admin, proposals["m2"]["id"], action="accept", note="looks right")
    assert done["status"] == "Accepted" and done["final"]["subject"] == "Options that fit your budget"
    assert done["decided_by"] == admin.full_name

    workflow = PayflowStrategyService(db_session).get_strategy(admin, weak_workflow.id)
    config = next(s for s in workflow["steps"] if s["id"] == "m2")["config"]
    assert (config["subject"], config["body"]) == ("Options that fit your budget", "New body {{payment_link}}")
    assert config["content_origin"] == "AI Generated"
    # The Active workflow was archived and must be reviewed again before it runs with the new text.
    assert workflow["status"] == "Under Review" and workflow["version"] == 2
    assert "AI proposal" in workflow["versions"][0]["reviewer_note"]
    with pytest.raises(ValidationAppError):
        service.decide(admin, proposals["m2"]["id"], action="reject")  # already decided


def test_editing_or_rejecting_a_proposal(db_session, admin, weak_workflow):
    service = PayflowEmailSubjectAiService(db_session)
    proposals = {p["step_id"]: p for p in service.analyze_workflow(admin, weak_workflow.id)["proposals"]}
    edited = service.decide(admin, proposals["m2"]["id"], action="edit", subject="My own subject")
    assert edited["status"] == "Edited" and edited["final"]["subject"] == "My own subject"
    config = next(s for s in db_session.get(PayflowStrategyModel, weak_workflow.id).steps if s["id"] == "m2")["config"]
    assert config["subject"] == "My own subject" and config["content_origin"] == "AI Generated, Human Edited"

    other = make_strategy(db_session, weak_workflow.client, [email_step("m2", subject=FRIENDLY, body="Old body")])
    for comm in db_session.query(PayflowCommunicationModel).filter_by(subject_key=FRIENDLY).all():
        comm.strategy_id = other.id
    db_session.commit()
    proposal = service.analyze_workflow(admin, other.id)["proposals"][0]
    rejected = service.decide(admin, proposal["id"], action="reject", note="keep it")
    assert rejected["status"] == "Rejected" and rejected["final"] is None
    assert db_session.get(PayflowStrategyModel, other.id).steps[0]["config"]["subject"] == FRIENDLY
    assert db_session.query(PayflowMessageProposalModel).filter_by(status="Rejected").count() == 1


def test_message_history_keeps_old_versions(db_session, admin, history):
    old = PayflowMessageVersionModel(
        client_id=history.client.id, content_hash="old", channel="Email", subject_template=OPTIONS, subject_key=OPTIONS,
        body_template="Old body", origin="Human Created", strategy_id=history.strategy.id, step_id="s1",
        first_used_at=NOW - timedelta(days=5),
    )
    new = PayflowMessageVersionModel(
        client_id=history.client.id, content_hash="new", channel="Email", subject_template=FRIENDLY, subject_key=FRIENDLY,
        body_template="New body", origin="AI Generated", strategy_id=history.strategy.id, step_id="s1",
        first_used_at=NOW - timedelta(days=1),
    )
    db_session.add_all([old, new])
    db_session.flush()
    for comm in db_session.query(PayflowCommunicationModel).filter_by(subject_key=OPTIONS).all():
        comm.message_version_id = old.id
    for comm in db_session.query(PayflowCommunicationModel).filter_by(subject_key=FRIENDLY).all():
        comm.message_version_id = new.id
    db_session.commit()

    items = PayflowEmailSubjectAiService(db_session).message_history(admin, history.strategy.id)["items"]
    assert [(i["subject"], i["origin"], i["completed"]) for i in items] == [
        (FRIENDLY, "AI Generated", 0),
        (OPTIONS, "Human Created", 3),
    ]
    assert [i["is_current_subject"] for i in items] == [True, False]  # the workflow now sends FRIENDLY


# --------------------------------------------------------------------------- the rest of the story


def test_purpose_filter(db_session, admin, history):
    result = section(db_session, admin, purpose="Firm Reminder")
    assert [row["subject"] for row in result["rows"]] == [FIRM]
    assert "Firm Reminder" in result["filter_options"]["purposes"]
    assert section(db_session, admin, purpose="Payment Plan Reminder")["rows"] == []


def test_automatic_opens_are_not_counted_as_opens(db_session, admin):
    client = make_client(db_session)
    preloaded = make_email(db_session, make_account(db_session, client), FRIENDLY)
    read = make_email(db_session, make_account(db_session, client), FRIENDLY)
    events = PayflowCommunicationService(db_session)
    for number in range(2):  # a mail app pre-loading the email, twice
        events.apply_provider_event(
            event_type="open", communication_id=preloaded.id, provider_event_id=f"machine-{number}",
            payload={"sg_machine_open": True},
        )
    events.apply_provider_event(event_type="open", communication_id=read.id, provider_event_id="human-1", payload={})
    events.apply_provider_event(
        event_type="open", communication_id=read.id, provider_event_id="machine-9", payload={"sg_machine_open": True}
    )
    db_session.refresh(preloaded)
    assert (preloaded.opens_count, preloaded.machine_opens_count) == (2, 2)  # the old counter is unchanged

    row = by_subject(section(db_session, admin))[FRIENDLY]
    assert (row["delivered"], row["opens"], row["open_rate"]) == (2, 1, 0.5)  # only the person's open


def test_section_needs_the_analytics_permission_for_the_client(db_session, admin, history):
    other = make_client(db_session, "Client B")
    make_email(db_session, make_account(db_session, other), "Client B subject")
    db_session.commit()
    service = PayflowEmailSubjectService(db_session)
    held = {(history.client.id, "view_analytics"), (other.id, "view_workflows")}
    with (
        patch.object(service.access, "is_operations_admin", return_value=False),
        patch.object(service.access, "build_context", return_value={"client_ids": [history.client.id, other.id]}),
        patch.object(service.access, "can", side_effect=lambda user, permission, client_id=None: (client_id, permission) in held),
    ):
        # Assigned to both clients, but analytics only for the first.
        assert service._visible_client_ids(admin) == [history.client.id]
        assert service._visible_client_ids(admin, "view_workflows") == [other.id]
        rows = service.get_section(admin, service.build_filters(admin))["rows"]
        assert "Client B subject" not in {row["subject"] for row in rows}
        with pytest.raises(ForbiddenError):
            service.build_filters(admin, client_id=other.id)


def test_insight_is_reused_while_the_numbers_are_the_same(db_session, admin, history):
    service = PayflowEmailSubjectAiService(db_session)
    filters = service.subjects.build_filters(admin)
    call, prompts = fake_ai({"insight": "Explained once."})
    with patch.object(message_writer, "call_llm", side_effect=call):
        first = service.insight(admin, filters, scope="subject", subject=FRIENDLY)
        second = service.insight(admin, filters, scope="subject", subject=FRIENDLY)
        assert (first["cached"], second["cached"], len(prompts)) == (False, True, 1)
        assert second["insight"] == "Explained once." and second["generated_by"] == "ai"

        make_email(db_session, history.accounts[5], FRIENDLY, strategy=history.strategy, stage="Second reminder", opens=1)
        db_session.commit()
        third = service.insight(admin, filters, scope="subject", subject=FRIENDLY)  # new numbers: asked again
        assert (third["cached"], len(prompts)) == (False, 2)


def stored_body(db_session, history, subject, body, origin="AI Generated"):
    version = PayflowMessageVersionModel(
        client_id=history.client.id, content_hash=f"h{_n()}", channel="Email", subject_template=subject,
        subject_key=subject, body_template=body, origin=origin, first_used_at=NOW,
    )
    db_session.add(version)
    db_session.flush()
    for comm in db_session.query(PayflowCommunicationModel).filter_by(subject_key=subject).all():
        comm.message_version_id = version.id
    db_session.commit()
    return version


def test_suggestions_and_drafts_come_with_a_body(db_session, admin, history):
    stored_body(db_session, history, OPTIONS, "Hi {{first_name}}, your options: {{payment_link}}")
    service = PayflowEmailSubjectAiService(db_session)
    rows = service.suggestions(
        admin, client_id=history.client.id, strategy_id=history.strategy.id, reminder_stage="Second reminder"
    )["rows"]
    assert rows[0]["body"].startswith("Hi {{first_name}}") and rows[0]["body_origin"] == "AI Generated"
    assert rows[1]["body"] is None  # no stored text for that subject

    steps = [email_step("m1", subject="Old subject", body="Old body"), email_step("m2", subject="Second", body="B")]
    workflow = make_strategy(db_session, history.client, steps)
    db_session.commit()
    before = json.dumps(workflow.steps, sort_keys=True)
    written = {"subject": "Options that fit you", "body": "A new body {{payment_link}}"}
    with patch.object(PayflowEmailSubjectAiService, "_rewrite", return_value=written) as rewrite:
        draft = service.draft_email(admin, workflow.id, "m2", remarks="shorter please")
    assert (draft["subject"], draft["body"], draft["generated_by"]) == ("Options that fit you", "A new body {{payment_link}}", "ai")
    assert draft["evidence"]["examples"][0]["subject"] == OPTIONS and draft["saved"] is False
    assert OPTIONS in rewrite.call_args.args[2] and "shorter please" in rewrite.call_args.args[2]

    fallback = service.draft_email(admin, workflow.id, "m2")  # the AI writer is off in tests
    assert (fallback["subject"], fallback["generated_by"]) == (OPTIONS, "history")
    assert fallback["body"].startswith("Hi {{first_name}}")
    db_session.refresh(workflow)
    assert json.dumps(workflow.steps, sort_keys=True) == before  # a draft never touches the workflow

    empty = make_strategy(db_session, make_client(db_session))
    db_session.commit()
    with pytest.raises(ServiceUnavailableError):
        service.draft_email(admin, empty.id, "s1")
    with pytest.raises(ValidationAppError):
        service.draft_email(admin, workflow.id, "nope")


def test_edited_proposal_text_must_pass_the_message_checks(db_session, admin, weak_workflow):
    service = PayflowEmailSubjectAiService(db_session)
    proposal = {p["step_id"]: p for p in service.analyze_workflow(admin, weak_workflow.id)["proposals"]}["m2"]
    assert service._wording_problems(weak_workflow, weak_workflow.steps[1], FRIENDLY, "Old body {{payment_link}}")
    with pytest.raises(ValidationAppError) as error:
        service.decide(
            admin, proposal["id"], action="edit",
            body="Old body {{payment_link}}. Pay now or we will take legal action. Call 416-555-0199.",
        )
    assert "does not pass the message checks" in str(error.value.message)
    db_session.refresh(weak_workflow)
    assert weak_workflow.status == "Active" and weak_workflow.steps[1]["config"]["body"] == "Old body {{payment_link}}"
    # The same proposal is still open and a clean edit goes through.
    assert service.decide(admin, proposal["id"], action="edit", subject="A calmer subject")["status"] == "Edited"


def test_automatic_check_raises_weak_emails_once(db_session, admin, weak_workflow):
    before = json.dumps(weak_workflow.steps, sort_keys=True)
    service = PayflowEmailSubjectAiService(db_session)

    found = service.low_performers(admin)["items"]
    assert [(i["strategy_id"], i["step_id"], i["recommendation"]) for i in found] == [(weak_workflow.id, "m2", "Replace")]
    assert found[0]["pending_proposal_id"] is None and found[0]["metrics"]["conversion"] == 0.0
    everything = service.low_performers(admin, client_id=weak_workflow.client_id, include_all=True)["items"]
    assert {i["step_id"] for i in everything if i["strategy_id"] == weak_workflow.id} == {"m1", "m2"}

    assert service.scan_low_performers() == {"workflows": 1, "proposals": 1}
    proposals = service.list_proposals(admin, strategy_id=weak_workflow.id)["items"]
    assert [(p["step_id"], p["recommendation"], p["created_by"]) for p in proposals] == [
        ("m2", "Replace", "PayFlow AI (automatic check)")
    ]
    notices = db_session.query(PayflowNotificationModel).filter_by(notification_type="email_proposals_ready").all()
    assert notices  # one per supervisor / operations admin of the client
    notice = notices[0]
    assert weak_workflow.name in notice.title and notice.link == f"/payflow/workflows/{weak_workflow.id}"
    assert service.low_performers(admin)["items"][0]["pending_proposal_id"] == proposals[0]["id"]

    # The same finding for the same text is not raised again, even after it was rejected.
    service.decide(admin, proposals[0]["id"], action="reject")
    assert service.scan_low_performers() == {"workflows": 0, "proposals": 0}
    db_session.refresh(weak_workflow)
    assert json.dumps(weak_workflow.steps, sort_keys=True) == before and weak_workflow.status == "Active"


def test_new_endpoints(client, admin_token, db_session, weak_workflow):
    headers = {"Authorization": f"Bearer {admin_token}"}
    low = client.get("/payflow/email-subjects/low-performers", headers=headers)
    assert low.status_code == 200, low.text
    assert low.json()["items"][0]["recommendation"] == "Replace"
    scan = client.post("/payflow/email-subjects/low-performers/scan", headers=headers)
    assert scan.status_code == 200 and scan.json()["proposals"] == 1
    listed = client.get("/payflow/email-proposals", params={"status": "Pending"}, headers=headers).json()
    assert listed["total"] == 1
    draft = client.post(
        f"/payflow/workflows/{weak_workflow.id}/email-suggestion", json={"step_id": "m2"}, headers=headers
    )
    assert draft.status_code == 502  # the AI is off and nothing measured has a stored body
    suggestions = client.get(
        "/payflow/email-subjects/suggestions",
        params={"client_id": weak_workflow.client_id, "reminder_stage": "Second reminder"},
        headers=headers,
    )
    assert suggestions.status_code == 200 and "body" in suggestions.json()["rows"][0]
    history_rows = client.get(f"/payflow/workflows/{weak_workflow.id}/message-history", headers=headers)
    assert history_rows.status_code == 200
    filtered = client.get("/payflow/dashboard/email-subjects", params={"purpose": "Firm Reminder"}, headers=headers)
    assert [row["subject"] for row in filtered.json()["rows"]] == [FIRM]


# --------------------------------------------------------------------------- closing the last gaps


def test_ai_is_given_one_clients_data_at_a_time(db_session, admin, history):
    other = make_client(db_session, "Client B")
    for index in range(4):
        make_email(db_session, make_account(db_session, other), "Client B subject", stage="Second reminder", opens=1)
        make_email(db_session, make_account(db_session, other), FRIENDLY, stage="Second reminder", opens=index % 2)
    db_session.commit()
    service = PayflowEmailSubjectAiService(db_session)
    everyone = service.subjects.build_filters(admin)  # all clients
    call, prompts = fake_ai({"insight": "Explained."})
    with patch.object(message_writer, "call_llm", side_effect=call):
        # OPTIONS is one client's subject: the AI is asked, with that client's subjects only.
        own = service.insight(admin, everyone, scope="subject", subject=OPTIONS)
        assert own["generated_by"] == "ai" and own["evidence"]["ai_context"] == "one_client"
        assert "Client B subject" not in prompts[0]
        # FRIENDLY is sent for both clients, and the whole section covers both: no AI.
        shared = service.insight(admin, everyone, scope="subject", subject=FRIENDLY)
        whole = service.insight(admin, everyone, scope="section")
        assert len(prompts) == 1
    for result in (shared, whole):
        assert result["generated_by"] == "rules" and "more than one client" in result["insight"]
        assert result["evidence"]["ai_context"] == "not_used_several_clients"
    with patch.object(message_writer, "call_llm", side_effect=call):
        one = service.insight(admin, service.subjects.build_filters(admin, client_id=history.client.id), scope="section")
    assert one["generated_by"] == "ai"


def test_trend_by_client_workflow_and_stage(db_session, admin, history):
    old = db_session.query(PayflowCommunicationModel).filter_by(subject_key=FRIENDLY).first()
    old.sent_at = NOW - timedelta(days=9)
    db_session.commit()
    service = PayflowEmailSubjectService(db_session)
    filters = service.build_filters(admin)

    overall = service.get_trend(admin, filters)
    assert [s["label"] for s in overall["series"]] == ["All emails in this selection"]
    assert len(overall["dates"]) == 2 and sum(p["payments"] for p in overall["series"][0]["points"]) == 4
    assert overall["series"][0]["totals"]["completed"] == 4

    by_stage = service.get_trend(admin, filters, group_by="stage", period="weekly")
    assert [s["label"] for s in by_stage["series"]] == ["Second reminder", "Final reminder"]  # most payments first
    assert all(len(s["points"]) == len(by_stage["dates"]) for s in by_stage["series"])  # one shared time axis
    assert service.get_trend(admin, filters, group_by="client")["series"][0]["label"] == "Client A"
    assert service.get_trend(admin, filters, group_by="workflow")["series"][0]["label"] == history.strategy.name
    with pytest.raises(ValidationAppError):
        service.get_trend(admin, filters, group_by="customer")


def test_draft_for_a_workflow_that_is_not_saved_yet(db_session, admin, history):
    stored_body(db_session, history, OPTIONS, "Hi {{first_name}}, your options: {{payment_link}}")
    service = PayflowEmailSubjectAiService(db_session)
    written = {"draft": {"subject": "Options for you", "body": "A new body {{payment_link}} {{brand_name}}", "tone": "Clear"}}
    with patch.object(message_writer, "write_messages", return_value=written) as writer:
        draft = service.draft_for_new_step(
            admin, client_id=history.client.id, purpose="Payment Reminder", reminder_stage="Second reminder",
            remarks="keep it short",
        )
    assert (draft["subject"], draft["generated_by"], draft["strategy_id"], draft["saved"]) == (
        "Options for you", "ai", None, False,
    )
    workflow = writer.call_args.args[1]
    assert [s["id"] for s in workflow["steps"]] == ["earlier1", "draft"]  # a second reminder follows an earlier email
    assert OPTIONS in writer.call_args.kwargs["remarks"] and "keep it short" in writer.call_args.kwargs["remarks"]

    fallback = service.draft_for_new_step(admin, client_id=history.client.id, reminder_stage="Second reminder")
    assert (fallback["subject"], fallback["generated_by"]) == (OPTIONS, "history")  # the AI is off in tests
    with pytest.raises(ServiceUnavailableError):
        service.draft_for_new_step(admin, client_id=make_client(db_session).id)
    with pytest.raises(ValidationAppError):
        service.draft_for_new_step(admin, client_id=history.client.id, reminder_stage="Third reminder")


def test_last_gap_endpoints(client, admin_token, db_session, weak_workflow):
    headers = {"Authorization": f"Bearer {admin_token}"}
    trend = client.get("/payflow/dashboard/email-subjects/trend", params={"group_by": "stage"}, headers=headers)
    assert trend.status_code == 200, trend.text
    assert trend.json()["series"][0]["points"][0].keys() >= {"date", "delivered", "opens", "clicks", "payments"}
    draft = client.post(
        "/payflow/email-subjects/draft",
        json={"client_id": weak_workflow.client_id, "reminder_stage": "Second reminder"},
        headers=headers,
    )
    assert draft.status_code == 502  # the AI is off and no measured email has a stored body
    client.post(f"/payflow/workflows/{weak_workflow.id}/email-optimization", headers=headers)
    listed = client.get("/payflow/email-proposals", headers=headers).json()
    assert listed["pending"] == listed["total"] == 2
    # Opening the lists never starts the automatic check during tests.
    assert email_subject_ai.scan_in_background_if_due() is False
