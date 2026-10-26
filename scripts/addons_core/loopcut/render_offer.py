"""Before a long render, the cloud or this computer (0.2.19, owner-approved 2026-10-25).

A ChatGPT-plan user's 15-hour laptop render was never offered the cloud. The cloud-render skill (v3) tells the model
to time a test frame and offer both, but Loopcut's Fast model ignores instructions in tool results, so the offer does
not rest on the model: Loopcut records the seconds a frame of its own local renders (see_render with render=true,
run_blender's frames on source.blend in the workspace and Codex routes, and render_job's), and when the model starts a
cloud render whose frames would take over about 15 minutes here, the paid call's card becomes the choice card
(agent._Consent._ask, ui/render_choice.py). A timing counts only for a saved .blend's same scene, from the last day,
at the call's engine, pixels and samples, and for Cycles only on the CPU, where a render job renders it; a job's or
a run's cold first frame is left out when it rendered more. Without one it stays today's paid card: no guess.

- In the cloud: the click approves the quote, exactly as Approve on the paid card does. Recommended, with ⏎, only
  when its estimate is clearly faster; then Enter runs it. It promises quitting Blender only once cloud jobs survive
  that (cloud_jobs.SURVIVES_QUIT).
- On this laptop: the paid call is declined, nothing is charged, and job_tools.start_render_job renders the same
  frames of the same scene in the background; the model's result says so, in its route's tools (here()). A render
  one job attempt cannot hold shows its time and no button.
- Not now (Esc): neither; the model asks which.
- Short of credits: the cloud option says what the call needs and offers the plan that covers it, on the pricing page
  the credits pop-up opens, or says no plan's month does; any paid card short of its price gets that Upgrade too
  (shortfall()).

A render_job start the model makes is offered the same way (0.2.20; rel019-cloud2: asked for the cloud so they could
close the laptop, the model started a render job): when its frames would take over LONG_SECONDS here by the same
timing rules and this account has the cloud render, its card is the choice (prepare_job, agent._ask_choice), even
with renders always allowed, since it is also a time choice (owner). A quote needs the scene packed and uploaded, so
nothing is priced before the click: the cloud option says it is priced before it starts and is never Recommended.
In the cloud runs the cloud render for the same frames in the job's place, its quote's own card asking as any paid
call's (short of it, shortfall()'s Upgrade); this computer runs the job as asked, with the time the card said it
takes; Not now runs neither (job_call, job_result).

No bpy at import: the layout and its tests read the copy here.
"""

import json
import math
import threading
import time
from contextlib import contextmanager

from . import chat_credits, config

LONG_SECONDS = 15 * 60  # Over this on this computer, a cloud render's card offers both.
# A long render_job asks as the choice even with renders always allowed ("Always allow renders"), since it is also a
# time choice: the owner's open question (out/plans/cloud-tools-implicit-use.md, section 3, "Still asks"). False: an
# always-allowed render job runs as asked, with no card.
ASK_EVEN_WHEN_ALWAYS_ALLOWED = True
LOCAL = "render_here"   # The CloudError code that carries "the user picked this computer" out of the consent.
OUTPUT_DIR = "//renders"  # The local render's frames: a loopcut-<id> folder in renders/ beside the .blend.
REASON = "insufficient_credits"  # The pricing page's reason, as the credits pop-up's server link names it.
ENGINES = {"cycles": "CYCLES", "eevee": "BLENDER_EEVEE"}  # The render tool's engine input, as Blender names it.
EEVEE = {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}  # Blender 4.2-4.x called EEVEE Next by its own id.
CLOUD_SAMPLES = {"CYCLES": 128, "BLENDER_EEVEE": 64}  # The render service's defaults (its adapter, render.ts).
CLOUD_MAX_SAMPLES = 4096  # The render tool's samples maximum (its input_schema).
ENGINE_LABELS = {"CYCLES": "Cycles", "BLENDER_EEVEE": "EEVEE"}
RESOLUTIONS = {(3840, 2160): "4K", (2560, 1440): "1440p", (1920, 1080): "1080p", (1280, 720): "720p"}
MAX_TIMINGS = 32
MAX_AGE = 24 * 3600  # A timing older than this is of a scene that may have changed since: never used.
MAX_BUDGET = 86400   # render_job's longest attempt (its schema's budget_seconds maximum).
BUDGET_MARGIN = 1.25  # The laptop job's time budget over the measured estimate, plus BUDGET_SLACK seconds.
BUDGET_SLACK = 600

# Quitting Blender cancels a cloud job and a restart forgets it until cloud jobs outlive the session (the R1 lane's
# cloud_jobs.SURVIVES_QUIT); until then the cloud option promises only what is true.
CLOUD_BULLETS = ("Quit Blender or turn your computer off: it keeps rendering",
                 "Work on something else meanwhile",
                 "The frames and the MP4 wait in this chat")  # An animation's MP4 beside them (contract 0.10).
CLOUD_BULLETS_ONE = CLOUD_BULLETS[:2] + ("The image waits in this chat",)  # One frame: no MP4.
CLOUD_BULLETS_OPEN = ("Work on something else meanwhile", "Blender stays open until it's done")
HERE_BULLETS = ("Blender stays open, plugged in and awake until it's done",
                "Your laptop is slow for anything else meanwhile")
HERE_RESULT = ("The user chose to render these frames on this computer, not in the cloud: the cloud render did not run "
               "and nothing was charged. Loopcut started local render job {id} for the same frames; check it with "
               "render_job action status and job_id {id}, and do not call loopcut_cloud.render for them.\n{job}")
HERE_FAILED = ("The user chose to render these frames on this computer, not in the cloud: the cloud render did not run "
               "and nothing was charged. The local render could not start: {error} Fix that and start it with "
               "render_job (action start), or tell the user.")
# The workspace and Codex routes have no render_job tool: what the result tells their model to do instead.
HERE_RESULT_WORKSPACE = ("The user chose to render these frames on this computer, not in the cloud: the cloud render did "
                         "not run and nothing was charged. Loopcut started a background render of the same {count} "
                         "frames; it goes on after this turn ends, and the panel's Jobs list shows its progress. Its PNG "
                         "frames appear in {output} as they finish: list that folder to see how far it is. Do not call "
                         "loopcut_cloud.render for them.")
HERE_FAILED_WORKSPACE = ("The user chose to render these frames on this computer, not in the cloud: the cloud render did "
                         "not run and nothing was charged. The local render could not start: {error} Tell the user, "
                         "and fix what you can.")
FRAME_PREFIX = "Loopcut frame: "  # render_timing.PREFIX (that prelude imports bpy, so it is not imported here).
SKIPPED = ("The user closed the render choice without picking the cloud or this computer. Nothing ran and nothing "
           "was charged. Ask which they want; do not start either render until they say.")
# A render_job's choice (0.2.20): no quote before the click, since a quote needs the scene packed and uploaded.
UNPRICED = "Priced before it starts"
JOB_HERE = "The user chose to render these frames on this computer, not in the cloud."
JOB_BUDGET = " Its time limit is {budget} s, the measured time of its {count} frames with a margin{asked}."
JOB_CLOUD = ("The user chose to render these frames in the cloud, not on this computer: render_job did not start, and "
             "Loopcut called loopcut_cloud.render for the same frames with {inputs}.{mp4} Its result follows; a job_id "
             "there is the render: follow it with loopcut_cloud.job_status, and do not start render_job for these "
             "frames.\n{result}")
JOB_MP4 = " The cloud render returns the frames and, for an animation, an MP4 beside them."

_lock = threading.Lock()
_timings: list[dict] = []  # Local renders this session: {key, engine, width, height, samples, seconds, at}.
_last: dict | None = None  # The newest see_render timing, for its card's line (take_last).


def _amount(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _family(engine) -> str:
    return "BLENDER_EEVEE" if engine in EEVEE else str(engine or "")


# --- Timings: the seconds a frame of this computer's own renders ----------------------------------------------------

def scene_key(scene) -> tuple[str, str]:
    """This scene, as a render job's spec names it: the .blend's path and the scene's name. Main thread."""
    import bpy
    return bpy.data.filepath, scene.name


def _device(scene, engine: str) -> str:
    """Where a render of the scene runs: Cycles on the CPU unless the scene asks for the GPU and the preferences have
    a compute device type; EEVEE always on the GPU."""
    if engine != "CYCLES":
        return "GPU"
    if getattr(getattr(scene, "cycles", None), "device", "CPU") != "GPU":
        return "CPU"
    import bpy
    addon = bpy.context.preferences.addons.get("cycles")
    kind = getattr(getattr(addon, "preferences", None), "compute_device_type", "NONE")
    return "CPU" if kind in ("NONE", "", None) else "GPU"


def settings(scene) -> dict:
    """What a render of the scene renders at now: engine, pixels, samples and device. Main thread."""
    r = scene.render
    engine = _family(r.engine)
    owner, attribute = {"CYCLES": ("cycles", "samples"), "BLENDER_EEVEE": ("eevee", "taa_render_samples")}.get(
        engine, ("", ""))
    samples = getattr(getattr(scene, owner, None), attribute, None) if owner else None
    return {"engine": engine, "width": max(4, r.resolution_x * r.resolution_percentage // 100),
            "height": max(4, r.resolution_y * r.resolution_percentage // 100), "samples": samples,
            "device": _device(scene, engine)}


def record(key, rendered: dict, seconds: float) -> None:
    """A local frame rendered at `rendered` (settings()) took `seconds`."""
    global _last
    if not _amount(seconds) or not seconds:
        return
    entry = {"key": tuple(key), **rendered, "seconds": float(seconds), "at": time.time()}
    with _lock:
        _timings.append(entry)
        del _timings[:-MAX_TIMINGS]
        _last = entry


def take_last() -> dict | None:
    """The timing record() kept last, once: see_render's card line for the render it just made."""
    global _last
    with _lock:
        entry, _last = _last, None
    return entry


def _job_timing(row: dict):
    """(seconds a frame, settings) of a local render job's rendered frames, or None. With two or more frames of a
    fresh attempt, the mean after the first, from when each frame's file was written: the first frame also pays for
    loading the scene and, in EEVEE, compiling its shaders (measured 60-75% slower). One frame counts as it is."""
    from pathlib import Path
    progress = row.get("progress") or {}
    rendered, spent = progress.get("rendered"), progress.get("elapsed_seconds")
    if row.get("kind", "render") != "render" or type(rendered) is not int or rendered < 1 or not _amount(spent) \
            or not spent:
        return None
    seconds, files = spent / rendered, progress.get("files") or []
    if rendered >= 2 and not progress.get("reused") and len(files) == rendered:
        try:
            output = Path(row["output"])
            first, last = ((output / files[k]["file"]).stat().st_mtime for k in (0, -1))
            if last > first:
                seconds = (last - first) / (rendered - 1)
        except (OSError, KeyError, TypeError):
            pass  # Frames moved or removed: the attempt's average.
    engine = _family(row.get("engine"))
    return seconds, {"engine": engine, "width": row.get("width"), "height": row.get("height"),
                              "samples": row.get("samples"), "device": row.get("device") or "CPU"}


def live_path() -> str:
    """The open .blend's path, the key's first half. Main thread."""
    import bpy
    return getattr(getattr(bpy, "data", None), "filepath", "") or ""


def log_frames(text: str) -> tuple[list, str]:
    """A workspace Blender's log (run_blender): render_timing's frame lines, and the log without them."""
    frames, kept = [], []
    for line in text.splitlines():
        if not line.startswith(FRAME_PREFIX):
            kept.append(line)
            continue
        try:
            row = json.loads(line[len(FRAME_PREFIX):])
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("scene"), str) and _amount(row.get("seconds")):
            frames.append(row)
    return frames, "\n".join(kept)


def run_seconds(frames: list) -> list:
    """(scene, settings, seconds a frame) per scene and settings a run rendered: after its first frame when it rendered
    more, since a fresh Blender compiles EEVEE's shaders on the first; else the one frame."""
    groups: dict = {}
    for row in frames:
        rendered = {key: row.get(key) for key in ("engine", "width", "height", "samples", "device")}
        groups.setdefault((row["scene"], json.dumps(rendered, sort_keys=True)), (rendered, []))[1].append(row["seconds"])
    out = []
    for (scene, _), (rendered, seconds) in groups.items():
        warm = seconds[1:] or seconds
        out.append((scene, rendered, sum(warm) / len(warm)))
    return out


def frame_line(seconds) -> str:
    """"31 s a frame": a test render's card line."""
    if not _amount(seconds) or not seconds:
        return ""
    if seconds < 10:
        text = f"{seconds:.1f}".rstrip("0").rstrip(".") + " s"
    elif seconds < 60:
        text = f"{round(seconds)} s"
    else:
        minutes, rest = divmod(round(seconds), 60)
        text = f"{minutes} min" + (f" {rest} s" if rest else "")
    return text + " a frame"


def job_line(row: dict) -> str:
    """A render job's card line once it has rendered a frame: what one took here."""
    timing = _job_timing(row)
    return frame_line(timing[0]) if timing else ""


def _alike(rendered: dict, wanted: dict) -> bool:
    """A timing measures the laptop job's frames: the same engine, pixels and samples, and for Cycles the CPU, where
    a render job renders it (job_worker.configure); a GPU frame would understate the laptop's time."""
    return (all(rendered.get(k) == wanted[k] for k in ("engine", "width", "height", "samples"))
            and (wanted["engine"] != "CYCLES" or rendered.get("device") == "CPU"))


def seconds_per_frame(key, wanted: dict, rows=(), now: float | None = None):
    """The latest measured seconds a frame of this scene at `wanted`'s engine, pixels and samples (_alike), from the
    last MAX_AGE: see_render's and run_blender's renders this session and render jobs' (rows: jobs.list_jobs). None
    without one: the card never guesses. The live scene has no revision to compare, so age bounds an edited scene."""
    now = time.time() if now is None else now
    best = None  # (when, seconds)
    with _lock:
        timings = list(_timings)
    for entry in timings:
        if entry["key"] == tuple(key) and _alike(entry, wanted) and now - entry["at"] <= MAX_AGE:
            best = max(best or (0, 0), (entry["at"], entry["seconds"]))
    for row in rows:
        timing = _job_timing(row)
        when = row.get("updated") or row.get("created") or 0
        if timing and (row.get("source"), row.get("scene")) == tuple(key) and _alike(timing[1], wanted) \
                and now - when <= MAX_AGE:
            best = max(best or (0, 0), (when, timing[0]))
    return best[1] if best else None


# --- The offer: made before the quote, on Blender's thread -----------------------------------------------------------

def prepare(arguments: dict) -> dict | None:
    """Main thread, as a cloud render's call starts: the same frames on this computer, from the latest measured local
    frame of this scene at the call's engine, resolution and samples. None (today's paid card) without one, at or
    under LONG_SECONDS, for a turntable (its orbit camera exists only in the cloud), for an unsaved file (every unsaved
    file's scenes share one key), or when anything here fails."""
    try:
        return _prepare(arguments)
    except Exception as ex:  # The offer is a convenience: the paid call goes on with its usual card.
        print(f"Loopcut: no render choice for this call: {ex!r}")
        return None


def _prepare(arguments: dict) -> dict | None:
    import bpy
    from . import checkpoints, jobs
    if arguments.get("turntable") is not None:
        return None
    scene = bpy.context.scene
    key = scene_key(scene)
    if not key[0]:
        return None
    engine = ENGINES.get(arguments.get("engine", "cycles"))
    r = scene.render
    if engine is None or _family(r.engine) != engine:
        return None  # The local job renders with the scene's engine; another one would not be the same frames.
    pct = arguments.get("resolution_pct", 100)
    wanted = {"engine": engine, "width": max(4, r.resolution_x * pct // 100),
              "height": max(4, r.resolution_y * pct // 100), "samples": arguments.get("samples", CLOUD_SAMPLES[engine])}
    seconds = seconds_per_frame(key, wanted, jobs.list_jobs(checkpoints.data_root()))
    start = arguments.get("frame_start", 1)
    end = arguments.get("frame_end", start)
    step = arguments.get("frame_step", 1)
    count = (end - start) // step + 1 if end >= start and step >= 1 else 0
    if seconds is None or count < 1 or seconds * count <= LONG_SECONDS:
        return None
    camera = arguments.get("camera") or (scene.camera.name if scene.camera else "")
    return {**wanted, "frames": [start, end, step], "count": count, "fps": r.fps / r.fps_base, "camera": camera,
            "seconds_per_frame": seconds, "key": list(key), "denoise": arguments.get("denoise", True)}


def choice(offer: dict, quote: dict) -> dict:
    """The card's facts (cloud_account.authorize): the offer, and the quote's credits and estimated_seconds (contract
    0.10: GPU time with start-up, a ceiling)."""
    cloud = quote.get("estimated_seconds")
    return {**offer, "credits": quote["credits"], "cloud_seconds": cloud if type(cloud) is int and cloud > 0 else None}


# --- A render job's offer (0.2.20): the model started a render here, and the same choice comes first -----------------

def cloud_tool() -> str:
    """The cloud render's tool name while this account can call it (managed, among its tools now), else "". Any
    thread. A tool locked for credits has no quote to name its need by, so it offers no choice."""
    from . import cloud
    runtime = cloud._runtime
    if runtime is None or not runtime.signed_in:
        return ""
    found = any(d.get("name") == "render" and d.get("backend") == "managed" for d in runtime.descriptors())
    return cloud.PREFIX + "render" if found else ""


def prepare_job(arguments: dict) -> dict | None:
    """Main thread, before a render_job start asks: prepare()'s offer for the job's frames, engine, pixels and samples,
    with the cloud render's inputs for the same frames ("cloud"). None (today's render job card) without a timing, at
    or under LONG_SECONDS, for an unsaved file, for frames or pixels the cloud render cannot name (not one evenly
    spaced run, no resolution_pct of the scene's), an engine it does not render, or when anything here fails."""
    try:
        return _prepare_job(arguments)
    except Exception as ex:  # The choice is a convenience: the render job asks with its usual card.
        print(f"Loopcut: no render choice for this render job: {ex!r}")
        return None


def _prepare_job(arguments: dict) -> dict | None:
    import bpy
    from . import checkpoints, job_tools, jobs
    scene = bpy.context.scene
    key, r = scene_key(scene), scene.render
    engine = _family(r.engine)
    if not key[0] or engine not in CLOUD_SAMPLES:
        return None
    frames = job_tools.frame_range(arguments.get("frames"), arguments.get("start"), arguments.get("end"))
    run = _evenly(frames)
    # What the job renders (job_tools.create_spec): its pixels and samples, else the scene's.
    width, height, samples = (arguments.get(k) for k in ("width", "height", "samples"))
    width = max(4, r.resolution_x * r.resolution_percentage // 100) if width is None else width
    height = max(4, r.resolution_y * r.resolution_percentage // 100) if height is None else height
    if samples is None:
        samples = (scene.cycles.samples if r.engine == "CYCLES" else
                   scene.eevee.taa_render_samples if r.engine == "BLENDER_EEVEE" else 32)
    size = lambda pct: (max(4, r.resolution_x * pct // 100), max(4, r.resolution_y * pct // 100))
    pct = next((pct for pct in (r.resolution_percentage, *range(100, 9, -1)) if 10 <= pct <= 100
                and size(pct) == (width, height)), None)  # The cloud render's resolution_pct for these pixels.
    if run is None or pct is None or not 1 <= samples <= CLOUD_MAX_SAMPLES:
        return None
    wanted = {"engine": engine, "width": width, "height": height, "samples": samples}
    seconds = seconds_per_frame(key, wanted, jobs.list_jobs(checkpoints.data_root()))
    if seconds is None or seconds * len(frames) <= LONG_SECONDS:
        return None
    start, end, step = run
    camera = arguments.get("camera")
    denoise = bool(getattr(scene.cycles, "use_denoising", True)) if engine == "CYCLES" else True
    inputs = {"frame_start": start, "frame_end": end, **({"frame_step": step} if step != 1 else {}),
              "engine": next(k for k, v in ENGINES.items() if v == engine), "samples": samples, "resolution_pct": pct,
              **({"camera": camera} if camera is not None else {}),
              **({"denoise": denoise} if engine == "CYCLES" else {})}
    return {**wanted, "frames": [start, end, step], "count": len(frames), "fps": r.fps / r.fps_base,
            "camera": camera or (scene.camera.name if scene.camera else ""), "seconds_per_frame": seconds,
            "key": list(key), "denoise": denoise, "cloud": inputs}


def _evenly(frames: list):
    """(start, end, step) of frames that are one evenly spaced run, as the cloud render names a range; else None."""
    step = frames[1] - frames[0] if len(frames) > 1 else 1
    if step < 1 or list(frames) != list(range(frames[0], frames[-1] + 1, step)):
        return None
    return frames[0], frames[-1], step


def job_choice(offer: dict) -> dict:
    """A render job's choice card (prepare_job): no quote before the click, so no price and no cloud time; the cloud
    option says it is priced before it starts, and nothing is Recommended."""
    return {**offer, "credits": None, "cloud_seconds": None, "balance": live_balance(), "plan": account_plan()}


# --- Copy ------------------------------------------------------------------------------------------------------------

def _n(value) -> str:
    """"1,850": credits as whole numbers, grouped."""
    return f"{round(value):,}"


def about(seconds: float) -> str:
    """"15½ h", "2 h", "40 min": a render's time, to the half hour past an hour."""
    if seconds < 60:
        return "under a minute"
    if seconds < 3600 - 15:
        return f"{max(1, round(seconds / 60))} min"
    hours, half = divmod(round(seconds / 1800), 2)
    return f"{hours}{'½' if half else ''} h"


def clip(seconds: float) -> str:
    """"75 s", "2 min 30 s": how long the frames play."""
    seconds = round(seconds)
    if seconds < 120:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} min" + (f" {rest} s" if rest else "")


def resolution(width: int, height: int) -> str:
    return RESOLUTIONS.get((width, height), f"{width}×{height}")


def title(data: dict) -> str:
    return f"Render {data['count']:,} frame{'' if data['count'] == 1 else 's'}"


def detail(data: dict) -> str:
    """"4K · 75 s · EEVEE": the frames' size, how long they play at the scene's frame rate, the engine."""
    start, end, _ = data["frames"]
    parts = [resolution(data["width"], data["height"])]
    if data["count"] > 1 and _amount(data.get("fps")) and data["fps"]:
        parts.append(clip((end - start + 1) / data["fps"]))
    parts.append(ENGINE_LABELS.get(data["engine"], data["engine"]))
    return " · ".join(parts)


def _next_plan(plan: str, balance, needed):
    """The free plan's way to `needed` credits: the cheapest paid plan whose monthly credits with the balance cover
    it, or "" when none does. None on a paid plan, whose way is a credit top-up. The manifest names only the
    account's own plan, so the other plans' credits are chat_credits.PLANS, the table the credits pop-up shows."""
    if plan in ("pro", "max"):
        return None
    paid = [name for name, row in chat_credits.PLANS.items() if row["price"]]
    return next((name for name in paid if chat_credits.PLANS[name]["credits"] + (balance or 0) >= needed), "")


def shortfall(needed, balance, plan: str = "", rate=None, *, render: bool = False) -> dict | None:
    """What a paid card says and offers when the balance is short of this call's price, or None. needed: the quote's
    credits; balance: the live balance; plan: the account's (the free plan when unknown); rate: seconds of cloud
    rendering a credit buys at this quote's own rate, for "up to 7 hours of cloud rendering" (omitted without it).
    The plans' prices and monthly credits are chat_credits.PLANS, the table the credits pop-up shows."""
    if not (_amount(needed) and _amount(balance)) or balance >= needed or not config.billing_open():
        return None
    warn = f"You have {_n(math.floor(balance))} credits; this needs {_n(math.ceil(needed))}."  # Never "100 of 100".
    name = _next_plan(plan, balance, needed)
    if name == "":  # No plan's month covers it: no Upgrade that would still fall short.
        top = [plan for plan, row in chat_credits.PLANS.items() if row["price"]][-1]
        return {"warn": warn, "badge": "", "plan": "", "label": "", "short_label": "", "url": "",
                "note": f"This needs more credits than a month of {top.capitalize()} includes."}
    if name is None:
        return {"warn": warn, "badge": "", "note": "", "plan": "", "short_label": "Add credits",
                "label": "Add credits and render in the cloud" if render else "Add credits",
                "url": config.credits_url(REASON)}
    row, label = chat_credits.PLANS[name], name.capitalize()
    note = f"{label} is ${row['price']} a month with {chat_credits.plan_dollars(row['credits'])} of credits every month"
    if _amount(rate) and rate:
        hours = math.floor(row["credits"] * rate / 3600)
        note += (f": up to {hours} hour{'' if hours == 1 else 's'} of cloud rendering" if hours >= 1 else
                 f": up to {math.floor(row['credits'] * rate / 60)} minutes of cloud rendering")
    return {"warn": warn, "badge": f"With {label}", "note": note + ".", "plan": name,
            "label": f"Upgrade to {label} and render in the cloud" if render else f"Upgrade to {label}",
            "short_label": f"Upgrade to {label}", "url": config.pricing_url(REASON, name)}


def rate(data: dict):
    """Seconds of cloud rendering one credit buys at this quote's rate (estimated_seconds over credits), or None."""
    seconds, credits = data.get("cloud_seconds"), data.get("credits")
    return seconds / credits if seconds and _amount(credits) and credits else None


def options(data: dict, balance) -> tuple[list, str]:
    """The two options, recommended first, and the balance footer. balance: the live one (else the card's). Short of
    the quote, the cloud option is the way to the credits (shortfall) and the laptop stays as it is. A render job's
    choice has no quote (job_choice): the cloud option says it is priced before it starts, and its quote's own card
    is short or not."""
    credits = data["credits"]
    quoted = credits is not None
    balance = balance if _amount(balance) else data.get("balance")
    short = shortfall(credits, balance, data.get("plan") or "", rate(data), render=True)
    facts = ([f"About {about(data['cloud_seconds'])}"] if data.get("cloud_seconds") else []) + [
        f"{_n(credits)} credits · {chat_credits.balance_dollars(credits)}, quoted" if quoted else UNPRICED]
    cloud = {"key": "cloud", "title": "In the cloud", "primary": True, "facts": facts,
             "bullets": cloud_bullets(data["count"])}
    if short:
        cloud.update(badge=short["badge"], warn=short["warn"], note=short["note"], action="upgrade",
                     **({"button": short["label"], "short": short["short_label"]} if short["label"] else {}))
    else:  # Enter picks it only when it is Recommended (enter_ok): the button says so with ⏎.
        enter = "  ⏎" if recommended(data) else ""
        price = f" · {_n(credits)} credits" if quoted else ""
        cloud.update(badge="Recommended" if enter else "", button=f"Render in the cloud{price}{enter}",
                     short=f"Render in the cloud{enter}", action="cloud")
    here = {"key": "here", "title": "On this laptop", "primary": False, "bullets": list(HERE_BULLETS)}
    measured = f"{frame_line(data['seconds_per_frame']).removesuffix(' a frame')} × {data['count']:,}"
    if fits(data):
        here.update(facts=[f"About {about(here_seconds(data))} ({measured})", "Free"], button="Render on my laptop",
                    action="here")
    else:  # One attempt would stop at render_job's limit: no one-click start for it.
        long = here_seconds(data) >= MAX_BUDGET
        here.update(facts=["Over a day on this computer" if long else
                           f"About {about(here_seconds(data))}: too long for one run here", f"({measured})"])
    footer = ("" if not _amount(balance) or short else f"Balance {_n(balance)} credits" if not quoted else
              f"Balance {_n(balance)} credits · after the cloud render {_n(balance - credits)}")
    return [cloud, here], footer


def recommended(data: dict) -> bool:
    """The cloud is clearly faster: its estimate is known and at most half the laptop's time, or 15 minutes less.
    Otherwise neither option is marked, and Enter picks neither."""
    cloud, here = data.get("cloud_seconds"), here_seconds(data)
    return bool(cloud) and (cloud <= here / 2 or cloud <= here - LONG_SECONDS)


def enter_ok(data: dict, balance) -> bool:
    """Enter approves the cloud only when it is Recommended and the balance covers it; a click on it always does."""
    return recommended(data) and not short(data, balance)


def cloud_bullets(count: int) -> list:
    """What the cloud means for the user's evening: quitting is safe only once cloud jobs survive it; one frame comes
    back as an image, more as frames and an MP4."""
    from . import cloud_jobs
    if getattr(cloud_jobs, "SURVIVES_QUIT", False) is not True:
        return list(CLOUD_BULLETS_OPEN)
    return list(CLOUD_BULLETS_ONE if count == 1 else CLOUD_BULLETS)


def short(data: dict, balance) -> bool:
    """Whether the cloud option is the way to credits now, not a render: Enter never picks it then."""
    return bool(shortfall(data["credits"], balance if _amount(balance) else data.get("balance"),
                          data.get("plan") or "", render=True))


def price_shortfall(item: dict, balance) -> dict | None:
    """A plain paid card's Upgrade, while the live balance (else the card's) is short of its price."""
    price = item.get("price") or {}
    if not price:
        return None
    return shortfall(price.get("credits"), balance if _amount(balance) else price.get("balance"),
                     price.get("plan") or "")


def live_balance():
    """The account's balance in the manifest now, or None."""
    from . import cloud, cloud_account
    runtime = cloud._runtime
    if runtime is None or not runtime.signed_in:
        return None
    with runtime.lock:
        return cloud_account.balance(runtime.manifest)


def account_plan() -> str:
    try:
        return chat_credits.plan_of(chat_credits._account()[0])
    except Exception:  # Display only: an account half way through loading gets the free plan's offer.
        return ""


def card_fields(details: dict) -> dict:
    """What a paid call's ask adds to its card (agent._Consent._ask): its price, balance and plan, which an Upgrade
    button needs when the balance is short, and the render choice when the call carries an offer
    (cloud_account.authorize)."""
    offer, credits, balance = details.get("render_offer"), details.get("credits"), details.get("balance")
    if details.get("vendor") or not _amount(credits) or not credits:
        return {}
    plan, balance = account_plan(), balance if _amount(balance) else None
    fields = {"price": {"credits": credits, "balance": balance, "plan": plan}}
    if offer:
        fields["render_choice"] = {**offer, "balance": balance, "plan": plan}
    return fields


# --- After the click -------------------------------------------------------------------------------------------------

def _drawn(item: dict, drawn) -> bool:
    """The card a panel frame drew (ui.host passes the clicked hit's (id, ask_serial)) is this card, in this ask."""
    return drawn is None or (id(item), item.get("ask_serial")) == tuple(drawn)


def click(session: dict, n: int, option: str, drawn=None) -> None:
    """Main thread, the card's buttons: the cloud approves its quote, as Approve does; the laptop declines the paid
    call with the pick, and agent._Consent._ask starts the local render. Only the card at n, while it waits, in the
    ask the frame drew (drawn)."""
    from . import agent
    item = session["items"][n] if 0 <= n < len(session["items"]) else {}
    if item.get("status") != "awaiting" or not item.get("render_choice") or not _drawn(item, drawn):
        return
    if option == "here" and not fits(item["render_choice"]):
        return  # Too long for one run: the card shows no button for it.
    item["picked"] = option  # A click, not Enter (agent.decide): the cloud runs even when it is not Recommended.
    if not agent.decide(option == "cloud", index=n, drawn=drawn):
        item.pop("picked", None)


def upgrade(item: dict, drawn=None) -> None:
    """Main thread: a short card's Upgrade (or Add credits) opens the pricing page the credits pop-up opens. A new plan
    refreshes the manifest, so the waiting card's balance and its cloud button follow; the card keeps waiting."""
    from . import account, cloud, cloud_http
    data = item.get("render_choice") or item.get("price")
    if item.get("status") != "awaiting" or not data or not _drawn(item, drawn):
        return
    balance = live_balance()
    found = shortfall(data["credits"], balance if _amount(balance) else data.get("balance"), data.get("plan") or "",
                      render=bool(item.get("render_choice")))
    if not found or not found["url"]:
        return
    cloud_http.validate_url(found["url"])
    account.open_browser(found["url"])
    if found["plan"]:
        def upgraded(_payload):
            runtime = cloud._runtime
            if runtime is not None:
                runtime.refresh_async()
        account.watch_plan(data.get("plan") or "free", upgraded)


def here_seconds(data: dict) -> float:
    """The same frames here, from the measured frame."""
    return data["seconds_per_frame"] * data["count"]


def budget(data: dict) -> int:
    """The laptop job's time budget: the estimate with a margin, so a slower frame or two never stops it."""
    return math.ceil(here_seconds(data) * BUDGET_MARGIN) + BUDGET_SLACK


def fits(data: dict) -> bool:
    """Whether one render job attempt can hold the laptop render: its budget within MAX_BUDGET."""
    return budget(data) <= MAX_BUDGET


def here_summary(data: dict) -> str:
    return f"{title(data)} on this laptop"


def _changed(data: dict) -> str:
    """Why the open scene is no longer the one the card offered to render, or "". Main thread."""
    import bpy
    from pathlib import Path
    scene = bpy.context.scene
    file, name = data.get("key") or ("", "")
    if list(scene_key(scene)) != [file, name]:
        return f'the open scene changed since the choice; it was "{name}" in {Path(file).name or "an unsaved file"}.'
    if _family(scene.render.engine) != data["engine"]:
        return "the scene's render engine changed since the choice."
    return ""


@contextmanager
def _denoised(data: dict):
    """The laptop job denoises as the cloud would (its denoise input, on by default; Cycles only): the job renders a
    saved copy of the scene, so the setting is the copy's, and the live scene keeps its own."""
    import bpy
    cycles = getattr(bpy.context.scene, "cycles", None)
    if data["engine"] != "CYCLES" or cycles is None or not hasattr(cycles, "use_denoising"):
        yield
        return
    own = cycles.use_denoising
    cycles.use_denoising = bool(data.get("denoise", True))
    try:
        yield
    finally:
        cycles.use_denoising = own


def _start(data: dict):
    """Main thread: the same frames as a local background render. (job id, the model's text, ok). The workspace and
    Codex routes (workspace.current()) have no render_job tool, so their text never names it. Only the scene the card
    was for: after a switch to another scene or file, it says so and renders nothing."""
    from . import job_tools, tools, workspace
    routed = workspace.current() is not None
    failed = HERE_FAILED_WORKSPACE if routed else HERE_FAILED
    start, end, step = data["frames"]
    if not fits(data):
        return None, failed.format(error="it would take longer than one local render job may run."), False
    changed = _changed(data)
    if changed:
        return None, failed.format(error=changed[0].upper() + changed[1:]), False
    try:
        with _denoised(data):
            result = job_tools.start_render_job(budget(data), output_dir=OUTPUT_DIR,
                                                frames=list(range(start, end + 1, step)),
                                                camera=data.get("camera") or None, width=data["width"],
                                                height=data["height"], samples=data["samples"])
    except Exception as ex:  # A ToolError says why (no camera, a missing asset); anything else, the same way.
        if not isinstance(ex, tools.ToolError):
            print(f"Loopcut: the local render did not start: {ex!r}")
        return None, failed.format(error=str(ex).rstrip(".") + "."), False
    summary = json.loads(result.text.split("\n", 1)[0])
    if routed:
        return summary["id"], HERE_RESULT_WORKSPACE.format(count=f"{data['count']:,}", output=summary["output"]), True
    return summary["id"], HERE_RESULT.format(id=summary["id"], job=result.text), True


def here(data: dict, card: dict):
    """Agent thread: the user picked this computer on the choice card. Starts the same frames as a local render job
    on Blender's thread and returns the CloudError that carries its result out of the declined paid call;
    cloud.execute makes it the tool result (result())."""
    from . import cloud_http, mainthread
    job_id, text, ok = mainthread.run_on_main(mainthread.named(
        "starting the render on this computer", lambda: _start(data))).result()
    if job_id:
        card["local_job"] = job_id  # Its card shows the job's frames (ui.render_choice.local_line).
    return cloud_http.CloudError(text, LOCAL, extra={"ok": ok, "job_id": job_id})


def job_call(pick: str, offer: dict, arguments: str, card: dict) -> tuple[str, str]:
    """Agent thread: what a render_job's card runs once the user picked on its choice (prepare_job): (tool name, its
    arguments). The cloud: the cloud render for the same frames, and the card becomes its card, so its quote, progress
    and charge show there; this computer: the job as asked, with time to render what the card said it takes here
    (budget()) when the model asked for less."""
    from . import cloud, job_tools
    if pick == "cloud":
        name, inputs = cloud.PREFIX + "render", offer["cloud"]
        card.update(name=name, summary=(cloud.describe(name, inputs) or (name,))[0], code=json.dumps(inputs, indent=2))
        return name, json.dumps(inputs)
    parsed = json.loads(arguments)
    asked = parsed.get("budget_seconds")
    if type(asked) is not int or asked < budget(offer):
        parsed["budget_seconds"] = min(MAX_BUDGET, budget(offer))
        summary, code = job_tools.describe("render_job", parsed)
        card.update(summary=summary, code=code)
    return "render_job", json.dumps(parsed)


def job_result(pick: str, offer: dict, arguments: str, ran: str, result) -> None:
    """The model's result for a render_job the user picked on (job_call): what they chose and what ran, then that
    call's own result. ran: the arguments it ran with."""
    parsed, made = json.loads(arguments), json.loads(ran)
    if pick == "cloud":
        mp4 = JOB_MP4 if parsed.get("format") == "MP4" else ""
        result.text = JOB_CLOUD.format(inputs=json.dumps(made), mp4=mp4, result=result.text)
        return
    asked, given = parsed.get("budget_seconds"), made["budget_seconds"]
    raised = "" if given == asked else JOB_BUDGET.format(
        budget=given, count=f"{offer['count']:,}", asked=f", not the {asked} s asked" if type(asked) is int else "")
    result.text = f"{JOB_HERE}{raised}\n{result.text}"


def result(error):
    """The tool result for a pick of this computer (here()): no charge; the local job's line on the card."""
    from . import spending, tools
    ok = bool(error.extra.get("ok"))
    return tools.ToolResult(str(error), ok=ok, receipt=spending.receipt_text(0),
                            outcome="Rendering on this laptop" if ok else "")
