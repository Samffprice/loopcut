"""The agent's tools for files on disk and for renders: listing, reading (text, or an image the
model then sees: a render, a texture, a photo), writing, moving, and see_render.

Access rules, applied by the gate in agent.py through needs_approval:
- Reads inside the project (the .blend's folder, the render output folder, Blender's temp folder)
  run unasked; reads elsewhere wait for the user, unless they said "Always allow".
- Every write or move waits for the user, unless they said "Always allow".
- A render (see_render with render=true) waits for the user unless they said "Always allow
  renders"; see tools.is_heavy.
- Hidden files and folders and Loopcut's own data are off limits altogether: they hold keys,
  tokens and conversations, and the model has no business there.
- Nothing here deletes, and nothing overwrites unless asked to in so many words.

needs_approval and the path checks are pure; the tools run on Blender's main thread.
"""

import contextlib
import fnmatch
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import cost_guard, scratch
from .tools import MAX_OUTPUT_CHARS, ToolError, ToolResult, _clip

READ_TOOLS = {"list_files", "read_file"}
WRITE_TOOLS = {"write_file", "move_file", "export_glb"}  # export_glb: the orchestrator's (glb.py).
MAX_ENTRIES = 200            # A folder listing past this says so and asks for a pattern.
MAX_TEXT_BYTES = 2_000_000   # A text file past this is read in line ranges.
IMAGE_SIDE = 768             # The copy the model sees; a viewport capture is 640.
PREVIEW_SIDE = 768           # see_render renders at this size unless full=true...
PREVIEW_SAMPLES = 32         # ...and with this many samples: a look, not the user's final frame.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".tga", ".exr", ".hdr")
# A copy of the last finished render, kept by on_render_complete: the agent's own viewport captures
# go through the same Render Result and would otherwise replace the user's render within a step.
LAST_RENDER = scratch.folder() / "last_render.png"

SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "list_files",
        "title": "Listing files",
        "description": (
            "Files in a folder: name, size, modified. The .blend's folder, the render output folder and "
            "Blender's temp folder are open; elsewhere the user is asked first. Hidden files are never shown."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Folder; // is the .blend's folder, ~ the home folder"},
            "pattern": {"type": "string", "description": "Glob such as *.png; default everything"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "title": "Reading a file",
        "description": (
            "A text file, or a range of its lines; or an image file (png, jpg, exr, ...), which you then "
            "see: a render, a texture, a photo. Same access rule as list_files."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
                      "description": "[first, last] line to read, counted from 1; default the whole file"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "title": "Writing a file",
        "description": "Write a text file. Refuses to replace an existing file unless overwrite is true. Asks the user.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean"},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "move_file",
        "title": "Moving a file",
        "description": (
            "Move a file or folder, or copy it with copy=true; into `destination` when that is an existing "
            "folder. Never replaces anything. Asks the user."),
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string"},
            "destination": {"type": "string"},
            "copy": {"type": "boolean"},
        }, "required": ["source", "destination"]},
    }},
    {"type": "function", "function": {
        "name": "see_render",
        "title": "Viewing the render",
        "description": (
            "The user's last render (F12) as an image you see. With render=true, a new render through the "
            "scene camera: sized for you and capped in samples unless full=true. A render blocks Blender "
            "and waits for the user's OK; prefer capture_viewport while building."),
        "parameters": {"type": "object", "properties": {
            "render": {"type": "boolean", "description": "Render now instead of showing the last render"},
            "full": {"type": "boolean", "description": "The user's own resolution and samples; slow"},
        }},
    }},
]


# ---------------------------------------------------------------- paths and the gate

def project_roots() -> tuple[Path, ...]:
    """Where reads run unasked: where relative paths land (relative_base; always first, which
    needs_approval relies on), the .blend's folder when that is not it (workspace mode), the render
    output folder, Blender's temp folder. Main thread: reads bpy."""
    import bpy
    roots = [relative_base()[0], blend_base()[0]]
    scene = bpy.context.scene
    if scene is not None and scene.render.filepath:
        out = Path(bpy.path.abspath(scene.render.filepath))
        roots.append((out if scene.render.filepath.endswith(("/", "\\")) or out.is_dir() else out.parent).resolve())
    if bpy.app.tempdir:
        roots.append(Path(bpy.app.tempdir).resolve())
    return tuple(dict.fromkeys(roots))


def relative_base() -> tuple[Path, str]:
    """(folder, note): where a relative path the model writes lands: the workspace in workspace mode
    (workspace.py: the shell's working directory too), else blend_base. Main thread."""
    from . import workspace
    ws = workspace.current()
    if ws is not None:
        return ws, ""
    return blend_base()


def blend_base() -> tuple[Path, str]:
    """(folder, note): what Blender's // means, and outside workspace mode where a relative path the
    model writes lands; when that is not the .blend's folder, a sentence saying where it is instead.
    Main thread: reads bpy.

    Never Blender's working directory: that is wherever Blender was started from, and during evals
    it is the harness folder in the git checkout (render_job put product_reveal_final/ there,
    2026-09-27). Saved: the .blend's folder, what Blender's // means. Unsaved: the render output
    folder when it is an absolute path (Blender's default is /tmp/), else a Loopcut folder in the
    system temp folder; not Blender's own temp folder, which is deleted when Blender quits."""
    import bpy
    if bpy.data.filepath:
        return Path(bpy.data.filepath).resolve().parent, ""
    scene = bpy.context.scene
    out = scene.render.filepath if scene is not None else ""
    if out and not out.startswith("//") and Path(os.path.expanduser(out)).is_absolute():
        folder, what = Path(os.path.expanduser(out)), "the render output folder"
        if not (out.endswith(("/", "\\")) or folder.is_dir()):
            folder = folder.parent  # A file name prefix such as /tmp/shot_.
    else:
        folder, what = Path(tempfile.gettempdir()) / "Loopcut", "a Loopcut folder in the system temp folder"
        with contextlib.suppress(OSError):
            folder.mkdir(exist_ok=True)
    folder = folder.resolve()
    return folder, (f"This scene is not saved to a .blend yet, so relative paths are under {what}, {folder}. "
                    "Save the file first, or give an absolute path, to put output somewhere else.")


def is_relative(raw: str) -> bool:
    """Whether resolve() puts this path under relative_base: // and bare relative paths."""
    text = str(raw or "").strip()
    return text.startswith("//") or not Path(os.path.expanduser(text)).is_absolute()


def relative_note(*raws: str) -> str:
    """"" or a line for a tool's result saying where relative paths went, when the scene is unsaved
    and one of these paths was relative. Main thread."""
    note = relative_base()[1] if any(is_relative(raw) for raw in raws) else ""
    return f"\n{note}" if note else ""


def resolve(raw: str, base: Path | None = None) -> Path:
    """An absolute path from what the model wrote: ~ is the user's home; // (Blender's "next to the
    .blend") and a bare relative path are under base, by default relative_base(). base lets the
    approval gate resolve off the main thread, from the roots it was given."""
    text = str(raw or "").strip()
    if not text:
        raise ToolError("path must not be empty")
    blend_relative = text.startswith("//")
    if blend_relative:
        text = text[2:]
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        if base is None:
            base = blend_base()[0] if blend_relative else relative_base()[0]
        path = base / path
    return path.resolve()


def _temp_roots() -> tuple[Path, ...]:
    """The system's and Blender's temp folders: Blender's own, the Loopcut folder for unsaved scenes
    and the images Loopcut shows the model are there. Where they are is a setting, not a hidden file."""
    roots = [tempfile.gettempdir()]
    with contextlib.suppress(ImportError, AttributeError):
        import bpy
        roots.append(bpy.app.tempdir)
    return tuple(Path(root).resolve() for root in roots if root)


def is_hidden(path: Path) -> bool:
    """A name starting with '.', counted below a temp folder the path is in, not in the temp folder's
    own path: TMPDIR under a dot folder (the eval harness's .<task>-xxxx) made Blender's temp folder
    hidden, and with it every inspect_scene and every read of its files (2026-09-27)."""
    for root in sorted(_temp_roots(), key=lambda root: len(root.parts), reverse=True):
        if path == root or root in path.parents:
            path = path.relative_to(root)
            break
    return any(part.startswith(".") and part not in (".", "..") for part in path.parts)


def inside(path: Path, roots) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _forbidden_roots() -> tuple[Path, ...]:
    from . import conversations
    try:
        return (conversations.root().resolve().parent,)  # Loopcut's data: conversations, checkpoints, keys.
    except Exception:  # No Blender, no data folder: nothing more to fence off.
        return ()


def check(path: Path) -> Path:
    """Raise for a path the model may never touch, whatever the user allows. A workspace (workspace.py)
    is the model's own, though it lives in Loopcut's data: inside one, only its hidden names count."""
    from . import workspace
    root = workspace.inside(path)
    if root is not None:
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            raise ToolError(f"{path} is hidden (a name starting with '.'): hidden files and folders are off limits.")
        return path
    if is_hidden(path):
        raise ToolError(f"{path} is hidden (a name starting with '.'): hidden files and folders are off limits.")
    if inside(path, _forbidden_roots()):
        raise ToolError(f"{path} is Loopcut's own data and off limits.")
    return path


def needs_approval(name: str, arguments: dict, roots) -> bool:
    """Whether the user must approve this call (unless they chose "Always allow"). Renders are
    handled separately by tools.is_heavy: "Always allow" does not waive them."""
    if name in WRITE_TOOLS:
        return True
    if name in READ_TOOLS:
        if not roots:
            return True
        try:  # Off the main thread: relative paths resolve under roots[0], relative_base when it was taken.
            return not inside(resolve(str(arguments.get("path", "")), base=roots[0]), roots)
        except ToolError:
            return True  # Unresolvable now; the tool will explain, after the user has seen the call.
    return False


def describe(name: str, arguments: dict) -> tuple[str, str] | None:
    """(summary, code) for the tool card, or None when this is not one of these tools."""
    path = str(arguments.get("path", ""))
    if name == "list_files":
        pattern = arguments.get("pattern")
        return f"List {path}" + (f" ({pattern})" if pattern else ""), ""
    if name == "read_file":
        lines = arguments.get("lines")
        return f"Read {path}" + (f" lines {lines[0]}-{lines[1]}" if isinstance(lines, list) and len(lines) == 2 else ""), ""
    if name == "write_file":
        verb = "Overwrite" if arguments.get("overwrite") else "Write"
        return f"{verb} {path}", str(arguments.get("content", ""))
    if name == "move_file":
        verb = "Copy" if arguments.get("copy") else "Move"
        return f"{verb} {arguments.get('source', '')} to {arguments.get('destination', '')}", ""
    if name == "see_render":
        if arguments.get("render"):
            return "Render the scene" + (" at full quality" if arguments.get("full") else " (preview)"), ""
        return "Show the last render", ""
    return None


# ---------------------------------------------------------------- the tools

def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} GB"


def list_files(path: str, pattern: str = "") -> ToolResult:
    folder = check(resolve(path))
    if not folder.is_dir():
        raise ToolError(f"{folder} is not a folder." if folder.exists() else f"{folder} does not exist.")
    try:
        entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as ex:
        raise ToolError(f"Cannot list {folder}: {ex.strerror or ex}") from ex
    shown = [p for p in entries if not p.name.startswith(".")
             and (not pattern or fnmatch.fnmatch(p.name, pattern))]
    lines = [f"{folder} ({len(shown)} entries" + (f" matching {pattern}" if pattern else "") + ")"]
    for entry in shown[:MAX_ENTRIES]:
        try:
            stat = entry.stat()
        except OSError:
            lines.append(f"{entry.name}  (unreadable)")
            continue
        when = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{entry.name}/  {when}" if entry.is_dir() else f"{entry.name}  {_human(stat.st_size)}  {when}")
    if len(shown) > MAX_ENTRIES:
        lines.append(f"... {len(shown) - MAX_ENTRIES} more; narrow with a pattern.")
    return ToolResult("\n".join(lines) + relative_note(path))


def _read_text(path: Path, lines) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(MAX_TEXT_BYTES)
    except OSError as ex:
        raise ToolError(f"Cannot read {path}: {ex.strerror or ex}") from ex
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as ex:
        raise ToolError(f"{path.name} is not a text file or an image ({_human(size)}).") from ex
    rows = text.splitlines()
    first, last = 1, len(rows)
    if lines is not None:
        try:
            first, last = int(lines[0]), int(lines[1])
        except (TypeError, ValueError, IndexError) as ex:
            raise ToolError("lines must be [first, last], counted from 1") from ex
        if first < 1 or last < first:
            raise ToolError("lines must be [first, last] with 1 <= first <= last")
        rows = rows[first - 1:last]
        last = min(last, first - 1 + len(rows))
    header = f"{path} ({len(text.splitlines())} lines" + (f", truncated at {_human(MAX_TEXT_BYTES)}" if size > len(data) else "")
    header += f", showing {first}-{last})" if lines is not None else ")"
    return f"{header}\n{_clip(chr(10).join(rows))}"


def _read_image(path: Path) -> ToolResult:
    from . import attachments
    out = scratch.folder() / "file.png"
    out.parent.mkdir(exist_ok=True)
    try:
        width, height = attachments._to_png(path, out, IMAGE_SIDE)
    except attachments.AttachmentError as ex:
        raise ToolError(str(ex)) from ex
    return ToolResult(f"Image attached: the file {path.name}, {width}x{height} px.", image_path=out, file_image=True)


def read_file(path: str, lines: list | None = None) -> ToolResult:
    target = check(resolve(path))
    if not target.is_file():
        raise ToolError(f"{target} is a folder; use list_files." if target.is_dir()
                        else f"{target} does not exist.{relative_note(path)}")
    if target.suffix.lower() in IMAGE_EXTENSIONS:
        return _read_image(target)
    return ToolResult(_read_text(target, lines))


def write_file(path: str, content: str, overwrite: bool = False) -> ToolResult:
    target = check(resolve(path))
    if target.is_dir():
        raise ToolError(f"{target} is a folder.")
    if target.exists() and not overwrite:
        raise ToolError(f"{target} already exists; pass overwrite=true to replace it, or choose another name.")
    from . import workspace
    if not target.parent.is_dir() and workspace.inside(target) is not None:
        target.parent.mkdir(parents=True, exist_ok=True)  # In the workspace, as a shell's tools would.
    if not target.parent.is_dir():
        raise ToolError(f"The folder {target.parent} does not exist.{relative_note(path)}")
    try:
        data = str(content).encode("utf-8")
        target.write_bytes(data)
    except OSError as ex:
        raise ToolError(f"Cannot write {target}: {ex.strerror or ex}") from ex
    return ToolResult(f"Wrote {_human(len(data))} to {target}.{relative_note(path)}")


def move_file(source: str, destination: str, copy: bool = False) -> ToolResult:
    origin = check(resolve(source))
    target = check(resolve(destination))
    if not origin.exists():
        raise ToolError(f"{origin} does not exist.")
    if target.is_dir():
        target = check(target / origin.name)
    if target.exists():
        raise ToolError(f"{target} already exists; nothing is replaced. Choose another name.")
    if not target.parent.is_dir():
        raise ToolError(f"The folder {target.parent} does not exist.")
    if origin == target or origin in target.parents:
        raise ToolError(f"Cannot move {origin} into itself.")
    try:
        if copy:
            (shutil.copytree if origin.is_dir() else shutil.copy2)(origin, target)
        else:
            shutil.move(str(origin), str(target))
    except OSError as ex:
        raise ToolError(f"Cannot {'copy' if copy else 'move'} {origin}: {ex.strerror or ex}") from ex
    return ToolResult(f"{'Copied' if copy else 'Moved'} {origin} to {target}.{relative_note(source, destination)}")


def _render_now(scene, out: Path, full: bool) -> str:
    """Render through the scene camera to `out` and return what was rendered, restoring every
    setting touched. Preview: the longest side PREVIEW_SIDE, samples capped."""
    import bpy
    if scene.camera is None:
        raise ToolError("The scene has no camera; add one and set it as the scene camera first.")
    render = scene.render
    settings = render.image_settings
    saved = {"filepath": render.filepath, "resolution_percentage": render.resolution_percentage,
             "use_file_extension": render.use_file_extension, "media_type": settings.media_type,
             "file_format": settings.file_format, "color_depth": settings.color_depth,
             "color_mode": settings.color_mode}
    samples = []  # (owner, attribute, value) to put back.
    try:
        render.filepath, render.use_file_extension = str(out), False
        settings.media_type = "IMAGE"
        settings.file_format = "PNG"
        if not full:
            longest = max(render.resolution_x, render.resolution_y)
            render.resolution_percentage = max(1, min(render.resolution_percentage,
                                                      round(PREVIEW_SIDE * 100 / max(1, longest))))
            for owner, attribute in ((getattr(scene, "cycles", None), "samples"),
                                     (getattr(scene, "eevee", None), "taa_render_samples")):
                if owner is not None and hasattr(owner, attribute):
                    samples.append((owner, attribute, getattr(owner, attribute)))
                    setattr(owner, attribute, min(getattr(owner, attribute), PREVIEW_SAMPLES))
        started = time.monotonic()
        bpy.ops.render.render(write_still=True)
        seconds = time.monotonic() - started
    except RuntimeError as ex:
        raise ToolError(f"The render failed: {ex}") from ex
    finally:
        for owner, attribute, value in samples:
            setattr(owner, attribute, value)
        for key, value in saved.items():
            setattr(settings if key in ("media_type", "file_format", "color_depth", "color_mode") else render, key, value)
    if not out.is_file():
        raise ToolError("The render produced no image.")
    return (f"a {'full' if full else 'preview'} render through {scene.camera.name} with {render.engine}"
            f", {seconds:.0f} s")


def on_render_complete(scene, *_) -> None:
    """bpy.app.handlers.render_complete (registered in lifecycle): keep a copy of every finished
    render. Viewport captures do not fire it, so the copy is always a real render."""
    import bpy
    image = bpy.data.images.get("Render Result")
    if image is None:
        return
    settings = scene.render.image_settings
    saved = settings.media_type, settings.file_format, settings.color_depth, settings.color_mode
    try:
        LAST_RENDER.parent.mkdir(exist_ok=True)
        settings.media_type = "IMAGE"
        settings.file_format = "PNG"
        image.save_render(str(LAST_RENDER), scene=scene)
    except (RuntimeError, OSError) as ex:
        print(f"Loopcut: could not keep a copy of the render: {ex}")
    finally:
        settings.media_type, settings.file_format, settings.color_depth, settings.color_mode = saved


on_render_complete._bpy_persistent = None  # What bpy.app.handlers.persistent does: the handler survives file loads.


def _last_render() -> str:
    if not LAST_RENDER.is_file():
        raise ToolError("No render has finished since Blender started. Ask again with render=true to "
                        "render now, which waits for the user's OK.")
    made = datetime.fromtimestamp(LAST_RENDER.stat().st_mtime).strftime("%H:%M")
    return f"the last render, finished at {made}"


def see_render(render: bool = False, full: bool = False) -> ToolResult:
    import bpy
    from . import attachments
    scene = bpy.context.scene
    folder = scratch.folder()
    folder.mkdir(exist_ok=True)
    out, shown = folder / "render.png", folder / "render_small.png"
    if render:
        refusal = cost_guard.render_refusal()  # Render levels are only ever evaluated by a render.
        if refusal:
            raise ToolError(refusal)
        what = _render_now(scene, out, bool(full))
    else:
        what, out = _last_render(), LAST_RENDER
    try:
        width, height = attachments._to_png(out, shown, IMAGE_SIDE)
    except attachments.AttachmentError as ex:
        raise ToolError(str(ex)) from ex
    return ToolResult(f"Image attached: {what}, {width}x{height} px.", image_path=shown, file_image=True)


DISPATCH = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "move_file": move_file,
    "see_render": see_render,
}
