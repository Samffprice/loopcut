"""The single agent's workspace end to end, headless: a scripted model works the way GPT-6.1 Sol and GPT-6
Astra did alone in Codex (workspace.py), against a real Blender, real background Blenders and a real
apply. No network and no tokens: the model is harness/fake_model.py.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/workspace_check.py [-- <dir> [bridge]]

With bridge, the same turn goes the eval's way instead of through fake_model: a real harness/cc_bridge.py
serves the add-on, and evals/codex_answer.py --thread answers it with a stand-in for the codex binary that
replays the script. Workspace mode sends the attached image directly, without a reference-card side
request. Asserted besides: the first step starts a Codex thread and every later step resumes it (the
add-on's keep mode makes each request an extension of the last), so an eval with GPT caches as a Codex
thread does.

The project: a Chest and a Barrel wearing one Wood material, a Lamp and a Camera, saved as the user's
file, and a reference image attached to the request. LOOPCUT_WORKSPACE on, orchestration off.

The turn: shell lists the workspace (source.blend is there, python3 is Blender's own with numpy),
write_file writes paint.py and build.py, shell runs paint.py (a PNG from numpy), run_blender runs
build.py on source.blend (a new ChestPaint material with the painted texture on the Chest, a new
ChestKey light, colour management and a small Workbench preview, result.blend), view_image looks at the
preview, apply_build brings the result in with owns Chest; edit_file raises the key's energy (once with
text that is not there, which fails), run_blender rebuilds from source.blend, apply_build again;
snapshot_scene; inspect_api names LoopTools (activation, or an unknown path in stock Blender); a closing reply.

Asserted: the workspace and its snapshot (stamped, numbered), the attached image copied under its own
name, the craft notes from a neutral local stand-in skill written at run time in LOOPCUT_SKILLS_DIR
for a painterly request, and the workspace note on the request (no private skill text); each tool's
result (numpy through python3, the files a build wrote with image sizes, the image in the next
request, the edit and its failure); after the first apply the live Chest wears ChestPaint with its
image packed, ChestKey is in the scene and the settings came over; after the second, still one
ChestKey and one ChestPaint (the rebuild replaced them, no .001), at the new energy; the Barrel, the
Wood material, the Lamp and the Camera are exactly as they were (ownership tokens), and so is
source.blend until snapshot_scene, which keeps it as source-1.blend; a checkpoint and a changes card.
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
import fake_model  # noqa: E402

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ROOT = Path(ARGS[0] if ARGS else tempfile.mkdtemp(prefix="loopcut-workspace-check-")).resolve()
BRIDGE = "bridge" in ARGS[1:]
EVALS = Path(__file__).resolve().parent / "evals"
if BRIDGE and not EVALS.exists():
    print("SKIP workspace bridge and thread checks (eval harness not in this checkout)", flush=True)
    BRIDGE = False
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state = os.environ["WORKSPACE_CHECK_CODEX"]
replies = json.load(open(os.path.join(state, "replies.json")))
counter = os.path.join(state, "count")
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps({"n": n, "resume": "resume" in args, "prompt_chars": len(args[-1])}) + "\n")
open(args[args.index("-o") + 1], "w").write(json.dumps(replies[n]))
thread = args[args.index("--") - 1] if "resume" in args else "T%d" % n
print(json.dumps({"type": "thread.started", "thread_id": thread}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 900 if "resume" in args else 0,
                                                      "output_tokens": 10, "reasoning_output_tokens": 5}}))
'''
PROJECT = ROOT / "project" / "shop.blend"
REFERENCE = ROOT / "refs" / "painted_ref.png"
DATA = ROOT / "data"
TMP = ROOT / "tmp"
SKILLS = ROOT / "skills"
CRAFT_MARKER = "Stand-in painterly note for workspace_check"
MODEL = "fake-single"
RUN_SECONDS = 600
USER = ("Give the chest a painterly, hand-painted look like the attached still, with a warm key light. "
        "Leave the barrel exactly as it is.")

PAINT = '''import os, struct, zlib
import numpy as np

def png(path, rgb):
    h, w, _ = rgb.shape
    raw = b"".join(b"\\x00" + rgb[y].astype(np.uint8).tobytes() for y in range(h))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    with open(path, "wb") as out:
        out.write(b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                  + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

os.makedirs("paint", exist_ok=True)
y, x = np.mgrid[0:64, 0:96]
png("paint/chest.png", np.stack([110 + x, 60 + y, 40 + 0 * x], -1))
print("painted paint/chest.png with numpy", np.__version__)
'''

BUILD = '''import os
import bpy
ROOT = os.path.dirname(os.path.abspath(__file__))
ENERGY = 500.0
chest = bpy.data.objects["Chest"]
mat = bpy.data.materials.new("ChestPaint")
tree = mat.node_tree
tex = tree.nodes.new("ShaderNodeTexImage")
tex.image = bpy.data.images.load(os.path.join(ROOT, "paint", "chest.png"))
bsdf = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
chest.data.materials.clear()
chest.data.materials.append(mat)
light = bpy.data.objects.new("ChestKey", bpy.data.lights.new("ChestKey", "AREA"))
light.data.energy = ENERGY
light.location = (1.5, -1.5, 2.0)
bpy.context.scene.collection.objects.link(light)
scene = bpy.context.scene
scene.view_settings.look = "AgX - Medium High Contrast"
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 320, 180, 100
scene.render.filepath = os.path.join(ROOT, "preview.png")
bpy.ops.render.render(write_still=True)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(ROOT, "result.blend"))
print("BUILD OK energy", ENERGY)
'''

CRATE = '''import bpy
old = bpy.data.objects["Crate"]
location = tuple(old.location)
bpy.data.objects.remove(old)
mesh = bpy.data.meshes.new("Crate")
s = 0.3
mesh.from_pydata([(x * s, y * s, z * s) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], [],
                 [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)])
crate = bpy.data.objects.new("Crate", mesh)
crate.location = location
bpy.context.scene.collection.objects.link(crate)
studio = bpy.data.scenes.new("Studio")  # A preview scene of its own, saved active: it must not move the user's camera.
studio.camera = bpy.data.objects.new("StudioCam", bpy.data.cameras.new("StudioCam"))
studio.collection.objects.link(studio.camera)
studio.render.engine = "CYCLES"
bpy.context.window.scene = studio
bpy.ops.wm.save_as_mainfile(filepath="crate_result.blend")
print("CRATE REBUILT")
'''

RESULTS: list = []


def finish(code: int, message: str):
    print(message, file=sys.stderr if code else sys.stdout)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def verdict(name: str, ok: bool, detail: str = "") -> None:
    detail = detail if isinstance(detail, str) else repr(detail)
    RESULTS.append((name, bool(ok), detail))
    print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)


def verify(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception as ex:
        traceback.print_exc()
        ok, detail = False, f"{type(ex).__name__}: {ex}"
    verdict(name, ok, detail)


def environment(base_url: str) -> None:
    for folder in (DATA, TMP, SKILLS, PROJECT.parent, REFERENCE.parent):
        folder.mkdir(parents=True, exist_ok=True)
    (SKILLS / "painterly.md").write_text(f"""---
id: workspace-check/painterly
version: 1
title: Workspace check stand-in
when: A painterly workspace check request
roles: [single, workspace]
requires_tools: [run_python]
tier: free
kind: assist
part: single
words: painterly|hand-painted
---
{CRAFT_MARKER}
""", encoding="utf-8")
    env_file = ROOT / "workspace_check.env"
    env_file.write_text("", encoding="utf-8")  # So config never walks up to a developer's .env.
    for key in [k for k in os.environ if k.startswith("LOOPCUT_")]:
        del os.environ[key]
    os.environ.update({
        "LOOPCUT_ENV_FILE": str(env_file), "LOOPCUT_DATA_DIR": str(DATA), "TMPDIR": str(TMP),
        "LOOPCUT_SKILLS_DIR": str(SKILLS),
        "LOOPCUT_BASE_URL": base_url, "LOOPCUT_API_KEY": "fake-key", "LOOPCUT_MODEL": MODEL,
        "LOOPCUT_ORCHESTRATE": "false", "LOOPCUT_WORKSPACE": "true",
        "LOOPCUT_AUTO_RUN": "true", "LOOPCUT_AUTO_LOOK": "false", "LOOPCUT_MAX_STEPS": "40",
        "LOOPCUT_KEEP_CONTEXT": "true", "LOOPCUT_CONTEXT_BUDGET": "400000",
    })
    tempfile.tempdir = str(TMP)


def make_project() -> None:
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    wood = bpy.data.materials.new("Wood")
    wood.diffuse_color = (0.4, 0.25, 0.1, 1.0)
    for name, x in (("Chest", -1.0), ("Barrel", 1.0), ("Crate", 3.0)):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, 0.0, 0.5))
        ob = bpy.context.active_object
        ob.name = ob.data.name = name
        ob.data.materials.append(wood)
    lamp = bpy.data.objects.new("Lamp", bpy.data.lights.new("Lamp", "POINT"))
    lamp.location = (0.0, -3.0, 3.0)
    bpy.context.scene.collection.objects.link(lamp)
    camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    camera.location = (0.0, -7.0, 2.5)
    camera.rotation_euler = (1.35, 0.0, 0.0)
    bpy.context.scene.collection.objects.link(camera)
    bpy.context.scene.camera = camera
    bpy.ops.wm.save_as_mainfile(filepath=str(PROJECT))
    image = bpy.data.images.new("painted_ref", 48, 32)
    image.pixels.foreach_set([0.6, 0.4, 0.2, 1.0] * (48 * 32))
    image.filepath_raw, image.file_format = str(REFERENCE), "PNG"
    image.save()
    bpy.data.images.remove(image)


def steps() -> list:
    """The workspace turn: ("text", words) or (tool, arguments), with no reference-card side reply."""
    return [
        ("shell", {"command": "ls; test -f source.blend && echo HAVE_SOURCE; python3 -c "
                              "\"import numpy, sys; print('numpy', numpy.__version__, sys.executable)\""}),
        ("write_file", {"path": "paint.py", "content": PAINT}),
        ("write_file", {"path": "build.py", "content": BUILD}),
        ("shell", {"command": "python3 paint.py"}),
        ("run_blender", {"script": "build.py"}),
        ("view_image", {"path": "preview.png"}),
        ("apply_build", {"blend": "result.blend", "owns": ["Chest"]}),
        ("edit_file", {"path": "build.py", "old_text": "ENERGY = 999.0", "new_text": "ENERGY = 800.0"}),
        ("edit_file", {"path": "build.py", "old_text": "ENERGY = 500.0", "new_text": "ENERGY = 800.0"}),
        ("run_blender", {"script": "build.py"}),
        ("apply_build", {"blend": "result.blend", "owns": ["Chest"]}),
        ("shell", {"command": "exit 3"}),
        ("snapshot_scene", {}),
        ("write_file", {"path": "crate.py", "content": CRATE}),
        ("run_blender", {"script": "crate.py"}),
        ("apply_build", {"blend": "crate_result.blend", "owns": ["Crate"]}),
        ("inspect_api", {"path": "looptools"}),
        ("text", "The chest is painted and lit; the barrel is as it was."),
    ]


class FakeModel:
    """harness/fake_model.py: the replies as SSE."""

    def __init__(self):
        self.server = fake_model.Server().start()
        self.base_url = self.server.base_url
        self.server.script(MODEL, [fake_model.text_reply(step[1]) if step[0] == "text" else
                                   fake_model.tool_reply(step[0], step[1], f"call_{n:02d}")
                                   for n, step in enumerate(steps())])

    def bodies(self) -> list:
        return [request.body for request in self.server.requests(MODEL)]

    def problems(self) -> list:
        left = self.server.remaining()
        return list(self.server.errors) + ([f"unused replies {left}"] if left else [])

    def finish(self) -> None:
        pass

    def stop(self) -> None:
        self.server.stop()


class BridgeModel:
    """The eval path: cc_bridge.py serving the add-on, and codex_answer.py --thread answering it with a
    stand-in for the codex binary (FAKE_CODEX) that replays steps() in order."""

    def __init__(self):
        sys.path.insert(0, str(EVALS))
        import codex_answer
        self.run_dir, self.state = (ROOT / "bridge").resolve(), ROOT / "codex"
        self.state.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        replies = [{"content": step[1], "tool_calls": [], "notes": "scripted"} if step[0] == "text" else
                   {"content": f"Step {n}.", "tool_calls": [{"name": step[0], "arguments_json": json.dumps(step[1])}],
                    "notes": "scripted"} for n, step in enumerate(steps())]
        (self.state / "replies.json").write_text(json.dumps(replies), encoding="utf-8")
        self.replies = len(replies)
        fake = self.state / "codex"
        fake.write_text(FAKE_CODEX, encoding="utf-8")
        fake.chmod(0o755)
        os.environ["WORKSPACE_CHECK_CODEX"] = str(self.state)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.log = (ROOT / "bridge.log").open("w")
        self.server = subprocess.Popen([sys.executable, str(Path(__file__).with_name("cc_bridge.py")), "serve",
                                        "--port", str(port), "--run-dir", str(self.run_dir)],
                                       stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("cc_bridge did not start; see bridge.log")
                time.sleep(0.1)
        self.base_url = f"http://127.0.0.1:{port}/v1"
        codex_answer.CODEX = fake
        self.codex_answer = codex_answer
        opts = SimpleNamespace(model="gpt-6.1-sol", effort="xhigh", workers=1, max_calls=None, call_seconds=600,
                               thread=True, primary=self.run_dir)
        self.driver = threading.Thread(target=codex_answer.dispatch, args=([self.run_dir], opts),
                                       name="codex-answer", daemon=True)
        self.driver.start()

    def bodies(self) -> list:
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.run_dir.glob("[0-9]*/request.json"))]

    def problems(self) -> list:
        count = int((self.state / "count").read_text()) if (self.state / "count").is_file() else 0
        return [] if count == self.replies else [f"{count} of {self.replies} replies used"]

    def usage(self) -> list:
        return self.codex_answer.read_usage([self.run_dir])

    def finish(self) -> None:
        (self.run_dir / "done").touch()
        self.driver.join(timeout=30)

    def stop(self) -> None:
        self.server.terminate()
        self.server.wait(timeout=10)
        self.log.close()


def pump(until, seconds: float, what: str) -> None:
    from loopcut import mainthread
    end = time.monotonic() + seconds
    while not until():
        mainthread._pump()
        if time.monotonic() > end:
            raise TimeoutError(f"no {what} after {seconds:.0f} s")
        time.sleep(0.02)


def tokens(names: list) -> dict:
    from loopcut import ownership
    return {key: ownership.token(ownership.find(key)) for key in names if ownership.find(key) is not None}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def last_body(model) -> dict:
    """The longest request: in keep mode, the turn's whole conversation."""
    return max(model.bodies(), key=lambda body: len(body.get("messages", [])), default={})


def tool_text(model, number: int) -> str:
    """The result of the one-based tool call, as the model's last request carried it."""
    results = [m for m in last_body(model).get("messages", []) if m.get("role") == "tool"]
    return fake_model.message_text(results[number - 1]) if len(results) >= number else ""


def image_after(model, number: int) -> bool:
    """Whether the message right after the number-th call's result carries an image."""
    messages = last_body(model).get("messages", [])
    at = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(at) < number:
        return False
    later = messages[at[number - 1] + 1:at[number - 1] + 3]
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
               for m in later)


def run(model) -> None:
    from loopcut import agent, attachments, bundled_addons, state, workspace
    has_bundles = bool(bundled_addons.packages())
    session = state.reset()
    kept = ["OBJECT:Barrel", "MESH:Barrel", "MATERIAL:Wood", "OBJECT:Lamp", "LIGHT:Lamp", "OBJECT:Camera",
            "CAMERA:Camera"]
    before = tokens(kept)
    problems = attachments.add(session, [str(REFERENCE)])
    if problems:
        raise RuntimeError(f"could not attach the reference: {problems}")
    if not agent.send(USER):
        raise RuntimeError(f"agent.send refused: {[i['text'] for i in session['items'] if i['kind'] == 'error']}")
    ws = workspace.folder(session["id"]).resolve()
    source_digest = {}

    def snapped():
        if (ws / "source.blend").is_file() and workspace.load_state(ws)["snapshots"][:1] and \
                workspace.load_state(ws)["snapshots"][0].get("stamped"):
            source_digest.setdefault("first", digest(ws / "source.blend"))
            return True
        return False
    pump(lambda: snapped() or not session["busy"], 120, "stamped snapshot")
    pump(lambda: not session["busy"], RUN_SECONDS, "end of the turn")
    for item in session["items"]:
        if item["kind"] in ("error", "notice"):
            print(f"  item {item['kind']}: {str(item.get('text'))[:300]}")
    model.finish()
    for line in model.problems():
        print(f"  model: {line}")
    state_data = workspace.load_state(ws)

    verify("the turn completed", lambda: (session.get("turn_outcome") == "completed", session.get("turn_outcome")))
    verify("the conversation is in workspace mode", lambda: (session.get("mode") == "workspace", session.get("mode")))
    verify("the workspace request offers inspect_api", lambda: (
        'inspect_api' in [s['function']['name'] for s in model.bodies()[0].get('tools', [])], 'live tool schema'))
    verify("the workspace has source.blend, the shims and the attached image under its own name", lambda: (
        all((ws / p).exists() for p in ("source-1.blend", ".loopcut/bin/blender", ".loopcut/bin/python3",
                                       "painted_ref.png")), sorted(p.name for p in ws.iterdir())))
    text = fake_model.first_user_text(model.bodies()[0])
    verify("the request carries the workspace note and local stand-in craft notes, without a reference card", lambda: (
        "<workspace path=" in text and "snapshot 1" in text and "<craft_notes>" in text and CRAFT_MARKER in text
        and "<reference_card" not in text,
        text[text.find("<workspace"):][:300]))
    out1 = tool_text(model, 1)
    verify("shell runs in the workspace, python3 is Blender's own with numpy", lambda: (
        "HAVE_SOURCE" in out1 and "numpy" in out1 and sys.executable in out1, out1[:300]))
    out4 = tool_text(model, 4)
    verify("shell ran paint.py", lambda: ("exit 0" in out4 and "painted paint/chest.png" in out4, out4[:200]))
    out5 = tool_text(model, 5)
    verify("run_blender reports the files the build wrote, with the preview's size", lambda: (
        "exit 0" in out5 and "result.blend" in out5 and "preview.png (320x180)" in out5 and "BUILD OK" in out5,
        out5[:600]))
    verify("view_image's image is in the next request", lambda: (image_after(model, 6), tool_text(model, 6)[:200]))
    out7 = tool_text(model, 7)
    verify("the first apply created ChestPaint and ChestKey, replaced the Chest, brought the settings", lambda: (
        "ChestKey" in out7 and "ChestPaint" in out7 and "OBJECT:Chest" in out7 and "view_settings.look" in out7, out7[:900]))
    out8 = tool_text(model, 8)
    verify("edit_file with text that is not there fails and says so", lambda: ("is not in build.py" in out8, out8[:200]))
    out9 = tool_text(model, 9)
    verify("edit_file replaced the energy", lambda: ("1 replacement(s) at line" in out9, out9[:200]))
    out11 = tool_text(model, 11)
    verify("the second apply replaced what the first brought", lambda: (
        "LIGHT:ChestKey" in out11 and "Renamed" not in out11 and "Kept live" not in out11, out11[:900]))
    out12 = tool_text(model, 12)
    verify("a failing command reports its exit code", lambda: ("exit 3" in out12, out12[:100]))

    def live_state():
        chest, keys = bpy.data.objects.get("Chest"), [o for o in bpy.data.objects if o.name.startswith("ChestKey")]
        paints = [m for m in bpy.data.materials if m.name.startswith("ChestPaint")]
        images = [i for i in bpy.data.images if i.name.startswith("chest")]
        ok = (chest is not None and [m.name for m in chest.data.materials] == ["ChestPaint"] and len(keys) == 1
              and keys[0].name == "ChestKey" and abs(keys[0].data.energy - 800.0) < 1e-3 and len(paints) == 1
              and len(images) == 1 and images[0].packed_file is not None
              and bpy.context.scene.view_settings.look == "AgX - Medium High Contrast")
        return ok, (f"chest materials {[m.name for m in chest.data.materials] if chest else None}, keys "
                    f"{[(o.name, o.data.energy) for o in keys]}, paints {[m.name for m in paints]}, images "
                    f"{[(i.name, i.packed_file is not None) for i in images]}, look {bpy.context.scene.view_settings.look}")
    verify("live: one ChestKey at 800 W, one ChestPaint, its image packed, the look applied", live_state)
    verify("the Barrel, Wood, the Lamp and the Camera are exactly as they were", lambda: (
        tokens(kept) == before, f"changed: {[k for k in kept if tokens([k]).get(k) != before.get(k)]}"))
    verify("source.blend was untouched by the builds; snapshot_scene kept it as source-1.blend", lambda: (
        source_digest.get("first") == digest(ws / "source-1.blend") and (ws / "source.blend").is_file()
        and [s["number"] for s in state_data["snapshots"]] == [1, 2] and all(s["stamped"] for s in state_data["snapshots"]),
        f"snapshots {[(s['number'], s['file'], s['stamped']) for s in state_data['snapshots']]}"))
    verify("three applies recorded, their reports kept", lambda: (
        len(state_data["applies"]) == 3 and (ws / ".loopcut" / "apply-2" / "report.json").is_file(),
        f"{len(state_data['applies'])} applies"))
    user_item = next(i for i in session["items"] if i["kind"] == "user")
    verify("a checkpoint was taken and the turn's changes card is there", lambda: (
        bool(user_item.get("checkpoint")) and any(i["kind"] == "changes" for i in session["items"]),
        f"checkpoint {user_item.get('checkpoint')!r}"))
    out16 = tool_text(model, 16)
    verify("a build from the new snapshot that made the Crate anew replaces it under its own name", lambda: (
        "OBJECT:Crate" in out16 and "snapshot 2" in out16 and [o.name for o in bpy.data.objects if o.name.startswith("Crate")]
        == ["Crate"] and abs(bpy.data.objects["Crate"].dimensions.x - 0.6) < 1e-4, out16[:600]))
    def studio():
        main, studio_ = bpy.context.scene, bpy.data.scenes.get("Studio")
        ok = ("its own scene Studio active" in out16 and "Scene Studio, the build's own" in out16 and main.name == "Scene"
              and main.camera.name == "Camera" and main.render.engine == "BLENDER_WORKBENCH" and studio_ is not None
              and studio_.camera is not None and studio_.camera.name == "StudioCam" and studio_.render.engine == "CYCLES"
              and "StudioCam" not in main.objects)
        return ok, (f"main {main.name} camera {main.camera.name} engine {main.render.engine}; scenes "
                    f"{list(bpy.data.scenes.keys())}; studio camera {studio_.camera.name if studio_ and studio_.camera else None}"
                    f" engine {studio_.render.engine if studio_ else None}; StudioCam in main {'StudioCam' in main.objects}")
    verify("a studio scene the build made comes in as a scene of its own; the user's camera and engine stay", studio)
    out17 = tool_text(model, 17)
    verify("inspect_api activates bundled LoopTools, or reports the unknown path in stock Blender", lambda: (
        ('Turned on the bundled LoopTools add-on for this session.' in out17 if has_bundles
         else "Nothing called 'looptools'" in out17), out17[:600]))
    verify("every scripted reply was used", lambda: (not model.problems(), "; ".join(model.problems())))
    if BRIDGE:
        def threaded():
            rows = sorted(model.usage(), key=lambda row: row["request"])
            modes = [(row["role"], row["thread"]) for row in rows]
            ok = modes == [("single", "fresh")] + [("single", "resume")] * (len(steps()) - 1)
            return ok, str(modes)
        verify("through the bridge: one Codex thread from the first step, without a reference-card request", threaded)


def main() -> None:
    checkout.use()
    model = None
    try:
        model = BridgeModel() if BRIDGE else FakeModel()
        environment(model.base_url)
        make_project()
        print(f"workspace check in {ROOT}" + (" through cc_bridge and codex_answer --thread" if BRIDGE else ""), flush=True)
        run(model)
    except Exception:
        traceback.print_exc()
        verdict("the check ran to the end", False, "see the traceback above")
    finally:
        if model is not None:
            model.stop()
    failed = [name for name, ok, _ in RESULTS if not ok]
    if failed:
        finish(1, f"WORKSPACE CHECK FAILED: {len(failed)} of {len(RESULTS)}; files kept in {ROOT}")
    if not ARGS:
        shutil.rmtree(ROOT, ignore_errors=True)
    finish(0, f"WORKSPACE CHECK OK: {len(RESULTS)} checks")


main()
