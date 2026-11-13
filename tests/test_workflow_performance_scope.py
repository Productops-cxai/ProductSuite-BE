"""The history the AI writers read (email_subject_ai.performance_context): the closest one with enough measured
subjects - this workflow, its portfolio, the whole client - recent months first, plus each audience's own results."""

from datetime import timedelta
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.infrastructure.database.models import PayflowPortfolioModel
from app.modules.payflow.services import email_subject_ai
from app.modules.payflow.services.email_subject_ai import PayflowEmailSubjectAiService
from app.modules.payflow.services.workflow_suggestion import message_brief, message_writer
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.message_brief import _results_lines
from app.modules.payflow.services.workflow_suggestion.message_prompt import _performance_lines
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService, _performance_note
from tests.test_email_subject_performance import NOW, make_account, make_client, make_email, make_payment, make_strategy
from tests.test_workflow_audience_messages import add_seniors, fake_planner
from tests.test_workflow_message_api import fake_message_ai
from tests.test_workflow_regenerate import REMARKS, admin, ai_workflow  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import fake_llm, portfolio  # noqa: F401 (fixtures)

OLD = NOW - timedelta(days=300)  # before the recent months


@pytest.fixture(autouse=True)
def small_minimum(monkeypatch):
    """Three delivered emails are enough to measure a subject in these tests (100 in production)."""
    monkeypatch.setattr(settings, "EMAIL_SUBJECT_MIN_DELIVERIES", 3)


def subjects(db, account, count, *, strategy=None, prefix="Subject", sent_at=None, audience=None, paid=1):
    """`count` measured subjects (3 delivered emails each, `paid` of them paid)."""
    for n in range(count):
        for i in range(3):
            email = make_email(db, account, f"{prefix} {n}", strategy=strategy, sent_at=sent_at)
            email.message_audience = audience
            if i < paid:
                make_payment(db, account, email)
    db.commit()


@pytest.fixture()
def book(db_session):
    """A client with two portfolios; workflow W1 in portfolio P1, W2 in P1 too."""
    client = make_client(db_session)
    p1, p2 = (PayflowPortfolioModel(client_id=client.id, name=f"P{i}", code=f"p{i}-{client.id}") for i in (1, 2))
    db_session.add_all([p1, p2])
    db_session.flush()
    return {"client": client, "p1": p1, "p2": p2, "w1": make_strategy(db_session, client),
            "w2": make_strategy(db_session, client), "in_p1": make_account(db_session, client, portfolio_id=p1.id),
            "in_p2": make_account(db_session, client, portfolio_id=p2.id), "outside": make_account(db_session, client)}


def context(db, book, **kwargs):
    return PayflowEmailSubjectAiService(db).performance_context(
        book["client"].id, portfolio_id=book["p1"].id, strategy_id=book["w1"].id, **kwargs
    )


def test_the_workflows_own_results_come_first(db_session, book):
    subjects(db_session, book["in_p1"], 3, strategy=book["w1"], prefix="W1")
    subjects(db_session, book["in_p2"], 4, prefix="Elsewhere")
    found = context(db_session, book)
    assert (found["scope"], found["period"]) == ("this workflow", "last 6 months")
    assert {item["subject"] for item in found["best"] + found["weak"]} <= {"W1 0", "W1 1", "W1 2"}


def test_too_little_for_the_workflow_its_portfolio_then_the_client(db_session, book):
    subjects(db_session, book["in_p1"], 1, strategy=book["w1"], prefix="W1")
    subjects(db_session, book["in_p1"], 2, strategy=book["w2"], prefix="W2")
    assert context(db_session, book)["scope"] == "this portfolio"  # 1 + 2 subjects in P1

    other = make_client(db_session)
    subjects(db_session, make_account(db_session, other), 3, prefix="Other")
    found = PayflowEmailSubjectAiService(db_session).performance_context(other.id, portfolio_id=book["p1"].id)
    assert found["scope"] == "this client"  # nothing in that portfolio for this client


def test_recent_months_first_then_all_time(db_session, book):
    subjects(db_session, book["in_p1"], 3, strategy=book["w1"], prefix="Old", sent_at=OLD)
    assert (context(db_session, book)["scope"], context(db_session, book)["period"]) == ("this workflow", "all time")
    subjects(db_session, book["in_p1"], 3, strategy=book["w1"], prefix="New")
    found = context(db_session, book)
    assert found["period"] == "last 6 months"
    assert all(item["subject"].startswith("New") for item in found["best"] + found["weak"])


def test_without_enough_anywhere_the_closest_with_any(db_session, book):
    subjects(db_session, book["in_p1"], 1, strategy=book["w1"], prefix="Only")
    found = context(db_session, book)
    assert (found["scope"], [item["subject"] for item in found["best"]]) == ("this workflow", ["Only 0"])
    empty = make_client(db_session)
    assert PayflowEmailSubjectAiService(db_session).performance_context(empty.id) is None


def test_each_audience_gets_its_own_results(db_session, book):
    subjects(db_session, book["in_p1"], 3, strategy=book["w1"], prefix="Main", audience="main")
    subjects(db_session, book["in_p1"], 2, strategy=book["w1"], prefix="Senior", audience="age_65_plus", paid=3)
    found = context(db_session, book, audiences=["age_65_plus", "age_18_34"])
    assert [item["subject"] for item in found["by_audience"]["age_65_plus"]] == ["Senior 0", "Senior 1"]
    assert found["by_audience"]["age_65_plus"][0]["conversion"] == "100.0%"
    assert "age_18_34" not in found["by_audience"]  # no version sent to them yet


def test_the_prompts_and_the_note_say_where_the_results_come_from():
    entry = {"subject": "A reminder from {{brand_name}}", "reminder_stage": "First reminder", "purpose":
             "Payment Reminder", "delivered": 300, "open_rate": "40.0%", "click_rate": "9.0%",
             "completed_payments": 12, "conversion": "4.0%"}
    performance = {"best": [entry], "weak": [], "min_deliveries": 100, "scope": "this portfolio",
                   "period": "last 6 months", "by_audience": {"age_65_plus": [{**entry, "conversion": "6.0%"}]}}
    planner = _results_lines(performance)
    assert planner.startswith("Measured results (this client's own past emails - this portfolio, last 6 months; "
                              "each subject with at least 100 delivered):")
    assert "Results by audience (the emails each audience's own version went to):\n- age_65_plus (Age 65 and over):" \
           in planner and '"conversion": "6.0%"' in planner
    assert _performance_lines(performance).startswith(
        "Measured results of this client's past emails (this portfolio, last 6 months; their own history;"
    )
    note = _performance_note({"email_performance": performance})[0]["value"]
    assert note.startswith("Emails written using this client's measured results (this portfolio, last 6 months): "
                           "1 strong and 0 weak subjects; own results for Age 65 and over.")


def test_suggest_regenerate_and_write_messages_ask_for_the_closest_history(db_session, admin, portfolio):  # noqa: F811
    add_seniors(db_session, portfolio)
    design, _ = fake_llm()
    writer, _ = fake_message_ai()
    planner, _ = fake_planner()
    with patch.object(email_subject_ai.PayflowEmailSubjectAiService, "performance_context", return_value=None) as spy, \
            patch.object(suggestion_service, "call_llm", side_effect=design), \
            patch.object(message_writer, "call_llm", side_effect=writer), \
            patch.object(message_brief, "call_llm", side_effect=planner):
        service = WorkflowSuggestionService(db_session)
        suggested = service.suggest_workflows(admin, portfolio["client_id"], portfolio["portfolio_id"])["workflows"][0]
        on_suggest = spy.call_args.kwargs
        service.write_workflow_messages(admin, suggested["id"])
        on_write = spy.call_args.kwargs
        service.regenerate_workflow(admin, suggested["id"], REMARKS)
        on_regenerate = spy.call_args.kwargs

    # A new workflow has no history of its own yet: its portfolio's.
    assert on_suggest == {"portfolio_id": portfolio["portfolio_id"], "strategy_id": None, "audiences": ["age_65_plus"]}
    for kwargs in (on_write, on_regenerate):  # a saved workflow: its own first
        assert kwargs == {"portfolio_id": portfolio["portfolio_id"], "strategy_id": suggested["id"],
                          "audiences": ["age_65_plus"]}
