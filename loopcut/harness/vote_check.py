"""The vote view end to end: a stand-in /v1 service, the real add-on, real clicks, screenshots.

    LOOPCUT_SHOTS=<dir> Blender --factory-startup --enable-event-simulate -p 0 0 1440 900 \
        --python harness/vote_check.py

The service (a thread in this process) serves fixtures/vote_manifest.json (loopcut.org's contract
0.6 manifest, long descriptions cut to the panel summaries) and /v1/votes as proposal 011 describes
it, seeded with other voters, with the server's smoothing: level = (n + K) / (max + K) in 0.05
steps. Writes <LOOPCUT_SHOTS>/vote-*.png (panel crops) and prints VOTE OK, or VOTE FAILED with the
step.
"""
import json
import os
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "fixtures" / "vote_manifest.json").read_text(encoding="utf-8"))
TOOLS = [t["name"] for t in MANIFEST["tools"] if t.get("status") == "coming_soon"]
SEED = {"render": 9, "image_to_3d": 7, "asset_search": 5, "retopology": 3, "auto_rig": 3, "image_generate": 2,
        "sfx": 2, "voice_line": 1, "image_edit": 1}
K, MAX_VOTES = 3, 3
VOTES: dict[str, set] = {}   # install id -> tools
CALLS: list[tuple[str, str]] = []


def tally(voter):
    counts = {tool: SEED.get(tool, 0) + sum(tool in mine for mine in VOTES.values()) for tool in TOOLS}
    top = max(counts.values())
    ranked = sorted(counts.values(), reverse=True)
    rows = []
    for tool in TOOLS:
        level = max(0.05, min(1.0, round((counts[tool] + K) / (top + K) * 20) / 20))
        badge = ("most_wanted" if counts[tool] == top and top >= 3 and ranked[0] > ranked[1]
                 else "popular" if level >= 0.7 and counts[tool] != top else None)
        rows.append({"tool": tool, "level": level, "badge": badge})
    return {"max_votes": MAX_VOTES, "mine": sorted(VOTES.get(voter, set())), "tools": rows}


class Service(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _error(self, status, code):
        # The service's envelope, as the add-on gets it from loopcut.org.
        self._send(status, {"error": {"message": code.replace("_", " "), "type": code, "code": code}})

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _voter(self):
        parts = urlsplit(self.path)
        voter = parse_qs(parts.query).get("install_id", [""])[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            voter = json.loads(self.rfile.read(length)).get("install_id", voter)
        return parts.path, voter

    def do_GET(self):
        path, voter = self._voter()
        CALLS.append(("GET", path))
        if path == "/v1/manifest":
            return self._send(200, MANIFEST)
        if path == "/v1/votes":
            return self._send(200, tally(voter))
        self._error(404, "not_found")

    def _vote(self, casting):
        path, voter = self._voter()
        CALLS.append(("PUT" if casting else "DELETE", path))
        tool = path.rsplit("/", 1)[-1]
        if not path.startswith("/v1/votes/") or tool not in TOOLS:
            return self._error(404, "unknown_tool")
        mine = VOTES.setdefault(voter, set())
        if casting and tool not in mine and len(mine) >= MAX_VOTES:
            return self._error(409, "vote_limit")
        (mine.add if casting else mine.discard)(tool)
        self._send(200, tally(voter))

    def do_PUT(self):
        self._vote(True)

    def do_DELETE(self):
        self._vote(False)


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Service)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
os.environ["LOOPCUT_SERVICE_URL"] = f"http://127.0.0.1:{SERVER.server_address[1]}"
os.environ.setdefault("LOOPCUT_DATA_DIR", tempfile.mkdtemp(prefix="loopcut-harness-"))

import bpy  # noqa: E402

sys.path.insert(0, str(HERE))
import checkout  # noqa: E402
checkout.use()

import loopcut  # noqa: E402
from loopcut import cloud, state  # noqa: E402
from loopcut.ui import host  # noqa: E402

SHOTS = Path(os.environ.get("LOOPCUT_SHOTS", tempfile.mkdtemp(prefix="loopcut-vote-")))
STATE: dict = {}
STEPS: list = []


def step(fn):
    STEPS.append(fn)
    return fn


def finish(code, message):
    print(message, file=sys.stderr if code else sys.stdout)
    sys.stdout.flush()
    sys.stderr.flush()
    SERVER.shutdown()
    os._exit(code)


def run_next():
    try:
        delay = STEPS.pop(0)()
    except Exception:
        traceback.print_exc()
        finish(1, "VOTE FAILED")
    if not STEPS:
        finish(0, f"VOTE OK: {SHOTS}")
    return delay if isinstance(delay, (int, float)) else 0.35


def display():
    return host._displays[STATE["panel"].as_pointer()]


def hit_center(id):
    hit = next(h for h in display()["hits"] if h["id"] == id)
    region = next(r for r in STATE["panel"].regions if r.type == "WINDOW")
    return region.x + int(hit["x"] + hit["w"] / 2), region.y + int(display()["height"] - (hit["y"] + hit["h"] / 2))


def click(id):
    x, y = hit_center(id)
    window = STATE["window"]
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)


def move_to(id):
    x, y = hit_center(id)
    STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)


def shot(name):
    """A screenshot reads the frame before the last one drawn, so the state is drawn twice before capture."""
    STATE["panel"].tag_redraw()

    def again():
        STATE["panel"].tag_redraw()
        return 0.12

    def capture():
        import numpy as np
        full = SHOTS / f"vote-{name}.full.png"
        with bpy.context.temp_override(window=STATE["window"]):
            bpy.ops.screen.screenshot(filepath=str(full))
        image = bpy.data.images.load(str(full))
        width, height = image.size
        pixels = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
        panel = STATE["panel"]
        crop = pixels.reshape(height, width, 4)[panel.y:panel.y + panel.height, panel.x:panel.x + panel.width]
        out = bpy.data.images.new("vote_" + name, crop.shape[1], crop.shape[0], alpha=True)
        out.pixels.foreach_set(np.ascontiguousarray(crop).ravel())
        out.filepath_raw, out.file_format = str(SHOTS / f"vote-{name}.png"), "PNG"
        out.save()
        full.unlink()
        return 0.05
    STEPS[0:0] = [again, capture]
    return 0.12


def wait_for(condition, what, timeout=6.0):
    """Insert a polling step that waits until condition() holds."""
    import time
    deadline = time.monotonic() + timeout

    def poll():
        if condition():
            return 0.05
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}: view={state.ui.get('view')} "
                                 f"calls={CALLS[-4:]} votes={getattr(cloud._runtime, 'votes', None)} "
                                 f"notice={cloud.vote_notice()!r}")
        STEPS.insert(0, poll)
        return 0.1
    STEPS.insert(0, poll)


@step
def open_panel():
    SHOTS.mkdir(parents=True, exist_ok=True)
    window = bpy.context.window_manager.windows[0]
    STATE["window"] = window
    if host.find_panel(window) is None:
        view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
        region = next(r for r in view.regions if r.type == "WINDOW")
        with bpy.context.temp_override(window=window, area=view, region=region):
            bpy.ops.screen.area_split(direction="VERTICAL", factor=0.7)
    return 0.3


@step
def show_panel():
    window = STATE["window"]
    panel = host.find_panel(window)
    if panel is None:
        panel = max((a for a in window.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.x)
        with bpy.context.temp_override(window=window, area=panel):
            bpy.ops.loopcut.open()
    STATE["panel"] = panel
    state.reset()
    cloud.refresh_async()
    wait_for(lambda: len(cloud.coming_soon()) == len(TOOLS) and any(h["id"] == "vote.strip" for h in display()["hits"]),
             "the catalog and the strip")


@step
def chat_strip():
    texts = {p["id"]: p.get("text") for p in display()["prims"] if p["t"] == "text"}
    assert "vote.strip.label" in texts, "no vote strip under the composer"


@step
def wake():
    # The first simulated click in a freshly opened window is taken by the window itself.
    click("input.card")
    return 0.5


@step
def open_votes():
    click("vote.strip")
    wait_for(lambda: state.ui.get("view") == "vote" and state.ui.get("vote_order"), "the ranked vote view")


@step
def intro_frame():
    return shot("2-intro")


@step
def opened():
    order = state.ui["vote_order"]
    assert order[0] == "render", f"most wanted first, got {order}"
    STATE["pick"] = order[4]
    return shot("3-open")


@step
def cast():
    click("vote." + STATE["pick"])
    wait_for(lambda: ("PUT", "/v1/votes/" + STATE["pick"]) in CALLS and not cloud._runtime.vote_states,
             "the first vote to land")


@step
def mid_ease():
    return shot("4-voting")


@step
def voted():
    rows = {r["name"]: r for r in cloud.coming_soon()}
    assert rows[STATE["pick"]]["voted"], "the vote did not stick"
    assert state.ui["vote_order"][4] == STATE["pick"], "rows moved while voting"
    for name in state.ui["vote_order"][5:7]:
        click("vote." + name)
    wait_for(lambda: sum(r["voted"] for r in cloud.coming_soon()) == 3 and not cloud._runtime.vote_states,
             "three votes")


@step
def over_limit():
    calls = len(CALLS)
    click("vote." + state.ui["vote_order"][0])
    wait_for(lambda: cloud.vote_notice(), "the limit notice")
    STATE["calls"] = calls


@step
def limit_shot():
    assert len(CALLS) == STATE["calls"], "a fourth vote went to the server"
    return shot("5-limit")


@step
def take_back():
    click("vote." + STATE["pick"])
    wait_for(lambda: ("DELETE", "/v1/votes/" + STATE["pick"]) in CALLS and not cloud._runtime.vote_states,
             "the take-back")


@step
def hover():
    assert not cloud.vote_notice(), "the notice should clear on the next vote"
    assert STATE["pick"] not in VOTES[next(iter(VOTES))], "the server still holds the vote"
    move_to("vote." + state.ui["vote_order"][2])
    return 0.6


@step
def hover_shot():
    return shot("6-hover")


@step
def back():
    click("vote.back")
    wait_for(lambda: state.ui.get("view") == "chat", "back to chat")


@step
def done():
    return shot("1-chat")


loopcut.register()
bpy.app.timers.register(run_next, first_interval=0.5)
