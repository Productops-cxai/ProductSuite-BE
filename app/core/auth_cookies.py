"""HttpOnly session cookies for browser auth (access + refresh JWTs)."""

from __future__ import annotations

from fastapi import Response

from app.core.config import settings

ACCESS_COOKIE = "ps_access"
REFRESH_COOKIE = "ps_refresh"


def _is_prod() -> bool:
    return (settings.APP_ENV or "").strip().lower() == "production"


def cookie_secure() -> bool:
    if settings.COOKIE_SECURE is not None:
        return bool(settings.COOKIE_SECURE)
    return _is_prod()


def cookie_samesite() -> str:
    raw = (settings.COOKIE_SAMESITE or "lax").strip().lower()
    if raw in {"lax", "strict", "none"}:
        return raw
    return "lax"


def cookie_domain() -> str | None:
    domain = (settings.COOKIE_DOMAIN or "").strip()
    return domain or None


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    secure = cookie_secure()
    samesite = cookie_samesite()
    # Browsers require Secure when SameSite=None.
    if samesite == "none":
        secure = True
    domain = cookie_domain()
    common = {
        "httponly": True,
        "secure": secure,
        "samesite": samesite,
        "path": "/",
    }
    if domain:
        common["domain"] = domain

    response.set_cookie(
        key=ACCESS_COOKIE,
        value=access_token,
        max_age=max(60, int(settings.JWT_ACCESS_EXPIRE_MINUTES) * 60),
        **common,
    )
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=refresh_token,
        max_age=max(3600, int(settings.JWT_REFRESH_EXPIRE_DAYS) * 86400),
        **common,
    )


def clear_auth_cookies(response: Response) -> None:
    secure = cookie_secure()
    samesite = cookie_samesite()
    if samesite == "none":
        secure = True
    domain = cookie_domain()
    common = {
        "httponly": True,
        "secure": secure,
        "samesite": samesite,
        "path": "/",
    }
    if domain:
        common["domain"] = domain
    response.delete_cookie(key=ACCESS_COOKIE, **common)
    response.delete_cookie(key=REFRESH_COOKIE, **common)


def tokens_for_json_body(access: str, refresh: str) -> tuple[str, str]:
    """Omit JWTs from JSON in production so browser clients never persist them."""
    if _is_prod():
        return "", ""
    return access, refresh
