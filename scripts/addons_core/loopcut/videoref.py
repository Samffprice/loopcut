"""Video reference (v0, local only), the parts that need no Blender: trimming, the motion-energy
curve and its beats, the timed contact sheets' layout, beats.json and camera_motion.json, and what
the model is told about a clip.

The add-on imports it (video_attach.py); video_worker.py imports it as a top-level module in a
background Blender. So: no relative imports, no bpy, no numpy.

Frames are 1-based clip frames, as Blender counts a movie clip's frames. Times are seconds from the
clip's first frame. A clip is attached trimmed to at most MAX_SECONDS; every clip frame of that part
is kept (timing), the model sees about SAMPLE_FPS of them on the sheets.
"""

import math

VIDEO_EXTENSIONS = (".mp4", ".mov")
MAX_SECONDS = 15.0
MIN_SECONDS = 0.5
MAX_IMAGES = 6            # attachments.MAX_PER_MESSAGE: sheets and any other images share one message.
SAMPLE_FPS = 12           # Frames per second the sheets show at most.
ANALYSIS_MAX_FPS = 60     # A 120 fps clip is measured on every 2nd frame.
ANALYSIS_SIDE = 64        # Long side of the grey frames the motion curve is measured on.
MAX_SHIFT = 0.08          # Camera shake or pan removed per frame, as a share of the frame's width and height.
FRAME_SIDE = 640          # Long side of the stored frames: sheet tiles, look_at_reference, compare.
# Sheets: about one capture's pixels each (context.IMAGE_TOKENS), one per beat range.
SHEET_WIDTH = 1280
SHEET_MAX_HEIGHT = 960
# JPEG: every request re-sends the sheets, and six photographic PNGs (~1.3 MB each) cut ChatGPT uploads off
# (2026-10-16). At 85 a photographic sheet is ~250 KB and its labels stay sharp.
SHEET_QUALITY = 85
SHEET_SECONDS = 2.5       # With no holds to split at, a sheet covers about this much.
TILES_PER_SHEET = 16
GAP = 6
HEADER = 24
LABEL = 18
# Beats, on the motion curve normalized to 0..1 within the clip.
STILL = 0.004             # Below this mean grey change per pixel (0..1) a clip has no motion to find.
HOLD_LEVEL = 0.15
HOLD_SECONDS = 0.3
HOLD_SMOOTH_SECONDS = 0.15  # Holds are read on a slower curve: a sway's turnarounds are not pauses.
PEAK_LEVEL = 0.35
PEAK_PROMINENCE = 0.2
# Handheld footage keeps some motion everywhere (parallax and fine texture survive the shift removal):
# the levels rise with the clip's own typical motion, its median, up to a cap.
HOLD_OVER_MEDIAN, HOLD_CAP = 2.0, 0.45
PEAK_OVER_MEDIAN, PEAK_CAP = 2.5, 0.6
PEAK_GAP_SECONDS = 0.25
# Camera solve: Blender reports the mean reprojection error in pixels; under 0.3 is good, over 1 rarely usable.
MAX_SOLVE_ERROR = 1.0
MIN_SOLVED_SHARE = 0.6
FOCAL_GUESSES = (14.0, 20.0, 26.0, 35.0, 50.0, 70.0)  # mm, full-frame equivalent: phone ultra-wide to 3x.
FULL_GUESSES = (26.0, 50.0, 35.0, 20.0, 70.0)  # Full solves, likeliest first (a phone's main camera).

BEATS_ABOUT = ("frame: 1-based clip frame, as Blender counts a movie clip's frames. time: seconds from the "
               "clip's first frame. motion: 0..1 within this clip (frame differencing after removing the frame-wide "
               "shift, so camera shake and pans mostly drop out). "
               "label: a guess from the motion curve alone; the frames say what really happens. "
               "motion_curve.image_shift: [x, y] how far the whole picture has moved since the first used frame, "
               "as shares of the frame's width and height (x right, y up); turning the camera right moves it left.")
CAMERA_ABOUT = ("Per solved frame, the camera's rotation (XYZ Euler, degrees) and translation relative to its "
                "pose at the first solved frame, in that camera's own axes (x right, y up, looking down -z). "
                "Translation is in the solve's arbitrary scale. mode tripod: solved as rotation only, "
                "translation 0.")


class ClipError(ValueError):
    """A clip or a trim that cannot be used; the message is for the user."""


def is_video(name: str) -> bool:
    return str(name).lower().endswith(VIDEO_EXTENSIONS)


def fps_pair(fps: float) -> tuple[int, float]:
    """Blender's scene frame rate as (fps, fps_base): 29.97 is (30, 1.001)."""
    if not fps or fps <= 0:
        raise ClipError("The clip has no frame rate Blender can read.")
    whole = max(1, round(fps))
    return whole, whole / fps


def time_of(frame: int, fps: float) -> float:
    return (frame - 1) / fps


def frame_file(frame: int) -> str:
    """The stored frame's file name; video_worker renders with the path '######'."""
    return f"{frame:06d}.jpg"


def trim(frames: int, fps: float, start: float = 0.0, end: float | None = None) -> tuple[int, int]:
    """The clip frames (first, last, both used) for seconds start..end. end None: MAX_SECONDS after
    start, or the clip's end. Longer than MAX_SECONDS is refused rather than cut, so what is used is
    what the user chose."""
    if not fps or fps <= 0 or frames < 1:
        raise ClipError("The clip has no frames or frame rate Blender can read.")
    duration = frames / fps
    if start < 0 or start >= duration:
        raise ClipError(f"The in point {start:.2f} s is outside the clip (0 to {duration:.2f} s).")
    end = min(duration, start + MAX_SECONDS) if end is None else min(float(end), duration)
    if end - start < MIN_SECONDS - 1e-6:
        raise ClipError(f"Use at least {MIN_SECONDS:g} s of the clip; in {start:.2f} s, out {end:.2f} s.")
    if end - start > MAX_SECONDS + 0.5 / fps:
        raise ClipError(f"That part is {end - start:.1f} s; trim it to {MAX_SECONDS:g} s or less.")
    first = int(math.floor(start * fps + 1e-6)) + 1
    last = max(first, min(frames, int(math.ceil(end * fps - 1e-6))))
    return first, last


def analysis_frames(first: int, last: int, fps: float) -> list[int]:
    """The clip frames the motion curve is measured on: every frame up to ANALYSIS_MAX_FPS."""
    step = max(1, round(fps / ANALYSIS_MAX_FPS))
    frames = list(range(first, last + 1, step))
    if frames[-1] != last:
        frames.append(last)
    return frames


def sample_frames(first: int, last: int, fps: float, rate: float = SAMPLE_FPS) -> list[int]:
    """About `rate` clip frames per second from first to last, both included."""
    step = max(1.0, fps / rate)
    frames, position = [], float(first)
    while int(position + 0.5) <= last:
        frame = int(position + 0.5)
        if not frames or frame != frames[-1]:
            frames.append(frame)
        position += step
    if frames[-1] != last:  # The last frame is always shown: nudged onto it, or added when the gap is wide.
        if last - frames[-1] < step / 2 and len(frames) > 1:
            frames[-1] = last
        else:
            frames.append(last)
    return frames


# ------------------------------------------------------------------ motion and beats

def motion_energy(frames: list, width: int = 0) -> list[float]:
    """Mean absolute grey change per pixel (0..1) from the frame before; see measure_motion."""
    return measure_motion(frames, width)["energy"]


def measure_motion(frames: list, width: int = 0) -> dict:
    """frames: grey frames (0..1), flat lists of rows `width` pixels wide. Per frame, from the frame
    before: "shift", the frame-wide (dx, dy) in pixels (x right, y along the rows), and "energy", the
    mean absolute grey change per pixel once that shift is taken out. Handheld footage shakes the
    whole picture all the time; without this a sway hides every hold (the harness clip, 2026-10-16).
    width 0: plain differencing, no shift. The first frame takes the second's values."""
    if not frames:
        return {"energy": [], "shift": []}
    size = len(frames[0])
    if size == 0 or any(len(f) != size for f in frames) or (width and size % width):
        raise ValueError("measure_motion needs frames of one size, whole rows of `width`")
    energy, shifts = [0.0], [(0.0, 0.0)]
    rows_before = _rows(frames[0], width) if width else None
    for before, now in zip(frames, frames[1:]):
        if not width:
            energy.append(sum(abs(a - b) for a, b in zip(now, before)) / size)
            shifts.append((0.0, 0.0))
            continue
        rows_now = _rows(now, width)
        shift = _frame_shift(rows_before, rows_now)
        energy.append(_residual(rows_before, rows_now, *shift))
        shifts.append(shift)
        rows_before = rows_now
    if len(energy) > 1:
        energy[0], shifts[0] = energy[1], (0.0, 0.0)
    return {"energy": energy, "shift": shifts}


def _rows(frame: list, width: int) -> list[list[float]]:
    return [frame[i:i + width] for i in range(0, len(frame), width)]


def _shift_1d(a: list[float], b: list[float], radius: int) -> float:
    """s, sub-pixel, such that b[x] is about a[x - s]: how far a profile's content moved."""
    n, costs = len(a), {}
    for s in range(-radius, radius + 1):
        low, high = max(0, s), min(n, n + s)
        if high - low >= max(2, n // 2):
            costs[s] = sum((b[x] - a[x - s]) ** 2 for x in range(low, high)) / (high - low)
    best = min(costs, key=costs.get)
    left, right = costs.get(best - 1), costs.get(best + 1)
    if left is None or right is None:
        return float(best)
    curve = left - 2 * costs[best] + right
    return best + (max(-0.5, min(0.5, 0.5 * (left - right) / curve)) if curve > 1e-12 else 0.0)


def _frame_shift(before: list[list[float]], now: list[list[float]]) -> tuple[float, float]:
    """The frame-wide shift between two frames, from their column and row means (integral projections):
    cheap, and a subject smaller than the frame barely moves them."""
    height, width = len(now), len(now[0])
    columns = [[sum(c) / height for c in zip(*rows)] for rows in (before, now)]
    lines = [[sum(r) / width for r in rows] for rows in (before, now)]
    return (_shift_1d(*columns, max(1, round(MAX_SHIFT * width))),
            _shift_1d(*lines, max(1, round(MAX_SHIFT * height))))


def _residual(before: list[list[float]], now: list[list[float]], dx: float, dy: float) -> float:
    """Mean |now - before moved by (dx, dy)| over where they overlap; bilinear for the sub-pixel part."""
    height, width = len(now), len(now[0])
    ix, iy = math.floor(dx), math.floor(dy)
    ax, ay = dx - ix, dy - iy
    x0, x1 = max(0, ix + 1), min(width, width + ix)
    y0, y1 = max(0, iy + 1), min(height, height + iy)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return sum(abs(a - b) for r, q in zip(now, before) for a, b in zip(r, q)) / (width * height)
    # before(x - dx) = (1 - ax) * before[x - ix] + ax * before[x - ix - 1]; the same down the rows.
    across = {y: [(1 - ax) * row[x - ix] + ax * row[x - ix - 1] for x in range(x0, x1)]
              for y, row in enumerate(before) if y0 - iy - 1 <= y <= y1 - iy - 1}
    total = 0.0
    for y in range(y0, y1):
        upper, lower, row = across[y - iy], across[y - iy - 1], now[y]
        total += sum(abs(row[x] - ((1 - ay) * u + ay * v)) for x, u, v in zip(range(x0, x1), upper, lower))
    return total / ((x1 - x0) * (y1 - y0))


def _smooth(values: list[float], radius: int) -> list[float]:
    out = []
    for i in range(len(values)):
        window = values[max(0, i - radius):i + radius + 1]
        out.append(sum(window) / len(window))
    return out


def _candidates(values: list[float]) -> list[int]:
    """Local maxima; a plateau counts once, at its middle."""
    found, i, n = [], 0, len(values)
    while i < n:
        j = i
        while j + 1 < n and values[j + 1] == values[i]:
            j += 1
        left = values[i - 1] if i > 0 else -math.inf
        right = values[j + 1] if j + 1 < n else -math.inf
        if values[i] > left and values[i] > right:
            found.append((i + j) // 2)
        i = j + 1
    return found


def _prominence(values: list[float], i: int) -> float:
    level, sides = values[i], []
    for direction in (-1, 1):
        lowest, j = level, i + direction
        while 0 <= j < len(values) and values[j] <= level:
            lowest = min(lowest, values[j])
            j += direction
        # Running off the curve's end counts as falling to its lowest point there.
        sides.append(lowest)
    return level - max(sides)


def find_beats(energy: list[float], frames: list[int], fps: float) -> list[dict]:
    """Peaks (fast motion) and holds (little motion) on the motion curve, in clip-frame order.
    energy[i] is measured at clip frame frames[i]. Labels are guesses from the curve alone."""
    if len(energy) != len(frames) or not frames:
        raise ValueError("find_beats needs one energy value per frame")
    first, last = frames[0], frames[-1]
    step = frames[1] - frames[0] if len(frames) > 1 else 1
    radius = max(1, round(fps / step / 24))
    smooth = _smooth(energy, radius)
    ordered = sorted(smooth)
    low, high = ordered[len(ordered) // 10], ordered[-1]
    if high - low < STILL:
        return [_hold(frames, 0, len(frames) - 1, [0.0] * len(frames), fps, "still")]
    level = [max(0.0, (v - low) / (high - low)) for v in smooth]
    typical = sorted(level)[len(level) // 2]
    hold_level = max(HOLD_LEVEL, min(HOLD_CAP, HOLD_OVER_MEDIAN * typical))
    peak_level = max(PEAK_LEVEL, min(PEAK_CAP, PEAK_OVER_MEDIAN * typical))
    calm = _smooth(level, max(radius, round(HOLD_SMOOTH_SECONDS * fps / step)))

    holds, i, n = [], 0, len(level)
    shortest = HOLD_SECONDS * fps / step
    while i < n:
        if calm[i] < hold_level:
            j = i
            while j + 1 < n and calm[j + 1] < hold_level:
                j += 1
            # The slow curve finds a hold; the fast one gives its ends (smoothing would eat into them).
            a, b = i, j
            while a > 0 and level[a - 1] < hold_level:
                a -= 1
            while b + 1 < n and level[b + 1] < hold_level:
                b += 1
            if holds and a <= holds[-1][1] + 1:
                a = holds.pop()[0]
            if b - a + 1 >= shortest:
                holds.append((a, b))
            i = max(j, b) + 1
        else:
            i += 1

    peaks = []
    gap = PEAK_GAP_SECONDS * fps / step
    for c in sorted(_candidates(level), key=lambda k: -level[k]):
        if level[c] < peak_level or _prominence(level, c) < PEAK_PROMINENCE:
            continue
        if any(abs(c - kept) < gap for kept in peaks):
            continue
        peaks.append(c)
    # The smoothed peak is within `radius` of the frame that changed most.
    peaks = [max(range(max(0, c - radius), min(n, c + radius + 1)), key=lambda k: (energy[k], -abs(k - c)))
             for c in peaks]

    beats = [_hold(frames, a, b, level, fps, "") for a, b in holds]
    beats += [{"kind": "peak", "frame": frames[c], "time": round(time_of(frames[c], fps), 3),
               "motion": round(level[c], 3), "label": ""} for c in peaks]
    beats.sort(key=lambda b: b["frame"])
    _guess_labels(beats, first, last)
    return beats


def _hold(frames, a, b, level, fps, label) -> dict:
    middle = frames[(a + b) // 2]
    return {"kind": "hold", "frame": middle, "time": round(time_of(middle, fps), 3),
            "start_frame": frames[a], "end_frame": frames[b],
            "start_time": round(time_of(frames[a], fps), 3), "end_time": round(time_of(frames[b], fps), 3),
            "motion": round(sum(level[a:b + 1]) / (b - a + 1), 3), "label": label}


def _guess_labels(beats: list[dict], first: int, last: int) -> None:
    peaks = [b for b in beats if b["kind"] == "peak"]
    strongest = max(peaks, key=lambda b: b["motion"]) if peaks else None
    for index, beat in enumerate(beats):
        if beat["label"]:
            continue
        after = beats[index + 1] if index + 1 < len(beats) else None
        if beat["kind"] == "hold":
            if beat["start_frame"] == first:
                beat["label"] = "start pose"
            elif beat["end_frame"] == last:
                beat["label"] = "end pose"
            elif after is strongest:
                beat["label"] = "anticipation"
            else:
                beat["label"] = "hold"
            continue
        if beat is strongest:
            beat["label"] = "fastest motion"
            continue
        between = [b for b in beats if min(beat["frame"], strongest["frame"]) < b["frame"]
                   < max(beat["frame"], strongest["frame"]) and b["kind"] == "hold"]
        if between:
            beat["label"] = "motion"
        else:
            beat["label"] = "wind-up" if beat["frame"] < strongest["frame"] else "follow-through"


def beats_document(clip: str, fps: float, first: int, last: int, frames: list[int], energy: list[float],
                   beats: list[dict], shifts: list | None = None, size: tuple[int, int] | None = None) -> dict:
    """beats.json. The curve is the normalized motion at the SAMPLE_FPS frames, for a glance at timing;
    with shifts (measure_motion's, on frames `size` pixels wide and high) also the picture's path."""
    ordered = sorted(energy)
    low, high = ordered[len(ordered) // 10], ordered[-1]
    span = high - low if high - low >= STILL else 0.0
    by_frame = dict(zip(frames, energy))
    curve_frames = [f for f in sample_frames(first, last, fps) if f in by_frame] or frames[:1]
    values = [round(max(0.0, (by_frame[f] - low) / span), 3) if span else 0.0 for f in curve_frames]
    curve = {"frames": curve_frames, "values": values}
    if shifts and size:
        path, x, y = {}, 0.0, 0.0
        for frame, (dx, dy) in zip(frames, shifts):
            x, y = x + dx / size[0], y + dy / size[1]  # Rows run bottom-up, as Blender's images do.
            path[frame] = [round(x, 4), round(y, 4)]
        curve["image_shift"] = [path[f] for f in curve_frames]
    return {"version": 1, "clip": clip, "fps": round(fps, 4), "in_frame": first, "out_frame": last,
            "in_time": round(time_of(first, fps), 3), "out_time": round(time_of(last, fps), 3),
            "about": BEATS_ABOUT, "motion_curve": curve, "beats": beats}


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise ValueError(what)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_beats(doc) -> dict:
    """A beats.json read back from disk, checked before anything is built on it."""
    _require(isinstance(doc, dict) and doc.get("version") == 1, "beats.json: not version 1")
    _require(isinstance(doc.get("clip"), str) and _number(doc.get("fps")) and doc["fps"] > 0, "beats.json: clip or fps")
    first, last = doc.get("in_frame"), doc.get("out_frame")
    _require(type(first) is int and type(last) is int and 1 <= first <= last, "beats.json: in and out frames")
    curve = doc.get("motion_curve")
    _require(isinstance(curve, dict) and isinstance(curve.get("frames"), list)
             and isinstance(curve.get("values"), list) and len(curve["frames"]) == len(curve["values"]),
             "beats.json: motion_curve")
    _require(all(type(f) is int and first <= f <= last for f in curve["frames"])
             and all(_number(v) and 0 <= v <= 1 for v in curve["values"]), "beats.json: motion_curve values")
    shift = curve.get("image_shift", [[0, 0]] * len(curve["frames"]))
    _require(isinstance(shift, list) and len(shift) == len(curve["frames"])
             and all(isinstance(p, list) and len(p) == 2 and all(map(_number, p)) for p in shift),
             "beats.json: image_shift")
    beats = doc.get("beats")
    _require(isinstance(beats, list), "beats.json: beats")
    for beat in beats:
        _require(isinstance(beat, dict) and beat.get("kind") in ("peak", "hold"), "beats.json: beat kind")
        _require(type(beat.get("frame")) is int and first <= beat["frame"] <= last, "beats.json: beat frame")
        _require(_number(beat.get("time")) and _number(beat.get("motion")) and 0 <= beat["motion"] <= 1,
                 "beats.json: beat time or motion")
        _require(isinstance(beat.get("label"), str) and beat["label"], "beats.json: beat label")
        if beat["kind"] == "hold":
            _require(type(beat.get("start_frame")) is int and type(beat.get("end_frame")) is int
                     and first <= beat["start_frame"] <= beat["frame"] <= beat["end_frame"] <= last,
                     "beats.json: hold range")
    _require([b["frame"] for b in beats] == sorted(b["frame"] for b in beats), "beats.json: beats out of order")
    return doc


def beats_line(beats: list[dict]) -> str:
    """The beats in one line of text for the model."""
    parts = []
    for beat in beats:
        if beat["kind"] == "hold":
            parts.append(f"{beat['time']:.2f} s f{beat['frame']} hold \"{beat['label']}\" "
                         f"({beat['start_time']:.2f}-{beat['end_time']:.2f} s)")
        else:
            parts.append(f"{beat['time']:.2f} s f{beat['frame']} peak {beat['motion']:.2f} \"{beat['label']}\"")
    return "; ".join(parts) or "none found"


# ------------------------------------------------------------------ camera solve

def accept_solve(valid: bool, error: float, solved: int, total: int) -> tuple[bool, str]:
    """Whether a camera solve is good enough to hand on, and if not, why."""
    if not valid or solved == 0:
        return False, "the tracker could not solve the camera (too few features held through the clip)"
    if not _number(error) or error > MAX_SOLVE_ERROR:
        return False, f"solve error {error:.2f} px is over the {MAX_SOLVE_ERROR:g} px limit"
    if solved < MIN_SOLVED_SHARE * total:
        return False, f"only {solved} of {total} frames solved"
    return True, ""


def _rotation_to_euler_xyz(r) -> list[float]:
    """Blender's XYZ Euler (R = Rz Ry Rx) of a 3x3 rotation, in degrees."""
    sy = -max(-1.0, min(1.0, r[2][0]))
    y = math.asin(sy)
    if abs(sy) < 0.999999:
        x = math.atan2(r[2][1], r[2][2])
        z = math.atan2(r[1][0], r[0][0])
    else:  # Gimbal lock: put it all on x.
        x = math.atan2(-r[1][2], r[1][1])
        z = 0.0
    return [round(math.degrees(v), 4) for v in (x, y, z)]


def relative_motion(cameras: list, fps: float) -> list[dict]:
    """cameras: (frame, 4x4 world matrix rows) per solved frame, in frame order. Each pose relative to
    the first: R0^T R and R0^T (t - t0)."""
    base = cameras[0][1]
    r0 = [row[:3] for row in base[:3]]
    t0 = [base[i][3] for i in range(3)]
    out = []
    for frame, matrix in cameras:
        r = [row[:3] for row in matrix[:3]]
        t = [matrix[i][3] - t0[i] for i in range(3)]
        rel = [[sum(r0[k][i] * r[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
        move = [sum(r0[k][i] * t[k] for k in range(3)) for i in range(3)]
        out.append({"frame": frame, "time": round(time_of(frame, fps), 4),
                    "rotation_deg": _rotation_to_euler_xyz(rel), "translation": [round(v, 5) for v in move]})
    return out


def camera_motion_document(clip: str, fps: float, first: int, last: int, error: float, focal_mm: float,
                           sensor_mm: float, cameras: list, mode: str = "full") -> dict:
    return {"version": 1, "clip": clip, "fps": round(fps, 4), "in_frame": first, "out_frame": last,
            "mode": mode, "solve_error_px": round(error, 4), "focal_length_mm": round(focal_mm, 3),
            "sensor_width_mm": round(sensor_mm, 3), "about": CAMERA_ABOUT, "frames": relative_motion(cameras, fps)}


def check_camera_motion(doc) -> dict:
    _require(isinstance(doc, dict) and doc.get("version") == 1, "camera_motion.json: not version 1")
    _require(doc.get("mode", "full") in ("full", "tripod"), "camera_motion.json: mode")
    _require(_number(doc.get("solve_error_px")) and doc["solve_error_px"] <= MAX_SOLVE_ERROR,
             "camera_motion.json: solve error missing or over the limit")
    first, last = doc.get("in_frame"), doc.get("out_frame")
    _require(type(first) is int and type(last) is int and first <= last, "camera_motion.json: in and out frames")
    frames = doc.get("frames")
    _require(isinstance(frames, list) and frames, "camera_motion.json: frames")
    for entry in frames:
        _require(isinstance(entry, dict) and type(entry.get("frame")) is int and first <= entry["frame"] <= last,
                 "camera_motion.json: frame")
        for key in ("rotation_deg", "translation"):
            _require(isinstance(entry.get(key), list) and len(entry[key]) == 3 and all(map(_number, entry[key])),
                     f"camera_motion.json: {key}")
    _require([e["frame"] for e in frames] == sorted({e["frame"] for e in frames}), "camera_motion.json: frame order")
    return doc


# ------------------------------------------------------------------ sheets

def beat_ranges(first: int, last: int, fps: float, beats: list[dict], count: int) -> list[tuple[int, int]]:
    """At most `count` ranges covering first..last: split at the middle of every hold inside the clip
    (a pause is where one action ends), long ranges halved while there is room, the shortest
    neighbours merged until `count` remain."""
    count = max(1, count)
    cuts = sorted({b["frame"] for b in beats if b["kind"] == "hold"
                   and b["start_frame"] > first and b["end_frame"] < last})
    edges = [first] + [c for c in cuts if first < c < last] + [last + 1]
    ranges = [(a, b - 1) for a, b in zip(edges, edges[1:]) if b - 1 >= a]
    shortest = max(1, round(MIN_SECONDS * fps))
    while len(ranges) > 1 and min(b - a + 1 for a, b in ranges) < shortest:
        i = min(range(len(ranges)), key=lambda k: ranges[k][1] - ranges[k][0])
        j = i - 1 if i == len(ranges) - 1 or (i > 0 and ranges[i - 1][1] - ranges[i - 1][0]
                                               < ranges[i + 1][1] - ranges[i + 1][0]) else i + 1
        a, b = sorted((i, j))
        ranges[a:b + 1] = [(ranges[a][0], ranges[b][1])]
    longest = 2 * SHEET_SECONDS * fps
    while len(ranges) < count:
        i = max(range(len(ranges)), key=lambda k: ranges[k][1] - ranges[k][0])
        a, b = ranges[i]
        if b - a + 1 <= longest:
            break
        middle = (a + b) // 2
        ranges[i:i + 1] = [(a, middle), (middle + 1, b)]
    while len(ranges) > count:
        i = min(range(len(ranges) - 1), key=lambda k: ranges[k + 1][1] - ranges[k][0])
        ranges[i:i + 2] = [(ranges[i][0], ranges[i + 1][1])]
    return ranges


def pick_tiles(samples: list[int], must: list[int], limit: int) -> list[int]:
    """The beat frames, then evenly spread samples, at most `limit`, in order."""
    must = sorted(set(must))[:limit]
    rest = [s for s in samples if s not in must]
    room = limit - len(must)
    if len(rest) <= room:
        chosen = rest
    elif room == 1:
        chosen = [rest[len(rest) // 2]]
    elif room > 1:
        chosen = [rest[round(i * (len(rest) - 1) / (room - 1))] for i in range(room)]
    else:
        chosen = []
    return sorted(set(must) | set(chosen))


def sheet_grid(count: int, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """(columns, tile width, tile height, sheet height) for `count` tiles of a clip `size` wide and
    high: the largest tiles that keep the sheet SHEET_WIDTH wide and at most SHEET_MAX_HEIGHT high."""
    aspect = size[0] / size[1]
    best = None
    for columns in range(1, count + 1):
        rows = math.ceil(count / columns)
        width = (SHEET_WIDTH - (columns + 1) * GAP) // columns
        height = max(1, round(width / aspect))
        total = HEADER + rows * (height + LABEL + GAP) + GAP
        if total <= SHEET_MAX_HEIGHT and (best is None or width > best[1]):
            best = (columns, width, height, total)
    if best is None:  # Very tall clips: one row, narrowed until it fits.
        room = SHEET_MAX_HEIGHT - HEADER - LABEL - 2 * GAP
        width = max(1, min((SHEET_WIDTH - (count + 1) * GAP) // count, int(room * aspect)))
        height = min(room, round(width / aspect))
        best = (count, width, height, HEADER + height + LABEL + 2 * GAP)
    return best


def tile_label(frame: int, fps: float) -> str:
    return f"{time_of(frame, fps):.2f} s  f{frame}"


def plan_sheets(clip: str, fps: float, first: int, last: int, beats: list[dict], size: tuple[int, int],
                count: int) -> list[dict]:
    """The timed contact sheets: one per beat range, each up to TILES_PER_SHEET frames at about
    SAMPLE_FPS with every beat frame in it, labelled with time and clip frame, beats marked.
    Boxes are [x, y, width, height] from the sheet's top-left."""
    samples = sample_frames(first, last, fps)
    ranges = beat_ranges(first, last, fps, beats, count)
    marks = {b["frame"]: f"{b['kind'].upper()} {b['label']}" for b in beats}
    sheets = []
    for index, (a, b) in enumerate(ranges, 1):
        inside = [s for s in samples if a <= s <= b] or [a]
        tiles = pick_tiles(inside, [f for f in marks if a <= f <= b], TILES_PER_SHEET)
        columns, width, height, total = sheet_grid(len(tiles), size)
        boxes = []
        for n, frame in enumerate(tiles):
            row, column = divmod(n, columns)
            boxes.append({"frame": frame, "time": round(time_of(frame, fps), 3), "label": tile_label(frame, fps),
                          "beat": marks.get(frame, ""),
                          "box": [GAP + column * (width + GAP), HEADER + row * (height + LABEL + GAP), width, height]})
        shown = len(tiles)
        title = (f"{clip}  sheet {index}/{len(ranges)}  {time_of(a, fps):.2f}-{time_of(b, fps):.2f} s  "
                 f"({shown} of {b - a + 1} frames)")
        sheets.append({"index": index, "range": [a, b], "time": [round(time_of(a, fps), 3), round(time_of(b, fps), 3)],
                       "title": title, "size": [SHEET_WIDTH, total], "tiles": boxes,
                       "file": f"sheet_{index}.jpg"})
    return sheets


def sheet_name(clip: str, time_range: list) -> str:
    """The sheet's attachment name; a clip's own name finds its sheets (tools._reference matches by part)."""
    return f"{clip} {time_range[0]:.2f}-{time_range[1]:.2f}s"


# ------------------------------------------------------------------ what the model gets

def model_text(record: dict, sheet_names: list[str]) -> str:
    """The clip, described for the model under the user's message. record: video_attach's clip record."""
    fps, first, last = record["fps"], record["in_frame"], record["out_frame"]
    width, height = record["size"]
    folder = record["folder"].rstrip("/\\")
    lines = [f"<video_reference clip=\"{record['name']}\">",
             f"Clip file, on this computer only: {record['source']} ({width}x{height}, {fps:.3f} fps, "
             f"{record['frames'] / fps:.2f} s). Used: {time_of(first, fps):.2f}-{time_of(last, fps):.2f} s, "
             f"clip frames {first}-{last}."]
    if record.get("watch"):
        lines.append(f"What to watch, in the user's words: {record['watch']}")
    lines.append(f"Every used clip frame, {FRAME_SIDE} px on the long side: {folder}/frames/{frame_file(first)} "
                 f"to {frame_file(last)}.")
    lines.append(f"Beats ({folder}/beats.json; labels are guesses from the motion curve): {record['beats_line']}")
    camera = record.get("camera") or {}
    if camera.get("solved"):
        how = " as rotation only (tripod)" if camera.get("mode") == "tripod" else ""
        lines.append(f"Camera motion ({folder}/camera_motion.json): solved{how}, error {camera['error_px']:.2f} px, "
                     f"{camera['frames']} frames.")
    elif camera.get("asked"):
        lines.append(f"Camera motion: not used, {camera.get('reason') or 'no solve'}.")
    lines.append("Sheets attached: " + ", ".join(sheet_names) + ". Each tile is labelled with its time and clip "
                 "frame; beat frames are marked. Tiles skip frames: time a fast move from the frames between two "
                 "tiles, not from the nearest tiles.")
    # Persona round 1 (F0299): an 8-frame swing between tiles f39 and f43 was keyed as 12 frames, in scene frames.
    lines.append(f"Tiles and beats count clip frames (f{first} is the first used one), not scene frames. "
                 "clip_background shows the clip behind a camera at the clip's frame rate and says which scene "
                 "frames show which clip frames. compare_with_reference and look_at_reference take "
                 "frame=<clip frame> with the clip's name and find the scene frame themselves.")
    lines.append("</video_reference>")
    return "\n".join(lines)


def user_content(text: str, attachments: list[dict]) -> list[dict]:
    """The content of a user message with attachments: the text, a <video_reference> per clip, and
    every image, at most MAX_IMAGES (attachments.add and video_attach keep within it)."""
    clips, names = {}, {}
    for attachment in attachments:
        record = attachment.get("video")
        if record:
            clips.setdefault(record["id"], record)
            names.setdefault(record["id"], []).append(attachment["name"])
    blocks = [model_text(record, names[key]) for key, record in clips.items()]
    images = [{"type": "image_url", "image_url": {"url": a["ref"]}} for a in attachments][:MAX_IMAGES]
    return [{"type": "text", "text": "\n\n".join([text, *blocks])}, *images]
