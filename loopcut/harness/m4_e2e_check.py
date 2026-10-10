"""M4 end to end, headless: a scripted orchestrator and two scripted workers against real child
Blenders and a real merge. No network and no tokens: the models are harness/fake_model.py.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/m4_e2e_check.py [-- <dir>]

Design 07 §4 W5. The parent runs in -b, where timers never fire, so this script pumps
mainthread._pump() itself. Everything goes under a fresh folder (or <dir>, which is kept): the
project, Loopcut's data (LOOPCUT_DATA_DIR) and TMPDIR.

Run 1. The user asks for a small box and a tall box resting on the rug. The router (a third scripted
model) routes it to level 3 with BoxA and BoxB, and its note rides on the orchestrator's first
request. The orchestrator makes a palette material itself, then delegates one wave of two modeling
workers. Each builds its prefixed
box on the Rug with run_python; BoxB first tries to scale the Rug, then builds its box 12 mm too
high; both call check_brief and report_back. The orchestrator's final reply bounces once on the
failing check, it lowers BoxB itself, and ends. Asserted: both children ran and exited; worker 2's
first request came after worker 1's first reply; the Rug edit was rolled back and reported, and
the live Rug is unchanged; BoxA and BoxB are in the scene, the wave checkpoint exists and the
merge is one undo step; the delegate result is compact; the Verified block ends the final reply;
the JobLog and usage_by_role (router included) are filled, and turn_levels records routed 3 with no
nudge; no temporary .blend is left.

Both boxes finish within BATCH_SECONDS of each other here, so they come back in one result.

Run 2, results as workers finish. Three boxes (BoxE, BoxF, BoxG); BoxF's and BoxG's workers are held
inside their first step until the orchestrator's third and fourth requests. delegate returns with
BoxE alone, naming the two still running; the orchestrator sends BoxE back to its worker (continues:
widen it) and that returns while the other two still build; delegate wait returns BoxF, with BoxG
still running; the final reply comes while BoxG builds, so Loopcut waits for it, merges it and bounces
once with its result; the next reply ends the turn with every check verified. Asserted: each of those
results, the fix in the live scene, a merge card per result with the wave's one checkpoint, each merge
its own undo step, the JobLog's merges, order and wait, no children and no temporary .blend.

Run 3. Two boxes (BoxC, BoxD); BoxC comes back at once, and Stop is pressed while the orchestrator
waits for BoxD (delegate wait) inside its slow step. Asserted: no children, BoxC merged and nothing
more, no leftovers.

Until W1-W4 are merged into this branch it stops at once, naming each missing piece and the
workstream that delivers it.
"""

import importlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
import fake_model  # noqa: E402

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ROOT = Path(ARGS[0] if ARGS else tempfile.mkdtemp(prefix="loopcut-m4-e2e-")).resolve()
PROJECT = ROOT / "project" / "room.blend"
DATA = ROOT / "data"
TMP = ROOT / "tmp"
ORCH, WORKER, ROUTER = "fake-orchestrator", "fake-worker", "fake-router"
ROUTED = json.dumps(dict(level=3, deliverables=[dict(name='BoxA', quote='a small box'),
                                               dict(name='BoxB', quote='a tall box')], why='two boxes'))
RUN_SECONDS = 300    # One turn with a wave in it, at most.
STOP_SECONDS = 30    # From Stop to the end of the turn, at most.
PID = os.getpid()

RUG_TOP = 0.015
FLOAT = 0.012        # How far above the rug BoxB is built: the check that bounces the final reply.
USER = "Keep the rug as it is. Put a small box and a tall box on it, each resting on the rug."
SMALL = {"prefix": "BoxA", "quote": "a small box", "size": (0.3, 0.3, 0.3), "x": -0.4}
TALL = {"prefix": "BoxB", "quote": "a tall box", "size": (0.2, 0.2, 0.6), "x": 0.4}

# What this check runs through, by the workstream that delivers it (design 07 §2 and §4).
NEEDS = {
    "W1 worker host": {
        "worker_ipc": ["child_env"],
        "worker_host": ["Child", "WorkerRuntime", "reap_orphans", "shutdown_all"],
        "worker_capture": ["look"],
    },
    "W2 merge, checks and camera": {
        "ownership": ["stamp_all", "changed_keys", "write_result", "token"],
        "merge": ["Spawn", "MergeReport", "MergeError", "save_spawn", "merge_wave", "accept_quarantine"],
        "acceptance": ["validate", "run"],
        "camera": ["coverage"],
        "checkpoints": ["ensure_for_wave"],
    },
    "W3 orchestrator, workers and loops": {
        "agent": ["LoopSpec", "run_loop"],
        "orchestrator": ["DISPATCH", "delegate"],
        "waves": ["run_wave", "collect", "drain", "end_turn", "cancel_worker", "render_report"],
        "workers": ["SPEC"],
        "config": ["Roles", "load_roles"],
        "state": ["item_wave", "item_merge"],
    },
    "W4 eval harness": {"pricing": ["usd"]},
}
NEED_FILES = {"W1 worker host": ["worker_child.py"]}  # A --python script: checked, never imported.

BOX = """import bpy
def box(name, size, base):
    # A closed box, its origin at the middle of its bottom face.
    sx, sy, sz = size
    verts = [(dx * sx / 2, dy * sy / 2, dz * sz) for dx in (-1, 1) for dy in (-1, 1) for dz in (0, 1)]
    faces = [(0, 2, 6, 4), (1, 5, 7, 3), (0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    obj.location = base
    bpy.context.scene.collection.objects.link(obj)
    return obj
"""

RESULTS: list[tuple[str, bool, str]] = []
SEEN: dict[int, str] = {}   # Every worker child seen during the current run: pid -> command line.
LIVE: dict[int, str] = {}   # The ones alive at the last sample.
_sampled = [0.0]


def finish(code: int, message: str):
    print(message, file=sys.stderr if code else sys.stdout)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def missing_pieces() -> list[str]:
    lines = []
    for owner, modules in NEEDS.items():
        for module, names in modules.items():
            if importlib.util.find_spec(f"loopcut.{module}") is None:
                lines.append(f"{owner}: loopcut/{module}.py does not exist")
                continue
            try:
                loaded = importlib.import_module(f"loopcut.{module}")
            except Exception as ex:  # Whatever it is, the message names the module that broke.
                lines.append(f"{owner}: loopcut/{module}.py does not import: {ex!r}")
                continue
            absent = [name for name in names if not hasattr(loaded, name)]
            if absent:
                lines.append(f"{owner}: loopcut.{module} has no {', '.join(absent)}")
        lines += [f"{owner}: loopcut/{name} does not exist" for name in NEED_FILES.get(owner, [])
                  if not (checkout.PACKAGE / name).is_file()]
    return lines


def environment(server: fake_model.Server) -> None:
    for folder in (DATA, TMP, PROJECT.parent):
        folder.mkdir(parents=True, exist_ok=True)
    env_file = ROOT / "e2e.env"
    env_file.write_text("", encoding="utf-8")  # So config never walks up to the developer's .env.
    for key in [k for k in os.environ if k.startswith("LOOPCUT_") and k != "LOOPCUT_BLENDER"]:
        del os.environ[key]
    os.environ.update({
        "LOOPCUT_ENV_FILE": str(env_file),
        "LOOPCUT_DATA_DIR": str(DATA),
        "TMPDIR": str(TMP),
        "LOOPCUT_BASE_URL": server.base_url, "LOOPCUT_API_KEY": "fake-orchestrator-key", "LOOPCUT_MODEL": ORCH,
        "LOOPCUT_SERVICE_URLS": server.base_url,  # The fake model stands in for the gateway.
        "LOOPCUT_PROMPT_INDEX": str(Path(__file__).parent / "fixtures" / "prompt-index.json"),
        "LOOPCUT_ORCH_BASE_URL": server.base_url, "LOOPCUT_ORCH_API_KEY": "fake-orchestrator-key",
        "LOOPCUT_ORCH_MODEL": ORCH,
        "LOOPCUT_WORKER_BASE_URL": server.base_url, "LOOPCUT_WORKER_API_KEY": "fake-worker-key",
        "LOOPCUT_WORKER_MODEL": WORKER,
        "LOOPCUT_ROUTER": "on", "LOOPCUT_ROUTER_MODEL": ROUTER,  # On the workers' URL and key otherwise.
        "LOOPCUT_ORCHESTRATE": "true",
        "LOOPCUT_AUTO_RUN": "true",    # delegate runs without its approval card.
        "LOOPCUT_AUTO_LOOK": "false",
        "LOOPCUT_MAX_WORKERS": "3",
        "LOOPCUT_WAVE_MAX_SECONDS": str(RUN_SECONDS - 60),
    })
    tempfile.tempdir = str(TMP)


def make_project() -> None:
    """The factory scene minus the cube, plus a 2 m rug 15 mm thick, saved as the user's file."""
    cube = bpy.data.objects.get("Cube")
    if cube is not None:
        bpy.data.objects.remove(cube)
    namespace: dict = {}
    exec(BOX, namespace)
    namespace["box"]("Rug", (2.0, 2.0, RUG_TOP), (0.0, 0.0, 0.0))
    bpy.ops.wm.save_as_mainfile(filepath=str(PROJECT))


def brief(spec: dict) -> dict:
    """A worker's brief as design 07 §2.5 freezes it."""
    (sx, sy, sz), x, prefix = spec["size"], spec["x"], spec["prefix"]
    return {"id": prefix.lower(), "deliverable": prefix, "role": "modeling", "prefix": prefix,
            "owns": [], "aspects": [],
            "requirements": [{"quote": spec["quote"], "do": f"a box {sx} x {sy} x {sz} m"}],
            "defaults": [f"{sx} x {sy} x {sz} m (default)"],
            "bounds": [[round(x - sx / 2, 4), -sy / 2, RUG_TOP], [round(x + sx / 2, 4), sy / 2, round(RUG_TOP + sz, 4)]],
            "rests_on": "Rug",
            "checks": [{"kind": "exists", "name": prefix, "quote": spec["quote"]},
                       {"kind": "contact", "a": prefix, "relation": "rests_on", "b": "Rug",
                        "quote": "resting on the rug"}]}


def wave(specs: list[dict], palette: list[str]) -> dict:
    return {"shared": {"keep": {"names": ["Rug"], "quote": "Keep the rug as it is"}, "palette": palette,
                       "supports": [{"name": "Rug", "top_z": RUG_TOP}], "units": "metres, Z up, floor z = 0"},
            "workers": [brief(spec) for spec in specs]}


def run_python(summary: str, code: str, call_id: str = "call_1") -> bytes:
    return fake_model.tool_reply("run_python", {"summary": summary, "code": code}, call_id)


def build(spec: dict, lift: float = 0.0, first: str = "") -> bytes:
    code = first + BOX + f"box({spec['prefix']!r}, {spec['size']!r}, ({spec['x']}, 0.0, {RUG_TOP + lift}))\n"
    return run_python(f"Build {spec['prefix']}", code)


def wrap_up(prefix: str) -> list[bytes]:
    return [fake_model.tool_reply("check_brief", {}),
            fake_model.tool_reply("report_back", {"summary": f"Built {prefix} on the rug.", "change_requests": []})]


def script_run_one(server: fake_model.Server) -> None:
    server.script(ROUTER, [fake_model.text_reply(ROUTED)])
    server.script(ORCH, [
        run_python("Palette material", "import bpy\nbpy.data.materials.new('Pal_Wood').use_fake_user = True\n",
                   "call_palette"),
        fake_model.tool_reply("delegate", {"wave": wave([SMALL, TALL], ["Pal_Wood"])}, "call_delegate"),
        fake_model.text_reply("Both boxes are on the rug."),      # Bounced: BoxB floats.
        # BoxB moved whole (the orchestrator's arrangement), BoxA's top raised 5 mm (a worker's mesh: P2's note).
        run_python("Lower BoxB onto the rug", f"import bpy\nbpy.data.objects['BoxB'].location.z = {RUG_TOP}\n"
                   "for v in bpy.data.objects['BoxA'].data.vertices:\n    v.co.z += 0.005 if v.co.z > 0.1 else 0\n",
                   "call_fix"),
        fake_model.text_reply("Both boxes rest on the rug now."),
    ])
    server.script(WORKER, {
        "BoxA": [build(SMALL), *wrap_up("BoxA")],
        "BoxB": [run_python("Widen the rug", "import bpy\nbpy.data.objects['Rug'].scale = (1.5, 1.5, 1.0)\n"),
                 build(TALL, lift=FLOAT), *wrap_up("BoxB")],
    }, route=fake_model.named_in_first_message(["BoxA", "BoxB"]))


THREE = [{**SMALL, "prefix": "BoxE", "x": -0.6}, {**TALL, "prefix": "BoxF", "x": 0.0},
         {"prefix": "BoxG", "quote": "a flat box", "size": (0.4, 0.3, 0.1), "x": 0.6}]
USER_THREE = ("Keep the rug as it is. Put a small box, a tall box and a flat box on it, each resting on the rug.")
WIDEN = 1.2  # The fix BoxE's worker is sent back with: 20 % wider along x.


def held(server: fake_model.Server, requests: int, reply: bytes):
    """A worker's reply that waits until the orchestrator has sent `requests` requests: until then the
    worker is inside that step, still running."""
    def wait(body):
        end = time.monotonic() + 90  # Well under llm's 120 s read timeout.
        while len(server.requests(ORCH)) < requests and time.monotonic() < end:
            time.sleep(0.05)
        return reply
    return wait


def rebrief(spec: dict) -> dict:
    """The brief sent back to the worker that built spec's box: continues, owning it, widened."""
    sx = spec["size"][0] * WIDEN
    return {**brief(spec), "continues": spec["prefix"].lower(), "owns": [spec["prefix"]],
            "requirements": [{"quote": spec["quote"], "do": f"widen the box to {sx:.2f} m along x, on the rug"}],
            "style": "the same box, 20 % wider along x", "defaults": [],
            "bounds": [[round(spec["x"] - sx / 2, 4), -spec["size"][1] / 2, RUG_TOP],
                       [round(spec["x"] + sx / 2, 4), spec["size"][1] / 2, round(RUG_TOP + spec["size"][2], 4)]]}


def script_run_two(server: fake_model.Server) -> None:
    e, f, g = THREE
    server.script(ROUTER, [fake_model.text_reply(json.dumps(dict(
        level=3, deliverables=[dict(name='BoxE', quote='a small box'), dict(name='BoxF', quote='a tall box'),
                               dict(name='BoxG', quote='a flat box')], why='three boxes')))])
    server.script(ORCH, [
        fake_model.tool_reply("delegate", {"wave": wave(THREE, [])}, "call_delegate"),   # Back with BoxE.
        fake_model.tool_reply("delegate", {"wave": {**wave([], []), "workers": [rebrief(e)]}}, "call_fix"),
        fake_model.tool_reply("delegate", {"wait": True}, "call_wait"),                   # Back with BoxF.
        fake_model.text_reply("All three boxes are on the rug."),                         # BoxG still builds.
        fake_model.text_reply("Reviewed: all three boxes rest on the rug."),
    ])
    widen = run_python("Widen BoxE", f"import bpy\nbpy.data.objects['BoxE'].scale.x = {WIDEN}\n")
    server.script(WORKER, {
        "BoxE": [build(e), *wrap_up("BoxE"), widen, *wrap_up("BoxE")],
        "BoxF": [held(server, 3, build(f)), *wrap_up("BoxF")],
        "BoxG": [held(server, 4, build(g)), *wrap_up("BoxG")],
    }, route=fake_model.named_in_first_message([s["prefix"] for s in THREE]))


def script_run_three(server: fake_model.Server) -> list[str]:
    specs = [{**SMALL, "prefix": "BoxC"}, {**TALL, "prefix": "BoxD"}]
    slow = "import time\ntime.sleep(40)\n"  # Under the child's 60 s run timeout; Stop comes first.
    server.script(ROUTER, [fake_model.text_reply(ROUTED)])
    server.script(ORCH, [fake_model.tool_reply("delegate", {"wave": wave(specs, [])}, "call_delegate"),
                         fake_model.tool_reply("delegate", {"wait": True}, "call_wait"),
                         fake_model.text_reply("Stopped.")])
    server.script(WORKER, {"BoxC": [build(specs[0]), *wrap_up("BoxC")],
                           "BoxD": [build(specs[1], first=slow), *wrap_up("BoxD")]},
                  route=fake_model.named_in_first_message([s["prefix"] for s in specs]))
    return [s["prefix"] for s in specs]


def children() -> dict[int, str]:
    """Live worker_child.py processes of this Blender: pid -> command line."""
    out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,stat=,command="], capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or "worker_child.py" not in parts[3] or parts[2].startswith("Z"):
            continue
        if int(parts[1]) == PID or str(ROOT) in parts[3]:
            found[int(parts[0])] = parts[3]
    return found


def sample(force: bool = False) -> None:
    if force or time.monotonic() - _sampled[0] > 0.25:
        _sampled[0] = time.monotonic()
        LIVE.clear()
        LIVE.update(children())
        SEEN.update(LIVE)


def pump(until, seconds: float, what: str) -> None:
    """Run the main-thread queue, as the add-on's timer would, until `until()` or the time is up."""
    from loopcut import mainthread
    end = time.monotonic() + seconds
    while not until():
        mainthread._pump()
        sample()
        if time.monotonic() > end:
            raise TimeoutError(f"no {what} after {seconds:.0f} s")
        time.sleep(0.02)


def settle(seconds: float) -> None:
    end = time.monotonic() + seconds
    pump(lambda: time.monotonic() > end, seconds + 5, "pause")


def children_gone(seconds: float) -> dict[int, str]:
    end = time.monotonic() + seconds
    while True:
        sample(force=True)
        if not LIVE or time.monotonic() > end:
            return dict(LIVE)
        time.sleep(0.2)


def end_turn(session: dict, label: str) -> None:
    """Wait for the turn; if it hangs, record that, Stop it and wait again so later runs start clean."""
    from loopcut import agent
    try:
        pump(lambda: not session["busy"], RUN_SECONDS, f"end of the turn ({label})")
    except TimeoutError as ex:
        verdict(f"{label}: the turn ends", False, str(ex))
        agent.stop()
        pump(lambda: not session["busy"], STOP_SECONDS, f"end of the turn after Stop ({label})")


def verdict(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)


def verify(name: str, fn) -> None:
    """fn() -> (ok, detail); an exception is a failure that says what broke."""
    try:
        ok, detail = fn()
    except Exception as ex:
        traceback.print_exc()
        ok, detail = False, f"{type(ex).__name__}: {ex}"
    verdict(name, ok, detail)


def rug_state() -> tuple:
    rug = bpy.data.objects["Rug"]
    return (tuple(round(v, 6) for row in rug.matrix_world for v in row), tuple(round(v, 6) for v in rug.dimensions),
            tuple(round(c, 6) for v in rug.data.vertices for c in v.co))


def leftover_blends(since: float) -> list[str]:
    """.blend files a wave left: anything but the project and the checkpoints (which are kept on purpose)."""
    project, kept = PROJECT.resolve(), (DATA / "checkpoints").resolve()
    found = set()
    for root in {ROOT.resolve(), Path(bpy.app.tempdir).resolve()}:
        if not root.is_dir():
            continue
        for path in root.rglob("*.blend*"):
            path = path.resolve()
            if (path == project or kept in path.parents or not path.is_file()
                    or not path.suffix.startswith(".blend") or path.stat().st_mtime < since - 1):
                continue
            found.add(str(path))
    return sorted(found)


def no_leftovers(since: float) -> tuple[bool, str]:
    left = leftover_blends(since)
    return not left, ", ".join(left) or "none"


def undo() -> bool:
    wm = bpy.context.window_manager
    if wm.windows:  # -b still has the file's window; undo needs it (merge spike, 120-139).
        window = wm.windows[0]
        with bpy.context.temp_override(window=window, screen=window.screen):
            return bpy.ops.ed.undo() == {"FINISHED"}
    return bpy.ops.ed.undo() == {"FINISHED"}


def last_assistant(session: dict) -> str:
    message = next((m for m in reversed(session["messages"]) if m.get("role") == "assistant"), {})
    return fake_model.message_text(message)


def delegate_result(server: fake_model.Server, call_id: str = "call_delegate") -> dict | None:
    """The tool message that answered a delegate call, as the orchestrator's next request carried it."""
    for request in server.requests(ORCH):
        found = next((m for m in request.body.get("messages", [])
                      if m.get("role") == "tool" and m.get("tool_call_id") == call_id), None)
        if found is not None:
            return found
    return None


def delegate_result_in(request) -> bool:
    return any(m.get("role") == "tool" and "Wave 1" in fake_model.message_text(m)
               for m in request.body.get("messages", []))


def errors(session: dict) -> list[str]:
    return [i["text"] for i in session["items"] if i["kind"] == "error"]


def diagnostics(session: dict, server: fake_model.Server) -> None:
    print(f"  turn outcome: {session.get('turn_outcome')}")
    for item in session["items"]:
        if item["kind"] in ("error", "wave", "merge", "notice"):
            print(f"  item {item['kind']}: {str(item.get('text'))[:200]}")
    for line in server.errors:
        print(f"  fake model: {line}")
    left = server.remaining()
    if left:
        print("  scripted replies never asked for: " + ", ".join(f"{m}/{k or '-'} x{n}" for (m, k), n in left.items()))


def run_one(server: fake_model.Server) -> None:
    from loopcut import agent, checkpoints, context, state, waves
    server.reset()
    script_run_one(server)
    session = state.reset()
    rug_before = rug_state()
    started = time.time()
    SEEN.clear()
    if not agent.send(USER):
        raise RuntimeError(f"agent.send refused the message: {errors(session)}")
    end_turn(session, "run 1")
    alive = children_gone(5)
    job = (session.get("jobs") or [{}])[-1]
    workers = {w.get("id"): w for w in ((job.get("waves") or [{}])[0].get("workers") or [])}

    def ran_and_exited():
        talked = [p for p in ("BoxA", "BoxB") if len(server.requests(WORKER, p)) >= 2]
        ok = len(SEEN) >= 2 and len(talked) == 2 and not alive
        return ok, f"{len(SEEN)} children seen, workers past their first step: {talked}, still running: {sorted(alive)}"
    verify("run 1: both children ran and exited", ran_and_exited)

    def spawn_rule():
        firsts = sorted((server.requests(WORKER, p)[0] for p in ("BoxA", "BoxB")), key=lambda r: r.received)
        one, two = firsts
        gap = (two.received - one.replied) * 1000
        return one.replied > 0 and gap > 0, f"{two.key}'s first request {gap:+.0f} ms after {one.key}'s first reply"
    verify("run 1: spawn rule (worker 2's first request after worker 1's first reply)", spawn_rule)

    def rolled_back():
        notes = [fake_model.message_text(m) for r in server.requests(WORKER, "BoxB") for m in r.body.get("messages", [])
                 if m.get("role") == "tool" and "Rolled back" in fake_model.message_text(m)]
        counted = workers.get("boxb", {}).get("rollbacks", 0)
        ok = any("Rug" in note for note in notes) and counted >= 1
        return ok, f"worker told: {notes[0][:120] if notes else 'nothing'}; JobLog rollbacks: {counted}"
    verify("run 1: the Rug edit was rolled back and reported", rolled_back)
    verify("run 1: the live Rug is unchanged",
           lambda: (rug_state() == rug_before, f"Rug scale {tuple(bpy.data.objects['Rug'].scale)}"))

    def merged():
        present = [n for n in ("BoxA", "BoxB") if n in bpy.context.scene.objects]
        return len(present) == 2, f"in the scene: {present}"
    verify("run 1: the merged objects are there", merged)

    def wave_checkpoint():
        merges = [i for i in session["items"] if i["kind"] == "merge"]
        if len(merges) != 1:
            return False, f"{len(merges)} merge cards"
        entry = checkpoints._store(session).get(merges[0]["checkpoint"])
        ok = entry.get("kind") == "wave" and checkpoints.status(session, entry["id"]) == "ok"
        return ok, f"checkpoint {entry['id'][:8]}, kind {entry.get('kind')!r}, {checkpoints.status(session, entry['id'])}"
    verify("run 1: the wave checkpoint exists", wave_checkpoint)

    def compact():
        message = delegate_result(server)
        if message is None:
            return False, "the orchestrator never got a delegate result"
        text = fake_model.message_text(message)
        tokens = len(text) / context.CHARS_PER_TOKEN
        limit = waves.REPORT_TOKENS * (len(workers) or 2) + 250  # A block per worker, the merge line and live checks.
        ok = (isinstance(message.get("content"), str) and tokens <= limit and "from_pydata" not in text
              and all(w in text.lower() for w in ("boxa", "boxb")))
        return ok, f"about {tokens:.0f} tokens (limit {limit}), text only: {isinstance(message.get('content'), str)}"
    verify("run 1: the report is compact", compact)

    def measured():
        """facts.measure in each child at report time: what the orchestrator reads beside the words."""
        message = delegate_result(server)
        text = fake_model.message_text(message) if message else ""
        want = ["measured BoxA: 12 tris; no modifiers; 0% smooth; 0 non-manifold edges; 0.3 x 0.3 x 0.3 m; inside its box",
                "measured BoxB: 12 tris; no modifiers; 0% smooth; 0 non-manifold edges; 0.2 x 0.2 x 0.6 m; "
                "outside its box z+ 12 mm"]
        missing = [w for w in want if w not in text]
        return not missing, f"missing {missing}" if missing else "a measured line per box"
    verify("run 1: the report measures each deliverable", measured)

    def contact_sheet():
        """One image with the delegate result: a cell per worker, labelled, within a worker look's size."""
        import base64
        import struct
        request = next((r for r in server.requests(ORCH) if delegate_result_in(r)), None)
        if request is None:
            return False, "no request carried the delegate result"
        messages = request.body.get("messages", [])
        at = next(k for k, m in enumerate(messages) if m.get("role") == "tool" and "Wave 1" in fake_model.message_text(m))
        text = fake_model.message_text(messages[at])
        after = [m for m in messages[at + 1:] if isinstance(m.get("content"), list)]
        urls = [p["image_url"]["url"] for m in after[:1] for p in m["content"] if p.get("type") == "image_url"]
        label = "Image attached: the contact sheet of these deliverables" in text and "labelled boxa, boxb." in text
        if not urls or not label:
            return False, f"images after the result: {len(urls)}, line: {label}"
        head = base64.b64decode(urls[0].split(",", 1)[1])[:24]
        width, height = struct.unpack(">II", head[16:24])
        ok = width <= 1024 and height <= 1024 and width > height
        return ok, f"one sheet, {width}x{height} px"
    verify("run 1: one contact sheet comes with the report", contact_sheet)

    def worker_note():
        """P2: the orchestrator's own edit to BoxA's mesh gets one line pointing back to its worker;
        lowering BoxB whole (arrangement) is its own and draws none. Counted for the evals (M1)."""
        messages = server.requests(ORCH)[-1].body.get("messages", []) if server.requests(ORCH) else []
        fix = next((fake_model.message_text(m) for m in messages
                    if m.get("role") == "tool" and m.get("tool_call_id") == "call_fix"), "")
        ok = (fix.endswith("BoxA was built by worker boxa: send fixes back with delegate continues.")
              and "boxb" not in fix.split("Scene changes", 1)[-1].lower().split("built by worker", 1)[-1]
              and [list(e["objects"]) for e in session.get("worker_edits") or []] == [["BoxA"]])
        return ok, f"the fix's result ends: {fix[-90:]!r}; recorded {session.get('worker_edits')}"
    verify("run 1: an edit to a worker's mesh draws one note; a whole move none", worker_note)

    def verified():
        final = last_assistant(session)
        last = server.requests(ORCH)[-1].body.get("messages", []) if server.requests(ORCH) else []
        bounces = [m for m in last if m.get("role") == "user"
                   and "checks fail on the live scene" in fake_model.message_text(m)]
        ok = "Verified by Loopcut on this scene" in final and len(bounces) == 1
        return ok, f"bounces: {len(bounces)}; final reply ends: {final[-120:]!r}"
    verify("run 1: the Verified block is present (after one bounce)", verified)

    def joblog_and_usage():
        problems = []
        if set(workers) != {"boxa", "boxb"}:
            problems.append(f"JobLog workers {sorted(workers)}")
        for id, w in workers.items():
            if not (w.get("steps") and (w.get("usage") or {}).get("requests") and (w.get("t") or {}).get("ready")):
                problems.append(f"{id}: steps {w.get('steps')}, usage {w.get('usage')}, t {w.get('t')}")
        by_role = session.get("usage_by_role") or {}
        total = 0
        for role, model in (("router", ROUTER), ("orchestrator", ORCH), ("worker", WORKER)):
            sent = [r for r in server.requests(model) if r.usage]
            want = {"requests": len(sent), "input": sum(r.usage["input"] for r in sent)}
            got = {k: (by_role.get(role) or {}).get(k) for k in want}
            total += want["input"]
            if got != want:
                problems.append(f"usage_by_role[{role}] {got}, the model served {want}")
        if (session.get("usage") or {}).get("input") != total:
            problems.append(f"usage input {(session.get('usage') or {}).get('input')} is not the roles' sum {total}")
        return not problems, "; ".join(problems) or f"{len(workers)} workers logged, usage by role matches the model's"
    verify("run 1: JobLog and usage_by_role are filled", joblog_and_usage)

    def routed():
        routes, orchestrator = server.requests(ROUTER), server.requests(ORCH)
        first = next((m for m in orchestrator[0].body.get("messages", []) if m.get("role") == "user"), {}) if orchestrator else {}
        noted = "<routing>level 3 — deliverables: BoxA (\"a small box\"), BoxB (\"a tall box\")" in \
            fake_model.message_text(first)
        before = bool(routes and orchestrator) and routes[0].received < orchestrator[0].received
        levels = session.get("turn_levels") or []
        ok = len(routes) == 1 and before and noted and levels == [{"turn": 1, "first": 1, "final": 3, "routed": 3}]
        return ok, f"router requests {len(routes)}, before the orchestrator {before}, note {noted}, levels {levels}"
    verify("run 1: the router routed level 3 first and its note reached the orchestrator", routed)

    verify("run 1: no temporary .blend left", lambda: no_leftovers(started))

    def one_undo_step():
        # Last, as it rewinds the scene. The orchestrator's fix after the merge is its own step.
        for n in range(1, 11):
            if not undo():
                return False, f"undo #{n} did nothing"
            present = [name for name in ("BoxA", "BoxB") if name in bpy.data.objects]
            if len(present) < 2:
                ok = not present and "Pal_Wood" in bpy.data.materials and rug_state() == rug_before
                return ok, (f"undo #{n} took both boxes out at once and left the palette from before the wave"
                            if ok else f"undo #{n} left {present}, palette there: {'Pal_Wood' in bpy.data.materials}")
        return False, "10 undos left both boxes in place"
    verify("run 1: the merge is one undo step", one_undo_step)
    if not all(ok for _, ok, _ in RESULTS):
        diagnostics(session, server)


def run_two(server: fake_model.Server) -> None:
    """Results as workers finish: an early return, a fix beside running workers, a wait, a late reply."""
    from loopcut import agent, checkpoints, state, waves
    bpy.ops.wm.open_mainfile(filepath=str(PROJECT))
    server.reset()
    script_run_two(server)
    session = state.reset()
    started, failures_before = time.time(), sum(1 for _, ok, _ in RESULTS if not ok)
    SEEN.clear()
    if not agent.send(USER_THREE):
        raise RuntimeError(f"agent.send refused the message: {errors(session)}")
    end_turn(session, "run 2")
    alive = children_gone(5)

    def result(call_id: str) -> str:
        message = delegate_result(server, call_id)
        return fake_model.message_text(message) if message else ""

    def early():
        text = result("call_delegate")
        ok = ("[boxe] BoxE" in text and "[boxf]" not in text and "[boxg]" not in text
              and "Still running: boxf (" in text and "boxg (" in text.split("Still running:", 1)[-1])
        return ok, f"the first delegate result: {text[:60]!r} ... {text[text.find('Still running'):][:90]!r}"
    verify("run 2: delegate returns with the first worker, naming the two still running", early)

    def fix_beside():
        text = result("call_fix")
        box = bpy.data.objects.get("BoxE")
        widened = box is not None and abs(box.scale.x - WIDEN) < 1e-6
        ok = ("[boxe] BoxE" in text and "Still running: boxf (" in text and "boxg (" in text
              and "Wave 2 started: BoxE." in text and widened)
        return ok, f"BoxE scale x {box.scale.x if box else None}; result: {text[text.find('Still running'):][:90]!r}"
    verify("run 2: a continues re-brief ran and merged while the other two still built", fix_beside)

    def waited():
        text = result("call_wait")
        ok = "[boxf] BoxF" in text and "[boxg]" not in text and "Still running: boxg (" in text
        return ok, f"the wait's result: {text[:60]!r} ... {text[text.find('Still running'):][:60]!r}"
    verify("run 2: delegate wait returns the next worker, with the last still running", waited)

    def late():
        last = server.requests(ORCH)[-1].body.get("messages", []) if server.requests(ORCH) else []
        bounces = [fake_model.message_text(m) for m in last if m.get("role") == "user"
                   and "You replied while workers were still running" in fake_model.message_text(m)]
        checks = [m for m in last if m.get("role") == "user" and "checks fail on the live scene" in fake_model.message_text(m)]
        final = last_assistant(session)
        ok = (len(bounces) == 1 and "[boxg] BoxG" in bounces[0] and "Every worker has reported." in bounces[0]
              and not checks and "Verified by Loopcut on this scene: 7 of 7 checks." in final
              and session.get("turn_outcome") == "completed")
        return ok, f"late bounces {len(bounces)}, check bounces {len(checks)}; final reply ends: {final[-70:]!r}"
    verify("run 2: a reply while a worker still built waited for it and bounced once with its result", late)

    def merges():
        cards = [i for i in session["items"] if i["kind"] == "merge"]
        store = checkpoints._store(session)
        by_wave = {}
        for card in cards:
            by_wave.setdefault(card["wave"], set()).add(card["checkpoint"])
        kinds = {store.get(c).get("kind") for ids in by_wave.values() for c in ids if c}
        present = [n for n in ("BoxE", "BoxF", "BoxG") if n in bpy.context.scene.objects]
        ok = len(cards) == 4 and sorted(len(ids) for ids in by_wave.values()) == [1, 1] and kinds == {"wave"} \
            and len(present) == 3
        return ok, f"{len(cards)} merge cards, checkpoints per wave {[len(i) for i in by_wave.values()]}, kinds {kinds}, " \
                   f"in the scene {present}"
    verify("run 2: a merge card per result, one checkpoint per wave taken at its start", merges)

    def joblog():
        job = (session.get("jobs") or [{}])[-1]
        waves_ = job.get("waves") or []
        first = waves_[0] if waves_ else {}
        order = [w.get("id") for w in first.get("workers") or []]
        calls = [m.get("workers") for m in first.get("merges") or []]
        reported = [(w.get("t") or {}).get("reported") for w in first.get("workers") or []]
        ok = (len(waves_) == 2 and order == ["boxe", "boxf", "boxg"] and calls == [["boxe"], ["boxf"], ["boxg"]]
              and all(isinstance(r, (int, float)) for r in reported) and reported[0] < reported[1] < reported[2]
              and first.get("merge", {}).get("objects") == 3 and float(job.get("waited") or 0) > 0)
        return ok, f"waves {len(waves_)}, order {order}, merges {calls}, reported {reported}, waited {job.get('waited')}"
    verify("run 2: the JobLog has a merge per result, the wave's order and the orchestrator's wait", joblog)
    verify("run 2: no children", lambda: (not alive, f"{len(SEEN)} seen, still running: {sorted(alive)}"))
    verify("run 2: no temporary .blend left", lambda: no_leftovers(started))

    def own_undo_steps():
        # Last, as it rewinds the scene: the late merge (BoxG) goes alone.
        ok = undo() and "BoxG" not in bpy.data.objects and "BoxF" in bpy.data.objects and "BoxE" in bpy.data.objects
        return ok, f"after one undo: {sorted(n for n in ('BoxE', 'BoxF', 'BoxG') if n in bpy.data.objects)}"
    verify("run 2: each merge is its own undo step", own_undo_steps)
    if sum(1 for _, ok, _ in RESULTS if not ok) > failures_before:
        diagnostics(session, server)


def run_three(server: fake_model.Server) -> None:
    from loopcut import agent, state
    bpy.ops.wm.open_mainfile(filepath=str(PROJECT))
    server.reset()
    prefixes = script_run_three(server)
    session = state.reset()
    names_before = {o.name for o in bpy.data.objects}
    started, failures_before = time.time(), sum(1 for _, ok, _ in RESULTS if not ok)
    SEEN.clear()
    if not agent.send(USER):
        raise RuntimeError(f"agent.send refused the message: {errors(session)}")

    def waiting_on_the_slow_one():
        asks = server.requests(ORCH)
        slow = server.requests(WORKER, prefixes[1])
        return len(asks) >= 2 and asks[1].replied and slow and slow[0].replied and len(LIVE) >= 1
    try:
        pump(waiting_on_the_slow_one, RUN_SECONDS, "the orchestrator waiting for BoxD")
        settle(1.0)
    except TimeoutError as ex:
        verdict("run 3: BoxC comes back and the orchestrator waits for BoxD", False, str(ex))
    agent.stop()
    end_turn(session, "run 3")
    alive = children_gone(3)
    verdict("run 3 (Stop while waiting for a worker): no children", not alive,
            f"{len(SEEN)} seen, still running: {sorted(alive)}")

    def only_what_came_back():
        merges = [i for i in session["items"] if i["kind"] == "merge"]
        added = sorted({o.name for o in bpy.data.objects} - names_before)
        quarantine = "LC_Quarantine" in bpy.data.collections
        detail = f"merge cards {len(merges)}, new objects {added}, quarantine collection {quarantine}"
        return len(merges) == 1 and added == ["BoxC"] and not quarantine, detail
    verify("run 3 (Stop while waiting for a worker): BoxC merged, nothing more", only_what_came_back)
    verify("run 3 (Stop while waiting for a worker): no leftovers", lambda: no_leftovers(started))
    if sum(1 for _, ok, _ in RESULTS if not ok) > failures_before:
        diagnostics(session, server)


def cleanup(server: fake_model.Server | None) -> None:
    for pid in children():
        os.kill(pid, signal.SIGKILL)
    try:
        from loopcut import worker_host
        worker_host.shutdown_all()
    except Exception as ex:  # Cleanup must not hide the verdict.
        print(f"shutdown_all: {ex!r}")
    if server is not None:
        server.stop()


def main() -> None:
    checkout.use()
    missing = missing_pieces()
    if missing:
        if not ARGS:
            shutil.rmtree(ROOT, ignore_errors=True)
        finish(1, "M4 E2E NOT READY: this branch lacks what the check runs through (design 07 §4):\n  "
               + "\n  ".join(missing))
    server = None
    try:
        server = fake_model.Server().start()
        environment(server)
        make_project()
        print(f"M4 e2e in {ROOT}", flush=True)
        run_one(server)  # Its two boxes finish within the real BATCH_SECONDS of each other: one result.
        from loopcut import waves
        waves.BATCH_SECONDS = 3.0  # Run 2's held workers finish seconds apart; 20 s would batch them.
        run_two(server)
        run_three(server)
    except Exception:
        traceback.print_exc()
        verdict("the check ran to the end", False, "see the traceback above")
    finally:
        cleanup(server)
    failed = [name for name, ok, _ in RESULTS if not ok]
    if failed:
        finish(1, f"M4 E2E FAILED: {len(failed)} of {len(RESULTS)}; files kept in {ROOT}")
    if not ARGS:
        shutil.rmtree(ROOT, ignore_errors=True)
    finish(0, f"M4 E2E OK: {len(RESULTS)} checks")


main()
