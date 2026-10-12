import copy
from unittest.mock import patch

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.core.security import hash_password
from app.infrastructure.database.models import (
    OrganizationModel,
    PayflowAccountModel,
    PayflowClientModel,
    PayflowPortfolioModel,
    PayflowRoleModel,
    PayflowStrategyModel,
    PayflowUserMembershipModel,
    ProductModel,
    RoleModel,
    UserModel,
    UserProductModel,
)
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.prompt import load_examples
from app.shared.enums import PayflowRoleCode, PlatformRole, UserStatus


def _account(client_id, portfolio_id, i, **values):
    return PayflowAccountModel(
        client_id=client_id,
        portfolio_id=portfolio_id,
        customer_name=f"Customer {i}",
        account_reference=f"ACC-{portfolio_id}-{i}",
        case_reference=f"CASE-{portfolio_id}-{i}-01",
        **values,
    )


@pytest.fixture()
def portfolio(db_session):
    client = PayflowClientModel(
        code="northstar", name="Northstar Utilities", channel_email=True, channel_sms=True
    )
    other = PayflowClientModel(code="other", name="Other Client")
    db_session.add_all([client, other])
    db_session.flush()
    port = PayflowPortfolioModel(client_id=client.id, name="Residential", code="res")
    mixed = PayflowPortfolioModel(client_id=client.id, name="Mixed", code="mixed")
    closed = PayflowPortfolioModel(client_id=client.id, name="Closed", code="closed")
    db_session.add_all([port, mixed, closed])
    db_session.flush()
    for i in range(30):
        status = "Resolved" if i == 0 else "Active"
        db_session.add(
            _account(client.id, port.id, i, outstanding_balance=300.0, days_past_due=10, collection_status=status)
        )
    # 4 groups of 25 accounts → 4 segments.
    groups = [("Active", 10), ("Active", 40), ("Promise to Pay", 10), ("Payment Plan", 10)]
    for g, (status, dpd) in enumerate(groups):
        for i in range(25):
            db_session.add(
                _account(
                    client.id, mixed.id, g * 100 + i,
                    outstanding_balance=300.0, days_past_due=dpd, collection_status=status,
                )
            )
    db_session.add(_account(client.id, closed.id, 0, outstanding_balance=300.0, collection_status="Resolved"))
    db_session.commit()
    return {
        "client_id": client.id,
        "other_id": other.id,
        "portfolio_id": port.id,
        "mixed_id": mixed.id,
        "closed_id": closed.id,
    }


@pytest.fixture()
def supervisor_token(client, db_session):
    """PayFlow supervisor not assigned to any client."""
    org = db_session.query(OrganizationModel).first()
    role = db_session.query(RoleModel).filter(RoleModel.code == PlatformRole.PLATFORM_USER.value).first()
    user = UserModel(
        full_name="Sam Supervisor",
        email="sam@example.com",
        password_hash=hash_password("User@12345"),
        organization_id=org.id,
        role_id=role.id,
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    db_session.flush()
    payflow = db_session.query(ProductModel).filter(ProductModel.code == "PAYFLOW").first()
    supervisor = (
        db_session.query(PayflowRoleModel)
        .filter(PayflowRoleModel.code == PayflowRoleCode.SUPERVISOR.value)
        .first()
    )
    db_session.add_all(
        [
            UserProductModel(user_id=user.id, product_id=payflow.id),
            PayflowUserMembershipModel(user_id=user.id, payflow_role_id=supervisor.id),
        ]
    )
    db_session.commit()
    response = client.post("/auth/login", json={"email": "sam@example.com", "password": "User@12345"})
    return response.json()["access_token"]


def _url(client_id, portfolio_id):
    return f"/payflow/clients/{client_id}/portfolios/{portfolio_id}/segments"


def test_preview_segments(client, admin_token, portfolio):
    response = client.get(
        _url(portfolio["client_id"], portfolio["portfolio_id"]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["portfolio_name"] == "Residential"
    assert body["total_accounts"] == 30
    assert body["eligible_accounts"] == 29
    assert body["excluded"]["resolved"] == 1
    assert len(body["segments"]) == 1
    segment = body["segments"][0]
    assert segment["segment"]["delinquency"] == "1 – 29 days past due"
    assert segment["segment"]["balance_band"] == "Under $500"
    assert segment["stats"]["accounts"] == 29


def test_preview_segments_portfolio_of_other_client(client, admin_token, portfolio):
    response = client.get(
        _url(portfolio["other_id"], portfolio["portfolio_id"]),
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 404


def test_preview_segments_requires_client_access(client, supervisor_token, portfolio):
    response = client.get(
        _url(portfolio["client_id"], portfolio["portfolio_id"]),
        headers={"Authorization": f"Bearer {supervisor_token}"},
    )
    assert response.status_code == 403


def test_preview_segments_requires_login(client, portfolio):
    response = client.get(_url(portfolio["client_id"], portfolio["portfolio_id"]))
    assert response.status_code == 401

# --------------------------------------------------------------------------- suggest


def _workflow_for(segment_id, broken=False):
    workflow = copy.deepcopy(load_examples()[0])
    workflow.pop("for_segment")
    workflow["segment_id"] = segment_id
    if broken:
        workflow["steps"][1]["config"]["template_id"] = "em-nope"
    return workflow


def fake_llm(broken_calls=0):
    """Fake AI: a workflow per requested segment; the first `broken_calls` replies are invalid."""
    calls = []

    def _call(messages, schema):
        calls.append(messages)
        ids = schema["properties"]["workflows"]["items"]["properties"]["segment_id"]["enum"]
        broken = len(calls) <= broken_calls
        return {"workflows": [_workflow_for(i, broken) for i in ids]}

    return _call, calls


def _suggest(client, token, client_id, portfolio_id):
    return client.post(
        "/payflow/workflows/suggest",
        json={"client_id": client_id, "portfolio_id": portfolio_id},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_suggest_saves_ai_proposed_workflows(client, admin_token, portfolio):
    call, calls = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        response = _suggest(client, admin_token, portfolio["client_id"], portfolio["portfolio_id"])
    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(calls) == 1
    assert body["portfolio_name"] == "Residential"
    [workflow] = body["workflows"]
    assert workflow["status"] == "AI Proposed"
    assert workflow["origin"] == "AI Proposed"
    assert workflow["source"] == "AI Generated"
    assert workflow["cases_covered"] == 29
    assert workflow["segment"]["delinquency"] == "1 – 29 days past due"
    assert workflow["segment"]["situation"] == "Active"
    assert workflow["ai_proposal_snapshot"] is not None
    labels = {item["label"] for item in workflow["ai_context"]}
    assert {"Segment", "Accounts", "AI rationale"} <= labels


def test_suggest_retries_with_errors_then_saves(client, admin_token, portfolio):
    call, calls = fake_llm(broken_calls=1)
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        response = _suggest(client, admin_token, portfolio["client_id"], portfolio["portfolio_id"])
    assert response.status_code == 200, response.json()
    assert len(calls) == 2
    retry_prompt = calls[1][1]["content"]
    assert "template 'em-nope' does not exist" in retry_prompt


def test_suggest_gives_up_and_saves_nothing(client, admin_token, portfolio, db_session):
    call, calls = fake_llm(broken_calls=99)
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        response = _suggest(client, admin_token, portfolio["client_id"], portfolio["portfolio_id"])
    assert response.status_code == 502
    assert "AI could not produce valid workflows" in response.json()["detail"]
    assert len(calls) == 1 + suggestion_service.MAX_FIX_ATTEMPTS
    assert db_session.query(PayflowStrategyModel).count() == 0


@pytest.mark.parametrize("per_call, expected_calls", [(3, 2), (0, 1)])
def test_suggest_batches_segments(client, admin_token, portfolio, per_call, expected_calls):
    call, calls = fake_llm()
    with patch.object(suggestion_service.settings, "GROQ_SEGMENTS_PER_CALL", per_call), patch.object(
        suggestion_service, "call_llm", side_effect=call
    ):
        response = _suggest(client, admin_token, portfolio["client_id"], portfolio["mixed_id"])
    assert response.status_code == 200, response.json()
    assert len(calls) == expected_calls
    assert len(response.json()["workflows"]) == 4


def test_suggest_ai_service_down(client, admin_token, portfolio):
    down = ServiceUnavailableError("AI service could not be reached")
    with patch.object(suggestion_service, "call_llm", side_effect=down):
        response = _suggest(client, admin_token, portfolio["client_id"], portfolio["portfolio_id"])
    assert response.status_code == 502
    assert response.json()["detail"] == "AI service could not be reached"


def test_suggest_portfolio_without_eligible_accounts(client, admin_token, portfolio):
    response = _suggest(client, admin_token, portfolio["client_id"], portfolio["closed_id"])
    assert response.status_code == 400
    assert response.json()["detail"] == "This portfolio has no accounts that need a workflow"


def test_suggest_requires_workflow_permission(client, supervisor_token, portfolio):
    with patch.object(suggestion_service, "call_llm") as call:
        response = _suggest(client, supervisor_token, portfolio["client_id"], portfolio["portfolio_id"])
    assert response.status_code == 403
    call.assert_not_called()
