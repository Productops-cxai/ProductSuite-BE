from uuid import UUID

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response

from app.core.deps import DbSession
from app.core.exceptions import AppError
from app.modules.payflow.deps import PayflowAccessContext, PayflowOpsAdmin, RequirePayflowProduct
from app.modules.payflow.schemas import (
    AccessContextResponse,
    CreatePayflowClientRequest,
    CreatePayflowRoleRequest,
    CreatePayflowUserRequest,
    CreatePortfolioRequest,
    IntegrationTestResponse,
    MessageResponse,
    MenusResponse,
    PayflowAccountsListResponse,
    PayflowClientsListResponse,
    PayflowIntegrationItem,
    PayflowIntegrationsListResponse,
    PayflowPermissionsCatalogResponse,
    PayflowRolesListResponse,
    PayflowUserDetailResponse,
    PayflowUsersListResponse,
    UpdatePayflowClientRequest,
    UpdatePayflowRoleRequest,
    UpdatePayflowUserRequest,
    UpdatePortfolioRequest,
)
from app.modules.payflow.service import PayflowUserService
from app.modules.payflow.services.access_context_service import AccessContextService
from app.modules.payflow.services.account_service import PayflowAccountService
from app.modules.payflow.services.client_service import PayflowClientService
from app.modules.payflow.services.integration_service import PayflowIntegrationService

router = APIRouter(prefix="/payflow", tags=["PayFlow"])


def _map_error(exc: AppError) -> HTTPException:
    code_map = {
        "unauthorized": status.HTTP_401_UNAUTHORIZED,
        "forbidden": status.HTTP_403_FORBIDDEN,
        "not_found": status.HTTP_404_NOT_FOUND,
        "conflict": status.HTTP_409_CONFLICT,
        "validation_error": status.HTTP_400_BAD_REQUEST,
    }
    return HTTPException(status_code=code_map.get(exc.code, 400), detail=exc.message)


@router.get("/health")
def payflow_health(user: RequirePayflowProduct, ctx: PayflowAccessContext):
    return {
        "status": "ok",
        "product": "PAYFLOW",
        "user_id": str(user.id),
        "role": ctx["role"]["code"],
        "is_operations_admin": ctx["is_operations_admin"],
    }


@router.get("/access-context", response_model=AccessContextResponse)
def get_access_context(ctx: PayflowAccessContext):
    return ctx


@router.get("/menus", response_model=MenusResponse)
def get_payflow_menus(user: RequirePayflowProduct, db: DbSession):
    try:
        return AccessContextService(db).menus_for_user(user)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/permissions", response_model=PayflowPermissionsCatalogResponse)
def list_permissions(_: PayflowOpsAdmin, db: DbSession):
    return PayflowUserService(db).list_permissions_catalog()


@router.get("/roles", response_model=PayflowRolesListResponse)
def list_roles(_: PayflowOpsAdmin, db: DbSession):
    return PayflowUserService(db).list_roles()


@router.post("/roles", response_model=PayflowRolesListResponse)
def create_role(payload: CreatePayflowRoleRequest, _: PayflowOpsAdmin, db: DbSession):
    try:
        return PayflowUserService(db).create_role(
            name=payload.name,
            scope=payload.scope,
            description=payload.description,
            permission_codes=payload.permission_codes,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/roles/{role_id}/update", response_model=PayflowRolesListResponse)
def update_role(
    role_id: int,
    payload: UpdatePayflowRoleRequest,
    _: PayflowOpsAdmin,
    db: DbSession,
):
    try:
        return PayflowUserService(db).update_role(
            role_id,
            name=payload.name,
            description=payload.description,
            permission_codes=payload.permission_codes,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/roles/{role_id}/delete", response_model=MessageResponse)
def delete_role(role_id: int, _: PayflowOpsAdmin, db: DbSession):
    try:
        return PayflowUserService(db).delete_role(role_id)
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@router.get("/clients/mapping-catalog")
def get_mapping_catalog(user: RequirePayflowProduct, db: DbSession):
    # Read-only catalog — available to any PayFlow user (supervisor config view).
    _ = user
    return PayflowClientService(db).mapping_catalog()


@router.get("/clients/bulk-template")
def download_bulk_template(_: PayflowOpsAdmin, db: DbSession):
    content = PayflowClientService(db).bulk_template_bytes()
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="payflow_clients_bulk_template.xlsx"'
        },
    )


@router.post("/clients/bulk-upload")
async def bulk_upload_clients(
    _: PayflowOpsAdmin,
    db: DbSession,
    file: UploadFile = File(...),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Upload an .xlsx Excel file")
    data = await file.read()
    try:
        return PayflowClientService(db).bulk_upload(data)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/clients", response_model=PayflowClientsListResponse)
def list_clients(
    user: RequirePayflowProduct,
    db: DbSession,
    search: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    client_type: str | None = Query(default=None),
    business_domain: str | None = Query(default=None),
    ai_mode: str | None = Query(default=None),
    supervisor_user_id: str | None = Query(default=None),
):
    try:
        access = AccessContextService(db)
        allowed_ids = None
        if not access.is_operations_admin(user):
            ctx = access.build_context(user)
            allowed_ids = list(ctx.get("client_ids") or [])
        return PayflowClientService(db).list_clients(
            search=search,
            status=status_filter,
            client_type=client_type,
            business_domain=business_domain,
            ai_mode=ai_mode,
            supervisor_user_id=supervisor_user_id,
            client_ids=allowed_ids,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients")
def create_client(payload: CreatePayflowClientRequest, _: PayflowOpsAdmin, db: DbSession):
    try:
        return PayflowClientService(db).create_client(payload.model_dump())
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/clients/{client_id}")
def get_client(client_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        access = AccessContextService(db)
        if not access.can_see_client(user, client_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client access denied")
        return PayflowClientService(db).get_client(client_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/update")
def update_client(
    client_id: int,
    payload: UpdatePayflowClientRequest,
    _: PayflowOpsAdmin,
    db: DbSession,
):
    try:
        data = payload.model_dump(exclude_unset=True)
        if "supervisor_user_ids" in data and data["supervisor_user_ids"] is not None:
            data["supervisor_user_ids"] = [str(u) for u in data["supervisor_user_ids"]]
        return PayflowClientService(db).update_client(client_id, data)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/activate")
def activate_client(client_id: int, _: PayflowOpsAdmin, db: DbSession):
    try:
        return PayflowClientService(db).activate_client(client_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/clients/{client_id}/portfolios")
def list_portfolios(client_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        access = AccessContextService(db)
        if not access.can_see_client(user, client_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client access denied")
        return PayflowClientService(db).list_portfolios(client_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/portfolios")
def create_portfolio(
    client_id: int,
    payload: CreatePortfolioRequest,
    _: PayflowOpsAdmin,
    db: DbSession,
):
    try:
        return PayflowClientService(db).create_portfolio(client_id, payload.model_dump())
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/portfolios/{portfolio_id}/update")
def update_portfolio(
    client_id: int,
    portfolio_id: int,
    payload: UpdatePortfolioRequest,
    _: PayflowOpsAdmin,
    db: DbSession,
):
    try:
        return PayflowClientService(db).update_portfolio(
            client_id, portfolio_id, payload.model_dump(exclude_unset=True)
        )
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Accounts / Cases
# ---------------------------------------------------------------------------


@router.get("/accounts", response_model=PayflowAccountsListResponse)
def list_accounts(
    user: RequirePayflowProduct,
    db: DbSession,
    client_id: int | None = Query(default=None),
    portfolio_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    workflow: str | None = Query(default=None),
    human_review: str | None = Query(default=None),
    search: str | None = Query(default=None),
):
    try:
        return PayflowAccountService(db).list_accounts(
            user,
            client_id=client_id,
            portfolio_id=portfolio_id,
            status=status_filter,
            workflow=workflow,
            human_review=human_review,
            search=search,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/accounts/{account_id}")
def get_account(account_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowAccountService(db).get_account(user, account_id)
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Integrations (derived from client configuration)
# ---------------------------------------------------------------------------


@router.get("/integrations", response_model=PayflowIntegrationsListResponse)
def list_integrations(
    user: RequirePayflowProduct,
    db: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
    client_id: int | None = Query(default=None),
    category: str | None = Query(default=None),
):
    try:
        return PayflowIntegrationService(db).list_integrations(
            user,
            status=status_filter,
            client_id=client_id,
            category=category,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/integrations/{integration_id}", response_model=PayflowIntegrationItem)
def get_integration(integration_id: str, user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowIntegrationService(db).get_integration(user, integration_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/integrations/{integration_id}/test", response_model=IntegrationTestResponse)
def test_integration(integration_id: str, user: PayflowOpsAdmin, db: DbSession):
    try:
        return PayflowIntegrationService(db).test_connection(user, integration_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/users", response_model=PayflowUsersListResponse)
def list_users(
    _: PayflowOpsAdmin,
    db: DbSession,
    search: str | None = Query(default=None),
    role_code: str | None = Query(default=None),
    status: str | None = Query(default=None),
    client_id: int | None = Query(default=None),
):
    return PayflowUserService(db).list_users(
        search=search, role_code=role_code, status=status, client_id=client_id
    )


@router.post("/users", response_model=PayflowUserDetailResponse)
def create_user(payload: CreatePayflowUserRequest, admin: PayflowOpsAdmin, db: DbSession):
    try:
        return PayflowUserService(db).create_user(
            admin,
            full_name=payload.full_name,
            email=str(payload.email),
            role_code=payload.role_code,
            status=payload.status,
            client_ids=payload.client_ids,
            permission_codes=payload.permission_codes,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/users/{user_id}", response_model=PayflowUserDetailResponse)
def get_user(user_id: UUID, _: PayflowOpsAdmin, db: DbSession):
    try:
        return PayflowUserService(db).get_user(user_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/users/{user_id}/update", response_model=PayflowUserDetailResponse)
def update_user(
    user_id: UUID,
    payload: UpdatePayflowUserRequest,
    _: PayflowOpsAdmin,
    db: DbSession,
):
    try:
        return PayflowUserService(db).update_user(
            user_id,
            full_name=payload.full_name,
            email=str(payload.email) if payload.email else None,
            role_code=payload.role_code,
            confirm_role_change=payload.confirm_role_change,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/users/{user_id}/resend-invitation", response_model=MessageResponse)
def resend_invitation(user_id: UUID, _: PayflowOpsAdmin, db: DbSession):
    try:
        return PayflowUserService(db).resend_invitation(user_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/users/{user_id}/deactivate", response_model=PayflowUserDetailResponse)
def deactivate_user(user_id: UUID, admin: PayflowOpsAdmin, db: DbSession):
    try:
        return PayflowUserService(db).deactivate_user(user_id, actor=admin)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/users/{user_id}/reactivate", response_model=PayflowUserDetailResponse)
def reactivate_user(user_id: UUID, admin: PayflowOpsAdmin, db: DbSession):
    try:
        return PayflowUserService(db).reactivate_user(user_id, actor=admin)
    except AppError as exc:
        raise _map_error(exc) from exc
