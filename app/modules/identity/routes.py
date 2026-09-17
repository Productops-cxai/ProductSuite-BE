from fastapi import APIRouter, Depends, Header
from fastapi.security import HTTPAuthorizationCredentials

from app.core.deps import CurrentPerson, DbSession, bearer_scheme
from app.core.exceptions import AppError
from app.modules.identity.schemas import (
    ActivateAccountRequest,
    ActivationPreviewResponse,
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    MessageResponse,
    RefreshRequest,
    ResetPasswordRequest,
    TokenResponse,
)
from app.modules.identity.service import IdentityService

router = APIRouter(prefix="/auth", tags=["Login"])


def _map_error(exc: AppError):
    from fastapi import HTTPException, status

    code_map = {
        "unauthorized": status.HTTP_401_UNAUTHORIZED,
        "forbidden": status.HTTP_403_FORBIDDEN,
        "not_found": status.HTTP_404_NOT_FOUND,
        "conflict": status.HTTP_409_CONFLICT,
        "validation_error": status.HTTP_400_BAD_REQUEST,
    }
    return HTTPException(status_code=code_map.get(exc.code, 400), detail=exc.message)


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: DbSession):
    try:
        return IdentityService(db).login(payload.email, payload.password)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/me", response_model=MeResponse)
def me(user: CurrentPerson, db: DbSession):
    return IdentityService(db).me(user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: DbSession):
    try:
        return IdentityService(db).refresh(payload.refresh_token)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/logout", response_model=MessageResponse)
def logout(
    user: CurrentPerson,
    db: DbSession,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_refresh_token: str | None = Header(default=None, alias="X-Refresh-Token"),
):
    access = credentials.credentials if credentials else None
    IdentityService(db).logout(user, access, x_refresh_token)
    return {"message": "Logged out successfully"}


@router.post("/forgot-password", response_model=MessageResponse)
def forgot_password(payload: ForgotPasswordRequest, db: DbSession):
    IdentityService(db).forgot_password(payload.email)
    return {"message": "If an account exists for that email, a reset link has been sent."}


@router.get("/activation/{token}", response_model=ActivationPreviewResponse)
def activation_preview(token: str, db: DbSession):
    try:
        return IdentityService(db).preview_activation(token)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/activate", response_model=MessageResponse)
def activate(payload: ActivateAccountRequest, db: DbSession):
    try:
        IdentityService(db).activate_account(
            payload.token, payload.new_password, payload.confirm_password
        )
        return {"message": "Account activated successfully. You can now sign in."}
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(payload: ResetPasswordRequest, db: DbSession):
    try:
        IdentityService(db).reset_password(
            payload.token, payload.new_password, payload.confirm_password
        )
        return {"message": "Password updated successfully. You can now sign in."}
    except AppError as exc:
        raise _map_error(exc) from exc
