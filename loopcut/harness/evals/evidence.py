"""Version the task contract separately from the contestant. No Blender or credentials required."""

import hashlib
import json
from pathlib import Path


def fingerprint(paths, root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def task_manifest(task) -> dict:
    here = Path(__file__).resolve().parent
    sources = [here / name for name in ("tasks.py", "projects.py", "showcase.py")]
    fixtures = list((here.parent / "fixtures").glob("*.blend"))
    contract = {"id": task.id, "prompt": task.prompt, "follow_up": task.follow_up,
                "evaluator_sha256": fingerprint(sources, here),
                "fixtures_sha256": fingerprint(fixtures, here.parent)}
    return {
        "schema_version": 1,
        "task_contract": hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest(),
        "contract": contract,
    }


def manifest(task, cfg, bpy, package: Path, timeout: float, approve_heavy: bool) -> dict:
    here = Path(__file__).resolve().parent
    return {
        **task_manifest(task),
        "agent_sha256": fingerprint(package.rglob("*.py"), package),
        "harness_sha256": fingerprint(here.glob("*.py"), here),
        "blender": {"version": bpy.app.version_string, "build_hash": bpy.app.build_hash.decode(),
                    "binary": bpy.app.binary_path},
        # Explicit allowlist: never serialize Config wholesale (it contains credentials).
        "config": {name: getattr(cfg, name) for name in
                   ("model", "reasoning_effort", "max_steps", "context_budget", "auto_run", "auto_look")},
        "timeout_per_stage": timeout, "approve_heavy": approve_heavy,
    }


def validate_arm(folder: Path, task, allow_unversioned: bool = False) -> str:
    """Reject grading an old brief with today's different checks. Return a warning for legacy runs."""
    path = folder / f"{task.id}.manifest.json"
    versioned = path.is_file()
    if versioned:
        recorded = json.loads(path.read_text(encoding="utf-8"))
        if recorded.get("task_contract") != task_manifest(task)["task_contract"]:
            raise ValueError(f"{folder.name}/{task.id}: task, checks or fixtures differ; use the matching evaluator checkout")
    elif not allow_unversioned:
        raise ValueError(f"{folder.name}/{task.id}: no task manifest; export a new start scene, or use "
                         "--allow-unversioned for an explicitly unverified historical regrade")
    # Even an exploratory regrade must not silently replace a known brief.
    result = folder / f"{task.id}.json"
    if result.is_file():
        recorded = json.loads(result.read_text(encoding="utf-8"))
        for field in ("prompt", "follow_up"):
            if field in recorded and (recorded[field] or "") != (getattr(task, field) or ""):
                raise ValueError(f"{folder.name}/{task.id}: recorded {field} differs from the current task")
    for suffix, field in (("prompt.txt", "prompt"), ("followup.txt", "follow_up")):
        text = folder / f"{task.id}.{suffix}"
        if text.is_file() and text.read_text(encoding="utf-8").strip() != (getattr(task, field) or "").strip():
            raise ValueError(f"{folder.name}/{task.id}: recorded {field} differs from the current task")
    return "" if versioned else f"UNVERIFIED task revision for {task.id}: historical run has no manifest"


def comparable(a: dict, b: dict) -> bool:
    """Unknown historical contracts are not evidence of a regression or improvement."""
    contract = (a.get("manifest") or {}).get("task_contract")
    return bool(contract) and contract == (b.get("manifest") or {}).get("task_contract")


def follow_up_allowed(task_has_follow_up: bool, outcome: str) -> bool:
    return task_has_follow_up and outcome == "completed"
