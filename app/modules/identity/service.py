import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Tuple
from uuid import UUID, uuid4

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
    UserSessionModel,
)
from app.infrastructure.email.service import send_email
from app.modules.identity.session_messages import (
    SESSION_ENDED_MESSAGE,
    SESSION_REPLACED_MESSAGE,
)
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
        "avatar_url": user.avatar_url,
    }


def _revoke_user_sessions(
    db: Session,
    user_id,
    *,
    reason: str,
    except_session_id=None,
) -> None:
    now = datetime.now(timezone.utc)
    session_q = db.query(UserSessionModel).filter(
        UserSessionModel.user_id == user_id,
        UserSessionModel.revoked_at.is_(None),
    )
    if except_session_id is not None:
        session_q = session_q.filter(UserSessionModel.id != except_session_id)
    for session in session_q.all():
        session.revoked_at = now
        session.revoke_reason = reason

    token_q = db.query(RefreshTokenModel).filter(
        RefreshTokenModel.user_id == user_id,
        RefreshTokenModel.revoked_at.is_(None),
    )
    if except_session_id is not None:
        token_q = token_q.filter(
            (RefreshTokenModel.session_id.is_(None))
            | (RefreshTokenModel.session_id != except_session_id)
        )
    for token in token_q.all():
        token.revoked_at = now


def issue_tokens(
    db: Session,
    user: UserModel,
    *,
    session: UserSessionModel | None = None,
    replace_existing: bool = False,
) -> Tuple[str, str]:
    now = datetime.now(timezone.utc)
    if replace_existing:
        _revoke_user_sessions(db, user.id, reason="replaced")

    access_jti = str(uuid4())
    refresh_jti = str(uuid4())

    if session is None:
        session = UserSessionModel(user_id=user.id, refresh_jti=refresh_jti)
        db.add(session)
        db.flush()
    else:
        session.refresh_jti = refresh_jti

    sid = str(session.id)
    access = create_access_token(
        subject=str(user.id),
        claims={
            "jti": access_jti,
            "sid": sid,
            "email": user.email,
            "role": user.role_code,
            "org_id": str(user.organization_id),
        },
    )
    refresh = create_refresh_token(
        subject=str(user.id),
        jti=refresh_jti,
        session_id=sid,
    )
    db.add(
        RefreshTokenModel(
            user_id=user.id,
            session_id=session.id,
            jti=refresh_jti,
            expires_at=now + timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS),
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
        access, refresh = issue_tokens(self.db, user, replace_existing=True)
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

    def update_profile(self, user: UserModel, full_name: str) -> dict:
        name = (full_name or "").strip()
        if not name:
            raise ValidationAppError("Full name is required")
        if len(name) > 255:
            raise ValidationAppError("Full name must be 255 characters or fewer")

        user.full_name = name
        self.db.commit()
        self.db.refresh(user)
        if user.organization is None:
            user = (
                self.db.query(UserModel)
                .options(joinedload(UserModel.organization))
                .filter(UserModel.id == user.id)
                .first()
            ) or user
        return self.me(user)

    def update_avatar(self, user: UserModel, *, file_bytes: bytes, content_type: str | None, filename: str | None) -> dict:
        from pathlib import Path

        allowed = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        ext = None
        if content_type and content_type.lower() in allowed:
            ext = allowed[content_type.lower()]
        elif filename:
            lower = filename.lower()
            if lower.endswith(".jpg") or lower.endswith(".jpeg"):
                ext = ".jpg"
            elif lower.endswith(".png"):
                ext = ".png"
            elif lower.endswith(".webp"):
                ext = ".webp"
            elif lower.endswith(".gif"):
                ext = ".gif"
        if not ext:
            raise ValidationAppError("Upload a JPG, PNG, WEBP or GIF image.")
        if len(file_bytes) > 2 * 1024 * 1024:
            raise ValidationAppError("Photo must be 2 MB or smaller.")

        project_root = Path(__file__).resolve().parents[3]
        upload_root = project_root / "uploads" / "avatars"
        upload_root.mkdir(parents=True, exist_ok=True)

        # Clear prior avatars for this user (any extension / cache-busting name).
        for old in upload_root.glob(f"{user.id}*"):
            try:
                old.unlink()
            except OSError:
                pass

        import time

        dest_name = f"{user.id}-{int(time.time())}{ext}"
        dest = upload_root / dest_name
        dest.write_bytes(file_bytes)
        user.avatar_url = f"/uploads/avatars/{dest_name}"
        self.db.commit()
        self.db.refresh(user)
        if user.organization is None:
            user = (
                self.db.query(UserModel)
                .options(joinedload(UserModel.organization), joinedload(UserModel.role))
                .filter(UserModel.id == user.id)
                .first()
            ) or user
        return self.me(user)

    def remove_avatar(self, user: UserModel) -> dict:
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[3]
        upload_root = project_root / "uploads" / "avatars"
        for old in upload_root.glob(f"{user.id}*"):
            try:
                old.unlink()
            except OSError:
                pass
        user.avatar_url = None
        self.db.commit()
        self.db.refresh(user)
        if user.organization is None:
            user = (
                self.db.query(UserModel)
                .options(joinedload(UserModel.organization), joinedload(UserModel.role))
                .filter(UserModel.id == user.id)
                .first()
            ) or user
        return self.me(user)

    def change_password(
        self,
        user: UserModel,
        current_password: str,
        new_password: str,
        confirm_password: str,
    ) -> None:
        if not user.password_hash or not verify_password(current_password, user.password_hash):
            raise UnauthorizedError("Current password is incorrect")
        if new_password != confirm_password:
            raise ValidationAppError("New password and confirm password do not match")
        policy_error = validate_password_policy(new_password)
        if policy_error:
            raise ValidationAppError(policy_error)
        if verify_password(new_password, user.password_hash):
            raise ValidationAppError("New password must be different from the current password")

        user.password_hash = hash_password(new_password)
        self.db.commit()

    def _session_for_refresh_row(
        self, row: RefreshTokenModel | None, payload: dict
    ) -> UserSessionModel | None:
        if row and row.session_id:
            return (
                self.db.query(UserSessionModel)
                .filter(UserSessionModel.id == row.session_id)
                .first()
            )
        sid = payload.get("sid")
        if sid:
            try:
                return (
                    self.db.query(UserSessionModel)
                    .filter(UserSessionModel.id == UUID(str(sid)))
                    .first()
                )
            except (TypeError, ValueError):
                return None
        jti = payload.get("jti")
        if jti:
            return (
                self.db.query(UserSessionModel)
                .filter(UserSessionModel.refresh_jti == jti)
                .first()
            )
        return None

    def refresh(self, refresh_token: str) -> dict:
        payload = safe_decode_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise UnauthorizedError("Invalid refresh token")

        jti = payload.get("jti")
        row = (
            self.db.query(RefreshTokenModel)
            .filter(RefreshTokenModel.jti == jti)
            .first()
        )
        session = self._session_for_refresh_row(row, payload)
        now = datetime.now(timezone.utc)

        if session and session.revoked_at is not None:
            if session.revoke_reason == "replaced":
                raise UnauthorizedError(
                    SESSION_REPLACED_MESSAGE, code="session_replaced"
                )
            raise UnauthorizedError(SESSION_ENDED_MESSAGE, code="session_ended")

        if not row or row.revoked_at is not None or row.expires_at < now:
            # Login elsewhere revoked this refresh — surface a clear reason when we can.
            if row is not None:
                newer = (
                    self.db.query(UserSessionModel)
                    .filter(
                        UserSessionModel.user_id == row.user_id,
                        UserSessionModel.revoked_at.is_(None),
                    )
                    .first()
                )
                if newer is not None:
                    raise UnauthorizedError(
                        SESSION_REPLACED_MESSAGE, code="session_replaced"
                    )
            raise UnauthorizedError("Refresh token expired or revoked")

        if session is None or session.revoked_at is not None:
            raise UnauthorizedError(SESSION_ENDED_MESSAGE, code="session_ended")

        user = (
            self.db.query(UserModel)
            .options(joinedload(UserModel.role), joinedload(UserModel.organization))
            .filter(UserModel.id == row.user_id)
            .first()
        )
        if not user or user.status != UserStatus.ACTIVE.value:
            raise UnauthorizedError("User not active")

        row.revoked_at = now
        access, new_refresh = issue_tokens(self.db, user, session=session)
        return {"access_token": access, "refresh_token": new_refresh, "token_type": "bearer"}

    def logout(self, user: UserModel, access_token: str | None, refresh_token: str | None) -> None:
        now = datetime.now(timezone.utc)
        session_id = None
        access_payload = safe_decode_token(access_token) if access_token else None
        if access_payload and access_payload.get("jti"):
            exp = access_payload.get("exp")
            expires_at = (
                datetime.fromtimestamp(exp, tz=timezone.utc)
                if isinstance(exp, (int, float))
                else now + timedelta(minutes=settings.JWT_ACCESS_EXPIRE_MINUTES)
            )
            existing = (
                self.db.query(TokenDenylistModel)
                .filter(TokenDenylistModel.jti == access_payload["jti"])
                .first()
            )
            if not existing:
                self.db.add(
                    TokenDenylistModel(jti=access_payload["jti"], expires_at=expires_at)
                )
            if access_payload.get("sid"):
                try:
                    session_id = UUID(str(access_payload["sid"]))
                except (TypeError, ValueError):
                    session_id = None

        refresh_payload = safe_decode_token(refresh_token) if refresh_token else None
        if session_id is None and refresh_payload and refresh_payload.get("sid"):
            try:
                session_id = UUID(str(refresh_payload["sid"]))
            except (TypeError, ValueError):
                session_id = None

        if session_id is not None:
            session = (
                self.db.query(UserSessionModel)
                .filter(
                    UserSessionModel.id == session_id,
                    UserSessionModel.user_id == user.id,
                    UserSessionModel.revoked_at.is_(None),
                )
                .first()
            )
            if session:
                session.revoked_at = now
                session.revoke_reason = "logout"
            for token in (
                self.db.query(RefreshTokenModel)
                .filter(
                    RefreshTokenModel.session_id == session_id,
                    RefreshTokenModel.revoked_at.is_(None),
                )
                .all()
            ):
                token.revoked_at = now
        else:
            _revoke_user_sessions(self.db, user.id, reason="logout")

        if refresh_payload and refresh_payload.get("jti"):
            row = (
                self.db.query(RefreshTokenModel)
                .filter(RefreshTokenModel.jti == refresh_payload["jti"])
                .first()
            )
            if row and row.revoked_at is None:
                row.revoked_at = now

        self.db.commit()

    def create_auth_token(self, user_id, token_type: AuthTokenType, *, minutes: int) -> str:
        now = datetime.now(timezone.utc)
        # Invalidate prior unused tokens of the same type so only the latest link works.
        for prior in (
            self.db.query(AuthTokenModel)
            .filter(
                AuthTokenModel.user_id == user_id,
                AuthTokenModel.token_type == token_type.value,
                AuthTokenModel.used_at.is_(None),
            )
            .all()
        ):
            prior.used_at = now

        raw = secrets.token_urlsafe(32)
        self.db.add(
            AuthTokenModel(
                user_id=user_id,
                token_hash=_hash_token(raw),
                token_type=token_type.value,
                expires_at=now + timedelta(minutes=minutes),
            )
        )
        self.db.commit()
        return raw

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _get_valid_auth_token(self, raw: str, token_type: AuthTokenType) -> AuthTokenModel:
        row = (
            self.db.query(AuthTokenModel)
            .filter(
                AuthTokenModel.token_hash == _hash_token(raw),
                AuthTokenModel.token_type == token_type.value,
            )
            .first()
        )
        now = datetime.now(timezone.utc)
        if (
            not row
            or row.used_at is not None
            or self._as_utc(row.expires_at) < now
        ):
            raise ValidationAppError(
                "This setup link is invalid, expired, or has already been used"
            )
        return row

    def _consume_auth_token(self, row: AuthTokenModel) -> None:
        """One-time use: mark this token and any other unused same-type tokens as used."""
        now = datetime.now(timezone.utc)
        row.used_at = now
        for other in (
            self.db.query(AuthTokenModel)
            .filter(
                AuthTokenModel.user_id == row.user_id,
                AuthTokenModel.token_type == row.token_type,
                AuthTokenModel.id != row.id,
                AuthTokenModel.used_at.is_(None),
            )
            .all()
        ):
            other.used_at = now

    def preview_activation(self, token: str) -> dict:
        row = self._get_valid_auth_token(token, AuthTokenType.ACTIVATION)
        user = self.db.query(UserModel).filter(UserModel.id == row.user_id).first()
        if not user:
            raise NotFoundError("User not found")
        if user.status == UserStatus.ACTIVE.value:
            # Account already activated — never show the setup form again.
            self._consume_auth_token(row)
            self.db.commit()
            raise ValidationAppError(
                "This setup link is invalid, expired, or has already been used"
            )
        return {"email": user.email, "full_name": user.full_name}

    def preview_password_reset(self, token: str) -> dict:
        row = self._get_valid_auth_token(token, AuthTokenType.PASSWORD_RESET)
        user = self.db.query(UserModel).filter(UserModel.id == row.user_id).first()
        if not user or user.status != UserStatus.ACTIVE.value:
            raise ValidationAppError(
                "This setup link is invalid, expired, or has already been used"
            )
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
        if user.status == UserStatus.ACTIVE.value:
            self._consume_auth_token(row)
            self.db.commit()
            raise ValidationAppError(
                "This setup link is invalid, expired, or has already been used"
            )

        user.password_hash = hash_password(new_password)
        user.status = UserStatus.ACTIVE.value
        self._consume_auth_token(row)
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

        minutes = settings.PASSWORD_RESET_EXPIRE_MINUTES
        raw = self.create_auth_token(
            user.id,
            AuthTokenType.PASSWORD_RESET,
            minutes=minutes,
        )
        link = f"{settings.FRONTEND_URL}/reset-password?token={raw}"
        expiry_label = (
            f"{minutes} minute{'s' if minutes != 1 else ''}"
            if minutes < 60
            else f"{minutes // 60} hour{'s' if minutes // 60 != 1 else ''}"
        )
        send_email(
            self.db,
            to_email=user.email,
            subject="Reset your Platform Suite password",
            body=(
                f"Use this link to reset your password (expires in "
                f"{expiry_label}):\n{link}"
            ),
            email_type="password_reset",
            action_link=link,
            related_user_id=user.id,
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
        self._consume_auth_token(row)
        _revoke_user_sessions(self.db, user.id, reason="password_reset")
        self.db.commit()

    def send_activation_invite(self, user: UserModel) -> str:
        minutes = settings.ACTIVATION_TOKEN_EXPIRE_MINUTES
        raw = self.create_auth_token(
            user.id, AuthTokenType.ACTIVATION, minutes=minutes
        )
        link = f"{settings.FRONTEND_URL}/activate?token={raw}"
        expiry_label = (
            f"{minutes} minute{'s' if minutes != 1 else ''}"
            if minutes < 60
            else f"{minutes // 60} hour{'s' if minutes // 60 != 1 else ''}"
        )
        send_email(
            self.db,
            to_email=user.email,
            subject="Activate your Platform Suite account",
            body=(
                f"Hello {user.full_name},\n\nSet your password using this link "
                f"(expires in {expiry_label}):\n{link}"
            ),
            email_type="activation",
            action_link=link,
            related_user_id=user.id,
        )
        return link
