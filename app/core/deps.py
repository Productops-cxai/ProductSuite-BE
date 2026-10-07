from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import safe_decode_token
from app.infrastructure.database.models import TokenDenylistModel, UserModel, UserSessionModel
from app.infrastructure.database.session import get_db
from app.modules.identity.session_messages import (
    SESSION_ENDED_MESSAGE,
    SESSION_REPLACED_MESSAGE,
)
from app.modules.platform.services.entitlement_service import has_product_access, load_user
from app.shared.enums import PlatformRole, UserStatus

bearer_scheme = HTTPBearer(auto_error=False)


def _auth_detail(message: str, code: str = "unauthorized") -> dict:
    return {"code": code, "message": message}


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> UserModel:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    # If user pasted "Bearer eyJ..." into Swagger value box, strip the extra word
    raw_token = credentials.credentials.strip()
    if raw_token.lower().startswith("bearer "):
        raw_token = raw_token[7:].strip()

    payload = safe_decode_token(raw_token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token. Use access_token from /auth/login (not refresh_token).",
        )

    jti = payload.get("jti")
    if jti:
        denied = (
            db.query(TokenDenylistModel)
            .filter(
                TokenDenylistModel.jti == jti,
                TokenDenylistModel.expires_at > datetime.now(timezone.utc),
            )
            .first()
        )
        if denied:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token revoked")

    sid = payload.get("sid")
    if not sid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_auth_detail(SESSION_ENDED_MESSAGE, "session_ended"),
        )
    try:
        session_id = UUID(str(sid))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_auth_detail(SESSION_ENDED_MESSAGE, "session_ended"),
        ) from exc

    session = db.query(UserSessionModel).filter(UserSessionModel.id == session_id).first()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_auth_detail(SESSION_ENDED_MESSAGE, "session_ended"),
        )
    if session.revoked_at is not None:
        if session.revoke_reason == "replaced":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_auth_detail(SESSION_REPLACED_MESSAGE, "session_replaced"),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_auth_detail(SESSION_ENDED_MESSAGE, "session_ended"),
        )

    try:
        user_id = UUID(str(payload.get("sub")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject") from exc

    if session.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_auth_detail(SESSION_ENDED_MESSAGE, "session_ended"),
        )

    user = load_user(db, user_id)
    if not user or user.status != UserStatus.ACTIVE.value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not active")
    return user


def require_platform_super_admin(
    user: Annotated[UserModel, Depends(get_current_user)],
) -> UserModel:
    if user.role_code != PlatformRole.PLATFORM_SUPER_ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform Super Admin required")
    return user


def require_product_access(product_code: str):
    def _dependency(
        user: Annotated[UserModel, Depends(get_current_user)],
        db: Annotated[Session, Depends(get_db)],
    ) -> UserModel:
        if not has_product_access(db, user, product_code):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Product access unavailable for your organization or account",
            )
        return user

    return _dependency


# Aliases for older imports
get_current_person = get_current_user
CurrentPerson = Annotated[UserModel, Depends(get_current_user)]
CurrentUser = Annotated[UserModel, Depends(get_current_user)]
SuperAdmin = Annotated[UserModel, Depends(require_platform_super_admin)]
DbSession = Annotated[Session, Depends(get_db)]
