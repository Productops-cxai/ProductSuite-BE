import pytest

from app.core.exceptions import NotFoundError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowPortfolioModel,
    PayflowRuleModel,
)
from app.modules.payflow.services.workflow_suggestion.context import (
    load_client_context,
    load_portfolio_accounts,
    load_rules,
)


@pytest.fixture()
def seeded(db_session):
    client = PayflowClientModel(
        code="northstar",
        name="Northstar Utilities",
        client_type="first_party",
        channel_email=True,
        channel_sms=False,
        governance_rules=["High Balance Human Review"],
    )
    other = PayflowClientModel(code="other", name="Other Client")
    db_session.add_all([client, other])
    db_session.flush()

    residential = PayflowPortfolioModel(client_id=client.id, name="Residential", code="res")
    commercial = PayflowPortfolioModel(client_id=client.id, name="Commercial", code="com")
    db_session.add_all([residential, commercial])
    db_session.flush()

    for i, portfolio in enumerate([residential, residential, commercial]):
        db_session.add(
            PayflowAccountModel(
                client_id=client.id,
                portfolio_id=portfolio.id,
                customer_name=f"Customer {i}",
                account_reference=f"ACC-{i}",
                case_reference=f"CASE-ACC-{i}-01",
            )
        )

    def rule(code, rule_type, status, client_id=None):
        return PayflowRuleModel(
            code=code,
            name=code,
            rule_type=rule_type,
            status=status,
            client_id=client_id,
            category="Amount",
            action="Require Human Review",
            conditions=[{"field": "Outstanding Balance", "operator": ">", "value": "5000"}],
        )

    db_session.add_all(
        [
            rule("system-active", "System Rule", "Active"),
            rule("system-draft", "System Rule", "Draft"),
            rule("client-active", "Client Rule", "Active", client.id),
            rule("client-inactive", "Client Rule", "Inactive", client.id),
            rule("other-client", "Client Rule", "Active", other.id),
        ]
    )
    db_session.commit()
    return {"client": client, "other": other, "residential": residential}


def test_load_client_context(db_session, seeded):
    ctx = load_client_context(db_session, seeded["client"].id)
    assert ctx["name"] == "Northstar Utilities"
    assert ctx["channels"] == ["Email"]
    assert ctx["governance_rules"] == ["High Balance Human Review"]


def test_load_client_context_unknown_client(db_session):
    with pytest.raises(NotFoundError):
        load_client_context(db_session, 9999)


def test_load_rules_only_active_system_and_own_client(db_session, seeded):
    rules = load_rules(db_session, seeded["client"].id)
    assert [r["name"] for r in rules["system"]] == ["system-active"]
    assert [r["name"] for r in rules["client"]] == ["client-active"]


def test_load_portfolio_accounts(db_session, seeded):
    portfolio, accounts = load_portfolio_accounts(
        db_session, seeded["client"].id, seeded["residential"].id
    )
    assert portfolio.code == "res"
    assert [a.account_reference for a in accounts] == ["ACC-0", "ACC-1"]


def test_load_portfolio_accounts_wrong_client(db_session, seeded):
    with pytest.raises(NotFoundError):
        load_portfolio_accounts(db_session, seeded["other"].id, seeded["residential"].id)
