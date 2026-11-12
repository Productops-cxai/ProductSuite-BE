"""Sample compliance rules (the prototype's CMP-001 ... CMP-010) for the Compliance screens."""

from app.infrastructure.database.models import (
    PayflowComplianceAuditModel,
    PayflowComplianceRuleModel,
    PayflowComplianceRuleVersionModel,
)
from app.modules.payflow.services.compliance import catalog
from app.modules.payflow.services.compliance.rule_service import (
    activation_problems,
    seed_compliance_library,
    seed_compliance_samples,
)


def _samples(db):
    return {
        rule.code: rule
        for rule in db.query(PayflowComplianceRuleModel).filter(PayflowComplianceRuleModel.code.like("CMP-0%"))
    }


def test_sample_rules_are_seeded_as_in_the_prototype(db_session):
    assert seed_compliance_samples(db_session) == {"rules": 10}
    rules = _samples(db_session)

    assert sorted(rules) == [f"CMP-{n:03d}" for n in range(1, 11)]
    assert {code: rule.status for code, rule in rules.items() if rule.status != "Active"} == {
        "CMP-009": "Pending Approval",
        "CMP-010": "Draft",
    }
    # Every category of the Compliance screens has at least one rule, bar "Notice & Waiting Periods"
    # (the prototype has none there either; the library covers it).
    assert {rule.category for rule in rules.values()} == set(catalog.CATEGORIES) - {"Notice & Waiting Periods"}

    voice = rules["CMP-004"]
    assert (voice.country, voice.channel, voice.action) == ("United States", "Voice", "Block")
    assert voice.conditions[0]["field"] == "Contact Attempts" and voice.conditions[0]["operator"] == "Greater Than"
    assert str(voice.conditions[0]["value"]) == "6" and voice.conditions[0]["period"] == 7
    ontario = rules["CMP-003"]
    assert (ontario.state, ontario.action, ontario.jurisdiction_type) == ("Ontario", "Reschedule", "State / Province")
    assert rules["CMP-007"].enforcement == "Client Configurable" and rules["CMP-009"].enforcement == "Advisory"
    assert rules["CMP-001"].approved_by == catalog.SAMPLE_ACTOR and rules["CMP-010"].approved_by is None
    assert not any(rule.is_library for rule in rules.values())


def test_active_samples_pass_the_same_checks_as_a_rule_put_in_force_by_hand(db_session):
    from app.modules.payflow.services.compliance.rule_service import ComplianceRuleService

    seed_compliance_samples(db_session)
    service = ComplianceRuleService(db_session)
    for code, rule in _samples(db_session).items():
        if rule.status == "Active":
            assert activation_problems(service._current(rule)) == [], code


def test_each_sample_has_a_history_row_and_an_audit_row(db_session):
    seed_compliance_samples(db_session)
    ids = [rule.id for rule in _samples(db_session).values()]

    versions = db_session.query(PayflowComplianceRuleVersionModel).filter(
        PayflowComplianceRuleVersionModel.rule_id.in_(ids)
    )
    audit = db_session.query(PayflowComplianceAuditModel).filter(PayflowComplianceAuditModel.rule_id.in_(ids))
    assert versions.count() == 10 and audit.count() == 10
    row = audit.first()
    assert row.event == "Sample rule created" and row.actor_name == catalog.SAMPLE_ACTOR
    assert row.approval == "Sample configuration" and row.after["code"].startswith("CMP-0")


def test_seeding_again_changes_nothing_and_keeps_edits(db_session):
    seed_compliance_samples(db_session)
    rule = _samples(db_session)["CMP-002"]
    rule.name, rule.status = "Edited by an admin", "Inactive"
    db_session.commit()

    assert seed_compliance_samples(db_session) == {"rules": 0}
    again = _samples(db_session)["CMP-002"]
    assert (again.name, again.status) == ("Edited by an admin", "Inactive")


def test_samples_and_library_live_side_by_side(db_session):
    seed_compliance_library(db_session)
    seed_compliance_samples(db_session)
    total = db_session.query(PayflowComplianceRuleModel).count()
    assert total == len(catalog.JURISDICTION_LIBRARY) + len(catalog.SAMPLE_RULES)


def test_the_compliance_screens_get_the_samples_from_the_api(client, admin_token, db_session):
    seed_compliance_library(db_session)
    seed_compliance_samples(db_session)
    headers = {"Authorization": f"Bearer {admin_token}"}

    active = client.get("/payflow/compliance/rules?status=Active&limit=100", headers=headers)
    assert active.status_code == 200, active.text
    items = active.json()["rules"]
    assert sorted(item["code"] for item in items) == [f"CMP-{n:03d}" for n in range(1, 9)]  # the library is Draft

    rule_id = next(item["id"] for item in items if item["code"] == "CMP-003")
    detail = client.get(f"/payflow/compliance/rules/{rule_id}", headers=headers).json()
    assert detail["name"] == "Ontario · recipient-local contact window" and detail["state"] == "Ontario"
    history = client.get(f"/payflow/compliance/rules/{rule_id}/history", headers=headers)
    assert history.status_code == 200

    audit = client.get("/payflow/compliance/audit", headers=headers)
    assert audit.status_code == 200 and "Sample rule created" in audit.text
    assert len(client.get("/payflow/compliance/holidays", headers=headers).json()["holidays"]) == 3
