"""Client setup gaps: postal codes outside Canada, payment settings, creator access."""

import pytest
from pydantic import ValidationError

from app.infrastructure.database.models import PayflowClientModel
from app.modules.payflow.schemas import UpdatePayflowClientRequest
from app.modules.payflow.services.client_service import PayflowClientService, _is_valid_postal_code
from tests.test_security_gaps import _auth, scoped_token, two_clients  # noqa: F401  (fixtures)


@pytest.mark.parametrize(
    ("postal", "country", "valid"),
    [
        ("M5V 2T6", "Canada", True),
        ("m5v2t6", "Canada", True),
        ("90210", "Canada", False),
        ("M5V 2T6", None, True),
        ("90210", None, False),  # no country on file: Canada is the default market
        ("90210", "United States", True),
        ("90210-1234", "United States", True),
        ("M5V 2T6", "United States", False),
        ("SW1A 1AA", "United Kingdom", True),
        ("75008", "France", True),
        ("!!", "France", False),
        ("", "Canada", True),
    ],
)
def test_postal_code_is_checked_for_the_clients_country(postal, country, valid):
    assert _is_valid_postal_code(postal, country) is valid


def test_update_request_accepts_a_us_zip_for_a_us_client():
    request = UpdatePayflowClientRequest(country="United States", postal_code="90210")
    assert request.postal_code == "90210"


def test_update_request_still_refuses_a_bad_canadian_postal_code():
    with pytest.raises(ValidationError):
        UpdatePayflowClientRequest(country="Canada", postal_code="90210")


def test_saving_payment_options_keeps_settings_the_form_does_not_know(db_session):
    client = PayflowClientModel(
        code="cg-pay",
        name="Pay Settings Client",
        payment_settings={"allow_partial": True, "custom_flag": "keep-me"},
    )
    db_session.add(client)
    db_session.commit()

    PayflowClientService(db_session).update_client(
        client.id, {"payment_settings": {"allow_partial": False}}
    )

    db_session.refresh(client)
    assert client.payment_settings == {"allow_partial": False, "custom_flag": "keep-me"}


def test_client_created_by_a_scoped_user_can_be_set_up_by_that_user(client, scoped_token):
    created = client.post(
        "/payflow/clients",
        json={"name": "Fresh Client", "code": "cg-fresh", "client_type": "First Party"},
        headers=_auth(scoped_token),
    )
    assert created.status_code == 200, created.text

    # The wizard's next call: before the fix this answered "Client access denied".
    updated = client.post(
        f"/payflow/clients/{created.json()['id']}/update",
        json={"contact_name": "Jane Doe"},
        headers=_auth(scoped_token),
    )

    assert updated.status_code == 200, updated.text
