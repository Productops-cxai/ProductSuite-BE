"""Client ↔ compliance rule mapping (FR-10): automatic, independent per client, preserved."""

from app.infrastructure.database.models import (
    PayflowClientComplianceRuleModel,
    PayflowClientModel,
    PayflowComplianceAuditModel,
    PayflowComplianceRuleModel,
)
from app.modules.payflow.services.client_service import PayflowClientService
from app.modules.payflow.services.compliance.client_mapping import ClientComplianceService
from tests.test_security_gaps import _auth, scoped_token, two_clients  # noqa: F401  (fixtures)

_SEQ = 0


def rule(db, *, country="Canada", state="", enforcement="Mandatory", status="Active", client_id=None):
    global _SEQ
    _SEQ += 1
    row = PayflowComplianceRuleModel(
        code=f"CMP-M{_SEQ:03d}",
        name=f"Mapping rule {_SEQ}",
        category="Consent & Opt-Out",
        rule_type="JUR-09",
        enforcement=enforcement,
        country=country,
        state=state,
        channel="All Channels",
        action="Suppress",
        status=status,
        client_id=client_id,
        conditions=[{"field": "Opt-Out", "operator": "Equals", "value": "Yes", "period": 7}],
        settings={},
    )
    db.add(row)
    db.commit()
    return row


def associations(db, client):
    return (
        db.query(PayflowClientComplianceRuleModel)
        .filter(PayflowClientComplianceRuleModel.client_id == client.id)
        .order_by(PayflowClientComplianceRuleModel.rule_id.asc())
        .all()
    )


def test_new_client_gets_the_rules_of_its_country(db_session):
    canada = rule(db_session)
    ontario = rule(db_session, state="Ontario")
    rule(db_session, country="United States")

    client, _action = PayflowClientService(db_session).upsert_client(
        {"name": "Maple Collections", "code": "map-new", "country": "Canada", "crm_client_number": "9001"}
    )

    assert [a.rule_id for a in associations(db_session, client)] == [canada.id, ontario.id]


def test_saving_the_client_again_creates_no_duplicates_and_keeps_choices(db_session):
    optional = rule(db_session, enforcement="Client Configurable")
    service = PayflowClientService(db_session)
    client, _ = service.upsert_client(
        {"name": "Maple Collections", "code": "map-keep", "country": "Canada", "crm_client_number": "9002"}
    )
    row = associations(db_session, client)[0]
    row.enabled = False  # the client switched the optional rule off
    db_session.commit()

    # Re-import the same client (same number) and update it from the UI.
    service.upsert_client(
        {"name": "Maple Collections Inc", "code": "map-keep", "country": "Canada", "crm_client_number": "9002"}
    )
    service.update_client(client.id, {"city": "Toronto"})

    rows = associations(db_session, client)
    assert [(r.rule_id, r.enabled) for r in rows] == [(optional.id, False)]


def test_rule_added_later_is_picked_up_on_the_next_save(db_session):
    first = rule(db_session)
    service = PayflowClientService(db_session)
    client, _ = service.upsert_client(
        {"name": "Maple Collections", "code": "map-later", "country": "Canada", "crm_client_number": "9003"}
    )
    later = rule(db_session, state="Quebec")

    service.update_client(client.id, {"city": "Montreal"})

    assert [a.rule_id for a in associations(db_session, client)] == [first.id, later.id]


def test_clients_in_one_import_are_mapped_independently(db_session):
    canada = rule(db_session)
    usa = rule(db_session, country="United States")
    service = PayflowClientService(db_session)

    north, _ = service.upsert_client(
        {"name": "North", "code": "map-n", "country": "Canada", "crm_client_number": "9101"}, commit=False
    )
    south, _ = service.upsert_client(
        {"name": "South", "code": "map-s", "country": "United States", "crm_client_number": "9102"},
        commit=False,
    )
    db_session.commit()

    assert [a.rule_id for a in associations(db_session, north)] == [canada.id]
    assert [a.rule_id for a in associations(db_session, south)] == [usa.id]


def test_another_clients_own_rule_is_never_associated(db_session, two_clients):
    mine, other, _portfolio = two_clients
    mine.country = other.country = "Canada"
    db_session.commit()
    theirs = rule(db_session, client_id=other.id)

    ClientComplianceService(db_session).sync_client(mine, commit=True)
    ClientComplianceService(db_session).sync_client(other, commit=True)

    assert theirs.id not in [a.rule_id for a in associations(db_session, mine)]
    assert theirs.id in [a.rule_id for a in associations(db_session, other)]


def test_client_without_a_supported_country_has_no_rules_and_says_why(client, admin_token, two_clients):
    mine, _other, _portfolio = two_clients

    body = client.get(f"/payflow/clients/{mine.id}/compliance-rules", headers=_auth(admin_token)).json()

    assert body["rules"] == []
    assert body["jurisdiction_resolved"] is False
    assert "country" in body["message"].lower()


def test_mapping_change_is_audited(db_session):
    rule(db_session)
    client, _ = PayflowClientService(db_session).upsert_client(
        {"name": "Audited", "code": "map-aud", "country": "Canada", "crm_client_number": "9201"}
    )

    entry = (
        db_session.query(PayflowComplianceAuditModel)
        .filter_by(entity_type="client_mapping", client_id=client.id)
        .one()
    )
    assert entry.event == "Client rule mapping updated"


def test_optional_rule_can_be_switched_off_and_the_list_shows_it(client, admin_token, two_clients, db_session):
    mine, _other, _portfolio = two_clients
    mine.country = "Canada"
    db_session.commit()
    optional = rule(db_session, enforcement="Client Configurable")
    mandatory = rule(db_session)

    response = client.post(
        f"/payflow/clients/{mine.id}/compliance-rules/update",
        json={"rule_id": optional.id, "enabled": False, "reason": "Client uses its own opt-out process"},
        headers=_auth(admin_token),
    )

    assert response.status_code == 200, response.text
    by_id = {r["id"]: r for r in response.json()["rules"]}
    assert by_id[optional.id]["enabled"] is False
    assert by_id[optional.id]["in_force"] is False
    assert by_id[mandatory.id]["locked"] is True
    assert by_id[mandatory.id]["in_force"] is True


def test_mandatory_rule_cannot_be_switched_off_for_a_client(client, admin_token, two_clients, db_session):
    mine, _other, _portfolio = two_clients
    mine.country = "Canada"
    db_session.commit()
    mandatory = rule(db_session)

    response = client.post(
        f"/payflow/clients/{mine.id}/compliance-rules/update",
        json={"rule_id": mandatory.id, "enabled": False, "reason": "We would rather not"},
        headers=_auth(admin_token),
    )

    assert response.status_code == 403
    assert db_session.query(PayflowClientComplianceRuleModel).filter_by(enabled=False).count() == 0


def test_user_of_one_client_cannot_change_another_clients_rules(client, scoped_token, two_clients, db_session):
    _mine, other, _portfolio = two_clients
    other.country = "Canada"
    db_session.commit()
    optional = rule(db_session, enforcement="Client Configurable")

    response = client.post(
        f"/payflow/clients/{other.id}/compliance-rules/update",
        json={"rule_id": optional.id, "enabled": False, "reason": "Not mine to change"},
        headers=_auth(scoped_token),
    )

    assert response.status_code == 403
