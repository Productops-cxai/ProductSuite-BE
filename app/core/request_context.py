"""Request-scoped frontend base URL for email action links.

When the UI is opened via localhost or ngrok, the browser sends Origin/Referer.
Activation and password-reset emails use that host so the invitee opens the same
public URL the admin was using. Falls back to settings.FRONTEND_URL.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from urllib.parse import urlsplit, urlunsplit

from app.core.config import settings

_frontend_base: ContextVar[str | None] = ContextVar("frontend_base", default=None)


def frontend_base_from_headers(
    origin: str | None,
    referer: str | None,
) -> str | None:
    for raw in (origin, referer):
        if not raw or not raw.strip():
            continue
        parts = urlsplit(raw.strip())
        if parts.scheme in ("http", "https") and parts.netloc:
            return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")
    return None


def set_frontend_base(url: str | None) -> Token:
    return _frontend_base.set(url)


def reset_frontend_base(token: Token) -> None:
    _frontend_base.reset(token)


def get_frontend_url() -> str:
    return (_frontend_base.get() or settings.FRONTEND_URL).rstrip("/")
