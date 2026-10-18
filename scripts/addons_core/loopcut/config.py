"""Configuration. Secrets never live in code.

Lookup order per key: process environment, then Loopcut's page in Blender's preferences
(settings.py; the key itself is kept by credentials.py), then the .env file named by
LOOPCUT_ENV_FILE, then a .env found by walking up from this package (dev checkouts).

Preferences belong to Blender's main thread: load() is called there (agent.send) and the result
handed to the worker.
"""

import os
import shutil
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path

SERVICE_URL = os.environ.get("LOOPCUT_SERVICE_URL", "https://loopcut.org").rstrip("/")
# loopcut.org's beta page, which redirects to the owner's form (REPORT_BUG_URL on the server) or shows an email page.
BUG_REPORT_URL = os.environ.get("LOOPCUT_BUG_REPORT_URL", "https://loopcut.org/beta/report-a-bug")
# LOOPCUT_SERVICE_URLS: comma-separated base URLs or case-sensitive globs for the harness's local
# proxy/bridges that stand in for the gateway. Read per call; a user never needs this setting.
SERVICE_BASE_URL = f"{SERVICE_URL}/v1"  # The gateway's chat completions (account.BASE_URL).
SERVICE_WORKER_MODEL = "fast"  # On the gateway, workers run the Fast alias whatever the orchestrator runs.
ROUTER_EFFORT = "low"  # The router's reasoning effort unless LOOPCUT_ROUTER_REASONING_EFFORT names one.

# A per-call output ceiling (llm.py's max_tokens), so a runaway reasoning call cannot burn tens of
# thousands of tokens and many minutes for nothing (a competing agent caps its workers at 64k; measured
# GLM-5.3-Flash steps average 3.6k-6.8k output tokens, so 32k leaves normal high/max effort room
# while still stopping a runaway one). The router's own JSON decision needs far less.
DEFAULT_MAX_OUTPUT_TOKENS = 32_000
ROUTER_MAX_OUTPUT_TOKENS = 4_000  # The router's default unless LOOPCUT_ROUTER_MAX_OUTPUT_TOKENS names one.
MAX_OUTPUT_TOKENS_CEILING = 200_000
# An optional per-call wall-clock limit (llm.py's call_max_seconds): a stream that runs longer is
# stopped and treated like hitting the output cap (agent._request's retry). Off (0) unless
# LOOPCUT_CALL_MAX_SECONDS or a role's own names one: at high effort a GLM-5.3-Flash worker's first
# reply measured 90-300 s (a competing agent's run: up to ~820 s), so a fixed 300 s killed a working worker twice
# (eval 20260929-101508). The output cap above bounds a runaway, and llm.py's per-read timeout
# catches a stalled stream (nothing arriving). Always exempted for a base URL on 127.0.0.1
# (llm.py): the Claude Code bridge (harness/cc_bridge.py) legitimately takes minutes.
DEFAULT_CALL_MAX_SECONDS = 0
CALL_MAX_SECONDS_CEILING = 7_200
# LOOPCUT_CODEX_APP (codex_app.py): the Codex CLI the ChatGPT app ships on macOS, else `codex` on PATH.
CODEX_APP_BINARY = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
CODEX_SUMMARIES = ("auto", "concise", "detailed", "none")


def pricing_url(reason: str = "", plan: str = "") -> str:
    """The plans page, marked as coming from the app so it says Blender is waiting. reason names the moment
    that sent the user (the conversion funnel's attribution); plan marks the suggested plan."""
    return (f"{SERVICE_URL}/pricing?from=app" + (f"&reason={reason}" if reason else "")
            + (f"&plan={plan}" if plan else ""))


def credits_url(reason: str = "") -> str:
    """The account page, where credit packs are bought; from=app and a reason say which moment sent the user
    (no #fragment: cloud_http.validate_url refuses them for every link the panel opens)."""
    return f"{SERVICE_URL}/account?from=app" + (f"&reason={reason}" if reason else "")


def account_url() -> str:
    return f"{SERVICE_URL}/account"


# The "ChatGPT plan" provider: this pseudo base URL (or just "chatgpt-plan") sends requests to OpenAI's
# Responses API on the user's ChatGPT plan, signed in with ChatGPT (chatgpt_auth.py, chatgpt_responses.py,
# llm.stream_chat). No API key: the account's token is read per request, so api_key is a placeholder.
# Kept equal to chatgpt_auth's constants (test_chatgpt); not imported, as run.py loads this file alone.
CHATGPT_PLAN_BASE_URL = "chatgpt-plan://api.openai.com/v1"
CHATGPT_PLAN_KEY = "chatgpt-plan"
CHATGPT_PLAN_MODEL = "gpt-6.1-sol"
# Signed in with ChatGPT, Loopcut works as in eval test F (20261001-130553, with Sol in Codex's own loop the best
# chest yet): the single agent in its workspace (keep mode, a 200k context floor), no orchestrator, workers or
# router, no craft notes, at high effort unless one is chosen (xhigh too). See load_roles.
CHATGPT_PLAN_EFFORT = "high"
CHATGPT_PLAN_EFFORTS = ("low", "medium", "high", "xhigh")


def chatgpt_plan(base_url: str) -> bool:
    return (base_url or "").rstrip("/") == "chatgpt-plan" or (base_url or "").startswith("chatgpt-plan:")


# The "Anthropic" provider: this pseudo base URL sends requests to Anthropic's Messages API (https:// plus the
# rest, /messages) with the user's own key, through anthropic_messages.py rather than chat completions; see
# llm.stream_chat. Not https://api.anthropic.com/v1 itself, which stays free for the Custom provider (Anthropic's
# OpenAI-compatible endpoint, without prompt caching), and each keeps its own stored key. Kept equal to
# anthropic_messages.MODELS[0] (test_anthropic).
ANTHROPIC_BASE_URL = "anthropic://api.anthropic.com/v1"
ANTHROPIC_MODEL = "claude-opus-5-5"


def anthropic(base_url: str) -> bool:
    return (base_url or "").strip().startswith("anthropic://")


# Tokens per request, the system prompt and tool schemas (about 7k) included, before the current
# turn's older steps are folded early and then the earlier conversation is summarized (context.py).
# A 25-step history sent whole is about 30k; longer turns fold older steps as needed.
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
    max_steps: int  # 0 = unlimited; orchestrated workers always use 0.
    context_budget: int  # Tokens one request may carry before old tool results are cut; see context.py.
    keep_context: bool = False  # Never rewrite what a turn already sent (context.py, keep mode); per model.
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS  # llm.py's max_tokens; see agent._completion_unusable.
    call_max_seconds: float = DEFAULT_CALL_MAX_SECONDS  # llm.py's call_max_seconds; 0 = none; ignored for 127.0.0.1.


@dataclass(frozen=True)
class Roles:
    """Who does what when the orchestrator hands work to workers (orchestrator.py, waves.py).
    orchestrate False is today's single loop, the baseline. The budgets below are per job: one
    user turn's workers, whatever waves it takes."""
    orchestrator: Config
    worker: Config
    orchestrate: bool
    max_workers: int = 3          # Children running at once.
    job_max_usd: float = 0.40     # Worker dollars in all (pricing.py); 0 = no limit.
    worker_max_seconds: int = 1800  # One worker run's wall time (LOOPCUT_WAVE_MAX_SECONDS is the old name).
    # The router (router.py): a cheap call naming each turn's level and deliverables before the
    # orchestrator's first request. None = off. Its provider, model and key default to the worker's;
    # it runs only on the gateway.
    router: Config | None = None
    # LOOPCUT_WORKSPACE: the single agent works with a workspace (workspace.py), a folder and a shell as
    # in a coding agent; orchestrated, the router sends a look there (router.to_workspace). Off by default.
    workspace: bool = False
    # LOOPCUT_SINGLE_CRAFT: the craft notes (skills.single_notes) on a workspace turn's message. On by default;
    # off measures the agent without them (Sol alone in Codex, with none, painted better: eval 20261001-000435).
    single_craft: bool = True


@dataclass(frozen=True)
class CodexApp:
    """LOOPCUT_CODEX_APP: every turn is the workspace agent's, run by Codex's own agent loop (codex_app.py)
    on the user's ChatGPT plan, with Loopcut's live-scene tools as its dynamic tools. "" for model and
    effort leaves them to Codex."""
    binary: str
    model: str = ""
    effort: str = ""
    summary: str = "detailed"   # Reasoning summaries, kept in the transcript; the chat does not show them.


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


# Display only now: the " beta" after the version. Purchase surfaces follow billing_open().
BETA = _as_bool(os.environ.get("LOOPCUT_BETA", "on"), "LOOPCUT_BETA")


def billing_open() -> bool:
    """Whether purchase surfaces show: plan names, credits, upgrade and pricing links, the conversion cards.
    The server decides with the manifest's flags.billing, so turning sales on or off needs no release. A
    manifest without the flag (servers before it existed) or no manifest at all keeps them hidden."""
    from . import cloud
    runtime = cloud._runtime
    if runtime is None:
        return False
    with runtime.lock:
        return (runtime.manifest.get("flags") or {}).get("billing") is True


def app_version() -> str:
    """The add-on's plain semver (machine-facing: events, headers); version_label() is for people."""
    from . import bl_info
    return ".".join(map(str, bl_info["version"]))


def bug_report_url() -> str:
    """BUG_REPORT_URL with hidden form fields so a report can be matched to a tester: tester=<install id>,
    and acct=<account id> when signed in and the service reports one. Never an email address."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    extra = {}
    try:
        from . import account, credentials
        install_id = credentials.interest_install_id()
        if install_id:
            extra["tester"] = install_id
        info = account.status().get("account") if account.signed_in() else None
        account_id = info.get("account_id") if isinstance(info, dict) else None  # /v1/me, contract 0.6.
        if isinstance(account_id, str) and account_id.strip() and "@" not in account_id:
            extra["acct"] = account_id.strip()
    except Exception as ex:  # The link must open even when the config folder or account state is unusable.
        print(f"Loopcut: bug report link without tester fields: {ex!r}")
    parts = urlsplit(BUG_REPORT_URL)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in extra]
    return urlunsplit(parts._replace(query=urlencode(query + list(extra.items()))))


def version_label(version=None) -> str:
    """Display-only beta marker; headers and update comparisons keep plain versions."""
    if version is None:
        from . import bl_info
        version = bl_info["version"]
    return ".".join(map(str, version)) + (" beta" if BETA else "")


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


def spending():
    """The user's credit spending settings (spending.py, C47): LOOPCUT_ASK_ABOVE_CREDITS (default 25),
    LOOPCUT_CHAT_BUDGET_CREDITS (150) and LOOPCUT_ASK_EVERY_PAID_CALL (false). Readable without an API key."""
    from . import spending as policy
    _, get = _getter()
    values = {}
    for key, default, low in (("LOOPCUT_ASK_ABOVE_CREDITS", policy.ASK_ABOVE, 0),
                              ("LOOPCUT_CHAT_BUDGET_CREDITS", policy.CHAT_BUDGET, 1)):
        value = get(key, str(default))
        if not value.isascii() or not value.isdigit() or not low <= int(value) <= policy.MAX_SETTING:
            raise ConfigError(f"{key} must be {low}-{policy.MAX_SETTING} credits, got {value!r}")
        values[key] = int(value)
    return policy.Settings(ask_above=values["LOOPCUT_ASK_ABOVE_CREDITS"],
                           chat_budget=values["LOOPCUT_CHAT_BUDGET_CREDITS"],
                           ask_every=_as_bool(get("LOOPCUT_ASK_EVERY_PAID_CALL", "false"), "LOOPCUT_ASK_EVERY_PAID_CALL"))


def skills_dir() -> Path | None:
    """Optional local skill source, using the same lookup as other LOOPCUT_* settings."""
    _, get = _getter()
    value = get("LOOPCUT_SKILLS_DIR")
    return Path(value).expanduser() if value else None


def service_policy(*, no_generation_default=False) -> dict[str, bool]:
    """Main-thread snapshot for service catalogs/runners; no account or model key required."""
    import threading
    if threading.current_thread() is not threading.main_thread():
        from . import mainthread
        return mainthread.run_on_main(lambda: service_policy(no_generation_default=no_generation_default)).result(timeout=30)
    _, get = _getter()
    from . import cloud_account
    return cloud_account.current_policy({
        "no_generation": _as_bool(get("LOOPCUT_NO_GENERATION", "true" if no_generation_default else "false"), "LOOPCUT_NO_GENERATION"),
        "private_mode": _as_bool(get("LOOPCUT_PRIVATE_MODE", "true"), "LOOPCUT_PRIVATE_MODE")})


# + a vendor key name ("LOOPCUT_OWN_KEY_FIRST_FAL_KEY"): that key's "Use this key instead of credits"
# checkbox in Preferences → Your own keys. On by default: a user who enters a key gets it used.
OWN_KEY_FIRST = "LOOPCUT_OWN_KEY_FIRST_"


def own_key_first() -> dict[str, bool]:
    """{vendor key name: use it instead of credits}, whether or not the key is set."""
    from . import credentials
    _, get = _getter()
    return {name: _as_bool(get(OWN_KEY_FIRST + name, "true"), OWN_KEY_FIRST + name)
            for name in credentials.VENDOR_KEY_NAMES}


def service_backends(keys_set=None) -> dict[str, str]:
    """The `preferred` routes for cloud.merge_tools: "byok" for every tool whose vendor key is set and
    marked "Use this key instead of credits". The rest take credits first when the account has them,
    then the key. A tool with no key set is left alone, so the default never hides a credits route."""
    from . import credentials
    from .byok import catalog
    keys_set = credentials.vendor_keys_set() if keys_set is None else keys_set
    first = own_key_first()
    return {tool: "byok" for tool, key in catalog.tool_keys().items() if keys_set.get(key) and first[key]}


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


def _check_base_url(base_url: str, key: str) -> None:
    if base_url == CHATGPT_PLAN_BASE_URL or anthropic(base_url):  # anthropic:// is always sent as https.
        return
    if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
        raise ConfigError(f"{key} must be https (http is allowed only for 127.0.0.1)")


def on_service(base_url: str) -> bool:
    """The gateway or a harness stand-in, without reading Blender preferences."""
    base_url = base_url.strip().rstrip("/")
    return base_url == SERVICE_BASE_URL or any(
        fnmatchcase(base_url, entry.strip().rstrip("/"))
        for entry in os.environ.get("LOOPCUT_SERVICE_URLS", "").split(",") if entry.strip().rstrip("/"))


def load() -> Config:
    env_file, get = _getter()

    api_key = get("LOOPCUT_API_KEY")
    base_url = get("LOOPCUT_BASE_URL").rstrip("/")
    model = get("LOOPCUT_MODEL")
    if chatgpt_plan(base_url):
        base_url, api_key, model = CHATGPT_PLAN_BASE_URL, CHATGPT_PLAN_KEY, model or CHATGPT_PLAN_MODEL
    missing = [k for k, v in (("LOOPCUT_API_KEY", api_key), ("LOOPCUT_BASE_URL", base_url),
                              ("LOOPCUT_MODEL", model)) if not v]
    # Codex runs its own model calls under its own sign-in (codex_app.py): no key or URL needed then.
    if missing and not _as_bool(get("LOOPCUT_CODEX_APP", "false"), "LOOPCUT_CODEX_APP"):
        raise ConfigError("No API key yet. Add one in Preferences > Add-ons > Loopcut."
                          if missing == ["LOOPCUT_API_KEY"] or len(missing) == 3 else
                          f"Missing {', '.join(missing)} (looked in Loopcut's preferences and "
                          f"{env_file or 'the environment'})")
    if base_url:
        _check_base_url(base_url, "LOOPCUT_BASE_URL")

    max_steps = get("LOOPCUT_MAX_STEPS", "0")
    step_digits = max_steps.lstrip("0") or "0"
    if (not max_steps.isascii() or not max_steps.isdigit()
            or len(step_digits) > 5 or int(step_digits) > 10_000):
        raise ConfigError(f"LOOPCUT_MAX_STEPS must be a non-negative integer from 0-10000 "
                          f"(0 = unlimited), got {max_steps!r}")
    context_budget = get("LOOPCUT_CONTEXT_BUDGET", str(DEFAULT_CONTEXT_BUDGET))
    if not context_budget.isdigit() or not MIN_CONTEXT_BUDGET <= int(context_budget) <= MAX_CONTEXT_BUDGET:
        raise ConfigError(f"LOOPCUT_CONTEXT_BUDGET must be {MIN_CONTEXT_BUDGET}-{MAX_CONTEXT_BUDGET} tokens, "
                          f"got {context_budget!r}")
    max_output_tokens = _whole(get, "LOOPCUT_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS, 1, MAX_OUTPUT_TOKENS_CEILING)
    call_max_seconds = _whole(get, "LOOPCUT_CALL_MAX_SECONDS", DEFAULT_CALL_MAX_SECONDS, 0, CALL_MAX_SECONDS_CEILING)

    return Config(
        api_key=api_key,
        base_url=base_url,
        model=model,
        reasoning_effort=get("LOOPCUT_REASONING_EFFORT", CHATGPT_PLAN_EFFORT if chatgpt_plan(base_url) else "medium"),
        # Model-written code only runs unprompted if the user opts in.
        auto_run=_as_bool(get("LOOPCUT_AUTO_RUN", "false"), "LOOPCUT_AUTO_RUN"),
        auto_look=_as_bool(get("LOOPCUT_AUTO_LOOK", "true"), "LOOPCUT_AUTO_LOOK"),
        max_steps=int(step_digits),
        context_budget=int(context_budget),
        max_output_tokens=max_output_tokens,
        call_max_seconds=float(call_max_seconds),
        # For a direct provider; on the Loopcut service the model's catalogue entry decides (agent.py). On by
        # default for Anthropic, whose cache and replayed thinking both reward a request that extends the last whole.
        keep_context=_as_bool(get("LOOPCUT_KEEP_CONTEXT", "true" if anthropic(base_url) else "false"),
                              "LOOPCUT_KEEP_CONTEXT"),
    )


def codex_app() -> CodexApp | None:
    """LOOPCUT_CODEX_APP's settings, or None while it is off: LOOPCUT_CODEX_BIN the codex binary (default
    the ChatGPT app's, else `codex` on PATH), LOOPCUT_CODEX_MODEL and LOOPCUT_CODEX_EFFORT the thread's
    model and reasoning effort, LOOPCUT_CODEX_SUMMARY its reasoning summaries."""
    _, get = _getter()
    if not _as_bool(get("LOOPCUT_CODEX_APP", "false"), "LOOPCUT_CODEX_APP"):
        return None
    binary = get("LOOPCUT_CODEX_BIN") or (CODEX_APP_BINARY if Path(CODEX_APP_BINARY).is_file()
                                          else shutil.which("codex") or "")
    if not binary:
        raise ConfigError("LOOPCUT_CODEX_APP is on, but Codex was not found: install it, or set LOOPCUT_CODEX_BIN")
    summary = get("LOOPCUT_CODEX_SUMMARY", "detailed")
    if summary not in CODEX_SUMMARIES:
        raise ConfigError(f"LOOPCUT_CODEX_SUMMARY must be one of {', '.join(CODEX_SUMMARIES)}, got {summary!r}")
    return CodexApp(binary=binary, model=get("LOOPCUT_CODEX_MODEL"), effort=get("LOOPCUT_CODEX_EFFORT"), summary=summary)


def _role(base: Config, get, prefix: str, service_model: str = "") -> Config:
    """base with the role's own provider, model, key, effort, output cap and wall-clock limit where
    set. service_model: the model this role runs on the Loopcut gateway unless it names one."""
    base_url = (get(prefix + "BASE_URL") or base.base_url).rstrip("/")
    if chatgpt_plan(base_url):
        base_url = CHATGPT_PLAN_BASE_URL
    _check_base_url(base_url, prefix + "BASE_URL")
    model = get(prefix + "MODEL") or (service_model if service_model and on_service(base_url) else base.model)
    max_output_tokens = _whole(get, prefix + "MAX_OUTPUT_TOKENS", base.max_output_tokens, 1, MAX_OUTPUT_TOKENS_CEILING)
    call_max_seconds = _whole(get, prefix + "CALL_MAX_SECONDS", int(base.call_max_seconds), 0, CALL_MAX_SECONDS_CEILING)
    return replace(base, base_url=base_url, model=model, api_key=get(prefix + "API_KEY") or base.api_key,
                   reasoning_effort=get(prefix + "REASONING_EFFORT") or base.reasoning_effort,
                   max_output_tokens=max_output_tokens, call_max_seconds=float(call_max_seconds))


def _whole(get, key: str, default: int, low: int, high: int) -> int:
    value = get(key, str(default))
    if not value.isdigit() or not low <= int(value) <= high:
        raise ConfigError(f"{key} must be {low}-{high}, got {value!r}")
    return int(value)


def load_roles() -> Roles:
    """load() per role: LOOPCUT_ORCH_* and LOOPCUT_WORKER_* override the LOOPCUT_* values, and
    LOOPCUT_ROUTER_* the worker's (LOOPCUT_ROUTER=off: no router). Workers default on only when both
    roles use the gateway; the router defaults on only on its own gateway URL. Main thread, like load(). Keep
    mode per role is agent._turn_roles' business. On the ChatGPT plan: the single agent in its workspace,
    never workers or the router, and no craft notes unless LOOPCUT_SINGLE_CRAFT asks for them."""
    base = load()
    _, get = _getter()
    plan = chatgpt_plan(base.base_url)
    try:
        usd = float(get("LOOPCUT_JOB_MAX_USD", "0.40"))
    except ValueError:
        usd = -1.0
    if not 0 <= usd <= 100:
        raise ConfigError(f"LOOPCUT_JOB_MAX_USD must be 0-100 (0 = no limit), got {get('LOOPCUT_JOB_MAX_USD')!r}")
    worker = _role(base, get, "LOOPCUT_WORKER_", SERVICE_WORKER_MODEL)
    orchestrator = _role(base, get, "LOOPCUT_ORCH_")
    router = None
    if not plan and _as_bool(get("LOOPCUT_ROUTER", "on"), "LOOPCUT_ROUTER"):
        router = _role(worker, get, "LOOPCUT_ROUTER_")
        if not get("LOOPCUT_ROUTER_REASONING_EFFORT"):
            router = replace(router, reasoning_effort=ROUTER_EFFORT)
        if not get("LOOPCUT_ROUTER_MAX_OUTPUT_TOKENS"):
            router = replace(router, max_output_tokens=ROUTER_MAX_OUTPUT_TOKENS)
        if not on_service(router.base_url):
            router = None
    return Roles(
        orchestrator=orchestrator,
        worker=worker,
        router=router,
        orchestrate=not plan and on_service(orchestrator.base_url) and on_service(worker.base_url)
                    and _as_bool(get("LOOPCUT_ORCHESTRATE", "true"), "LOOPCUT_ORCHESTRATE"),
        workspace=plan or _as_bool(get("LOOPCUT_WORKSPACE", "false"), "LOOPCUT_WORKSPACE"),
        single_craft=_as_bool(get("LOOPCUT_SINGLE_CRAFT", "false" if plan else "true"), "LOOPCUT_SINGLE_CRAFT"),
        max_workers=_whole(get, "LOOPCUT_MAX_WORKERS", 3, 1, 6),
        job_max_usd=usd,
        worker_max_seconds=_whole(get, "LOOPCUT_WORKER_MAX_SECONDS" if get("LOOPCUT_WORKER_MAX_SECONDS")
                                  else "LOOPCUT_WAVE_MAX_SECONDS", 1800, 60, 7200),
    )
