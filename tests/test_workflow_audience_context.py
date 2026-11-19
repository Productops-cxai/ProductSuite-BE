"""GET /payflow/workflows/{id}/audiences - who gets the main text and each audience version, from today's accounts
(the Personalization Context of each text). Totals only; no AI call and nothing is saved."""

from unittest.mock import patch

from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion import message_brief, message_writer
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from tests.test_workflow_audience_messages import _post_update, add_seniors, write_with
from tests.test_workflow_message_api import get
from tests.test_workflow_regenerate import admin, ai_workflow  # noqa: F401 (fixtures)
from tests.test_workflow_suggestion_api import fake_llm, portfolio  # noqa: F401 (fixtures)


def audiences(client, token, workflow_id):
    return client.get(f"/payflow/workflows/{workflow_id}/audiences", headers={"Authorization": f"Bearer {token}"})


def _no_ai(*_):
    raise AssertionError("the AI must not be called")


def test_each_text_has_its_own_context(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    # 29 accounts without a date of birth, email or mobile + 25 aged 70 with both → a 65+ version on every message.
    add_seniors(db_session, portfolio, email="senior@example.com", phone_mobile="+14165550100")
    saved = write_with(db_session, admin, ai_workflow["id"])

    response = audiences(client, admin_token, saved["id"])
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["workflow_id"] == saved["id"] and body["accounts"] == 54
    assert body["situation"] == (saved.get("segment") or {}).get("situation")
    main, seniors = body["texts"]

    assert (main["key"], main["label"], main["accounts"]) == ("main", "Main text", 29)
    assert main["profile"]["age"] == {"Unknown": 100}
    assert (main["profile"]["with_email_pct"], main["profile"]["with_mobile_pct"]) == (0, 0)
    assert main["profile"]["contact_history"] == {"Never contacted": 100}

    assert (seniors["key"], seniors["label"], seniors["accounts"]) == ("age_65_plus", "Age 65 and over", 25)
    assert seniors["profile"]["age"] == {"65 and over": 100} and seniors["profile"]["avg_balance"] == 300.0
    assert (seniors["profile"]["with_email_pct"], seniors["profile"]["with_mobile_pct"]) == (100, 100)
    for key in ("regions", "countries", "languages", "balance_bands", "partly_paid_pct", "avg_days_past_due"):
        assert key in seniors["profile"], key


def test_without_versions_everyone_gets_the_main_text(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio)  # big enough for a version, but none was written
    body = audiences(client, admin_token, ai_workflow["id"]).json()
    assert [(text["key"], text["accounts"]) for text in body["texts"]] == [("main", 54)]


def test_a_removed_version_sends_its_customers_back_to_the_main_text(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio)
    saved = write_with(db_session, admin, ai_workflow["id"])
    steps = [{**step, "config": {**step["config"], "versions": {}}} if step["kind"] == "Communication" else step
             for step in saved["steps"]]
    assert _post_update(client, admin_token, saved["id"], steps).status_code == 200

    body = audiences(client, admin_token, saved["id"]).json()
    assert [(text["key"], text["accounts"]) for text in body["texts"]] == [("main", 54)]  # as the worker sends


def test_it_calls_no_ai_and_changes_nothing(client, admin_token, db_session, admin, portfolio, ai_workflow):  # noqa: F811,E501
    add_seniors(db_session, portfolio)
    saved = write_with(db_session, admin, ai_workflow["id"])
    before = get(client, admin_token, saved["id"])
    with patch.object(message_writer, "call_llm", side_effect=_no_ai), \
            patch.object(message_brief, "call_llm", side_effect=_no_ai), \
            patch.object(suggestion_service, "call_llm", side_effect=_no_ai):
        assert audiences(client, admin_token, saved["id"]).status_code == 200
    assert get(client, admin_token, saved["id"]) == before


def test_a_payment_plan_workflow_says_so(client, admin_token, db_session, admin, portfolio):  # noqa: F811
    call, _ = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        result = WorkflowSuggestionService(db_session).suggest_workflows(admin, portfolio["client_id"],
                                                                          portfolio["mixed_id"])
    plan = next(w for w in result["workflows"] if (w.get("segment") or {}).get("situation") == "Payment Plan")
    body = audiences(client, admin_token, plan["id"]).json()
    assert body["situation"] == "Payment Plan" and body["accounts"] == 25


def test_hand_made_workflows_and_unknown_ones(client, admin_token, db_session, admin, portfolio):  # noqa: F811
    workflow = PayflowStrategyService(db_session).create_strategy(admin, {
        "name": "Manual workflow", "client_id": portfolio["client_id"],
        "portfolio_id": portfolio["portfolio_id"], "steps": load_examples()[0]["steps"],
    })
    body = audiences(client, admin_token, workflow["id"]).json()
    assert [(text["key"], text["accounts"]) for text in body["texts"]] == [("main", 29)]  # the portfolio's eligible
    assert audiences(client, admin_token, 99999).status_code == 404
