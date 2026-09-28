"""PayFlow membership helpers shared by platform grant + PayFlow user APIs."""

from uuid import UUID

from sqlalchemy.orm import Session

from app.infrastructure.database.models import (
    PayflowRoleModel,
    PayflowUserMembershipModel,
    ProductModel,
    UserProductAssignmentModel,
)
from app.shared.enums import PayflowRoleCode


def ensure_payflow_ops_admin_membership(db: Session, user_id: UUID) -> PayflowUserMembershipModel | None:
    """If user has no PayFlow membership, create Operations Admin (decision 2A)."""
    existing = (
        db.query(PayflowUserMembershipModel)
        .filter(PayflowUserMembershipModel.user_id == user_id)
        .first()
    )
    if existing:
        return existing

    role = (
        db.query(PayflowRoleModel)
        .filter(PayflowRoleModel.code == PayflowRoleCode.OPERATIONS_ADMIN.value)
        .first()
    )
    if not role:
        return None

    row = PayflowUserMembershipModel(user_id=user_id, payflow_role_id=role.id)
    db.add(row)
    db.flush()
    return row


def ensure_payflow_membership_for_role(
    db: Session, user_id: UUID, role_code: str
) -> PayflowUserMembershipModel:
    role = db.query(PayflowRoleModel).filter(PayflowRoleModel.code == role_code).first()
    if not role:
        raise ValueError(f"PayFlow role '{role_code}' not found — run seeder")

    existing = (
        db.query(PayflowUserMembershipModel)
        .filter(PayflowUserMembershipModel.user_id == user_id)
        .first()
    )
    if existing:
        existing.payflow_role_id = role.id
        db.flush()
        return existing

    row = PayflowUserMembershipModel(user_id=user_id, payflow_role_id=role.id)
    db.add(row)
    db.flush()
    return row


def ensure_user_product_assignment(db: Session, user_id: UUID, product_code: str = "PAYFLOW") -> None:
    product = db.query(ProductModel).filter(ProductModel.code == product_code.upper()).first()
    if not product:
        raise ValueError(f"Product '{product_code}' not found")
    exists = (
        db.query(UserProductAssignmentModel)
        .filter(
            UserProductAssignmentModel.user_id == user_id,
            UserProductAssignmentModel.product_id == product.id,
        )
        .first()
    )
    if not exists:
        db.add(UserProductAssignmentModel(user_id=user_id, product_id=product.id))
        db.flush()


def on_payflow_product_assigned(db: Session, user_id: UUID, product_code: str) -> None:
    """Hook after platform grants a product to a user."""
    if product_code.upper() != "PAYFLOW":
        return
    ensure_payflow_ops_admin_membership(db, user_id)
