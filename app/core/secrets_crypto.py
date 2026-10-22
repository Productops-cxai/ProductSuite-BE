"""Decrypt sensitive .env values with a single master key (Fernet + ``enc:``).

Encrypt secrets with the Windows tool:
``Deployment things/PayFlowEnvSecretTool`` (not a Python CLI).

Master key (``ENV_MASTER_KEY``) must live outside the dotenv file — systemd /
process environment only. Values in ``.env.production`` may look like::

    SENDGRID_API_KEY=enc:<fernet-token>

Plain values (no ``enc:`` prefix) pass through unchanged — local ``.env`` stays simple.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

ENC_PREFIX = "enc:"
MASTER_KEY_ENV = "ENV_MASTER_KEY"


class SecretsCryptoError(RuntimeError):
    pass


def _pad_urlsafe_b64(raw: str) -> bytes:
    """Accept url-safe base64 with or without ``=`` padding (WinForms may omit it)."""
    s = (raw or "").strip().encode("ascii")
    pad = (4 - (len(s) % 4)) % 4
    if pad:
        s = s + (b"=" * pad)
    return s


def _fernet_from_env() -> Fernet:
    raw = (os.environ.get(MASTER_KEY_ENV) or "").strip()
    if not raw:
        raise SecretsCryptoError(
            f"{MASTER_KEY_ENV} is not set. Export it in the process environment "
            "(do not put the master key inside the encrypted .env file)."
        )
    try:
        return Fernet(_pad_urlsafe_b64(raw))
    except Exception as exc:  # noqa: BLE001 — invalid key shape
        raise SecretsCryptoError(f"Invalid {MASTER_KEY_ENV}: {exc}") from exc


def decrypt_value(value: str, *, master_key: str | None = None) -> str:
    """Decrypt ``enc:…`` values; return plain strings unchanged."""
    if value is None:
        return value
    if not isinstance(value, str) or not value.startswith(ENC_PREFIX):
        return value
    token = _pad_urlsafe_b64(value[len(ENC_PREFIX) :])
    if master_key:
        f = Fernet(_pad_urlsafe_b64(master_key))
    else:
        f = _fernet_from_env()
    try:
        return f.decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise SecretsCryptoError(
            "Failed to decrypt an enc: value — wrong ENV_MASTER_KEY or corrupt token."
        ) from exc


def decrypt_settings_values(data: dict) -> dict:
    """Walk settings field dict and decrypt any ``enc:`` string values."""
    out = {}
    for key, val in data.items():
        if isinstance(val, str) and val.startswith(ENC_PREFIX):
            out[key] = decrypt_value(val)
        else:
            out[key] = val
    return out
