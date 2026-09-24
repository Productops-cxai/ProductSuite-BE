from typing import List
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from app.infrastructure.database.models import (
    OrganizationProductModel,
    ProductModel,
    UserModel,
    UserProductModel,
)
from app.shared.enums import EntitlementStatus, ProductStatus, UserStatus


def get_effective_products(db: Session, user: UserModel) -> List[ProductModel]:
    """Active product + org granted + user assigned.

    Product entry requires both organization entitlement and people assignment.
    Platform Super Admin still uses the same rules for product entry; platform
    administration access is separate from product assignment.
    """
    if user.status != UserStatus.ACTIVE.value:
        return []

    assigned_ids = {
        link.product_id
        for link in db.query(UserProductModel).filter(UserProductModel.user_id == user.id).all()
    }
    if not assigned_ids:
        return []

    granted_ids = {
        link.product_id
        for link in db.query(OrganizationProductModel)
        .filter(
            OrganizationProductModel.organization_id == user.organization_id,
            OrganizationProductModel.status == EntitlementStatus.GRANTED.value,
        )
        .all()
    }
    effective_ids = assigned_ids & granted_ids
    if not effective_ids:
        return []

    return (
        db.query(ProductModel)
        .filter(
            ProductModel.id.in_(effective_ids),
            ProductModel.status == ProductStatus.ACTIVE.value,
        )
        .order_by(ProductModel.name)
        .all()
    )


def has_product_access(db: Session, user: UserModel, product_code: str) -> bool:
    code = product_code.upper()
    return any(p.code == code for p in get_effective_products(db, user))


def load_user(db: Session, user_id: UUID) -> UserModel | None:
    return (
        db.query(UserModel)
        .options(
            joinedload(UserModel.organization),
            joinedload(UserModel.role),
            joinedload(UserModel.product_links),
        )
        .filter(UserModel.id == user_id)
        .first()
    )


# Aliases
load_person = load_user
