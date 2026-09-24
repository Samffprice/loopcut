"""Configuration. Secrets never live in code.

Lookup order per key: process environment, then Loopcut's page in Blender's preferences
(settings.py; the key itself is kept by credentials.py), then the .env file named by
LOOPCUT_ENV_FILE, then a .env found by walking up from this package (dev checkouts).

Preferences belong to Blender's main thread: load() is called there (agent.send) and the result
handed to the worker.
"""

import os
from dataclasses import dataclass
from pathlib import Path

SERVICE_URL = os.environ.get("LOOPCUT_SERVICE_URL", "https://loopcut.org").rstrip("/")


def pricing_url(reason: str = "") -> str:
    """The plans page, marked as coming from the app so it says Blender is waiting."""
    return f"{SERVICE_URL}/pricing?from=app" + (f"&reason={reason}" if reason else "")


def account_url() -> str:
    return f"{SERVICE_URL}/account"


# Tokens per request, the system prompt and tool schemas (about 7k) included, before the current
# turn's older steps are folded early and then the earlier conversation is summarized (context.py).
# A 25-step turn sent whole is about 30k, so at this size a turn is almost never cut while it runs.
DEFAULT_CONTEXT_BUDGET = 40_000
MIN_CONTEXT_BUDGET, MAX_CONTEXT_BUDGET = 16_000, 2_000_000


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str
    auto_run: bool
    auto_look: bool  # Capture the viewport after every step that changes the scene; see agent._auto_look.
    max_steps: int
    context_budget: int  # Tokens one request may carry before old tool results are cut; see context.py.


def _parse_env_file(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _find_env_file() -> Path | None:
    explicit = os.environ.get("LOOPCUT_ENV_FILE")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"LOOPCUT_ENV_FILE points to a missing file: {path}")
        return path
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    return None


def _as_bool(value: str, key: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("", "0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be a boolean, got {value!r}")


def _preference_values() -> dict[str, str]:
    try:
        from . import settings
    except (ImportError, AttributeError):  # Outside Blender (tests, harness/evals/run.py).
        return {}
    return settings.values()


def _getter():
    env_file = _find_env_file()
    file_values = _parse_env_file(env_file) if env_file else {}
    preference_values = _preference_values()

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or preference_values.get(key) or file_values.get(key) or default
    return env_file, get


def checkpoint_budget_mb() -> int:
    """Disk budget for one conversation's checkpoints. Readable without an API key."""
    _, get = _getter()
    value = get("LOOPCUT_CHECKPOINT_BUDGET_MB", "2048")
    if not value.isdigit() or not 1 <= int(value) <= 1_000_000:
        raise ConfigError(f"LOOPCUT_CHECKPOINT_BUDGET_MB must be 1-1000000, got {value!r}")
    return int(value)


def run_timeout() -> float:
    """Seconds one run_python call may take before it is stopped. Readable without an API key."""
    _, get = _getter()
    value = get("LOOPCUT_RUN_TIMEOUT", "60")
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    if not 1 <= seconds <= 3600:
        raise ConfigError(f"LOOPCUT_RUN_TIMEOUT must be 1-3600 seconds, got {value!r}")
    return seconds


def load() -> Config:
    env_file, get = _getter()

    api_key = get("LOOPCUT_API_KEY")
    base_url = get("LOOPCUT_BASE_URL").rstrip("/")
    model = get("LOOPCUT_MODEL")
    missing = [k for k, v in (("LOOPCUT_API_KEY", api_key), ("LOOPCUT_BASE_URL", base_url),
                              ("LOOPCUT_MODEL", model)) if not v]
    if missing:
        raise ConfigError("No API key yet. Add one in Preferences > Add-ons > Loopcut."
                          if missing == ["LOOPCUT_API_KEY"] or len(missing) == 3 else
                          f"Missing {', '.join(missing)} (looked in Loopcut's preferences and "
                          f"{env_file or 'the environment'})")
    if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
        raise ConfigError("LOOPCUT_BASE_URL must be https (http is allowed only for 127.0.0.1)")

    max_steps = get("LOOPCUT_MAX_STEPS", "25")
    if not max_steps.isdigit() or not 1 <= int(max_steps) <= 200:
        raise ConfigError(f"LOOPCUT_MAX_STEPS must be 1-200, got {max_steps!r}")
    context_budget = get("LOOPCUT_CONTEXT_BUDGET", str(DEFAULT_CONTEXT_BUDGET))
    if not context_budget.isdigit() or not MIN_CONTEXT_BUDGET <= int(context_budget) <= MAX_CONTEXT_BUDGET:
        raise ConfigError(f"LOOPCUT_CONTEXT_BUDGET must be {MIN_CONTEXT_BUDGET}-{MAX_CONTEXT_BUDGET} tokens, "
                          f"got {context_budget!r}")

    return Config(
        api_key=api_key,
        base_url=base_url,
        model=model,
        reasoning_effort=get("LOOPCUT_REASONING_EFFORT", "medium"),
        # Model-written code only runs unprompted if the user opts in.
        auto_run=_as_bool(get("LOOPCUT_AUTO_RUN", "false"), "LOOPCUT_AUTO_RUN"),
        auto_look=_as_bool(get("LOOPCUT_AUTO_LOOK", "true"), "LOOPCUT_AUTO_LOOK"),
        max_steps=int(max_steps),
        context_budget=int(context_budget),
    )
