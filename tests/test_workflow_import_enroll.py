"""Import / governance gaps: enroll Active strategies for new scopes; scan newly assigned RHR rules."""

from unittest.mock import patch

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowHumanReviewModel,
    PayflowPortfolioModel,
    PayflowRuleModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
)
from app.modules.payflow.services.client_service import PayflowClientService
from app.modules.payflow.services.rule_service import PayflowRuleService
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.modules.payflow.services.workflow_queue import enqueue_job
from app.shared.enums import PayflowWorkflowJobType


def _client_portfolio(db):
    client = PayflowClientModel(code="enroll-c", name="Enroll Client", governance_rules=[])
    db.add(client)
    db.flush()
    portfolio = PayflowPortfolioModel(client_id=client.id, name="Res", code="res")
    db.add(portfolio)
    db.flush()
    return client, portfolio


def _enqueue_without_notify(db, strategy_id, *, commit=False):
    """SQLite tests cannot run Postgres NOTIFY."""
    return enqueue_job(
        db,
        job_type=PayflowWorkflowJobType.ENROLL_STRATEGY.value,
        strategy_id=strategy_id,
        priority=50,
        notify=False,
        commit=commit,
    )


def test_enqueue_enrolls_for_scopes_only_active_matching_strategies(db_session):
    client, portfolio = _client_portfolio(db_session)
    other = PayflowClientModel(code="enroll-other", name="Other")
    db_session.add(other)
    db_session.flush()

    active = PayflowStrategyModel(
        code="wf-active",
        name="Active Portfolio WF",
        client_id=client.id,
        portfolio_id=portfolio.id,
        status="Active",
        steps=[{"id": "s1", "kind": "Trigger", "title": "Start"}],
        entry_node_id="s1",
    )
    draft = PayflowStrategyModel(
        code="wf-draft",
        name="Draft Portfolio WF",
        client_id=client.id,
        portfolio_id=portfolio.id,
        status="Draft",
        steps=[{"id": "s1", "kind": "Trigger", "title": "Start"}],
        entry_node_id="s1",
    )
    other_active = PayflowStrategyModel(
        code="wf-other",
        name="Other Client WF",
        client_id=other.id,
        portfolio_id=None,
        status="Active",
        steps=[{"id": "s1", "kind": "Trigger", "title": "Start"}],
        entry_node_id="s1",
    )
    parent_active = PayflowStrategyModel(
        code="wf-parent",
        name="Parent Client WF",
        client_id=client.id,
        portfolio_id=None,
        status="Active",
        steps=[{"id": "s1", "kind": "Trigger", "title": "Start"}],
        entry_node_id="s1",
    )
    db_session.add_all([active, draft, other_active, parent_active])
    db_session.commit()

    with patch(
        "app.modules.payflow.services.workflow_execution_service.enqueue_enroll_strategy",
        side_effect=_enqueue_without_notify,
    ):
        jobs = WorkflowExecutionService(db_session).enqueue_enrolls_for_scopes(
            portfolio_pairs={(client.id, portfolio.id)},
            parent_client_ids={client.id},
            commit=True,
        )
    assert {j.strategy_id for j in jobs} == {active.id, parent_active.id}
    assert all(j.job_type == PayflowWorkflowJobType.ENROLL_STRATEGY.value for j in jobs)
    stored = (
        db_session.query(PayflowWorkflowJobModel)
        .filter(PayflowWorkflowJobModel.job_type == PayflowWorkflowJobType.ENROLL_STRATEGY.value)
        .all()
    )
    assert {row.strategy_id for row in stored} == {active.id, parent_active.id}


def test_enqueue_enrolls_for_scopes_noop_when_empty(db_session):
    jobs = WorkflowExecutionService(db_session).enqueue_enrolls_for_scopes(
        portfolio_pairs=set(),
        parent_client_ids=set(),
    )
    assert jobs == []


def test_client_governance_assignment_scans_active_system_rhr(db_session):
    client, _portfolio = _client_portfolio(db_session)
    rule = PayflowRuleModel(
        code="sys-rhr",
        name="High Balance Human Review",
        rule_type="System Rule",
        status="Active",
        category="Amount",
        action="Require Human Review",
        conditions=[{"field": "Outstanding Balance", "operator": ">", "value": "100"}],
    )
    db_session.add(rule)
    db_session.add(
        PayflowAccountModel(
            client_id=client.id,
            portfolio_id=None,
            customer_name="Raam Laal",
            account_reference="100266000",
            case_reference="CASE-100266000-01",
            outstanding_balance=1000.0,
            collection_status="Active",
        )
    )
    db_session.commit()

    # Assigning the system rule on the client must scan existing accounts now.
    PayflowClientService(db_session).update_client(
        client.id, {"governance_rules": ["High Balance Human Review"]}
    )
    reviews = db_session.query(PayflowHumanReviewModel).all()
    assert len(reviews) == 1
    assert reviews[0].rule_id == rule.id

    # Re-saving the same assignment must not create a duplicate open review.
    PayflowClientService(db_session).update_client(
        client.id, {"governance_rules": ["High Balance Human Review"]}
    )
    assert db_session.query(PayflowHumanReviewModel).count() == 1


def test_scan_assigned_rules_ignores_non_rhr(db_session):
    client, _portfolio = _client_portfolio(db_session)
    db_session.add(
        PayflowRuleModel(
            code="sys-hold",
            name="Hold Big Balances",
            rule_type="System Rule",
            status="Active",
            category="Amount",
            action="Hold Action",
            conditions=[{"field": "Outstanding Balance", "operator": ">", "value": "100"}],
        )
    )
    db_session.add(
        PayflowAccountModel(
            client_id=client.id,
            customer_name="A",
            account_reference="A-1",
            case_reference="CASE-A-1",
            outstanding_balance=500.0,
        )
    )
    db_session.commit()
    client.governance_rules = ["Hold Big Balances"]
    db_session.commit()

    created = PayflowRuleService(db_session).scan_human_reviews_for_assigned_rules(
        rule_names=["Hold Big Balances"],
        actor_name="test",
    )
    assert created == 0
    assert db_session.query(PayflowHumanReviewModel).count() == 0


def test_import_service_wires_post_import_enroll():
    import inspect

    from app.modules.payflow.services import import_service

    source = inspect.getsource(import_service.PayflowImportService)
    assert "enqueue_enrolls_for_scopes" in source
    assert "created or updated" in source