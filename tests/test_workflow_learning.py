"""Suggest learns from this client's past reviews: example workflows + lessons (guidance, not rules)."""

import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.infrastructure.database.models import PayflowStrategyModel, UserModel
from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.workflow_suggestion import learning
from app.modules.payflow.services.workflow_suggestion import service as suggestion_service
from app.modules.payflow.services.workflow_suggestion.prompt import (
    BRANCHING_EXAMPLE,
    build_system_prompt,
    load_examples,
)
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService
from tests.test_workflow_suggestion_api import fake_llm, portfolio  # noqa: F401 (fixture)

EARLY = "1 – 29 days past due"
LIMITS = {"channels": ["Email", "SMS"], "min_steps": 3, "max_steps": 15, "max_communications": 5,
          "max_messages_per_7_days": None}


@pytest.fixture()
def admin(db_session):
    return db_session.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()


def _suggest(db_session, admin, client_id, portfolio_id):
    call, calls = fake_llm()
    with patch.object(suggestion_service, "call_llm", side_effect=call):
        result = WorkflowSuggestionService(db_session).suggest_workflows(admin, client_id, portfolio_id)
    return result["workflows"], calls


@pytest.fixture()
def reviewed(db_session, admin, portfolio):  # noqa: F811
    """This client's history: one workflow edited + approved, one rejected; plus noise that must be ignored."""
    strategies = PayflowStrategyService(db_session)
    client_id, residential = portfolio["client_id"], portfolio["portfolio_id"]

    [edited], _ = _suggest(db_session, admin, client_id, residential)
    steps = copy.deepcopy(edited["steps"])
    steps[2]["config"]["amount"] = 7  # s3: wait 7 days instead of 3
    steps[2]["timing"] = None
    strategies.update_strategy(admin, edited["id"], {"name": "Maple Early Stage", "steps": steps, "note": "SMS was too early"})
    strategies.approve(admin, edited["id"], note="Gentle tone fits")

    [rejected], _ = _suggest(db_session, admin, client_id, residential)
    strategies.reject(admin, rejected["id"], note="Too aggressive")

    db_session.add_all(
        [
            PayflowStrategyModel(  # another client's approved workflow — never used
                code="other-client", name="Other Client Workflow", client_id=portfolio["other_id"],
                status="Approved", origin="Human Modified", source="AI Generated", entry_node_id="s1",
                steps=load_examples()[0]["steps"],
                versions=[{"version": 1, "date": "x", "note": "Approved by Bo", "changes": [], "reviewer_note": "Other client secret"}],
            ),
            PayflowStrategyModel(  # this client's old-format approved workflow — fails today's checks
                code="legacy", name="Legacy Workflow", client_id=client_id, status="Approved",
                origin="Human Created", source="Human Created",
                steps=[{"id": "t1", "kind": "Trigger", "title": "Account overdue"}],
            ),
        ]
    )
    db_session.commit()
    return edited["id"]


def test_suggest_uses_the_clients_reviewed_workflows_and_lessons(db_session, admin, portfolio, reviewed):  # noqa: F811
    # One segment per AI call: A = Active 1–29, B = Active 30–59, C = Promise to Pay, D = Payment Plan.
    with patch.object(suggestion_service.settings, "GROQ_SEGMENTS_PER_CALL", 1):
        saved, calls = _suggest(db_session, admin, portfolio["client_id"], portfolio["mixed_id"])
    system = calls[0][0]["content"]

    # A (similar: Active): the client's workflow + 1 generic filler + the branching example (3 in total).
    assert "The first 1 are this client's own workflows, approved by its reviewers" in system
    assert "Maple Early Stage" in system and BRANCHING_EXAMPLE in system
    assert "Early Stage Friendly Reminder" in system and "Promise-to-Pay Follow-Up" not in system
    assert "Legacy Workflow" not in system  # fails today's checks
    assert "Other Client Workflow" not in system and "Other client secret" not in system  # never another client

    # C (Promise to Pay): no similar client workflow → the generic examples, branching one included.
    promise = calls[2][0]["content"]
    assert "this client's own workflows" not in promise and "Maple Early Stage" not in promise
    assert all(example["name"] in promise for example in load_examples())

    # Lessons: guidance, not rules — with where + what + why.
    assert "## Lessons from this client's past reviews (guidance, not rules)" in system
    assert "Only the Limits and Allowed values above are hard rules." in system
    assert f"[Active · {EARLY} · Never contacted · Under $500] EDITED: " in system
    assert "Wait 3 days: timing changed from 3 Days After Previous Email to 7 Days After Previous Email" in system
    assert "WHY: SMS was too early" in system
    assert "APPROVED. WHY: Gentle tone fits" in system
    assert (
        "REJECTED: Email Payment Reminder → wait 3 Days → SMS Payment Link → wait 5 Days → Human Review. "
        "WHY: Too aggressive"
    ) in system

    # Traceability: A and B used the client's workflow; C and D only its lessons.
    learned = [next(i["value"] for i in w["ai_context"] if i["label"] == "Learned from") for w in saved]
    assert learned[0].startswith(f"examples #{reviewed}; ") and learned[1].startswith(f"examples #{reviewed}; ")
    assert learned[2] == learned[3] == "3 reviewer lessons"


def test_prompt_keeps_three_examples_with_the_branching_one():
    client_example = {"for_segment": "Active", "name": "Client Flow", "steps": []}
    one = build_system_prompt(LIMITS, [client_example], [])
    assert "Client Flow" in one and "Early Stage Friendly Reminder" in one and BRANCHING_EXAMPLE in one
    assert "Promise-to-Pay Follow-Up" not in one

    two = build_system_prompt(LIMITS, [client_example, {**client_example, "name": "Client Flow 2"}], [])
    assert "Client Flow 2" in two and BRANCHING_EXAMPLE in two
    assert "Early Stage Friendly Reminder" not in two and "Promise-to-Pay Follow-Up" not in two


def test_client_without_history_gets_todays_prompt(db_session, admin, portfolio):  # noqa: F811
    saved, calls = _suggest(db_session, admin, portfolio["client_id"], portfolio["portfolio_id"])
    system = calls[0][0]["content"]
    assert "Lessons from this client" not in system and "this client's own workflows" not in system
    assert all(example["name"] in system for example in load_examples())
    assert all(item["label"] != "Learned from" for item in saved[0]["ai_context"])
    assert build_system_prompt(LIMITS) == build_system_prompt(LIMITS, [], [])


def test_pick_examples_best_match_per_segment():
    def item(item_id, situation, stage, portfolio_id=2, human=False):
        return {"id": item_id, "portfolio_id": portfolio_id, "human_touched": human, "example": {},
                "segment": {"situation": situation, "delinquency": stage}}

    pool = [  # newest first
        item(1, "Active", "90+ days past due", portfolio_id=1),
        item(2, "Promise to Pay", "All stages"),
        item(3, "Active", EARLY),
        item(4, "Active", EARLY, human=True),
    ]
    segments = [
        {"segment": {"situation": "Active", "delinquency": EARLY}},
        {"segment": {"situation": "Promise to Pay", "delinquency": "All stages"}},
    ]
    assert [i["id"] for i in learning.pick_examples(pool, segments, portfolio_id=1)] == [4, 2]
    # One segment: its best match, then the next best similar one fills the spare slot.
    assert [i["id"] for i in learning.pick_examples(pool, segments[:1], portfolio_id=1)] == [4, 3]
    # Similar only: nothing for a Payment Plan segment → generic examples are used instead.
    plan = [{"segment": {"situation": "Payment Plan", "delinquency": "All stages"}}]
    assert learning.pick_examples(pool, plan, portfolio_id=1) == []
    assert learning.pick_examples([], segments, portfolio_id=1) == []


def test_lessons_for_similar_segments_come_first():
    lessons = [  # newest first
        {"segment": {"situation": "Active", "delinquency": "90+ days past due"}, "line": "newest, other stage"},
        {"segment": {"situation": "Payment Plan"}, "line": "not similar"},
        {"segment": {"situation": "Active", "delinquency": EARLY}, "line": "older, same stage"},
        {"segment": {}, "line": "no segment"},
    ]
    batch = [{"segment": {"situation": "Active", "delinquency": EARLY}}]
    assert learning.pick_lessons(lessons, batch) == [
        "older, same stage",  # most similar first, even though older
        "newest, other stage",  # same situation
        "not similar",  # the rest, newest first
        "no segment",
    ]


def test_lessons_are_short_deduplicated_and_capped():
    row = SimpleNamespace(
        segment={"situation": "Active", "delinquency": EARLY},
        steps=[],
        ai_context=[],
        versions=[  # newest first
            {"note": "Updated by Sara", "changes": [f"change {i}" for i in range(5)], "reviewer_note": "why"},
            {"note": "Updated by Sam", "changes": ["same change"]},
            {"note": "Updated by Sam", "changes": ["same change"]},
            {"note": "Review started by Sam", "changes": []},  # no feedback
            *[{"note": "Updated by Ali", "changes": [f"c{i}"]} for i in range(20)],
        ],
    )
    lessons = learning._lessons([row])
    assert len(lessons) == 1 + 1 + 20  # the repeat and the no-feedback entry are dropped
    lines = learning.pick_lessons(lessons, [{"segment": row.segment}])
    assert len(lines) == learning.MAX_LESSONS
    assert lines[0] == f"- [Active · {EARLY}] EDITED: change 0; change 1; change 2 (+2 more). WHY: why"
    assert lines[1] == f"- [Active · {EARLY}] EDITED: same change" and lines[2] != lines[1]
