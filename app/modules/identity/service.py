import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Tuple
from uuid import uuid4

from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.exceptions import NotFoundError, UnauthorizedError, ValidationAppError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    safe_decode_token,
    validate_password_policy,
    verify_password,
)
from app.infrastructure.database.models import (
    AuthTokenModel,
    ProductModel,
    RefreshTokenModel,
    TokenDenylistModel,
    UserModel,
)
from app.infrastructure.email.service import email_service
from app.modules.platform.services.entitlement_service import get_effective_products
from app.shared.enums import AuthTokenType, LoginNextStep, PlatformRole, UserStatus


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_next_step(user: UserModel, products: List[ProductModel]) -> LoginNextStep:
    if user.role_code == PlatformRole.PLATFORM_SUPER_ADMIN.value:
        return LoginNextStep.PLATFORM_ADMIN
    if len(products) == 0:
        return LoginNextStep.NO_ACCESS
    if len(products) == 1:
        return LoginNextStep.DIRECT_ENTRY
    return LoginNextStep.PRODUCT_SELECTION


def user_brief_dict(user: UserModel) -> dict:
    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role_code,
        "status": user.status,
        "organization_id": user.organization_id,
        "organization_name": user.organization.name if user.organization else None,
    }


def issue_tokens(db: Session, user: UserModel) -> Tuple[str, str]:
    access_jti = str(uuid4())
    refresh_jti = str(uuid4())
    access = create_access_token(
        subject=str(user.id),
        claims={
            "jti": access_jti,
            "email": user.email,
            "role": user.role_code,
            "org_id": str(user.organization_id),
        },
    )
    refresh = create_refresh_token(subject=str(user.id), jti=refresh_jti)
    db.add(
        RefreshTokenModel(
            user_id=user.id,
            jti=refresh_jti,
            expires_at=datetime.now(timezone.utc)
            + timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS),
        )
    )
    db.commit()
    return access, refresh


class IdentityService:
    def __init__(self, db: Session):
        self.db = db

    def login(self, email: str, password: str) -> dict:
        user = (
            self.db.query(UserModel)
            .options(joinedload(UserModel.organization), joinedload(UserModel.role))
            .filter(UserModel.email == email.lower())
            .first()
        )
        if (
            not user
            or not user.password_hash
            or user.status != UserStatus.ACTIVE.value
            or not verify_password(password, user.password_hash)
        ):
            raise UnauthorizedError("Invalid email or password")

        products = get_effective_products(self.db, user)
        access, refresh = issue_tokens(self.db, user)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "user": user_brief_dict(user),
            "products": products,
            "next_step": resolve_next_step(user, products),
        }

    def me(self, user: UserModel) -> dict:
        products = get_effective_products(self.db, user)
        return {
            "user": user_brief_dict(user),
            "products": products,
            "next_step": resolve_next_step(user, products),
        }

    def refresh(self, refresh_token: str) -> dict:
        payload = safe_decode_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise UnauthorizedError("Invalid refresh token")

        jti = payload.get("jti")
        row = (
            self.db.query(RefreshTokenModel)
            .filter(
                RefreshTokenModel.jti == jti,
                RefreshTokenModel.revoked_at.is_(None),
            )
            .first()
        )
        if not row or row.expires_at < datetime.now(timezone.utc):
            raise UnauthorizedError("Refresh token expired or revoked")

        user = (
            self.db.query(UserModel)
            .options(joinedload(UserModel.role), joinedload(UserModel.organization))
            .filter(UserModel.id == row.user_id)
            .first()
        )
        if not user or user.status != UserStatus.ACTIVE.value:
            raise UnauthorizedError("User not active")

        row.revoked_at = datetime.now(timezone.utc)
        access, new_refresh = issue_tokens(self.db, user)
        return {"access_token": access, "refresh_token": new_refresh, "token_type": "bearer"}

    def logout(self, user: UserModel, access_token: str | None, refresh_token: str | None) -> None:
        now = datetime.now(timezone.utc)
        if access_token:
            payload = safe_decode_token(access_token)
            if payload and payload.get("jti"):
                exp = payload.get("exp")
                expires_at = (
                    datetime.fromtimestamp(exp, tz=timezone.utc)
                    if isinstance(exp, (int, float))
                    else now + timedelta(minutes=settings.JWT_ACCESS_EXPIRE_MINUTES)
                )
                existing = (
                    self.db.query(TokenDenylistModel)
                    .filter(TokenDenylistModel.jti == payload["jti"])
                    .first()
                )
                if not existing:
                    self.db.add(TokenDenylistModel(jti=payload["jti"], expires_at=expires_at))

        for token in (
            self.db.query(RefreshTokenModel)
            .filter(
                RefreshTokenModel.user_id == user.id,
                RefreshTokenModel.revoked_at.is_(None),
            )
            .all()
        ):
            token.revoked_at = now

        if refresh_token:
            payload = safe_decode_token(refresh_token)
            if payload and payload.get("jti"):
                row = (
                    self.db.query(RefreshTokenModel)
                    .filter(RefreshTokenModel.jti == payload["jti"])
                    .first()
                )
                if row:
                    row.revoked_at = now

        self.db.commit()

    def create_auth_token(self, user_id, token_type: AuthTokenType, hours: int) -> str:
        raw = secrets.token_urlsafe(32)
        self.db.add(
            AuthTokenModel(
                user_id=user_id,
                token_hash=_hash_token(raw),
                token_type=token_type.value,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=hours),
            )
        )
        self.db.commit()
        return raw

    def _get_valid_auth_token(self, raw: str, token_type: AuthTokenType) -> AuthTokenModel:
        row = (
            self.db.query(AuthTokenModel)
            .filter(
                AuthTokenModel.token_hash == _hash_token(raw),
                AuthTokenModel.token_type == token_type.value,
            )
            .first()
        )
        if not row or row.used_at is not None or row.expires_at < datetime.now(timezone.utc):
            raise ValidationAppError("Setup link is invalid, expired, or already used")
        return row

    def preview_activation(self, token: str) -> dict:
        row = self._get_valid_auth_token(token, AuthTokenType.ACTIVATION)
        user = self.db.query(UserModel).filter(UserModel.id == row.user_id).first()
        if not user:
            raise NotFoundError("User not found")
        return {"email": user.email, "full_name": user.full_name}

    def activate_account(self, token: str, new_password: str, confirm_password: str) -> None:
        if new_password != confirm_password:
            raise ValidationAppError("New password and confirm password do not match")
        policy_error = validate_password_policy(new_password)
        if policy_error:
            raise ValidationAppError(policy_error)

        row = self._get_valid_auth_token(token, AuthTokenType.ACTIVATION)
        user = self.db.query(UserModel).filter(UserModel.id == row.user_id).first()
        if not user:
            raise NotFoundError("User not found")

        user.password_hash = hash_password(new_password)
        user.status = UserStatus.ACTIVE.value
        row.used_at = datetime.now(timezone.utc)
        self.db.commit()

    def forgot_password(self, email: str) -> None:
        user = (
            self.db.query(UserModel)
            .filter(
                UserModel.email == email.lower(),
                UserModel.status == UserStatus.ACTIVE.value,
            )
            .first()
        )
        if not user or not user.password_hash:
            return

        raw = self.create_auth_token(
            user.id, AuthTokenType.PASSWORD_RESET, settings.PASSWORD_RESET_EXPIRE_HOURS
        )
        link = f"{settings.FRONTEND_URL}/reset-password?token={raw}"
        email_service.send(
            user.email,
            "Reset your Platform Suite password",
            f"Use this link to reset your password (expires in "
            f"{settings.PASSWORD_RESET_EXPIRE_HOURS} hours):\n{link}",
        )

    def reset_password(self, token: str, new_password: str, confirm_password: str) -> None:
        if new_password != confirm_password:
            raise ValidationAppError("New password and confirm password do not match")
        policy_error = validate_password_policy(new_password)
        if policy_error:
            raise ValidationAppError(policy_error)

        row = self._get_valid_auth_token(token, AuthTokenType.PASSWORD_RESET)
        user = self.db.query(UserModel).filter(UserModel.id == row.user_id).first()
        if not user:
            raise NotFoundError("User not found")

        user.password_hash = hash_password(new_password)
        row.used_at = datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)
        for rt in (
            self.db.query(RefreshTokenModel)
            .filter(
                RefreshTokenModel.user_id == user.id,
                RefreshTokenModel.revoked_at.is_(None),
            )
            .all()
        ):
            rt.revoked_at = now
        self.db.commit()

    def send_activation_invite(self, user: UserModel) -> None:
        raw = self.create_auth_token(
            user.id, AuthTokenType.ACTIVATION, settings.ACTIVATION_TOKEN_EXPIRE_HOURS
        )
        link = f"{settings.FRONTEND_URL}/activate?token={raw}"
        email_service.send(
            user.email,
            "Activate your Platform Suite account",
            f"Hello {user.full_name},\n\nSet your password using this link "
            f"(expires in {settings.ACTIVATION_TOKEN_EXPIRE_HOURS} hours):\n{link}",
        )
