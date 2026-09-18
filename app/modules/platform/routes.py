from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from app.core.deps import CurrentPerson, DbSession, SuperAdmin
from app.core.exceptions import AppError
from app.modules.platform.schemas import (
    AssignProductRequest,
    EmailLogResponse,
    EnterProductResponse,
    GrantAccessRequest,
    InviteResendResponse,
    MenusResponse,
    OrganizationResponse,
    OrganizationSave,
    OverviewResponse,
    PersonIdRequest,
    PersonResponse,
    PersonSave,
    ProductAccessItem,
    ProductResponse,
    ProductSave,
)
from app.modules.platform.service import PlatformService
from app.modules.platform.services.entitlement_service import get_effective_products
from app.shared.enums import MenuContext

router = APIRouter(tags=["Platform"])


def _map_error(exc: AppError) -> HTTPException:
    code_map = {
        "unauthorized": status.HTTP_401_UNAUTHORIZED,
        "forbidden": status.HTTP_403_FORBIDDEN,
        "not_found": status.HTTP_404_NOT_FOUND,
        "conflict": status.HTTP_409_CONFLICT,
        "validation_error": status.HTTP_400_BAD_REQUEST,
    }
    return HTTPException(status_code=code_map.get(exc.code, 400), detail=exc.message)


@router.get("/platform/overview", response_model=OverviewResponse)
def platform_overview(_: SuperAdmin, db: DbSession):
    return PlatformService(db).overview()


@router.get("/menus", response_model=MenusResponse)
def get_menus(
    person: CurrentPerson,
    db: DbSession,
    context: MenuContext = Query(default=MenuContext.PLATFORM_ADMIN),
):
    if context == MenuContext.PLATFORM_ADMIN and person.role_code != "platform_super_admin":
        raise HTTPException(status_code=403, detail="Platform Super Admin required")
    return PlatformService(db).menus(context.value, person.role_code, person.email)


@router.get("/products", response_model=List[ProductResponse])
def list_products(_: SuperAdmin, db: DbSession):
    return PlatformService(db).list_products()


@router.post("/products", response_model=ProductResponse)
def save_product(payload: ProductSave, _: SuperAdmin, db: DbSession):
    """Create product when id is omitted; update when id is sent."""
    try:
        return PlatformService(db).save_product(payload)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/products/{product_id}", response_model=ProductResponse)
def get_product(product_id: int, _: SuperAdmin, db: DbSession):
    try:
        return PlatformService(db).get_product(product_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/organizations", response_model=List[OrganizationResponse])
def list_organizations(_: SuperAdmin, db: DbSession):
    return PlatformService(db).list_organizations()


@router.post("/organizations", response_model=OrganizationResponse)
def save_organization(payload: OrganizationSave, _: SuperAdmin, db: DbSession):
    """Create organization when id is omitted; update when id is sent."""
    try:
        return PlatformService(db).save_organization(payload)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/product-access", response_model=List[ProductAccessItem])
def list_product_access(
    _: SuperAdmin,
    db: DbSession,
    search: Optional[str] = Query(default=None),
    product_id: Optional[int] = Query(default=None),
    access_status: Optional[str] = Query(default=None, pattern="^(granted|revoked)$"),
):
    return PlatformService(db).list_product_access(search, product_id, access_status)


@router.post("/product-access/grant", response_model=ProductAccessItem)
def grant_product_access(payload: GrantAccessRequest, _: SuperAdmin, db: DbSession):
    try:
        return PlatformService(db).grant_access(payload.organization_id, payload.product_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/product-access/revoke", response_model=ProductAccessItem)
def revoke_product_access(payload: GrantAccessRequest, _: SuperAdmin, db: DbSession):
    try:
        return PlatformService(db).revoke_access(payload.organization_id, payload.product_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/people", response_model=List[PersonResponse])
def list_people(
    _: SuperAdmin,
    db: DbSession,
    search: Optional[str] = Query(default=None),
    organization_id: Optional[int] = Query(default=None),
):
    return PlatformService(db).list_people(search, organization_id)


@router.post("/people", response_model=PersonResponse)
def save_person(payload: PersonSave, _: SuperAdmin, db: DbSession):
    """Create person when id is omitted; update when id is sent."""
    try:
        return PlatformService(db).save_person(payload)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/people/assign-product", response_model=PersonResponse)
def assign_product(payload: AssignProductRequest, _: SuperAdmin, db: DbSession):
    try:
        return PlatformService(db).assign_product(payload.user_id, payload.product_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/people/remove-product", response_model=PersonResponse)
def remove_product(payload: AssignProductRequest, _: SuperAdmin, db: DbSession):
    try:
        return PlatformService(db).remove_product(payload.user_id, payload.product_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/people/resend-invite", response_model=InviteResendResponse)
def resend_invite(payload: PersonIdRequest, _: SuperAdmin, db: DbSession):
    try:
        return PlatformService(db).resend_invite(payload.user_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/email-logs", response_model=List[EmailLogResponse])
def list_email_logs(
    admin: SuperAdmin,
    db: DbSession,
    search: Optional[str] = Query(default=None),
    email_type: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Private ops view — only the seeded suite admin (SEED_SUPER_ADMIN_EMAIL)."""
    try:
        return PlatformService(db).list_email_logs(admin.email, search, email_type, limit)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/me/products", response_model=List[ProductResponse])
def my_products(person: CurrentPerson, db: DbSession):
    return get_effective_products(db, person)


@router.post("/products/{code}/enter", response_model=EnterProductResponse)
def enter_product(code: str, person: CurrentPerson, db: DbSession):
    try:
        return PlatformService(db).enter_product(person, code)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/platform/billing")
def billing_soon(_: SuperAdmin):
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Billing & Invoices is coming soon (Phase 2)",
    )
