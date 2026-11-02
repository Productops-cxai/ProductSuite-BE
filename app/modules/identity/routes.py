from fastapi import APIRouter, Body, Depends, File, Header, Request, Response, UploadFile
from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth_cookies import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    set_auth_cookies,
    tokens_for_json_body,
)
from app.core.deps import CurrentPerson, DbSession, _extract_access_token, bearer_scheme
from app.core.exceptions import AppError
from app.modules.identity.schemas import (
    ActivateAccountRequest,
    ActivationPreviewResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    MessageResponse,
    RefreshRequest,
    ResetPasswordRequest,
    TokenResponse,
    UpdateProfileRequest,
)
from app.modules.identity.service import IdentityService

router = APIRouter(prefix="/auth", tags=["Auth"])


def _map_error(exc: AppError):
    from fastapi import HTTPException, status

    code_map = {
        "unauthorized": status.HTTP_401_UNAUTHORIZED,
        "session_replaced": status.HTTP_401_UNAUTHORIZED,
        "session_ended": status.HTTP_401_UNAUTHORIZED,
        "refresh_reused": status.HTTP_401_UNAUTHORIZED,
        "forbidden": status.HTTP_403_FORBIDDEN,
        "not_found": status.HTTP_404_NOT_FOUND,
        "conflict": status.HTTP_409_CONFLICT,
        "validation_error": status.HTTP_400_BAD_REQUEST,
    }
    status_code = code_map.get(exc.code, 400)
    if exc.code in {"session_replaced", "session_ended", "refresh_reused"}:
        return HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message},
        )
    return HTTPException(status_code=status_code, detail=exc.message)


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response, db: DbSession):
    try:
        data = IdentityService(db).login(payload.email, payload.password)
    except AppError as exc:
        raise _map_error(exc) from exc

    access = data["access_token"]
    refresh = data["refresh_token"]
    set_auth_cookies(response, access, refresh)
    body_access, body_refresh = tokens_for_json_body(access, refresh)
    return {
        **data,
        "access_token": body_access,
        "refresh_token": body_refresh,
    }


@router.get("/me", response_model=MeResponse)
def me(user: CurrentPerson, db: DbSession):
    return IdentityService(db).me(user)


@router.patch("/me", response_model=MeResponse)
def update_profile(payload: UpdateProfileRequest, user: CurrentPerson, db: DbSession):
    try:
        return IdentityService(db).update_profile(user, payload.full_name)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/me/avatar", response_model=MeResponse)
async def upload_avatar(
    user: CurrentPerson,
    db: DbSession,
    file: UploadFile = File(...),
):
    try:
        data = await file.read()
        return IdentityService(db).update_avatar(
            user,
            file_bytes=data,
            content_type=file.content_type,
            filename=file.filename,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/me/avatar/delete", response_model=MeResponse)
def delete_avatar(user: CurrentPerson, db: DbSession):
    try:
        return IdentityService(db).remove_avatar(user)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/change-password", response_model=MessageResponse)
def change_password(payload: ChangePasswordRequest, user: CurrentPerson, db: DbSession):
    try:
        IdentityService(db).change_password(
            user,
            payload.current_password,
            payload.new_password,
            payload.confirm_password,
        )
        return {"message": "Password updated successfully."}
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    response: Response,
    request: Request,
    db: DbSession,
    payload: RefreshRequest | None = Body(default=None),
):
    token = (payload.refresh_token if payload else None) or request.cookies.get(REFRESH_COOKIE)
    if not token:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        data = IdentityService(db).refresh(token)
    except AppError as exc:
        raise _map_error(exc) from exc

    access = data["access_token"]
    refresh_tok = data["refresh_token"]
    set_auth_cookies(response, access, refresh_tok)
    body_access, body_refresh = tokens_for_json_body(access, refresh_tok)
    return {
        "access_token": body_access,
        "refresh_token": body_refresh,
        "token_type": "bearer",
    }


@router.post("/logout", response_model=MessageResponse)
def logout(
    request: Request,
    response: Response,
    user: CurrentPerson,
    db: DbSession,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_refresh_token: str | None = Header(default=None, alias="X-Refresh-Token"),
):
    access = _extract_access_token(request, credentials)
    refresh = x_refresh_token or request.cookies.get(REFRESH_COOKIE)
    IdentityService(db).logout(user, access, refresh)
    clear_auth_cookies(response)
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


@router.get("/password-reset/{token}", response_model=ActivationPreviewResponse)
def password_reset_preview(token: str, db: DbSession):
    try:
        return IdentityService(db).preview_password_reset(token)
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
