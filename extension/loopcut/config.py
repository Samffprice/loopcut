"""Configuration, read from the environment or a .env file. Secrets never live in code.

Lookup order per key: process environment, then the .env file named by
LOOPCUT_ENV_FILE, then a .env found by walking up from this package (dev checkouts).
"""

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str
    auto_run: bool
    max_steps: int


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


def _getter():
    env_file = _find_env_file()
    file_values = _parse_env_file(env_file) if env_file else {}

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_values.get(key) or default
    return env_file, get


def checkpoint_budget_mb() -> int:
    """Disk budget for one conversation's checkpoints. Readable without an API key."""
    _, get = _getter()
    value = get("LOOPCUT_CHECKPOINT_BUDGET_MB", "2048")
    if not value.isdigit() or not 1 <= int(value) <= 1_000_000:
        raise ConfigError(f"LOOPCUT_CHECKPOINT_BUDGET_MB must be 1-1000000, got {value!r}")
    return int(value)


def load() -> Config:
    env_file, get = _getter()

    api_key = get("LOOPCUT_API_KEY")
    base_url = get("LOOPCUT_BASE_URL").rstrip("/")
    model = get("LOOPCUT_MODEL")
    missing = [k for k, v in (("LOOPCUT_API_KEY", api_key), ("LOOPCUT_BASE_URL", base_url),
                              ("LOOPCUT_MODEL", model)) if not v]
    if missing:
        where = env_file or "the environment"
        raise ConfigError(f"Missing {', '.join(missing)} (looked in {where})")
    if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
        raise ConfigError("LOOPCUT_BASE_URL must be https (http is allowed only for 127.0.0.1)")

    max_steps = get("LOOPCUT_MAX_STEPS", "25")
    if not max_steps.isdigit() or not 1 <= int(max_steps) <= 200:
        raise ConfigError(f"LOOPCUT_MAX_STEPS must be 1-200, got {max_steps!r}")

    return Config(
        api_key=api_key,
        base_url=base_url,
        model=model,
        reasoning_effort=get("LOOPCUT_REASONING_EFFORT", "medium"),
        # Model-written code only runs unprompted if the user opts in.
        auto_run=_as_bool(get("LOOPCUT_AUTO_RUN", "false"), "LOOPCUT_AUTO_RUN"),
        max_steps=int(max_steps),
    )
