"""Shared auth session end messages (used by deps + identity service)."""

SESSION_REPLACED_MESSAGE = (
    "Your account was signed in from another location. "
    "This session has ended. Please sign in again."
)
SESSION_ENDED_MESSAGE = "Your session has ended. Please sign in again."
# Explicit Sign out — FE must not show this as a forced kick / "another location".
SESSION_LOGGED_OUT_MESSAGE = "Signed out."
SESSION_LOGGED_OUT_CODE = "logged_out"
