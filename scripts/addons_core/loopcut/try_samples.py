"""The setup's Try it cards for photo → 3D and text → motion run on the house (owner, 2026-10-21): their cloud
call returns the tool's real output for that exact input, made once in production and shipped in media/samples,
instead of a job. No quote, job, upload or charge. The run plays out like a real one: the server's own progress
messages at the pace production measured, then the normal apply (import_model, apply_motion).

Only a turn the card started (agent.Turn.try_it, set when its prompt was sent unedited), only the card's own tool,
and for photo → 3D only the card's own photo; every other call is an ordinary cloud call.
"""

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

MEDIA = Path(__file__).parent / "media"
SAMPLE_PHOTO = MEDIA / "dwarf-warrior.jpg"  # The same file as onboarding.SAMPLE_PHOTO.
SAMPLES = MEDIA / "samples"
TICK = 0.25  # Seconds between cancel checks while a run plays.

# card id → its tool, the photo it must be given (None: no file) and its outputs as the server returns them:
# (name, type, mime, file in media/samples, meta). photo_3d: Tripo H3.1 with HD texture on dwarf-warrior.jpg
# (it beat Rodin Gen-2.5 High on this character, owner-judged 2026-10-21), decimated from 1.47M to 300K triangles
# with 2K JPEG textures (10.9 MB) to ship;
# motion: Kimodo, "a figure jumps for joy", 3 s.
CARDS = {
    "photo_3d": {"tool": "image_to_3d", "photo": SAMPLE_PHOTO,
                 "outputs": (("model", "model_3d", "model/gltf-binary", "photo_3d.glb", {"topology": "triangles"}),)},
    "motion": {"tool": "motion_from_text", "photo": None,
               "outputs": (("motion.bvh", "motion", "application/x-bvh", "motion_jump.bvh",
                            {"fps": 30, "frames": 90, "seconds": 3, "skeleton": "somaskel77"}),
                           ("motion.json", "motion", "application/json", "motion_jump.json", {}))},
}


def timeline(tool: str, arguments: dict) -> list[tuple[float, int, str]]:
    """(seconds from the start, pct, message) as the job card shows them, ending at the run's length. Messages
    and pct follow the server adapters: fal.ts (5% queued, then 10 + elapsed / scale × 80, at most 95, every
    2 s) and motion.ts ("Queued" at 0%, then the worker's "Generating motion" at 5%). Motion lasts as long as
    production's average (36 s over 20 jobs, 2026-10-21). The photo sample is shorter than a real model (149 s
    final, 33 s draft over production's 2026-10-21 jobs): the owner wants it 30 s (2026-10-23), with the same
    climb compressed into it."""
    if tool == "image_to_3d":
        length, scale = 30.0, 25.0  # Final or draft alike.
        steps = [(0.0, 5, "Queued for model processing.")]
        t = 2.0
        while t < length:
            steps.append((t, min(95, max(10, int(10 + (t - 2.0) / scale * 80))), "Generating model."))
            t += 2.0
        return steps + [(length, 100, "Done")]
    return [(0.0, 0, "Queued"), (28.0, 5, "Generating motion"), (36.0, 100, "Done")]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def match(card: str, tool: str, files: dict) -> dict | None:
    """The card's sample when this call is the one its card asked for, else None."""
    sample = CARDS.get(card or "")
    if sample is None or sample["tool"] != tool:
        return None
    if sample["photo"] is not None:
        try:
            wanted = _sha256(sample["photo"])
            if not any(Path(path).is_file() and _sha256(Path(path)) == wanted for path in files.values()):
                return None
        except OSError:
            return None
    if not all((SAMPLES / output[3]).is_file() for output in sample["outputs"]):
        return None  # A build without the files runs the ordinary call.
    return {**sample, "card": card}


def job(sample: dict, kind: str, inputs: dict, apply_args: dict) -> dict:
    """A succeeded local job with the sample's outputs: what cloud_jobs.apply_result takes (a BYOK job's shape). Its
    id names the card (the Outliner shows it, "Loopcut try_photo_3d model"): traceable, never a server job id."""
    job_id = f"try_{sample['card']}"
    outputs = []
    for name, kind_, mime, file, meta in sample["outputs"]:
        path = SAMPLES / file
        data = path.read_bytes()
        outputs.append({"name": name, "type": kind_, "mime": mime, "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data), "meta": dict(meta), "path": str(path)})
    return {"id": job_id, "tool": sample["tool"], "status": "succeeded", "error": None,
            "progress": {"pct": 100, "message": "Done"},
            "credits": {"reserved": 0, "charged": 0, "refunded": 0},
            "provenance": {"job_id": job_id, "tool": sample["tool"], "kind": kind, "vendor": "loopcut",
                           "model": "onboarding-sample", "created_at": datetime.now(timezone.utc).isoformat()},
            "outputs": outputs, "input": dict(inputs), "apply_args": dict(apply_args)}


def play(steps: list[tuple[float, int, str]], progress, cancelled, clock=time.monotonic, sleep=time.sleep) -> bool:
    """Show each step at its time; False as soon as the call is cancelled."""
    start = clock()
    for at, pct, message in steps:
        while clock() - start < at:
            if cancelled():
                return False
            sleep(min(TICK, at - (clock() - start)))
        if pct < 100:
            progress({"pct": pct, "message": message})
    return not cancelled()
