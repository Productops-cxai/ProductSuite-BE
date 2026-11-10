"""Small in-process request throttle for the endpoints an attacker can hammer.

Sign-in, password reset and the public token pages accept guesses from anyone,
so each client address gets a fixed number of tries per window. Counters live
in this process only: with several API processes each one counts on its own,
which still bounds brute force to (limit x processes). A shared store (Redis)
is the next step when the API is scaled out.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# bucket -> (requests allowed, per seconds)
LIMITS: dict[str, tuple[int, int]] = {
    "login": (10, 60),
    "password_reset": (5, 60),
    "auth_token": (20, 60),
    # The payment page polls while a payment settles, so this one is generous.
    "public_token": (120, 60),
}

_AUTH_TOKEN_PATHS = (
    "/auth/activation/",
    "/auth/password-reset/",
    "/auth/activate",
    "/auth/reset-password",
)

_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def bucket_for(method: str, path: str) -> str | None:
    """Which limit applies to this request, if any."""
    if path == "/auth/login" and method == "POST":
        return "login"
    if path == "/auth/forgot-password" and method == "POST":
        return "password_reset"
    if path.startswith(_AUTH_TOKEN_PATHS):
        return "auth_token"
    if path.startswith("/payflow/public/"):
        return "public_token"
    return None


def check(bucket: str, client: str) -> int | None:
    """Count this request. Returns seconds to wait when the limit is used up."""
    limit, window = LIMITS[bucket]
    now = time.monotonic()
    with _lock:
        hits = _hits[(bucket, client)]
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) >= limit:
            return max(1, int(window - (now - hits[0])) + 1)
        hits.append(now)
        if len(_hits) > 50_000:  # forget idle clients so the table cannot grow forever
            for key in [k for k, v in _hits.items() if not v or now - v[-1] >= 3600]:
                del _hits[key]
    return None


def reset() -> None:
    """Forget all counters (tests)."""
    with _lock:
        _hits.clear()
