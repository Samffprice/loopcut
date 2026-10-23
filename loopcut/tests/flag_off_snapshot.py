"""What the panel and the setup draw for an account the credits contract does not touch: no account.billing, as
from every server before it and every account outside its arms. test_chat_credits compares this with the same
snapshot taken from the 0.2.13 add-on (fixtures/flag_off_0.2.13.json), so 0.2.14 with the flag off is 0.2.13.

To write the fixture again from another add-on folder (0.2.13's, from git archive a3c75c53133):

    python3 loopcut/tests/flag_off_snapshot.py <folder holding loopcut/__init__.py> loopcut/tests/fixtures/flag_off_0.2.13.json

Deterministic: a measure by character count, date-only resets (no local time), no network, no Blender.
"""

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parents[1] / "scripts" / "addons_core" / "loopcut"
FIXTURE = HERE / "fixtures" / "flag_off_0.2.13.json"


def measure(font, size, text):
    return len(text) * size * 0.6


def _load(folder: Path, name: str):
    """The add-on in `folder` as package `name`, without bpy (as test_layout loads it)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, folder / "__init__.py", submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _plain(value, version: str = ""):
    """JSON's view of a value: tuples as lists, floats rounded, and the add-on's own version as <version> (the one
    line every release changes), so both snapshots compare alike."""
    text = json.dumps(value, default=str)
    if version:
        text = text.replace(version, "<version>")
    return json.loads(text, parse_float=lambda number: round(float(number), 4))


def _version(name: str) -> str:
    return ".".join(map(str, sys.modules[name].bl_info["version"]))


def panel(folder: Path = PACKAGE, name: str = "_flag_off_panel") -> dict:
    _load(folder, name)
    layout = importlib.import_module(name + ".ui.layout")
    state = importlib.import_module(name + ".state")
    reset = "2099-10-21"  # Date only: reset labels then read the same in every timezone.
    account = {"plan": "free", "session": {"used": 0.84, "resets_at": reset}, "week": {"used": 0.4, "resets_at": reset}}
    upgrade = {"url": "https://loopcut.org/pricing", "plan": "pro", "label": "Upgrade to Pro", "note": "Pro: more."}
    shots = {}
    for billing in (True, False):
        with patch.object(layout.config, "billing_open", return_value=billing):
            for name_, reason, mid_turn, width in (("session", "session_limit", False, 420),
                                                   ("week_mid_turn", "week_limit", True, 420),
                                                   ("narrow", "session_limit", False, 260),
                                                   ("daily", "free_daily_limit", True, 420)):
                session = state.new_session()
                session["id"] = "f" * 32
                user = state.item_user("make more details")
                reply = state.item_assistant()
                reply.update(text="Added brass key caps.", streaming=False)
                session["items"] = [user, reply, state.item_changes("3 objects added", ["+ Keys"], "")]
                session["spend"] = {"credits": 12, "auto": 12, "allowed": 0}
                card = state.item_limit("Chat allowance used.", reason, "free", reset, upgrade)
                if mid_turn:
                    session["items"].append(card)
                    session["messages"] = [{"role": "tool", "content": "Stopped after a tool."}]
                ui = {"selection": [], "context_budget": 24000, "account": account, "credits": "94 credits ($0.94)",
                      "credit_balance": 94, "today": "Today: 6 credits", "now": 0.0, "chatgpt_plan": False,
                      "allowance_notice": {"item": card, "conversation": session["id"]}}
                display = layout.build(session, width, 900, 1.0, measure, "fast", None, ui)
                shots[f"{name_}_billing_{billing}"] = {"prims": display["prims"], "hits": display["hits"]}
            session = state.new_session()
            session["items"] = [state.item_user("hello")]
            display = layout.build(session, 420, 700, 1.0, measure, "fast", None,
                                   {"selection": [], "context_budget": 24000, "account": account, "now": 0.0,
                                    "credits": "100 credits ($1.00)", "credit_balance": 100})
            shots[f"idle_billing_{billing}"] = {"prims": display["prims"], "hits": display["hits"]}
    return _plain(shots, _version(name))


def _records(splash) -> list:
    out = []
    for record in splash.records:
        if record[0] == "operator":
            _, name, text, props, owner, kwargs = record
            out.append(["operator", name, text, sorted((k, str(v)) for k, v in vars(props).items()),
                        bool(getattr(owner, "enabled", True)), sorted((k, str(v)) for k, v in kwargs.items())])
        else:
            out.append([str(part) for part in record])
    return out


def splash(folder: Path = PACKAGE, name: str = "_flag_off_splash") -> dict:
    """Every setup step for a signed-in account without account.billing, drawn with test_onboarding's recording
    layout and private fake bpy (loaded from `folder`)."""
    sys.path.insert(0, str(HERE))
    import test_onboarding as base
    saved = base.PACKAGE, base.NAME
    base.PACKAGE, base.NAME = Path(folder), name

    class Snap(base.OnboardingTest):  # Defined here, so test discovery never collects it.
        def runTest(self):
            pass

    shots = {}
    try:
        Snap.setUpClass()
        case = Snap()
        case.setUp()
        version = _version(name)
        try:
            on, account = case.onboarding, case.account
            credits = {"balance": 100, "monthly": 0}
            with patch.object(account, "signed_in", return_value=True), \
                    patch.object(on, "_credits", return_value=credits), \
                    patch.object(on, "_account_plan", return_value="free"), \
                    patch.object(on, "_descriptors", return_value=case.LIVE), \
                    patch.object(on, "_connected", return_value=True), \
                    patch.object(account, "account_line", return_value="tester@example.test"):
                for billing in (True, False):
                    with patch.object(on.config, "billing_open", return_value=billing):
                        for step in ("account", "how", "connect", "privacy", "done"):
                            on._state.update(step=step, own_key=False, change_model=False, existing=False)
                            recorder = base.Splash()
                            on.draw_splash(recorder, case.context, first_run=False)
                            shots[f"{step}_billing_{billing}"] = _records(recorder)
                            recorder = base.StyledSplash()
                            on.draw_splash(recorder, case.context, first_run=False)
                            shots[f"{step}_styled_billing_{billing}"] = _records(recorder)
        finally:
            case.doCleanups()
    finally:
        Snap.doClassCleanups()
        base.PACKAGE, base.NAME = saved
        for module in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            sys.modules.pop(module, None)
    return _plain(shots, version)


def snapshot(folder: Path = PACKAGE) -> dict:
    """One add-on folder per process: the package names are fixed so the snapshots compare."""
    return {"panel": panel(folder), "splash": splash(folder)}


if __name__ == "__main__":
    folder, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.write_text(json.dumps(snapshot(folder), indent=0, sort_keys=True))
    print(f"wrote {out}")
