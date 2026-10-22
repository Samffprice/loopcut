"""Allowance display values. Admission remains the gateway's decision."""

import math
import time
from datetime import datetime, timezone

LIMIT_WINDOWS = {"session_limit": "session", "week_limit": "week"}


def fraction(value) -> float | None:
    """Ignore malformed fractions; cap valid over-budget reports at a full meter."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    return min(1.0, value) if math.isfinite(value) and value >= 0 else None


def used(window) -> float | None:
    """The reservation-inclusive fraction is already a total, not an increment."""
    if not isinstance(window, dict):
        return None
    reserved = fraction(window.get("used_with_reservations"))
    return reserved if reserved is not None else fraction(window.get("used"))


def _reset_time(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        reset = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return reset.replace(tzinfo=reset.tzinfo or timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def refused_window(account: dict | None, notice: dict | None, now: float | None = None) -> str | None:
    """A current refusal overrides its window until success, reset or account/plan change.

    Unknown reset times keep the refusal visible; a refresh within the same window does too.
    Success and sign-out clear the notice in state, without changing reported usage.
    """
    item = (notice or {}).get("item") or {}
    window = LIMIT_WINDOWS.get(item.get("reason"))
    if not window or item.get("resolved") or item.get("status") == "done":
        return None
    account = account or {}
    if item.get("plan") and account.get("plan") and item["plan"] != account["plan"]:
        return None
    reported = account.get(window)
    reset = _reset_time(item.get("resets_at"))
    current = _reset_time(reported.get("resets_at")) if isinstance(reported, dict) else None
    if reset is not None and (reset <= (time.time() if now is None else now)
                              or (current is not None and current > reset)):
        return None
    return window


def refused_windows(account: dict | None, notice: dict | None, now: float | None = None) -> set[str]:
    """Repeated refusals can exhaust both windows before any request succeeds."""
    notice = notice or {}
    windows = dict(notice.get("windows") or {})
    item = notice.get("item") or {}
    window = LIMIT_WINDOWS.get(item.get("reason"))
    if window:
        windows[window] = item
    return {window for item in windows.values()
            if (window := refused_window(account, {"item": item}, now)) is not None}
