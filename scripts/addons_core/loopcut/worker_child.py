"""A worker's background Blender. worker_host.Child starts it on the wave's spawn copy:

    Blender -b --factory-startup --disable-autoexec --python-exit-code 1 \\
        --python worker_child.py -- --job <job> --worker <id> --spawn <wave>/spawn.blend --port <port> --parent <pid>

The authentication token arrives on redirected stdin. Connect before loading the add-on or scene,
so their failures keep the normal crash diagnostics and do not disable workers for the conversation.

It runs the worker's scene tools and nothing else: no model calls, no keys (worker_ipc.child_env),
no external network. Requests and replies use an authenticated loopback socket (worker_ipc); the
serve loop runs on Blender's main thread, so bpy is safe with no timers. Ops:

- init: stamp every local datablock with its spawn name (ownership.stamp_all), keep the spawn
  tokens, probe undo, set up the role's looks and warm the render engine;
- tool: one tool call. run_python, import_asset and frame_camera (unless report_only) are rolled
  back when they change a datablock this worker neither owns nor created under its prefix, and
  the worker's own meshes are repaired after them (worker_capture.repair_meshes), so a face its
  code left without corners cannot crash the next look;
- check: the brief's checks (acceptance.run);
- finish: result.blend and manifest.json (ownership.write_result), with what the deliverable
  measures (facts), the deliverable when it does not exist (missing) and the contact sheet cell;
- exit: leave at once.

It dies with its parent: EOF on the connection, or a watchdog thread seeing its parent exit.
"""

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import bpy

PACKAGE = Path(__file__).resolve().parent
STARTED = time.monotonic()
WATCH_SECONDS = 0.5
ROLLBACK_NOTE = ("Rolled back: this call changed {names}, which your brief doesn't own. Nothing from this call was "
                 "kept. Redo it without touching {them}, or file a change request.")
UNDONE_NOTE = ("This call changed {names}, which your brief doesn't own, and it could not be rolled back here. The "
               "merge will drop those edits; leave {them} alone.")
SURVIVED_NOTE = "Undo could not put back {names}; the merge will drop those edits, so leave {them} alone."
MATERIAL_NOTE = ("{name} is a palette material, everyone's. To change how your own parts look, copy it: "
                 "m = bpy.data.materials['{name}'].copy(); m.name = '{prefix}_{base}'; edit m and assign it only to "
                 "objects you created or own.")
ADVISORY_LIMIT = 12
CRAFT_LOOK = ("Your checks pass. Your deliverable as the user will see it, in colour, is attached: your craft-pass "
              "view. Compare it with the brief's style before you report.")


def _watch_parent(parent: int) -> None:
    """Leave when the parent is gone: liveness on Windows, reparenting to launchd/init on POSIX.
    A thread, so it also fires while the serve loop waits; not while a native call holds the GIL,
    which is what worker_host.reap_orphans is for."""
    from loopcut import jobs

    def watch():
        while True:
            time.sleep(WATCH_SECONDS)
            # Windows retains the original parent PID after exit; it never reparents to init.
            gone = not jobs._alive(parent) if os.name == "nt" else os.getppid() != parent
            if gone:
                os._exit(3)
    threading.Thread(target=watch, name="loopcut-worker-watchdog", daemon=True).start()


def _load_package():
    """This folder's package as `loopcut`, the way harness/checkout.use loads a checkout, and never
    register(): no panel, timers or handlers in a child. A copy the build enabled at startup is
    switched off first."""
    import importlib.util
    loaded = [name for name in sys.modules if name == "loopcut" or name.startswith("loopcut.")]
    if loaded:
        import addon_utils
        addon_utils.disable("loopcut")
        for name in loaded:
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "loopcut", PACKAGE / "__init__.py", submodule_search_locations=[str(PACKAGE)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["loopcut"] = module
    spec.loader.exec_module(module)
    return module


def _peak_rss_mb() -> float | None:
    # Optional telemetry must never prevent the init/tool reply on Windows.
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, OSError):
        return None
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)  # Bytes on macOS, KB on Linux.


def _window_override():
    windows = bpy.context.window_manager.windows
    if not windows:
        return None
    return bpy.context.temp_override(window=windows[0], screen=windows[0].screen)


def _undo_op(name: str, **arguments) -> bool:
    """ed.undo_push / ed.undo with the first window: background Blender has windows but no active one."""
    try:
        override = _window_override()
        if override is None:
            return False
        with override:
            return "FINISHED" in getattr(bpy.ops.ed, name)(**arguments)
    except RuntimeError as ex:
        print(f"worker: {name} failed: {ex}")
        return False


def _names(keys) -> str:
    names = sorted({k.split(":", 1)[1] for k in keys})
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


class Worker:
    def __init__(self, args: dict):
        self.job, self.id = Path(args["job"]), args["worker"]
        self.folder = Path(bpy.data.filepath).parent / self.id   # worker_ipc.worker_dir
        self.folder.mkdir(parents=True, exist_ok=True)
        self.role, self.prefix, self.owned, self.checks, self.boxes = "modeling", "", set(), [], []
        self.advisory: list = []
        self.aspects: list = []
        self.deliverable = ""
        self.brief: dict = {}
        self.spawn_tokens: dict = {}
        self.undo = "none"
        self.not_undone: set = set()   # Keys a rollback could not restore: reported, never rolled back again.
        self.rollbacks = []
        self.warnings = []
        self.craft_look = False        # The final look check_brief attaches once, the first time it passes.

    # ------------------------------------------------------------------ ops

    def serve(self, channel) -> None:
        from loopcut import worker_ipc
        handlers = {"init": self.init, "tool": self.tool, "check": self.check, "finish": self.finish,
                    "exit": lambda payload: {}}
        while True:
            try:
                message = channel.receive()
            except worker_ipc.ProtocolError as ex:
                channel.send(worker_ipc.Reply(-1, False, error=str(ex)).to_message())
                continue
            if message is None:
                _leave(0)  # The parent closed the connection: it is gone or done with us.
            try:
                request = worker_ipc.Request.from_message(message)
            except worker_ipc.ProtocolError as ex:
                number = message.get("id")
                channel.send(worker_ipc.Reply(number if type(number) is int else -1, False, error=str(ex)).to_message())
                continue
            try:
                reply = worker_ipc.Reply(request.id, True, handlers[request.op](request.payload))
            except Exception as ex:  # noqa: BLE001 - whatever breaks, the parent gets a reply, not a hang.
                traceback.print_exc()
                reply = worker_ipc.Reply(request.id, False, error=f"{type(ex).__name__}: {ex}")
            reply.data["peak_rss_mb"] = _peak_rss_mb()
            channel.send(reply.to_message())
            if request.op == "exit":
                _leave(0)

    def init(self, payload: dict) -> dict:
        from loopcut import scene_diff, state, worker_capture
        started = time.monotonic()
        self.role = payload.get("role", "modeling")
        self.aspects = list(payload.get("aspects") or [])
        self.deliverable = payload.get("deliverable", "")
        self.prefix = payload.get("prefix", "")
        self.owned = set(payload.get("owned", []))
        self.checks = payload.get("checks", [])
        self.advisory = payload.get("advisory") or []   # The brief's advisory checks: shown, never failing.
        self.boxes = payload.get("boxes") or []   # The brief's planned boxes, for facts.measure.
        # What the child knows of the brief's words, for what the merge will let through (the
        # advisory): the checks always, requirements and defaults when the parent sends them.
        self.brief = {"deliverable": self.deliverable, "checks": self.checks,
                      "requirements": payload.get("requirements") or [], "defaults": payload.get("defaults") or [],
                      "owns": payload.get("owns") or []}
        from loopcut import ownership
        self.spawn_tokens = ownership.stamp_all()
        state.session()["scene_turn_start"] = scene_diff.snapshot()
        self.undo = self._probe_undo()
        self.warnings = _compatibility()
        capture = payload.get("capture") or {}
        # The look's size is the child's (worker_capture.WIDTH): the parent's "width" is the live
        # viewport capture's and too small to judge detail; "long_side" overrides it.
        worker_capture.configure(self.role, self.folder / "captures", capture.get("long_side") or worker_capture.WIDTH,
                                 capture.get("region") or _region(self.checks),
                                 brief={"deliverable": self.deliverable, "prefix": self.prefix,
                                        "owned": sorted(self.owned), "checks": self.checks,
                                        "reference": payload.get("reference")})
        warm = worker_capture.warm() if payload.get("warm", True) else 0.0
        return {"pid": os.getpid(), "blender": bpy.app.version_string, "stamped": len(self.spawn_tokens),
                "undo": self.undo, "seconds": round(time.monotonic() - STARTED, 3),
                "init_seconds": round(time.monotonic() - started, 3), "warm_seconds": warm,
                "warnings": self.warnings}

    def tool(self, payload: dict) -> dict:
        from loopcut import tools, worker_capture
        started = time.monotonic()
        name, arguments = payload.get("name", ""), dict(payload.get("arguments") or {})
        step = payload.get("step")
        capture = arguments.pop("capture", "") if name == "run_python" else ""
        guarded = name in ("run_python", "import_asset", "uv_unwrap", "bake_paint_masks", "stylize_material") or (
            name == "frame_camera" and not tools.only_reads(name, arguments))
        before = None
        if guarded and name == "import_asset":
            from loopcut import scene_diff
            before = scene_diff.snapshot()
        mark = self._before_call() if guarded else None
        result = self._call(name, arguments, payload, step)
        rolled_back, advisory = None, None
        if guarded:
            if before is not None:
                from loopcut import scene_diff
                result.scene_before, result.scene_after = before, scene_diff.snapshot()
            rolled_back, not_undone = self._after_call(mark)
            them = "them" if len(not_undone) > 1 else "it"
            if rolled_back:
                result.text += "\n\n" + rolled_back["note"]
                if not_undone:
                    result.text += " " + SURVIVED_NOTE.format(names=_names(not_undone), them=them)
            elif not_undone:
                result.text += "\n\n" + UNDONE_NOTE.format(names=_names(not_undone), them=them)
            advisory = self._advisory(not_undone)
            repaired = self._repair()  # Before the look, the checks or the report read what it left.
            if repaired:
                result.text += "\n\n" + worker_capture.repair_note(repaired)
        changed = not rolled_back and result.scene_before is not None and result.scene_before != result.scene_after
        if capture:
            try:
                shot = worker_capture.look(angle=capture, step=step)
                result.text += f"\n\n{shot.text}"
                result.image_path = shot.image_path
            except tools.ToolError as ex:
                result.text += f"\n\nCapture failed: {ex}"
        return {"text": result.text, "image": str(result.image_path) if result.image_path else None,
                "tool_ok": bool(result.ok), "changed": bool(changed), "rolled_back": rolled_back,
                "advisory": advisory, "ms": round((time.monotonic() - started) * 1000)}

    def check(self, payload: dict) -> dict:
        """The brief's checks, then its advisory ones (each result marked "advisory")."""
        return {"results": _run_checks(payload.get("checks", self.checks), self._scope())
                + _run_checks(payload.get("advisory", self.advisory), self._scope(), advisory=True)}

    def _scope(self) -> dict:
        """What is this worker's to make, for the checks: another worker's objects are deferred."""
        return {"prefix": self.prefix, "deliverable": self.deliverable, "owned": sorted(self.owned)}

    def finish(self, payload: dict) -> dict:
        from loopcut import ownership
        # Before write_result, which may leave the scene as it saved it.
        facts, missing, look = self._facts(), self._missing(), self._cell()
        path = self.folder / "result.blend"
        manifest = ownership.write_result(path, self.owned, self.prefix, self.role, self.aspects, self.brief)
        manifest.update(worker=self.id, role=self.role, prefix=self.prefix, rollbacks=self.rollbacks,
                        not_undone=sorted(self.not_undone), warnings=self.warnings, facts=facts, missing=missing,
                        look=look)
        (self.folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        return {"result": str(path), "manifest": manifest}

    def _facts(self) -> dict:
        """What the deliverable measures at report time (facts.py): what the orchestrator reads beside,
        and instead of, what the worker says it built. A measuring bug costs the facts, not the result."""
        from loopcut import facts, worker_capture
        try:
            self._repair()
            return facts.deliverables(worker_capture.own_objects(bpy.context.scene), self.boxes)
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc()
            return {"error": f"{type(ex).__name__}: {ex}"[:200]}

    def _missing(self) -> list[str]:
        """The deliverable, when no object of that name is in the scene at report time: a worker can
        report a prop it never built (a continued brief whose earlier work did not merge)."""
        return [self.deliverable] if self.deliverable and bpy.context.scene.objects.get(self.deliverable) is None else []

    def _repair(self) -> list[str]:
        """worker_capture.repair_meshes on the worker's own objects."""
        from loopcut import worker_capture
        return worker_capture.repair_meshes(worker_capture.own_objects(bpy.context.scene))

    def _cell(self) -> str | None:
        """Its cell on the wave's contact sheet (worker_capture.cell), or None when there is nothing to
        show or the render failed: a missing picture never costs the result."""
        from loopcut import worker_capture
        try:
            return str(worker_capture.cell(self.id, self.folder / "cell"))
        except Exception as ex:  # noqa: BLE001
            print(f"Loopcut worker: no contact sheet cell: {ex}")
            return None

    # ------------------------------------------------------------------ tools

    def _call(self, name: str, arguments: dict, payload: dict, step):
        from loopcut import tools, worker_capture
        try:
            if name == "capture_viewport":
                return worker_capture.look(angle=arguments.get("angle") or None,
                                           focus=arguments.get("focus"), frames=arguments.get("frames"), step=step,
                                           angles=arguments.get("angles"), shading=arguments.get("shading"),
                                           isolate=bool(arguments.get("isolate")), camera=arguments.get("camera"),
                                           view_layer=arguments.get("view_layer"),
                                           labels=arguments.get("labels", False))
            if name == "check_brief":
                return self._check_brief(step)
            if name == "import_asset":
                from loopcut import assets
                rest = {k: v for k, v in arguments.items() if k not in ("source", "id")}
                return assets.load_fetched(payload["fetched"], **rest)
        except tools.ToolError as ex:
            return tools.ToolResult(str(ex), ok=False)
        except TypeError as ex:
            return tools.ToolResult(f"Bad arguments for {name}: {ex}", ok=False)
        refusal = self._not_mine(arguments, name) if name in ("uv_unwrap", "stylize_material") else ""
        if refusal:
            return tools.ToolResult(refusal, ok=False)
        if name in ("run_python", "get_scene_info", "get_object_info", "inspect_api", "check_contact", "frame_camera",
                    "inspect_uv", "uv_unwrap", "inspect_texture", "bake_paint_masks", "stylize_material"):
            return tools.execute(name, json.dumps(arguments))
        return tools.ToolResult(f"{name} is not a tool a worker's Blender runs.", ok=False)

    def _not_mine(self, arguments: dict, name: str = "uv_unwrap") -> str:
        """Why uv_unwrap or stylize_material may not touch these meshes, or "": one that existed at spawn
        and the brief does not own. Refused before it runs, rather than rolled back after (a mesh's token
        counts its UV maps and materials, so run_python changing one is rolled back like any other edit it
        does not own). bake_paint_masks changes no mesh or material: any mesh may be baked."""
        from loopcut import ownership
        names = arguments.get("objects", arguments.get("object"))
        names = [names] if isinstance(names, str) else names if isinstance(names, list) else []
        theirs = [n for n in names if isinstance(n, str) and n in bpy.data.objects
                  and bpy.data.objects[n].type == "MESH" and bpy.data.objects[n].data.get(ownership.STAMP)
                  and ownership.key_of(bpy.data.objects[n].data) not in self.owned]
        if not theirs:
            return ""
        return (f"{name} refused: the mesh of {', '.join(theirs)} is not yours (your brief doesn't own it). If "
                f"the brief needs it {'unwrapped' if name == 'uv_unwrap' else 'restyled'}, file a change request in "
                f"report_back.")

    def _check_brief(self, step):
        """The checks, and the first time they all pass, a final look of the deliverable (framed on it,
        worker_capture.own_focus): LOW workers' craft passes never saw a colour (analysis §5.5, §6.4)."""
        from loopcut import tools, worker_capture
        results = _run_checks(self.checks, self._scope())
        advice = _run_checks(self.advisory, self._scope(), advisory=True)
        result = tools.ToolResult(_checks_text(results) + _advice_text(advice) + _paint_text())
        if self.craft_look or any(r.get("status") not in ("pass", "deferred") for r in results):
            return result
        self.craft_look = True
        try:
            shot = worker_capture.look(angle="final", step=step)
        except tools.ToolError as ex:
            result.text += f"\n\nNo final look: {ex}"
            return result
        result.text += f"\n\n{CRAFT_LOOK}\n{shot.text}"
        result.image_path = shot.image_path
        return result

    # ------------------------------------------------------------------ rollback

    def _probe_undo(self) -> str:
        """"undo" when an undo push and ed.undo put back a change here; else "save" when a per-call
        save works (0.17 s on the heavy fixture); else "none": detection only, the merge still
        drops edits to what this worker does not own."""
        scene = bpy.context.scene
        if _undo_op("undo_push", message="Loopcut worker: probe"):
            scene["lc_undo_probe"] = 1
            if _undo_op("undo_push", message="Loopcut worker: probe change") and _undo_op("undo"):
                if "lc_undo_probe" not in bpy.context.scene:
                    return "undo"
            if "lc_undo_probe" in bpy.context.scene:
                del bpy.context.scene["lc_undo_probe"]
        try:
            bpy.ops.wm.save_as_mainfile(filepath=str(self.folder / "percall.blend"), copy=True, compress=False)
            return "save"
        except RuntimeError:
            return "none"

    def _before_call(self):
        if self.undo == "undo":
            return _undo_op("undo_push", message="Loopcut worker: before call")
        if self.undo == "save":
            bpy.ops.wm.save_as_mainfile(filepath=str(self.folder / "percall.blend"), copy=True, compress=False)
            return True
        return False

    def _changed(self) -> list[str]:
        from loopcut import ownership
        changed = ownership.changed_keys(self.spawn_tokens, self.owned, self.prefix)
        return [k for k in changed if k not in self.not_undone]

    def _after_call(self, saved: bool) -> tuple:
        """(rolled_back or None, keys changed that could not be put back). Once undo has run, the
        whole call is gone, so it is a rollback even when some change survived it; those keys are
        reported and never rolled back for again."""
        bad = self._changed()
        if not bad:
            return None, []
        restored = False
        if saved and self.undo == "undo":
            # The call's own push (run_python, frame_camera) came before its change, as ours did:
            # one undo from a push of the state after it lands on the state before it.
            restored = _undo_op("undo_push", message="Loopcut worker: after call") and _undo_op("undo")
        elif saved and self.undo == "save":
            bpy.ops.wm.open_mainfile(filepath=str(self.folder / "percall.blend"), load_ui=False)
            restored = True
        if not restored:
            self.not_undone.update(bad)
            return None, bad
        survived = sorted(set(bad) & set(self._changed()))
        self.not_undone.update(survived)
        note = ROLLBACK_NOTE.format(names=_names(bad), them="them" if len(bad) > 1 else "it")
        material = next((k.split(":", 1)[1] for k in bad if k.startswith("MATERIAL:")), None)
        if material and "materials" not in self.aspects:
            note += " " + MATERIAL_NOTE.format(name=material, prefix=self.prefix or "My",
                                               base=material.split("_")[-1] or material)
        self.rollbacks.append(bad)
        return {"keys": bad, "note": note, **({"survived": survived} if survived else {})}, survived

    def _advisory(self, not_undone) -> dict:
        """What the merge will quarantine (outside_role), said while the worker can still act on it:
        the same rule as ownership.write_result's manifest. Names are the worker's own; the merge
        renames only a genuine collision, which cannot be known here (it depends on the live scene
        and the rest of the wave at merge time)."""
        from loopcut import ownership
        created = ownership.created_ids()
        owned = {idb.session_uid for idb in ownership.all_ids() if idb.get(ownership.STAMP)
                 and ownership.key_of(idb) in self.owned}
        held = ownership.verdicts(created, self.role, self.aspects, self.brief, owned)
        outside = [f"{idb.id_type}:{idb.name}" for idb in created if not held.get(idb.session_uid, (True,))[0]]
        advisory = {"outside_role": outside[:ADVISORY_LIMIT]}
        if not_undone:
            advisory["not_owned_changed"] = sorted(not_undone)
        return advisory


# ------------------------------------------------------------------ checks

def _run_checks(checks: list, scope: dict | None = None, advisory: bool = False) -> list:
    """The brief's checks on this copy; `unchanged` compares with the stamp_all tokens, and a check
    on another worker's objects is deferred to the live scene (acceptance.run's scope). advisory:
    the brief's advisory list."""
    from loopcut import acceptance
    return acceptance.run(checks, scope=scope, advisory=advisory)


def _checks_text(results: list) -> str:
    if not results:
        return "Your brief has no checks."
    passed = sum(r.get("status") == "pass" for r in results)
    deferred = sum(r.get("status") == "deferred" for r in results)
    here = len(results) - deferred
    lines = [f"{passed} of {here} checks pass" + (f"; {deferred} deferred: {'it names' if deferred == 1 else 'they name'} "
                                                   f"another worker's objects and run{'s' if deferred == 1 else ''} on the "
                                                   f"live scene after the merge, not yours to fix." if deferred else ".")]
    for r in results:
        lines.append(f"{r.get('id')} {r.get('status')}: {r.get('quote', '')}" + (f" ({r['detail']})" if r.get("detail") else ""))
    return "\n".join(lines)


ADVICE_HEAD = ("Advisory (the orchestrator's targets from your brief: they never fail your work or hold up your "
               "report, but the orchestrator reads every miss):")


def _advice_text(results: list) -> str:
    if not results:
        return ""
    lines = [ADVICE_HEAD]
    for r in results:
        status = {"pass": "met", "fail": "missed"}.get(r.get("status"), r.get("status"))
        lines.append(f"{r.get('id')} {status}: {r.get('title')}" + (f" ({r['detail']})" if r.get("detail") else ""))
    return "\n" + "\n".join(lines)


def _paint_text() -> str:
    """The worker's own parts' paint stats (paint_stats.brief_lines): advisory, and a measuring bug
    costs the lines, not check_brief."""
    from loopcut import paint_stats, worker_capture
    try:
        scene = bpy.context.scene
        lines = paint_stats.brief_lines(paint_stats.measured(worker_capture.own_objects(scene), scene))
    except Exception as ex:  # noqa: BLE001
        return f"\nPaint stats: not measured ({type(ex).__name__}: {ex})"[:200]
    return "\n" + "\n".join(lines) if lines else ""


def _region(checks: list):
    """The camera role draws the region of the brief's first in_frame check that has one."""
    return next((c["region"] for c in checks if c.get("kind") == "in_frame" and c.get("region")), None)


def _compatibility() -> list[str]:
    """What may render or evaluate differently here than in the user's Blender: nodes from add-ons
    that are off (--factory-startup), Python drivers that do not run (--disable-autoexec)."""
    notes = []
    trees = [m.node_tree for m in bpy.data.materials if m.node_tree] + list(bpy.data.node_groups) + [
        w.node_tree for w in bpy.data.worlds if w.node_tree]
    undefined = sorted({t.name for t in trees for n in t.nodes if n.bl_idname == "NodeUndefined"})
    if undefined:
        notes.append(f"Nodes from add-ons that are off here, in {', '.join(undefined[:6])}: values may differ.")
    python_drivers = sorted({f"{idb.name}" for collection in (bpy.data.objects, bpy.data.materials, bpy.data.shape_keys)
                             for idb in collection if idb.animation_data
                             for fc in idb.animation_data.drivers
                             if fc.driver.type == "SCRIPTED" and not fc.driver.is_simple_expression})
    if python_drivers:
        notes.append(f"Python drivers do not run here, on {', '.join(python_drivers[:6])}: values may differ.")
    return notes


# ------------------------------------------------------------------ entry

def _leave(code: int) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # No Blender shutdown: nothing is saved, no handlers run.


def _connect():
    # Load only the stdlib transport before authenticating, without executing the add-on package.
    import importlib.util
    spec = importlib.util.spec_from_file_location("_loopcut_worker_ipc", PACKAGE / "worker_ipc.py")
    worker_ipc = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = worker_ipc
    spec.loader.exec_module(worker_ipc)
    args = worker_ipc.parse_child_args(sys.argv)
    with sys.stdin.buffer as credentials:
        token = worker_ipc.read_token(credentials)
    return args, worker_ipc.connect(args["port"], token), worker_ipc


def main() -> None:
    args, channel, protocol = _connect()
    try:
        package = _load_package()
        # Reuse the bootstrap module: Channel.receive and Worker.serve must share ProtocolError.
        package.worker_ipc = protocol
        sys.modules["loopcut.worker_ipc"] = protocol
        _watch_parent(args["parent"])
        if "FINISHED" not in bpy.ops.wm.open_mainfile(filepath=args["spawn"], use_scripts=False):
            raise RuntimeError("could not open the worker's spawn scene")
        Worker(args).serve(channel)
    except Exception:
        # Keep initialization failures in worker.log before EOF lets the parent reap this process.
        traceback.print_exc()
        sys.stderr.flush()
        raise
    finally:
        channel.close()


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        _leave(1)
