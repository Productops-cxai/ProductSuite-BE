from typing import Annotated

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


PayflowOpsAdmin = Annotated[UserModel, Depends(require_payflow_operations_admin)]
PayflowAccessContext = Annotated[dict, Depends(get_payflow_access_context)]
