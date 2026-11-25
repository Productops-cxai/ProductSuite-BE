"""Per-client SendGrid provider + From identity routes."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.exceptions import AppError
from app.modules.payflow.deps import DbSession, PayflowEditClient, RequirePayflowProduct
from app.modules.payflow.routes import _assert_client_permission, _assert_view_client, _map_error
from app.modules.payflow.schemas import (
    MessageResponse,
    PayflowEmailProviderStatusResponse,
    PayflowEmailProviderUpsertRequest,
    PayflowSenderDomainListResponse,
    PayflowSenderIdentityCreateRequest,
    PayflowSenderIdentityListResponse,
    PayflowSenderIdentityResponse,
    PayflowSenderIdentityToggleRequest,
    PayflowSenderIdentityUpdateRequest,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.modules.payflow.services.email_provider_service import PayflowEmailProviderService
from app.modules.payflow.services.sender_identity_service import PayflowSenderIdentityService

router = APIRouter(prefix="/payflow", tags=["PayFlow · Client Email"])


@router.get(
    "/clients/{client_id}/email-provider",
    response_model=PayflowEmailProviderStatusResponse,
)
def get_email_provider(client_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        access = AccessContextService(db)
        _assert_view_client(access, user, client_id)
        return PayflowEmailProviderService(db).get_status(client_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/clients/{client_id}/email-provider/upsert",
    response_model=PayflowEmailProviderStatusResponse,
)
def upsert_email_provider(
    client_id: int,
    payload: PayflowEmailProviderUpsertRequest,
    user: PayflowEditClient,
    db: DbSession,
):
    _assert_client_permission(db, user, client_id, "edit_client")
    try:
        return PayflowEmailProviderService(db).upsert(
            client_id,
            api_key=payload.api_key,
            webhook_verification_key=payload.webhook_verification_key,
            label=payload.label,
            is_active=payload.is_active,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/clients/{client_id}/email-provider/test",
    response_model=PayflowEmailProviderStatusResponse,
)
def test_email_provider(client_id: int, user: PayflowEditClient, db: DbSession):
    _assert_client_permission(db, user, client_id, "edit_client")
    try:
        return PayflowEmailProviderService(db).test_connection(client_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/clients/{client_id}/email-provider/delete",
    response_model=PayflowEmailProviderStatusResponse,
)
def delete_email_provider(client_id: int, user: PayflowEditClient, db: DbSession):
    _assert_client_permission(db, user, client_id, "edit_client")
    try:
        return PayflowEmailProviderService(db).delete(client_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/clients/{client_id}/sender-identities",
    response_model=PayflowSenderIdentityListResponse,
)
def list_sender_identities(client_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        access = AccessContextService(db)
        _assert_view_client(access, user, client_id)
        items = PayflowSenderIdentityService(db).list_identities(client_id)
        return {"items": items}
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/clients/{client_id}/sender-domains",
    response_model=PayflowSenderDomainListResponse,
)
def list_sender_domains(client_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        access = AccessContextService(db)
        _assert_view_client(access, user, client_id)
        items = PayflowSenderIdentityService(db).list_domains(client_id)
        return {"items": items}
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/clients/{client_id}/sender-identities",
    response_model=PayflowSenderIdentityResponse,
)
def create_sender_identity(
    client_id: int,
    payload: PayflowSenderIdentityCreateRequest,
    user: PayflowEditClient,
    db: DbSession,
):
    _assert_client_permission(db, user, client_id, "edit_client")
    try:
        return PayflowSenderIdentityService(db).add_identity(
            client_id,
            email=payload.email,
            display_name=payload.display_name,
            is_primary=payload.is_primary,
            failover_order=payload.failover_order,
            user_id=getattr(user, "id", None),
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/clients/{client_id}/sender-identities/{identity_id}/update",
    response_model=PayflowSenderIdentityResponse,
)
def update_sender_identity(
    client_id: int,
    identity_id: int,
    payload: PayflowSenderIdentityUpdateRequest,
    user: PayflowEditClient,
    db: DbSession,
):
    _assert_client_permission(db, user, client_id, "edit_client")
    try:
        return PayflowSenderIdentityService(db).update_identity(
            client_id,
            identity_id,
            display_name=payload.display_name,
            failover_order=payload.failover_order,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/clients/{client_id}/sender-identities/{identity_id}/toggle",
    response_model=PayflowSenderIdentityResponse,
)
def toggle_sender_identity(
    client_id: int,
    identity_id: int,
    payload: PayflowSenderIdentityToggleRequest,
    user: PayflowEditClient,
    db: DbSession,
):
    _assert_client_permission(db, user, client_id, "edit_client")
    try:
        return PayflowSenderIdentityService(db).toggle(
            client_id, identity_id, is_enabled=payload.is_enabled
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/clients/{client_id}/sender-identities/{identity_id}/set-primary",
    response_model=PayflowSenderIdentityResponse,
)
def set_primary_sender_identity(
    client_id: int,
    identity_id: int,
    user: PayflowEditClient,
    db: DbSession,
):
    _assert_client_permission(db, user, client_id, "edit_client")
    try:
        return PayflowSenderIdentityService(db).set_primary(client_id, identity_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/clients/{client_id}/sender-identities/{identity_id}/verify",
    response_model=PayflowSenderIdentityResponse,
)
def verify_sender_identity(
    client_id: int,
    identity_id: int,
    user: PayflowEditClient,
    db: DbSession,
):
    _assert_client_permission(db, user, client_id, "edit_client")
    try:
        return PayflowSenderIdentityService(db).verify(client_id, identity_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/clients/{client_id}/sender-identities/{identity_id}/delete",
    response_model=MessageResponse,
)
def delete_sender_identity(
    client_id: int,
    identity_id: int,
    user: PayflowEditClient,
    db: DbSession,
):
    _assert_client_permission(db, user, client_id, "edit_client")
    try:
        PayflowSenderIdentityService(db).delete(client_id, identity_id)
        return {"message": "Sender identity deleted"}
    except AppError as exc:
        raise _map_error(exc) from exc
