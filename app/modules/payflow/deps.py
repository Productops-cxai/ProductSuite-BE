from typing import Annotated, Callable

from fastapi import Depends, HTTPException, status

from app.core.deps import DbSession, require_product_access
from app.core.exceptions import ForbiddenError
from app.infrastructure.database.models import UserModel
from app.modules.payflow.services.access_context_service import AccessContextService

RequirePayflowProduct = Annotated[UserModel, Depends(require_product_access("PAYFLOW"))]


def get_payflow_access_context(
    user: RequirePayflowProduct,
    db: DbSession,
) -> dict:
    try:
        return AccessContextService(db).build_context(user)
    except ForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message) from exc


def require_payflow_operations_admin(
    user: RequirePayflowProduct,
    db: DbSession,
) -> UserModel:
    svc = AccessContextService(db)
    try:
        if not svc.is_operations_admin(user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Operations Admin access required",
            )
    except ForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message) from exc
    return user


def require_payflow_permission(permission: str) -> Callable:
    """Dependency factory — user must hold the permission (ops roles usually hold all)."""

    def _dep(user: RequirePayflowProduct, db: DbSession) -> UserModel:
        svc = AccessContextService(db)
        try:
            if not svc.can(user, permission):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission required: {permission}",
                )
        except ForbiddenError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=exc.message
            ) from exc
        return user

    return _dep


def require_payflow_any_permission(*permissions: str) -> Callable:
    """User must hold at least one of the listed permissions."""

    def _dep(user: RequirePayflowProduct, db: DbSession) -> UserModel:
        svc = AccessContextService(db)
        try:
            if not svc.can_any(user, list(permissions)):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission required: one of {', '.join(permissions)}",
                )
        except ForbiddenError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=exc.message
            ) from exc
        return user

    return _dep


PayflowOpsAdmin = Annotated[UserModel, Depends(require_payflow_operations_admin)]
PayflowManageUsers = Annotated[UserModel, Depends(require_payflow_permission("manage_users"))]
PayflowManageIntegrations = Annotated[
    UserModel, Depends(require_payflow_permission("manage_integrations"))
]
PayflowCreateClient = Annotated[UserModel, Depends(require_payflow_permission("create_client"))]
PayflowEditClient = Annotated[UserModel, Depends(require_payflow_permission("edit_client"))]
PayflowDeleteClient = Annotated[UserModel, Depends(require_payflow_permission("delete_client"))]
PayflowImportClients = Annotated[UserModel, Depends(require_payflow_permission("import_clients"))]
PayflowImportAccounts = Annotated[UserModel, Depends(require_payflow_permission("import_accounts"))]
PayflowViewImports = Annotated[
    UserModel,
    Depends(require_payflow_any_permission("import_clients", "import_accounts")),
]
PayflowManageWorkflows = Annotated[
    UserModel, Depends(require_payflow_permission("create_edit_workflows"))
]
PayflowDeleteWorkflows = Annotated[
    UserModel, Depends(require_payflow_permission("delete_workflows"))
]
PayflowDeleteRules = Annotated[UserModel, Depends(require_payflow_permission("delete_rules"))]
PayflowAccessContext = Annotated[dict, Depends(get_payflow_access_context)]
