from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    NavigationSectionModel,
    OrganizationModel,
    OrganizationProductEntitlementModel,
    PlatformRoleModel,
    ProductModel,
    UserModel,
    UserProductAssignmentModel,
)
from app.shared.deletion import (
    list_deletion_logs,
    purge_user_identity,
    record_deletion,
    snapshot_model,
)
from app.modules.identity.service import IdentityService
from app.modules.platform.services.entitlement_service import get_effective_products, has_product_access
from app.shared.enums import (
    EntitlementStatus,
    PlatformRole,
    ProductStatus,
    UserStatus,
)


class PlatformService:
    def __init__(self, db: Session):
        self.db = db

    def _role_by_code(self, code: str) -> PlatformRoleModel:
        role = self.db.query(PlatformRoleModel).filter(PlatformRoleModel.code == code).first()
        if not role:
            raise NotFoundError(f"Role '{code}' not found — run seeder")
        return role

    # ---- Overview KPIs ----
    def overview(self) -> dict:
        products = self.db.query(ProductModel).order_by(ProductModel.name).all()
        orgs = self.db.query(OrganizationModel).order_by(OrganizationModel.name).all()
        active_count = sum(1 for p in products if p.status == ProductStatus.ACTIVE.value)

        granted_org_ids = {
            e.organization_id
            for e in self.db.query(OrganizationProductEntitlementModel)
            .filter(
                OrganizationProductEntitlementModel.status == EntitlementStatus.GRANTED.value
            )
            .all()
        }

        org_access_summary = [
            {"id": o.id, "name": o.name, "has_access": o.id in granted_org_ids} for o in orgs
        ]
        with_access = sum(1 for x in org_access_summary if x["has_access"])

        return {
            "registered_products_count": len(products),
            "active_products_count": active_count,
            "organizations_with_access_count": with_access,
            "organizations_total_count": len(orgs),
            "organizations_with_access_label": f"{with_access} of {len(orgs)}",
            "products_summary": [
                {"id": p.id, "name": p.name, "code": p.code, "status": p.status} for p in products
            ],
            "org_access_summary": org_access_summary,
        }

    # ---- Menus ----
    def menus(self, context: str, role: str, email: str | None = None) -> dict:
        sections = (
            self.db.query(NavigationSectionModel)
            .filter(
                NavigationSectionModel.context == context,
            )
            .order_by(NavigationSectionModel.sort_order)
            .all()
        )
        email_logs_admin = settings.SEED_SUPER_ADMIN_EMAIL.lower()
        caller_email = (email or "").lower()
        result = []
        for section in sections:
            # Platform menus only (empty / null product_code)
            if (section.product_code or "") != "":
                continue
            items = []
            for item in sorted(section.items, key=lambda i: i.sort_order):
                if not item.is_active:
                    continue
                if item.required_role_code and item.required_role_code != role:
                    continue
                # Email Logs is a private ops view for the seeded suite admin only
                if item.key == "email_logs" and caller_email != email_logs_admin:
                    continue
                items.append(
                    {
                        "key": item.key,
                        "label": item.label,
                        "route": item.route,
                        "icon": item.icon,
                        "sort_order": item.sort_order,
                        "is_coming_soon": item.is_coming_soon,
                        "badge": "Soon" if item.is_coming_soon else None,
                    }
                )
            if items:
                result.append(
                    {
                        "key": section.key,
                        "label": section.label,
                        "sort_order": section.sort_order,
                        "items": items,
                    }
                )
        return {"sections": result}

    # ---- Products ----
    def list_products(self) -> List[ProductModel]:
        return self.db.query(ProductModel).order_by(ProductModel.name).all()

    def get_product(self, product_id: int) -> ProductModel:
        product = self.db.query(ProductModel).filter(ProductModel.id == product_id).first()
        if not product:
            raise NotFoundError("Product not found")
        return product

    def get_product_by_code(self, code: str) -> ProductModel:
        product = self.db.query(ProductModel).filter(ProductModel.code == code.upper()).first()
        if not product:
            raise NotFoundError("Product not found")
        return product

    def save_product(self, data) -> ProductModel:
        status_value = data.status.value if hasattr(data.status, "value") else data.status
        if data.id is None:
            raw = (data.code or "").strip().upper()
            if not raw:
                raise ValidationAppError("Product code is required")
            # Product code is the unique identity key (case-insensitive).
            existing = (
                self.db.query(ProductModel)
                .filter(func.upper(ProductModel.code) == raw)
                .first()
            )
            if existing:
                raise ConflictError(
                    f"Product code '{raw}' already exists. Choose a unique code."
                )
            product = ProductModel(
                name=data.name.strip(),
                code=raw,
                description=data.description,
                status=status_value,
            )
            self.db.add(product)
            try:
                self.db.commit()
            except IntegrityError as exc:
                self.db.rollback()
                raise ConflictError(
                    f"Product code '{raw}' already exists. Choose a unique code."
                ) from exc
            self.db.refresh(product)
            return product

        product = self.get_product(data.id)
        # AC6 — product identity (code) is immutable once registered
        if data.code is not None and data.code.strip().upper() != product.code.upper():
            raise ValidationAppError("Product code cannot be changed")
        product.name = data.name.strip()
        if data.description is not None:
            product.description = data.description
        product.status = status_value
        self.db.commit()
        self.db.refresh(product)
        return product

    def delete_product(self, product_id: int, *, actor: UserModel, source: str | None = None) -> dict:
        product = self.get_product(product_id)
        snapshot = snapshot_model(product)
        related = []
        entitlements = (
            self.db.query(OrganizationProductEntitlementModel)
            .filter(OrganizationProductEntitlementModel.product_id == product_id)
            .all()
        )
        for row in entitlements:
            related.append(
                {
                    "entity_type": "organization_entitlement",
                    "id": str(row.id),
                    "label": f"org:{row.organization_id}",
                }
            )
            self.db.delete(row)
        assignments = (
            self.db.query(UserProductAssignmentModel)
            .filter(UserProductAssignmentModel.product_id == product_id)
            .all()
        )
        for row in assignments:
            related.append(
                {
                    "entity_type": "user_product_assignment",
                    "id": str(row.id),
                    "label": str(row.user_id),
                }
            )
            self.db.delete(row)
        record_deletion(
            self.db,
            actor=actor,
            module="platform",
            entity_type="product",
            entity_id=product.id,
            entity_label=f"{product.name} ({product.code})",
            source=source,
            record_snapshot=snapshot,
            related_deleted=related,
        )
        self.db.delete(product)
        self.db.commit()
        return {"message": "Product deleted"}

    # ---- Organizations ----
    def list_organizations(self) -> List[OrganizationModel]:
        return self.db.query(OrganizationModel).order_by(OrganizationModel.name).all()

    def save_organization(self, data) -> OrganizationModel:
        name = data.name.strip()
        if data.id is None:
            if self.db.query(OrganizationModel).filter(OrganizationModel.name == name).first():
                raise ConflictError("Organization already exists")
            org = OrganizationModel(name=name, is_internal=data.is_internal)
            self.db.add(org)
        else:
            org = self.db.query(OrganizationModel).filter(OrganizationModel.id == data.id).first()
            if not org:
                raise NotFoundError("Organization not found")
            clash = (
                self.db.query(OrganizationModel)
                .filter(OrganizationModel.name == name, OrganizationModel.id != data.id)
                .first()
            )
            if clash:
                raise ConflictError("Organization already exists")
            org.name = name
            org.is_internal = data.is_internal
        self.db.commit()
        self.db.refresh(org)
        return org

    # ---- Organization ↔ Product (link) ----
    def list_product_access(
        self,
        search: Optional[str] = None,
        product_id: Optional[int] = None,
        access_status: Optional[str] = None,
    ) -> List[dict]:
        orgs = self.db.query(OrganizationModel).order_by(OrganizationModel.name).all()
        products = self.db.query(ProductModel).order_by(ProductModel.name).all()
        if product_id:
            products = [p for p in products if p.id == product_id]

        links = {
            (e.organization_id, e.product_id): e
            for e in self.db.query(OrganizationProductEntitlementModel).all()
        }

        rows: List[dict] = []
        for org in orgs:
            if search and search.lower() not in org.name.lower():
                continue
            for product in products:
                ent = links.get((org.id, product.id))
                status = ent.status if ent else EntitlementStatus.REVOKED.value
                if access_status and status != access_status:
                    continue
                rows.append(
                    {
                        "entitlement_id": ent.id if ent else None,
                        "organization_id": org.id,
                        "organization_name": org.name,
                        "product_id": product.id,
                        "product_name": product.name,
                        "product_code": product.code,
                        "access_status": status,
                    }
                )
        return rows

    def grant_access(self, organization_id: int, product_id: int) -> dict:
        org = self.db.query(OrganizationModel).filter(OrganizationModel.id == organization_id).first()
        product = self.db.query(ProductModel).filter(ProductModel.id == product_id).first()
        if not org or not product:
            raise NotFoundError("Organization or product not found")

        now = datetime.now(timezone.utc)
        row = (
            self.db.query(OrganizationProductEntitlementModel)
            .filter(
                OrganizationProductEntitlementModel.organization_id == organization_id,
                OrganizationProductEntitlementModel.product_id == product_id,
            )
            .first()
        )
        if row:
            row.status = EntitlementStatus.GRANTED.value
            row.granted_at = now
            row.revoked_at = None
        else:
            row = OrganizationProductEntitlementModel(
                organization_id=organization_id,
                product_id=product_id,
                status=EntitlementStatus.GRANTED.value,
                granted_at=now,
            )
            self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return {
            "entitlement_id": row.id,
            "organization_id": org.id,
            "organization_name": org.name,
            "product_id": product.id,
            "product_name": product.name,
            "product_code": product.code,
            "access_status": row.status,
        }

    def revoke_access(self, organization_id: int, product_id: int) -> dict:
        org = self.db.query(OrganizationModel).filter(OrganizationModel.id == organization_id).first()
        product = self.db.query(ProductModel).filter(ProductModel.id == product_id).first()
        if not org or not product:
            raise NotFoundError("Organization or product not found")

        now = datetime.now(timezone.utc)
        row = (
            self.db.query(OrganizationProductEntitlementModel)
            .filter(
                OrganizationProductEntitlementModel.organization_id == organization_id,
                OrganizationProductEntitlementModel.product_id == product_id,
            )
            .first()
        )
        if not row:
            row = OrganizationProductEntitlementModel(
                organization_id=organization_id,
                product_id=product_id,
                status=EntitlementStatus.REVOKED.value,
                revoked_at=now,
            )
            self.db.add(row)
        else:
            row.status = EntitlementStatus.REVOKED.value
            row.revoked_at = now
        self.db.commit()
        return {
            "entitlement_id": row.id,
            "organization_id": org.id,
            "organization_name": org.name,
            "product_id": product.id,
            "product_name": product.name,
            "product_code": product.code,
            "access_status": EntitlementStatus.REVOKED.value,
        }

    # ---- Users (People API) ----
    def _user_response(self, user: UserModel, activation_link: str | None = None) -> dict:
        effective_ids = {p.id for p in get_effective_products(self.db, user)}
        assigned = []
        for link in user.product_links:
            product = link.product or self.db.query(ProductModel).filter(
                ProductModel.id == link.product_id
            ).first()
            if not product:
                continue
            assigned.append(
                {
                    "id": product.id,
                    "name": product.name,
                    "code": product.code,
                    "effectively_entitled": product.id in effective_ids,
                }
            )
        return {
            "id": user.id,
            "full_name": user.full_name,
            "email": user.email,
            "organization_id": user.organization_id,
            "organization_name": user.organization.name if user.organization else "",
            "role": user.role_code,
            "status": user.status,
            "assigned_products": assigned,
            "created_at": user.created_at,
            "activation_link": activation_link,
        }

    def _load_user(self, user_id: UUID) -> UserModel | None:
        return (
            self.db.query(UserModel)
            .options(
                joinedload(UserModel.organization),
                joinedload(UserModel.role),
                joinedload(UserModel.product_links).joinedload(UserProductAssignmentModel.product),
            )
            .filter(UserModel.id == user_id)
            .first()
        )

    def list_people(self, search: Optional[str] = None, organization_id: Optional[int] = None) -> List[dict]:
        query = self.db.query(UserModel).options(
            joinedload(UserModel.organization),
            joinedload(UserModel.role),
            joinedload(UserModel.product_links).joinedload(UserProductAssignmentModel.product),
        )
        if organization_id:
            query = query.filter(UserModel.organization_id == organization_id)
        if search:
            like = f"%{search.lower()}%"
            query = query.filter(
                or_(UserModel.full_name.ilike(like), UserModel.email.ilike(like))
            )
        return [self._user_response(u) for u in query.order_by(UserModel.full_name).all()]

    def save_person(self, data) -> dict:
        org = self.db.query(OrganizationModel).filter(OrganizationModel.id == data.organization_id).first()
        if not org:
            raise NotFoundError("Organization not found")
        user_role = self._role_by_code(PlatformRole.PLATFORM_USER.value)

        if data.id is None:
            email = data.email.lower()
            if self.db.query(UserModel).filter(UserModel.email == email).first():
                raise ConflictError("A user with this email already exists")

            user = UserModel(
                full_name=data.full_name.strip(),
                email=email,
                organization_id=org.id,
                role_id=user_role.id,
                status=UserStatus.INVITED.value,
                password_hash=None,
            )
            self.db.add(user)
            self.db.flush()

            for product_id in data.product_ids:
                product = self.db.query(ProductModel).filter(ProductModel.id == product_id).first()
                if not product:
                    raise NotFoundError(f"Product {product_id} not found")
                self.db.add(UserProductAssignmentModel(user_id=user.id, product_id=product.id))
                from app.modules.payflow.membership import on_payflow_product_assigned

                on_payflow_product_assigned(self.db, user.id, product.code)

            self.db.commit()
            user = self._load_user(user.id)
            link = IdentityService(self.db).send_activation_invite(user)
            return self._user_response(user, activation_link=link)

        user = self._load_user(data.id)
        if not user:
            raise NotFoundError("User not found")
        if user.role_code == PlatformRole.PLATFORM_SUPER_ADMIN.value and data.status == UserStatus.DISABLED:
            raise ValidationAppError("Cannot disable the Platform Super Admin")

        user.full_name = data.full_name.strip()
        user.organization_id = org.id
        if data.status is not None:
            user.status = data.status.value if hasattr(data.status, "value") else data.status

        if data.product_ids is not None:
            existing = {
                a.product_id: a
                for a in self.db.query(UserProductAssignmentModel).filter(UserProductAssignmentModel.user_id == user.id).all()
            }
            desired = set(data.product_ids)
            for product_id in desired - set(existing):
                product = self.db.query(ProductModel).filter(ProductModel.id == product_id).first()
                if not product:
                    raise NotFoundError(f"Product {product_id} not found")
                self.db.add(UserProductAssignmentModel(user_id=user.id, product_id=product.id))
                from app.modules.payflow.membership import on_payflow_product_assigned

                on_payflow_product_assigned(self.db, user.id, product.code)
            for product_id, row in existing.items():
                if product_id not in desired:
                    self.db.delete(row)

        self.db.commit()
        return self._user_response(self._load_user(user.id))

    def delete_person(self, user_id: UUID, *, actor: UserModel, source: str | None = None) -> dict:
        user = self._load_user(user_id)
        if not user:
            raise NotFoundError("User not found")
        snapshot = snapshot_model(user)
        snapshot["organization_name"] = user.organization.name if user.organization else None
        snapshot["assigned_products"] = [
            {"id": link.product_id, "code": link.product.code if link.product else None}
            for link in user.product_links
        ]
        related = purge_user_identity(self.db, user, actor=actor)
        record_deletion(
            self.db,
            actor=actor,
            module="platform",
            entity_type="person",
            entity_id=user_id,
            entity_label=f"{snapshot.get('full_name')} ({snapshot.get('email')})",
            source=source,
            record_snapshot=snapshot,
            related_deleted=related,
        )
        self.db.commit()
        return {"message": "Person deleted"}

    def assign_product(self, user_id: UUID, product_id: int) -> dict:
        user = self._load_user(user_id)
        if not user:
            raise NotFoundError("User not found")
        product = self.db.query(ProductModel).filter(ProductModel.id == product_id).first()
        if not product:
            raise NotFoundError("Product not found")

        exists = (
            self.db.query(UserProductAssignmentModel)
            .filter(UserProductAssignmentModel.user_id == user_id, UserProductAssignmentModel.product_id == product_id)
            .first()
        )
        if not exists:
            self.db.add(UserProductAssignmentModel(user_id=user_id, product_id=product_id))
            from app.modules.payflow.membership import on_payflow_product_assigned

            on_payflow_product_assigned(self.db, user_id, product.code)
            self.db.commit()
        return self._user_response(self._load_user(user_id))

    def remove_product(self, user_id: UUID, product_id: int) -> dict:
        user = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        if not user:
            raise NotFoundError("User not found")
        row = (
            self.db.query(UserProductAssignmentModel)
            .filter(UserProductAssignmentModel.user_id == user_id, UserProductAssignmentModel.product_id == product_id)
            .first()
        )
        if row:
            self.db.delete(row)
            self.db.commit()
        return self._user_response(self._load_user(user_id))

    def resend_invite(self, user_id: UUID) -> dict:
        user = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        if not user:
            raise NotFoundError("User not found")
        if user.status == UserStatus.ACTIVE.value:
            raise ValidationAppError("User is already active")
        link = IdentityService(self.db).send_activation_invite(user)
        return {"message": "Invitation sent", "activation_link": link}

    def list_email_logs(
        self,
        actor_email: str,
        search: Optional[str] = None,
        email_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        if (actor_email or "").lower() != settings.SEED_SUPER_ADMIN_EMAIL.lower():
            raise ForbiddenError("Email Logs are only available to the suite admin")

        from app.infrastructure.database.models import EmailLogModel

        query = self.db.query(EmailLogModel)
        if email_type:
            query = query.filter(EmailLogModel.email_type == email_type)
        if search:
            like = f"%{search.lower()}%"
            query = query.filter(
                or_(
                    EmailLogModel.to_email.ilike(like),
                    EmailLogModel.subject.ilike(like),
                )
            )
        rows = query.order_by(EmailLogModel.id.desc()).limit(min(limit, 500)).all()
        return [
            {
                "id": r.id,
                "to_email": r.to_email,
                "subject": r.subject,
                "body": r.body,
                "email_type": r.email_type,
                "action_link": r.action_link,
                "related_user_id": r.related_user_id,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    def list_deletion_logs(
        self,
        search: Optional[str] = None,
        entity_type: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict]:
        return list_deletion_logs(
            self.db,
            search=search,
            entity_type=entity_type,
            limit=limit,
        )

    def enter_product(self, user: UserModel, code: str) -> dict:
        product = self.get_product_by_code(code)
        if product.status != ProductStatus.ACTIVE.value:
            raise ForbiddenError("Product is not active")
        if not has_product_access(self.db, user, code):
            raise ForbiddenError("Product access unavailable for your organization or account")
        return {"product": product, "message": "Product access granted"}
