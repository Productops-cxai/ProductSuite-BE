from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.core.deps import DbSession
from app.core.exceptions import AppError
from app.modules.payflow.deps import PayflowAccessContext, PayflowOpsAdmin, RequirePayflowProduct
from app.modules.payflow.schemas import (
    AccessContextResponse,
    CreatePayflowRoleRequest,
    CreatePayflowUserRequest,
    MessageResponse,
    MenusResponse,
    PayflowClientsListResponse,
    PayflowPermissionsCatalogResponse,
    PayflowRolesListResponse,
    PayflowUserDetailResponse,
    PayflowUsersListResponse,
    UpdatePayflowRoleRequest,
    UpdatePayflowUserRequest,
)
from app.modules.payflow.service import PayflowUserService
from app.modules.payflow.services.access_context_service import AccessContextService

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


@router.get("/clients", response_model=PayflowClientsListResponse)
def list_clients(_: PayflowOpsAdmin, db: DbSession):
    return PayflowUserService(db).list_clients()


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
