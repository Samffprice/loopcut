"""One delegate call's workers, from the scene copy to the merges and the reports.

prepare() runs on the main thread inside the delegate tool: the job and wave folders, the wave's
checkpoint (restoring it undoes every merge of the wave and cuts the conversation back to the
delegate call), each worker's opening scene context, the scene copy (merge.save_spawn) and the wave
card. run_wave() is the tool result's `then` and runs on the agent thread: the wave's workers join
the turn's job (start), whose own thread runs them (_schedule), and delegate returns as soon as one
has finished (collect), so the orchestrator reviews each result while the others still build:

- Spawn rule: the first min(n, max_workers) children start at once (prewarm), worker 1's loop at
  once too, and every other worker's first request waits until worker 1's first reply has come
  back (or 90 s): a prefix is cached only once a request with it has completed, and every worker
  shares one prefix (workers.py). Briefs past max_workers start as slots free; the slots are the
  turn's, so a later wave's briefs (a continues re-brief) queue behind the earlier ones.
- Each worker is agent.run_loop with workers.SPEC on its own thread; its model calls are made
  here, in the parent (keys never reach a child), its tools run in its child (worker_host.py).
- One JobBudget per turn (dollars and seconds per worker run) is checked before
  every worker request; spent, it stops each worker with a wrap-up (status partial).
- A child that dies is retried once from the pristine copy, in the same conversation.
- Stop kills every child and nothing more merges; a row's Cancel stops that worker only.
- collect: once a worker has finished, up to BATCH_SECONDS more for others finishing about then;
  then report: the finished ones merged (main thread, one call per wave, each its own undo step),
  their checks on the live scene (one naming a worker still running waits for it), the compact
  report and who is still running. delegate wait (no briefs) collects the next; the finish gate
  waits for the rest (drain) and has the orchestrator review them before the turn ends; end_turn
  stops or finishes whatever a turn that ended otherwise left.

The report is built from structured data. The only words of a worker the orchestrator reads are
its summary, clipped, beside what its deliverables measured (facts.py); never a transcript. One
image goes with it: a contact sheet of the deliverables it reports, each in colour (contact_sheet).
"""

import copy
import dataclasses
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import (acceptance, agent, checkpoints, config, context, conversations, facts, llm, merge, ownership,
               paint_stats, pricing, state, tools, workers)

FIRST_REPLY_WAIT = 90.0   # Seconds workers 2..N wait for worker 1's first reply before going anyway.
POLL = 0.05
HARD_GRACE = 120.0        # Past a worker's time budget, a worker still busy in a tool is killed after this.
JOIN_SECONDS = 5.0        # After a stop, how long the job waits for its worker threads.
BATCH_SECONDS = 20.0      # Once a worker has finished, how long delegate waits for others finishing about then.
REPORT_TOKENS = 360       # One worker's block, at most: the design's 250, plus a measured line per deliverable.
AUTO_LOOK_ROLES = {"modeling", "camera"}
MAX_FAIL_LINES = 4
CRASH_NOTE = ("[Loopcut] Your Blender crashed during {tool}; the scene is back at the start of your brief, and "
              "nothing you built is kept. Build it again, in smaller steps.")
_usage_lock = threading.Lock()  # Worker threads add to the parent session's usage at once.
_running: dict[tuple[str, str], "Worker"] = {}  # (wave, worker) -> Worker, for the rows' Cancel buttons.


def jobs_root() -> Path:
    """Worker jobs, one folder per conversation: <root>/<conversation id>/<turn>-<n>/ holds job.json
    and, per wave, w<n>/spawn.blend and w<n>/<worker>/ (transcript, captures, log). Not jobs/,
    which is the render jobs' (jobs.py) and listed in the Jobs view."""
    return checkpoints.data_root() / "workers"


KEEP_DAYS = 7      # reap(): job folders older than this go,
KEEP_NEWEST = 20   # and so do all but the newest this many.


def reap_wave(folder: Path) -> None:
    """A finished wave's temporary files: the spawn copy, each worker's result and per-call copies
    and its TMPDIR. job.json, transcripts, captures and logs stay. The merge deletes the copies it
    used; this covers a wave that was stopped, crashed or had nothing to merge."""
    folder = Path(folder)
    for path in [*folder.glob("*.blend*"), *folder.glob("*/*.blend*")]:
        try:
            path.unlink()
        except OSError:
            pass
    for tmp in folder.glob("*/tmp"):
        shutil.rmtree(tmp, ignore_errors=True)


def _owner_alive(job: Path) -> bool:
    try:
        owner = json.loads((job / "job.json").read_text(encoding="utf-8")).get("owner")
    except (OSError, ValueError, AttributeError):
        return False
    if owner == os.getpid():
        return True
    from . import worker_host
    return worker_host._alive(owner)


def reap(now: float | None = None) -> None:
    """At add-on start: kill worker Blenders an earlier Blender left running, remove job folders
    older than KEEP_DAYS or beyond the newest KEEP_NEWEST, and the temporary copies of jobs whose
    Blender is gone. A job of a Blender still running (another window, an eval) is left alone."""
    from . import worker_host
    root = jobs_root()
    if not root.is_dir():
        return
    now = time.time() if now is None else now
    conversations_ = [c for c in root.iterdir() if c.is_dir()]
    for conversation in conversations_:
        worker_host.reap_orphans(conversation)
    jobs = sorted((j for c in conversations_ for j in c.iterdir() if j.is_dir()),
                  key=lambda j: j.stat().st_mtime, reverse=True)
    for rank, job in enumerate(jobs):
        if _owner_alive(job):
            continue
        if rank >= KEEP_NEWEST or now - job.stat().st_mtime > KEEP_DAYS * 86400:
            shutil.rmtree(job, ignore_errors=True)
        else:
            for wave in job.glob("w*"):
                reap_wave(wave)
    for conversation in conversations_:
        try:
            conversation.rmdir()  # Only when empty.
        except OSError:
            pass


# ------------------------------------------------------------------ the job: one turn's waves

def budget_steps_left(*, steps: int, elapsed: float, seconds: float, job_steps: int, usd: float,
                      max_usd: float, running: int, priced: bool) -> float | None:
    """Estimate from observed mean seconds/step and shared dollars/step, before the next request.
    No sample on the first request. Unknown prices and zero-cost samples leave only the time estimate;
    concurrent workers share the dollars remaining. This is a warning estimate, never a step limit."""
    if steps < 1:
        return None
    estimates = []
    if elapsed > 0:
        estimates.append(max(0, seconds - elapsed) / (elapsed / steps))
    if priced and max_usd > 0 and usd > 0 and job_steps > 0:
        estimates.append(max(0, max_usd - usd) / (usd / job_steps * max(1, running)))
    return min(estimates) if estimates else None


class JobBudget:
    """Shared dollars for one turn and seconds per worker run; worker steps are counted, never capped."""

    def __init__(self, usd: float, seconds: float, clock=None):
        self.max_usd, self.seconds = usd, seconds
        self.steps, self.usd = 0, 0.0
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()

    def spent(self, started: float | None = None) -> str:
        """Why a worker may not go on, or "". started: when that worker's run began (its seconds)."""
        if self.max_usd and self.usd >= self.max_usd:
            return f"the job's ${self.max_usd:.2f} is used"
        if started is not None and self.clock() - started >= self.seconds:
            return f"its {self.seconds:g} s ran out"
        return ""

    def take_step(self, started: float, *, worker_steps: int = 0, running: int = 1,
                  priced: bool = True) -> float | None:
        """Before a worker's request: check, estimate, then count it. A spent budget raises BudgetStop."""
        with self._lock:
            reason = self.spent(started)
            if reason:
                raise agent.BudgetStop(reason)
            left = budget_steps_left(steps=worker_steps, elapsed=self.clock() - started, seconds=self.seconds,
                                     job_steps=self.steps, usd=self.usd, max_usd=self.max_usd,
                                     running=running, priced=priced)
            self.steps += 1
            return left

    def add(self, model: str, usage: dict) -> None:
        cost = pricing.usd(model, usage)  # None for a model with no price: then time bounds the run.
        if cost:
            with self._lock:
                self.usd += cost


class Job:
    """What one turn's delegate calls share: the folder, the budget, the workers of every wave and the
    thread that runs them, every check written so far (the last version of each), the orchestrator's
    own checks and the ones it retired, the change requests already filed, and the JobLog
    (session["jobs"]). A turn that only adds checks has one too: the finish gate runs on it."""

    def __init__(self, session: dict, roles: config.Roles):
        self.session = session
        turn = session.get("turn_number") or 1
        n = 1 + sum(1 for log in session["jobs"] if log.get("turn") == turn)
        while (jobs_root() / session["id"] / f"{turn}-{n}").exists():
            n += 1
        self.id = f"{turn}-{n}"
        self.folder = jobs_root() / session["id"] / self.id
        self.budget = JobBudget(roles.job_max_usd, roles.worker_max_seconds)
        self.max_workers = roles.max_workers
        self.bounces = 0
        self.checks: dict[tuple[str, str], dict] = {}  # (worker, check id) -> check
        self.own: dict[str, dict] = {}                  # The orchestrator's (orchestrator.checks add): "o1" -> check.
        self.retired: dict[str, tuple] = {}             # Label -> (check, reason): out of the gate, said in its block.
        # Earlier turns' standing checks (label -> check), which this turn's gate runs too: what a later
        # turn breaks is found, and what the user has since overridden is retired, not "fixed".
        self.carried = session.setdefault("checks", {})
        self.earlier = {label: check for label, check in self.carried.items() if check.get("kind") != "unchanged"}
        self.keep: dict[str, dict] = {}                 # shared.keep's unchanged checks: "keep", "keep 3" (_kept).
        self.requests: set = set()                      # (target, normalised change) filed so far.
        self.checker: Callable[[list], list] | None = None  # Runs checks on the live scene, from the agent thread.
        # The workers of every wave, in the order asked for, and what runs them (start, _schedule).
        self.waves: list["Wave"] = []
        self.crew: list["Worker"] = []
        self.lock = threading.Lock()                    # crew, scheduler, stopped: the job's thread, collect and Stop.
        self.scheduler: threading.Thread | None = None
        self.stopped = False                            # Stop: nothing more starts or merges.
        self.steers: list[tuple[str, dict | None]] = []  # Corrections and parent chat items; each worker has a cursor.
        self.on_main: Callable | None = None            # fn -> fn() on the main thread, for the finish gate's drain.
        self.held: list[tuple[str, dict]] = []          # Live checks naming a worker still running: run once it merges.
        self.sheets = 0
        # What `unchanged` compares with: the first wave's spawn (identity map and tokens), extended by
        # later waves' datablocks that did not exist then (absorb), its identity map following what
        # each merge replaced (follow). A check from a later wave brings that wave's tokens (_pinned).
        self.baseline = None
        # waited: seconds the orchestrator spent blocked on its workers (delegate, and the finish gate's drain).
        self.log = {"job": self.id, "turn": turn, "level": 0, "bounces": 0, "final_checks": None, "waves": [],
                    "waited": 0.0, "folder": str(self.folder), "owner": os.getpid()}
        session["jobs"].append(self.log)
        self.write()  # The folder and its owner, before anything else lands in it (reap).

    def absorb(self, spawn) -> None:
        if self.baseline is None:
            self.baseline = copy.copy(spawn)
            for attr in ("ids", "tokens"):  # Its own dicts: follow changes the ids, never the spawn's.
                if isinstance(getattr(spawn, attr, None), dict):
                    setattr(self.baseline, attr, dict(getattr(spawn, attr)))
            return
        for attr in ("ids", "tokens"):
            known, new = getattr(self.baseline, attr, None), getattr(spawn, attr, None)
            if isinstance(known, dict) and isinstance(new, dict):
                setattr(self.baseline, attr, {**new, **known})

    def follow(self, spawn) -> None:
        """After a merge: what it replaced (merge.Spawn.moved, key -> the replacement's session_uid) is
        found by its key from now on. Without this every datablock a worker owned read as gone to the
        job's later checks (the Arcane look pass, 20260929-235231: seven painted props "gone" after
        the lighting wave, retired by the orchestrator)."""
        moved = getattr(spawn, "moved", None)
        if self.baseline is not None and isinstance(moved, dict) and isinstance(getattr(self.baseline, "ids", None), dict):
            self.baseline.ids.update(moved)

    def final_checks(self) -> list[tuple[str, dict]]:
        """(label, check) the finish gate runs: earlier turns' (unless this turn wrote one by that
        label), each worker's last version ("armchair c1"), the orchestrator's own ("o1") and keep;
        none retired."""
        current = [(f"{worker} {cid}", check) for (worker, cid), check in self.checks.items()] + list(self.own.items())
        labels = {label for label, _ in current}
        earlier = [(label, check) for label, check in self.earlier.items() if label not in labels]
        return [(label, check) for label, check in earlier + current + list(self.keep.items())
                if label not in self.retired]

    def finish(self, bounces: int, results: list) -> None:
        """The finish gate's outcome, into the JobLog; the checks that stand carry to the next turn
        (not the unchanged ones: they compare with this job's copies)."""
        from . import orchestrator
        failed = [r for r in results if r[2].get("status") != "pass"]
        self.log.update(bounces=bounces, final_checks={
            "passed": len(results) - len(failed), "failed": len(failed),
            "lines": [orchestrator.result_line(*r) for r in failed]},
            retired=[{"id": label, "reason": reason} for label, (_check, reason) in self.retired.items()])
        standing = {label: check for label, check in self.final_checks() if check.get("kind") != "unchanged"}
        self.carried.clear()  # Earlier turns' that still stand are in final_checks; retired ones are not.
        self.carried.update(standing)
        self.write()

    def write(self) -> None:
        _write_job_log(self.folder, self.log)


def _write_job_log(folder: Path, log: dict) -> None:
    """Persist receipts even after the turn's live Job is gone."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "job.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
    except OSError as ex:
        print(f"Loopcut: could not write the job log: {ex}")


def job_for(session: dict, turn) -> Job:
    if getattr(turn, "job", None) is None:
        turn.job = Job(session, turn.roles)
    return turn.job


# ------------------------------------------------------------------ the wave

@dataclasses.dataclass(eq=False)  # A wave is itself: compared and hashed by identity.
class Wave:
    session: dict
    turn: Any                 # The orchestrator's agent.Turn.
    job: Job
    number: int
    id: str                   # The merge's wave id (Spawn.wave), what accept_quarantine takes.
    folder: Path              # <job>/w<n>: spawn.blend, and a folder per worker.
    plan: dict                # orchestrator.normalise_wave's.
    spawn: Any                # merge.Spawn
    openings: dict            # worker id -> its first message.
    item: dict                # The wave card.
    message_index: int        # The orchestrator's delegate message, where the wave checkpoint cuts back to.
    item_index: int           # The wave card.
    checkpoint: str = ""      # Taken before the workers start; every merge of the wave names it.
    scene_before: dict | None = None
    # Seams, replaced by the tests: a worker's runtime, the merge, the checks, the scene snapshot, a
    # datablock's token (ownership.token), all but make_runtime called on the main thread.
    make_runtime: Callable = None
    merge: Callable = None
    run_checks: Callable = None
    snapshot: Callable = None
    token_of: Callable = None
    on_main: Callable = None  # Set by run_wave: fn -> fn() on the main thread.
    threads: dict = dataclasses.field(default_factory=dict)  # Worker id -> the earlier thread it continues (remember).
    first_reply: threading.Event = dataclasses.field(default_factory=threading.Event)
    started: float = 0.0
    crew: list = dataclasses.field(default_factory=list)  # Its workers (start).
    entry: dict | None = None  # Its JobLog entry: filled in as its workers report.
    steer_index: int = 0       # Live corrections after this cursor arrive before each request.
    opening_notes: dict = dataclasses.field(default_factory=dict)  # Worker id -> note items kept in its opening.
    plan_card: dict | None = None  # The delegate attempt, marked only after a helper successfully boots.


def _mentions(brief: dict, shared: dict) -> str:
    """@names the worker's scene context describes: what it owns and what it rests on."""
    names = list(brief["owns"]) + ([brief["rests_on"]] if brief.get("rests_on") else [])
    return " ".join('@"' + name + '"' for name in dict.fromkeys(names))


def _owned_keys(brief: dict) -> list:
    """An owned object's data too when only owned objects use it, and the aspects' datablocks."""
    return sorted(ownership.expand_owns(brief["owns"], brief["aspects"]))


def _save_spawn(folder: Path, palette=()):
    return merge.save_spawn(folder, palette)


def _scene_context(text: str) -> str:
    from . import scene_context
    return scene_context.for_message(text, None)


def _wave_checkpoint(session: dict, number: int, message_index: int, item_index: int) -> str:
    """Before the workers start: restoring it returns the scene to before the delegate call, every merge
    of the wave undone, and both transcripts to before the call. Raises checkpoints.CheckpointError:
    then nothing starts, as no merge may go on without one."""
    # prepare() receives the future wave card's index. Include the executing delegate card in the
    # restore cut too: leaving its startup marker would falsely recover earlier refused plans.
    item_index = next((n for n in reversed(range(item_index))
                       if session["items"][n].get("kind") == "tool"
                       and session["items"][n].get("name") == "delegate"
                       and session["items"][n].get("status") == "running"), item_index)
    return checkpoints.ensure_for_wave(session, f"Before wave {number}", message_index, item_index)


def prepare(session: dict, turn, plan: dict, message_index: int, save_spawn=None, scene_context=None) -> Wave:
    """Main thread. Everything a wave needs before its workers start; raises when the checkpoint or the
    copy cannot be saved (then nothing started)."""
    roles = turn.roles
    from . import cloud
    if not config.on_service(roles.worker.base_url):
        raise config.ConfigError("Workers require the Loopcut service")
    cloud.hosted_index(required=True)  # Also for same-role continuations, which keep earlier notes.
    job = job_for(session, turn)
    with job.lock:
        steer_index = len(job.steers)
        note_ids = {id(note[1]) for note in job.steers if not isinstance(note, str)}
    number = session.setdefault("wave_count", 0) + 1
    folder = job.folder / f"w{number}"
    folder.mkdir(parents=True, exist_ok=True)
    shared = plan["shared"]
    for brief in plan["workers"]:
        brief["owned"] = _owned_keys(brief)
    openings, threads, opening_notes = {}, {}, {}
    request_items = [item for item in session["items"] if item.get("kind") == "user"]
    requests = [item["text"] for item in request_items]  # The user's own words.
    for brief in plan["workers"]:
        included = []
        others = [w for w in plan["workers"] if w is not brief]
        scene = (scene_context or _scene_context)(_mentions(brief, shared))
        thread = (session.get("worker_threads") or {}).get(brief.get("continues") or "")
        if thread is not None:
            threads[brief["id"]] = thread
            openings[brief["id"]] = workers.continuation(brief, shared, others, scene, requests, thread["role"],
                                                         merged=thread.get("merged", True), status=thread.get("status", ""),
                                                         request_indices=included)
        else:
            openings[brief["id"]] = workers.opening(brief, shared, others, scene, requests, request_indices=included)
        opening_notes[brief["id"]] = [request_items[n] for n in included if id(request_items[n]) in note_ids]
    palette = [n for n in shared.get("palette") or () if isinstance(n, str)]
    item_index = len(session["items"])  # Where the wave card goes.
    plan_card = next((item for item in reversed(session["items"])
                     if item.get("kind") == "tool" and item.get("name") == "delegate"
                     and item.get("status") == "running"), None)
    checkpoint = _wave_checkpoint(session, number, message_index, item_index)
    spawn = save_spawn(folder) if save_spawn else _save_spawn(folder, palette)
    job.absorb(spawn)
    session["wave_count"] = number
    wave_id = str(getattr(spawn, "wave", "") or f"w{number}-{uuid.uuid4().hex[:4]}")
    rows = [state.wave_row(b["id"], b["deliverable"], b["role"]) for b in plan["workers"]]
    item = state.item_wave(wave_id, number, rows)
    item["job"] = job.id
    session["items"].append(item)
    turn.delegations.append([b["deliverable"] for b in plan["workers"]])
    return Wave(session=session, turn=turn, job=job, number=number, id=wave_id, folder=folder, plan=plan,
                spawn=spawn, openings=openings, item=item, message_index=message_index,
                item_index=item_index, checkpoint=checkpoint, threads=threads, steer_index=steer_index,
                opening_notes=opening_notes, plan_card=plan_card)


# ------------------------------------------------------------------ the child, the merge, the checks

class _HostRuntime:
    """A worker's child Blender on the wave's copy (worker_host.Child) and its tool routing
    (worker_host.WorkerRuntime): scene tools there, network tools and asset downloads here, on the
    main thread. A child that dies comes back from run_tool as a failed result with .crashed set."""

    def __init__(self, wave: Wave, worker: "Worker"):
        from . import worker_host
        brief = worker.brief
        child = worker_host.Child(wave.job.folder, worker.id, wave.spawn.path, brief["role"])
        self.init = {"role": brief["role"], "aspects": brief.get("aspects") or [], "prefix": brief["prefix"],
                     "owned": brief["owned"], "deliverable": brief["deliverable"],
                     "checks": brief["checks"], "advisory": brief.get("advisory") or [],
                     "requirements": brief.get("requirements") or [],
                     "defaults": brief.get("defaults") or [], "owns": brief.get("owns") or [],
                     "boxes": _planned_boxes(brief), "run_timeout": config.run_timeout(),
                     "reference": getattr(worker, "reference", None)}
        parent = {name: self._parent_tool(wave, name, worker.cancelled) for name in ("search_assets", "look_at_reference")}
        parent["fetch_asset"] = lambda arguments: wave.on_main(lambda: _fetch_asset(arguments))
        self.tools = worker_host.WorkerRuntime(child, parent)
        self.is_cancelled = worker.cancelled
        self.warnings: list = []

    @staticmethod
    def _parent_tool(wave: Wave, name: str, is_cancelled=None):
        def run(arguments_json):
            result = wave.on_main(lambda: tools.execute(name, arguments_json))
            if result.then:
                return result.then(is_cancelled or wave.turn.cancel.is_set, wave.on_main)
            return result
        return run

    def _call(self, op: str, timeout: float, is_cancelled) -> dict:
        reply = self.tools.child.call(op, self.init if op == "init" else {}, timeout, is_cancelled)
        if not reply.get("ok"):
            raise RuntimeError(f"{op} failed in the worker's Blender: {reply.get('error')}")
        return reply

    def start(self) -> None:
        self.tools.child.start(is_cancelled=self.is_cancelled)
        self.warnings = list(self._call("init", 60, self.is_cancelled).get("warnings") or [])

    def run_tool(self, name: str, arguments: str, is_cancelled):
        return self.tools.run_tool(name, arguments, is_cancelled)

    def check(self, is_cancelled) -> list:
        return self._call("check", 60, is_cancelled).get("results", [])

    def finish(self, is_cancelled) -> dict:
        return self._call("finish", 120, is_cancelled)

    def close(self) -> None:
        self.tools.child.exit()

    def kill(self) -> None:
        self.tools.child.kill()

    @property
    def peak_rss_mb(self):
        return self.tools.child.peak_rss_mb or None


def _planned_boxes(brief: dict) -> list:
    """The brief's planned bounds as [[x0, y0, z0], [x1, y1, z1]] boxes, for the child's facts."""
    from .orchestrator import _boxes
    return [[list(low), list(high)] for low, high in _boxes(brief.get("bounds")) or ()]


def _fetch_asset(arguments: dict) -> dict:
    from . import assets
    return assets.fetch_asset(**arguments)


def _merge(spawn, results: list, checkpoint, keep_spawn: bool = False):
    return merge.merge_wave(spawn, results, checkpoint=checkpoint, keep_spawn=keep_spawn)


def _run_checks(checks: list, spawn=None) -> list:
    return acceptance.run(checks, spawn)


def _snapshot():
    from . import scene_diff
    return scene_diff.snapshot()


def _token_of(name: str) -> str | None:
    """A change request's target now ("Rug" or "OBJECT:Rug"), so the eval can tell whether the
    orchestrator acted on it (run_task.change_requests finds it the same way)."""
    idb = ownership.find(name)
    return ownership.token(idb) if idb is not None else None


# ------------------------------------------------------------------ a worker

class Worker:
    def __init__(self, wave: Wave, brief: dict, index: int):
        self.wave, self.brief, self.index, self.id = wave, brief, index, brief["id"]
        self.row = wave.item["rows"][index]
        self.steer_index = wave.steer_index
        roles = wave.turn.roles
        self.model = roles.worker.model
        self.session = state.new_session()
        self.attempt_item_start = 0  # Earlier attempts stay in history, but their scene was discarded.
        self.folder = wave.folder / self.id
        conversations.FOLDERS[self.session["id"]] = self.folder
        thread = wave.threads.get(self.id)
        if thread is not None:  # A brief sent back to the worker that built this: its conversation goes on.
            self.session["messages"] += copy.deepcopy(thread["messages"])
            conversations.close_open_tool_calls(self.session["messages"], "Not run: that work ended here.")
            _copy_images(Path(thread["folder"]), self.folder)
        self.session["items"].append(state.item_user(f"{'More work' if thread else 'Brief'} for {brief['deliverable']}"))
        earlier = copy.deepcopy(thread.get("references") or []) if thread is not None else []
        new = attach_references(self.session, wave.session, brief.get("references") or [], earlier)
        self.session["messages"].append(opening_message(wave.openings[self.id], new))
        self.reference = reference_tile(self.session, brief.get("references") or [])
        self.turn = self._new_turn(agent.Turn().cancel)
        self.spec = workers.SPEC(brief["role"], save=conversations.save, before_request=self.before_request,
                                 on_usage=self.on_usage, after_step=self.after_step)
        self.allowed = workers.tool_names()
        self.runtime = None
        self.ready = threading.Event()
        self.boot_error: Exception | None = None
        self.thread: threading.Thread | None = None
        self.status = "queued"
        self.note = ""
        self.report: dict | None = None
        self.crashed = ""         # The tool its child died in, until the retry.
        self.crash_error = ""
        self.crashes: list = []   # {tool, error, kept}: each time its child died; kept, its crash kit (worker_host).
        self.retries = self.rollbacks = self.steps = 0
        self.budget_warned = False  # One warning per run, also across a child crash/retry.
        self.first_call_cached = None
        self.usage = {"input": 0, "cached": 0, "output": 0, "requests": 0}
        self.checks: list = []    # The child's results for the brief's checks, at the end.
        self.advice: list = []    # And for its advisory checks: shown, never counted.
        self.manifest: dict | None = None
        self.outside_role: list = []
        self.user_cancelled = False
        self.started = 0.0        # When its thread started: its seconds run from here.
        self.finished: float | None = None  # When it ended, or was stopped before it started.
        self.reported = False     # Its result went to the orchestrator (report), merged or not.
        self.log: dict | None = None  # Its JobLog entry, once reported.
        self.t = {"spawn": None, "ready": None, "first_request": None, "first_reply": None, "end": None,
                  "reported": None}
        self.row["timing"] = self.t  # The panel reads existing request/reply milestones, without polling.

    def _new_turn(self, cancel: threading.Event) -> agent.Turn:
        roles = self.wave.turn.roles
        turn = agent.Turn()
        turn.cancel = cancel  # Kept across a retry, so Stop and the row's Cancel reach the new loop.
        turn.config = dataclasses.replace(roles.worker, max_steps=0, auto_run=True,
                                          auto_look=roles.worker.auto_look and self.brief["role"] in AUTO_LOOK_ROLES)
        # Credits (C47): a worker spends from the user's chat and turn, never a budget of its own; a paid call
        # that asks shows in the user's panel under this name, and the worker waits for the answer.
        turn.chat = (self.wave.session, self.wave.turn)
        turn.worker = f"worker {self.id} ({self.brief['role']})"
        return turn

    def _now(self) -> float:
        return round(time.monotonic() - self.wave.started, 3)

    def cancelled(self) -> bool:
        return self.turn.cancel.is_set()

    def _stopped(self) -> None:
        """A row's Cancel is "cancelled"; Stop, for every worker of the turn, is "stopped"."""
        # Startup can be cancelled before the parent turn is available.
        parent_cancel = getattr(getattr(self.wave, "turn", None), "cancel", None)
        if self.user_cancelled:
            self.set_status("cancelled", "stopped by the user")
        elif parent_cancel is not None and parent_cancel.is_set():
            self.set_status("stopped", "stopped by the user")
        else:
            self.set_status("stopped")

    def set_status(self, status: str, note: str | None = None) -> None:
        self.status = self.row["status"] = status
        if note is not None:
            self.note = self.row["note"] = note
        agent._redraw()

    # -- the loop's hooks

    def before_request(self, step: int) -> None:
        if self.cancelled():
            raise llm.Cancelled()
        spent = self.wave.job.budget.spent(self.started)
        if spent:  # Before any wait: a spent budget stops the worker now.
            raise agent.BudgetStop(spent)
        if self.index > 0 and not self.wave.first_reply.is_set():
            deadline = time.monotonic() + FIRST_REPLY_WAIT
            while not self.wave.first_reply.wait(POLL):
                if self.cancelled():
                    raise llm.Cancelled()
                if time.monotonic() > deadline:
                    break
        job = self.wave.job
        with job.lock:
            # A failed boot can open first_reply just as Stop cancels the waiting siblings.
            if self.cancelled() or job.stopped:
                raise llm.Cancelled()
            running = sum(w.thread is not None and w.finished is None for w in job.crew)
        left = job.budget.take_step(self.started, worker_steps=self.steps, running=running,
                                    priced=pricing.usd(self.model, {"input": 0}) is not None)
        if not self.budget_warned and left is not None and left <= agent.STEPS_LEFT_NOTICE:
            self.budget_warned = True
            note = self.spec.steps_left_note.format(left=agent.STEPS_LEFT_NOTICE).strip()
            self.session["messages"].append({"role": "user", context.STEP_LIMIT: True, "content": note})
            self.session["items"].append(state.item_notice(note))
        if self.t["first_request"] is None:
            self.t["first_request"] = self._now()
        self.steps += 1
        # Read after the first-reply wait, too: queued workers may have waited through a correction.
        with job.lock:
            if self.cancelled() or job.stopped:
                raise llm.Cancelled()
            notes = job.steers[self.steer_index:]
            self.steer_index += len(notes)
        for note in notes:
            text = note if isinstance(note, str) else note[0]  # Jobs started before receipts stored strings.
            item = state.item_user(text)
            self.session["items"].append(item)
            with self.turn.notes_lock:
                self.turn.notes.append((f"{workers.STEER_INSTRUCTION}\n\n{text}", item))
        try:
            with job.lock:
                # Only the older notes actually kept in this opening receive a first-request receipt.
                opening_notes = self.wave.opening_notes.get(self.id, []) if self.steps == 1 else []
                for item in opening_notes + [note[1] for note in notes if not isinstance(note, str)]:
                    _note_delivery(item, self, self.steps)
            if opening_notes or notes:
                agent._redraw()
        except Exception:  # Receipts must not interrupt note delivery, requests or crash recovery.
            pass

    def on_usage(self, usage: dict) -> None:
        self.row["steps"] = self.steps  # A reply has arrived; an outstanding request is not a step yet.
        if self.t["first_reply"] is None:
            self.t["first_reply"] = self._now()
            self.first_call_cached = int(usage.get("cached") or 0)
            if self.index == 0:
                self.wave.first_reply.set()
        parent = self.wave.session
        with _usage_lock:
            bucket = parent["usage_by_role"].setdefault("worker", {"input": 0, "cached": 0, "output": 0, "requests": 0})
            for key in ("input", "cached", "output"):
                value = int(usage.get(key) or 0)
                self.usage[key] += value
                bucket[key] += value
                parent["usage"][key] += value  # The sum the UI shows stays the sum.
            self.usage["requests"] += 1
            bucket["requests"] += 1
        self.wave.job.budget.add(self.model, usage)

    def after_step(self, session: dict, turn) -> bool:
        # The loop has already stored captures under stable conversation paths. Keep the latest
        # scene preview within this attempt, without a new capture or a second copy of the image.
        try:
            start = getattr(self, "attempt_item_start", 0)
            if start is not None:  # An unknown restart boundary must never reuse the discarded scene.
                items = session.get("items") or []
                preview = next((item["image_path"] for item in reversed(items[start:])
                                if isinstance(item, dict) and item.get("name") in {"capture_viewport", "run_python"}
                                and item.get("image_path")), None)
                if preview:
                    self.row["image_path"] = preview
        except Exception:  # Optional UI bookkeeping must not interrupt work or crash recovery.
            pass
        return self.report is not None or bool(self.crashed)

    def run_tool(self, name: str, arguments: str):
        if name not in self.allowed:
            return tools.ToolResult(f"{name} is not one of your tools.", ok=False)
        self.row["summary"] = agent._describe(llm.ToolCall("", name, arguments), self.spec.schemas)[0]
        agent._redraw()
        if name == "report_back":
            if self.report is None:
                self.report = workers.parse_report(agent._arguments(arguments))
            return tools.ToolResult("Report received. Your work is over.")
        if self.report is not None:
            return tools.ToolResult("Not run: you already reported back.", ok=False)
        if self.crashed:
            return tools.ToolResult("Not run: your Blender stopped.", ok=False)
        while not self.ready.wait(POLL):  # No tools before startup completes.
            if self.cancelled():
                raise llm.Cancelled()
        try:
            if self.boot_error:
                raise self.boot_error
            result = self.runtime.run_tool(name, arguments, self.cancelled)
        except llm.Cancelled:
            raise
        except Exception as ex:  # Its Blender died, hung or never came up: the crash path.
            if self.cancelled():
                raise llm.Cancelled()
            self._crashed(name, f"{ex}", getattr(ex, "kept", ""))
            return tools.ToolResult(f"Your Blender stopped during {name}: {ex}", ok=False)
        if getattr(result, "crashed", ""):  # worker_host reports a dead child as a result, not an exception.
            if self.cancelled():
                raise llm.Cancelled()
            self._crashed(name, result.text, getattr(result, "crash_kept", ""))
            return result
        if getattr(result, "rolled_back", None):
            self.rollbacks += 1
        advisory = getattr(result, "advisory", None) or {}
        for key in advisory.get("outside_role") or ():
            if key not in self.outside_role:
                self.outside_role.append(key)
        return result

    def _crashed(self, tool: str, error: str, kept: str = "") -> None:
        self.crashed, self.crash_error = tool, error[:200]
        self.crashes.append({"tool": tool, "error": error[:200], "kept": self._relative(kept)})

    def _relative(self, path: str) -> str:
        """A crash kit's folder from the job's (w1/chair/crash-1), which is also where an eval's copy of
        the job (<task>.workers/<job>/) has it."""
        if not path:
            return ""
        try:
            return Path(path).relative_to(self.wave.job.folder).as_posix()
        except ValueError:
            return str(path)

    def crash_note(self) -> str:
        """"crashed (crash report kept at …)": each time its Blender died and where that crash was kept;
        "" when it never crashed. In its status note, so in its report, the wave's row and job.json."""
        if not self.crashes:
            return ""
        times = {1: "crashed", 2: "crashed twice"}.get(len(self.crashes), f"crashed {len(self.crashes)} times")
        kept = [c["kept"] for c in self.crashes if c["kept"]]
        where = (f"crash report{'s' if len(kept) > 1 else ''} kept at {', '.join(kept)}" if kept
                 else "no crash report kept")
        return f"{times} in {', '.join(dict.fromkeys(c['tool'] for c in self.crashes))} ({where})"

    # -- the thread

    def _boot(self) -> None:
        """Startup belongs to the worker thread, outside job.lock. Model calls wait for it;
        cancellation must unwind it before the worker can finish and its wave can be reaped."""
        from .worker_host import WorkerUnavailable
        self.ready.clear()
        self.boot_error = None
        self.t["spawn"] = self.t["spawn"] if self.t["spawn"] is not None else self._now()
        try:
            if self.cancelled():
                raise llm.Cancelled()
            self.runtime = self.wave.make_runtime(self.wave, self)
            self.runtime.start()
            self.t["ready"] = self._now()
        except Exception as ex:
            self.boot_error = ex
            if not self.cancelled() and isinstance(ex, WorkerUnavailable):
                self.wave.session["workers_unavailable"] = True
                print(f"Loopcut: worker startup failed: {ex}")
        finally:
            self.ready.set()

    def start(self) -> None:
        self.started = self.wave.job.budget.clock()
        self.row["started_at"] = time.time()
        self.set_status("starting")
        self.thread = threading.Thread(target=self._work, name=f"loopcut-worker-{self.id}", daemon=True)
        self.thread.start()

    def never_started(self, note: str | None = None) -> None:
        """Stopped while queued: the budget before its turn came, its row's Cancel, or Stop."""
        if self.user_cancelled:
            self.set_status("cancelled", "stopped by the user")
        else:
            self.set_status("stopped", note)
        self.finished = time.monotonic()
        conversations.FOLDERS.pop(self.session["id"], None)
        if self.index == 0:
            self.wave.first_reply.set()  # The others go without waiting for it.

    def kill(self) -> None:
        if self.runtime is not None:
            try:
                self.runtime.kill()
            except Exception as ex:
                print(f"Loopcut: could not stop worker {self.id}: {ex!r}")

    def _work(self) -> None:
        from .worker_host import ChildCrashed, UNAVAILABLE_NOTE, WorkerUnavailable
        wave = self.wave
        try:
            for attempt in (0, 1):
                if self.cancelled():
                    raise llm.Cancelled()
                if wave.session.get("workers_unavailable"):
                    self.row["unavailable"] = True
                    self.set_status("failed", UNAVAILABLE_NOTE)
                    return
                self._boot()
                # Verify startup before spending a model request. Only environment failures suppress retries.
                if self.cancelled() or isinstance(self.boot_error, llm.Cancelled):
                    raise llm.Cancelled()
                if isinstance(self.boot_error, WorkerUnavailable):
                    self.row["unavailable"] = True
                    self.set_status("failed", UNAVAILABLE_NOTE)
                    return
                if isinstance(self.boot_error, ChildCrashed):
                    self._crashed("init", str(self.boot_error), self.boot_error.kept)
                elif self.boot_error:
                    raise self.boot_error
                else:
                    # A sibling's failed boot must not discard this child's successful startup/work.
                    try:
                        card = getattr(wave, "plan_card", None)
                        if isinstance(card, dict):
                            card["plan_started"] = True
                    except Exception:  # Plan recovery is UI bookkeeping, never a condition for work/retry.
                        pass
                    self.set_status("working")
                    agent.run_loop(self.session, self.turn, self.spec, self.run_tool, lambda session: None,
                                   getattr(wave, "alike", None))
                if not (self.crashed and attempt == 0 and not self.cancelled()):
                    break
                self.kill()
                self.attempt_item_start = None  # If bookkeeping fails, omit previews for the new attempt.
                try:
                    self.row.pop("image_path", None)
                    self.attempt_item_start = len(self.session.get("items") or [])
                except Exception:  # A preview must never prevent restarting the child.
                    pass
                self.retries += 1
                self.session["messages"].append({"role": "user", context.STEER: True,
                                                 "content": CRASH_NOTE.format(tool=self.crashed)})
                self.crashed = ""
                self.turn = self._new_turn(self.turn.cancel)
            self._end()
        except llm.Cancelled:
            self._stopped()
        except Exception as ex:  # Never lose a worker silently: it fails, the wave goes on.
            import traceback
            traceback.print_exc()
            self.set_status("failed", f"internal error: {ex!r}"[:160])
        finally:
            self.row["summary"] = self.summary()
            if self.status in ("starting", "working", "checking"):
                self.set_status("stopped" if self.cancelled() else "failed")
            self.kill()
            self.t["end"] = self._now()
            if self.index == 0:
                wave.first_reply.set()  # Worker 1 failed before replying: the others go anyway.
            conversations.FOLDERS.pop(self.session["id"], None)
            self.row["elapsed"] = max(0.0, self.wave.job.budget.clock() - self.started)
            self.finished = time.monotonic()

    def _end(self) -> None:
        """After the loop: the brief's checks and the result from the child, then the status, which
        comes from the checks and never from what the worker said."""
        if self.cancelled():
            self._stopped()
            return
        if self.crashed:
            self.set_status("failed", f"its Blender {self.crash_note()}: {self.crash_error}")
            return
        self.set_status("checking")
        from . import worker_host
        step = "check"
        try:
            results = list(self.runtime.check(self.cancelled) or [])
            self.checks = [r for r in results if not r.get("advisory")]
            self.advice = [r for r in results if r.get("advisory")]
            step = "finish"
            self.manifest = dict(self.runtime.finish(self.cancelled) or {}).get("manifest") or {}
        except llm.Cancelled:
            self._stopped()
            return
        except worker_host.ChildCrashed as ex:  # Its Blender died running the checks, measuring or rendering its cell.
            self._crashed(step, f"{ex}", ex.kept)
            self.set_status("failed", f"no result: its Blender {self.crash_note()}: {ex}")
            return
        except Exception as ex:
            self.set_status("failed", f"no result: {ex}"[:160])
            return
        finally:
            try:
                self.runtime.close()
            except Exception:
                pass
        passed, failing = _check_counts(self.checks)
        self.row["checks"] = f"{passed}/{passed + failing}" if passed + failing else ""
        if self.stopped_without_report:
            self.report = self._synthesised(passed, failing)
        error = next((i["text"] for i in reversed(self.session["items"]) if i["kind"] == "error"), "")
        note = {"budget": f"stopped: {self.turn.stop_reason}", "no_progress": f"stopped: {self.turn.stop_reason}",
                "error": f"stopped: {error}"[:160]}.get(self.turn.outcome, "")
        missing = _missing(self)
        notes = [n for n in (f"deliverable missing: no {', '.join(missing)} in its copy at report time" if missing else "",
                             note, f"its Blender {self.crash_note()}, restarted" if self.crashes else "") if n]
        note = "; ".join(notes)
        if self.report and self.report.get("question"):
            self.set_status("question", note)
        elif self.turn.outcome in ("budget", "no_progress") or failing or missing:  # Not done: a budget/guard stopped it,
            self.set_status("partial", note)  # a check fails, or what it was to build is not there, whatever it said.
        else:
            self.set_status("done", note)

    @property
    def stopped_without_report(self) -> bool:
        """Kept as cap_hit in reports/JobLog for existing readers; now budgets and the guard stop workers."""
        return (self.turn.outcome in ("budget", "no_progress")
                and (self.report is None or bool(self.report.get("synthesised"))))

    def _synthesised(self, passed: int, failing: int) -> dict:
        """A worker stopped by the budget or guard before report_back: its report made by
        Loopcut from what was measured (its checks here, its facts in the manifest, shown with every
        report) and its own last words, so the orchestrator reads a report, not a bare partial."""
        checks = f"checks {passed} of {passed + failing} pass in its copy" if passed + failing else "no checks"
        last = self.summary()
        summary = f"Loopcut's report: stopped before report_back ({self.turn.stop_reason}); {checks}" + (
            f". Its last words: {last}" if last else ".")
        return {"summary": workers.clip(summary, workers.MAX_SUMMARY + 80), "numbers": {}, "change_requests": [],
                "question": None, "synthesised": True}

    def summary(self) -> str:
        if self.report:
            return self.report["summary"]
        text = next((m.get("content") or "" for m in reversed(self.session["messages"])
                     if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip()), "")
        return workers.parse_report({"summary": text})["summary"]


def attach_references(session: dict, parent: dict, names: list, earlier: list) -> list[str]:
    """The user's images a brief names (its references), in the worker's own conversation folder: the
    worker session's references become those an earlier brief of its thread sent (earlier), pinned
    only if this brief names them too (unpinned ones are no longer sent), plus this brief's new ones.
    Returns the new ones' refs, for the opening to carry. parent: the user's session; each image is
    its SEND_SIDE copy (attachments), 768 px on its long side."""
    by_name = {r["name"]: r for r in parent.get("references") or ()}
    references = [dict(r, pinned=False) for r in earlier]
    new = []
    for name in names:
        source = by_name.get(name)
        if source is None:
            continue  # validate_wave refuses an unknown name; one gone since is left out.
        try:
            ref = conversations.store_image(session, conversations.image_path(parent["id"], source["ref"]))
        except (OSError, conversations.ConversationError) as ex:
            print(f"Loopcut: could not give a worker the reference {name}: {ex}")
            continue
        known = next((r for r in references if r["ref"] == ref), None)
        if known is not None:
            known["pinned"] = True  # Sent with an earlier brief and still in its conversation.
        else:
            references.append({"ref": ref, "full": ref, "name": name, "pinned": True})
            new.append(ref)
    session["references"] = references
    return new


def opening_message(text: str, refs: list[str]) -> dict:
    """A worker's first message of a brief: its text, and the reference images it has not been sent,
    as the user's own attachments are (context.kept_messages keeps them while pinned). Its reference
    card is the brief's style, so no card is asked for (agent._reference_card)."""
    if not refs:
        return {"role": "user", "content": text}
    return {"role": "user", context.ATTACHED: True, context.REFERENCE_CARD: True,
            "content": [{"type": "text", "text": text}, *({"type": "image_url", "image_url": {"url": r}} for r in refs)]}


def reference_tile(session: dict, names: list) -> dict | None:
    """The reference a worker's looks show beside the render (worker_capture): the first its brief
    names, as {path, name}; None without one."""
    pinned = {r["name"]: r for r in session.get("references") or () if r.get("pinned")}
    for name in names:
        if name in pinned:
            try:
                return {"path": str(conversations.image_path(session["id"], pinned[name]["ref"])), "name": name}
            except conversations.ConversationError:
                return None
    return None


def _copy_images(source: Path, folder: Path) -> None:
    """An earlier thread's captures, so its image references still resolve in the new folder; a
    thread whose folder was reaped just loses them ("[image no longer available]")."""
    try:
        if (source / "images").is_dir():
            shutil.copytree(source / "images", folder / "images", dirs_exist_ok=True)
    except OSError as ex:
        print(f"Loopcut: could not carry a worker's images over: {ex}")


def _created_names(worker: "Worker", merge_report=None) -> list[str]:
    """Plain names of what the worker made, as they are called live when its result merged, else as
    its child listed them; an object's own data left out (owning the object covers it)."""
    merged = [e for e in _field(merge_report, "created") if isinstance(e, dict) and _belongs(e, worker.id)]
    if merged:
        pairs = [(e.get("id_type") or "", str(e.get("name"))) for e in merged]
    else:
        keys = [e.get("key", "") if isinstance(e, dict) else str(e) for e in (worker.manifest or {}).get("created") or []]
        pairs = [tuple(key.split(":", 1)) if ":" in key else ("", key) for key in keys]
    return list(dict.fromkeys(name for kind, name in pairs if kind not in _IMPLIED and name))


def remember(wave: Wave, crew: list, merge_report=None) -> None:
    """Each worker's thread, so a later brief can go back to it (orchestrator: continues): its
    conversation, where its captures are, and what it made. Kept in memory for the session; a worker
    the user cancelled, or one that never ran, leaves none."""
    threads = wave.session.setdefault("worker_threads", {})
    for worker in crew:
        if worker.user_cancelled or not worker.steps:
            continue
        continued = worker.brief.get("continues")
        if continued and continued != worker.id:
            threads.pop(continued, None)  # One thread per deliverable: the newest conversation.
        threads[worker.id] = {"deliverable": worker.brief["deliverable"], "role": worker.brief["role"],
                              "prefix": worker.brief["prefix"], "wave": wave.number, "status": worker.status,
                              "merged": bool(worker.row.get("merged")),  # What it built is in the live scene.
                              "folder": str(worker.folder), "messages": worker.session["messages"],
                              "references": worker.session.get("references") or [],
                              "created": _created_names(worker, merge_report)}


def cancel_worker(wave: str, worker: str) -> bool:
    """A row's Cancel: that worker stops and is not merged; the rest carry on."""
    found = _running.get((wave, worker))
    if found is None or found.reported or found.row.get("merged"):
        return False
    found.user_cancelled = True
    found.row["cancelled_before_merge"] = True
    found.turn.cancel.set()
    found._stopped()
    found.kill()
    return True


# ------------------------------------------------------------------ the report

_IMPLIED = {"MESH", "CURVE", "FONT", "LIGHT", "CAMERA"}  # An object's own data: the object says it.


def _created(worker: "Worker", merge_report=None) -> list[str]:
    """What the worker made, objects first: as merged (the live names, after a genuine collision's
    .001) when its result merged, else as its child listed them."""
    merged = [e for e in _field(merge_report, "created") if isinstance(e, dict) and _belongs(e, worker.id)]
    if merged:
        pairs = [(e.get("id_type") or "", str(e.get("name"))) for e in merged]
    else:
        keys = [e.get("key", "") if isinstance(e, dict) else str(e) for e in (worker.manifest or {}).get("created") or []]
        pairs = [tuple(key.split(":", 1)) if ":" in key else ("", key) for key in keys]
    objects = [name for kind, name in pairs if kind in ("", "OBJECT")]
    others = [f"{kind[:3]} {name}" for kind, name in pairs if kind not in ("", "OBJECT") and kind not in _IMPLIED
              and kind not in ownership.SUPPORT]  # Text blocks and images: their own line (_support).
    return objects + others  # render_worker says as many whole names as fit.


def _support(worker: "Worker", merge_report=None) -> list[dict]:
    """Its text blocks and images: as the merge checked them live (merge._support), else, with no
    merge, as its child listed them (ownership.support_entry), new and updated ones as "unmerged":
    in its copy only."""
    if merge_report is not None:
        return [e for e in _field(merge_report, "support") if _belongs(e, worker.id)]
    found = (worker.manifest or {}).get("support")
    return [{**e, "worker": worker.id, **({"state": "unmerged"} if e.get("state") in ("new", "updated") else {})}
            for e in found if isinstance(e, dict)] if isinstance(found, list) else []


def support_text(entry: dict) -> str:
    """One text block or image for the orchestrator: "text ArcaneRecipe new #1a2b3c (212 lines)",
    "image Mask not merged: unused and never painted", "text Notes MISSING: not in the scene after the
    merge"."""
    if "more" in entry:
        return f"+{entry['more']} more"
    if "error" in entry:
        return f"not listed ({entry['error']})"
    kind, _, name = str(entry.get("key") or "").partition(":")
    kind, name = {"TEXT": "text", "IMAGE": "image"}.get(kind, kind.lower()), entry.get("name") or name
    state, why = entry.get("state") or "", entry.get("why") or ""
    if state == "skipped":
        return f"{kind} {name} not merged: {why}"
    if state in ("missing", "held"):
        return f"{kind} {name} {'MISSING' if state == 'missing' else 'held back'}: {why}"
    digest = f" #{entry['hash'][:6]}" if entry.get("hash") else ""
    about = ", ".join(filter(None, [entry.get("about"), why]))
    state = "in its copy only, not merged" if state == "unmerged" else state
    return f"{kind} {name} {state}{digest}" + (f" ({about})" if about else "")


def _check_counts(results: list) -> tuple[int, int]:
    """(passed, failing) of a worker's checks in its copy; deferred ones (another worker's objects,
    measured on the live scene after the merge) are neither."""
    passed = sum(1 for r in results if r.get("status") == "pass")
    return passed, sum(1 for r in results if r.get("status") not in ("pass", "deferred"))


def _unfinished(worker: Worker) -> list[str]:
    """What a worker stopped by a budget or guard left open, by its checks and brief: the checks that fail
    in its copy, and the requirements no check quotes (nothing measured says they are done)."""
    failing = [c["id"] for c, r in _paired(worker) if r.get("status") not in ("pass", "deferred")]
    items = [f"{', '.join(failing)} fail{'s' if len(failing) == 1 else ''}"] if failing else []
    quotes = [acceptance.normalise(c.get("quote", "")) for c in worker.brief["checks"]]
    for requirement in worker.brief["requirements"]:
        said = acceptance.normalise(requirement.get("quote", ""))
        if said and not any(q and (q in said or said in q) for q in quotes):
            items.append(f"unchecked: {requirement.get('do')}")
    return items


def _numbers(worker: Worker) -> dict:
    """The numbers the worker wrote in report_back: its claim, shown apart from what was measured."""
    return (worker.report or {}).get("numbers") or {}


def _facts(worker: Worker) -> dict:
    """What its deliverables measured in its Blender at report time (facts.deliverables)."""
    found = (worker.manifest or {}).get("facts")
    return found if isinstance(found, dict) and "error" not in found else {}


def _missing(worker: Worker) -> list[str]:
    """Its deliverable, when its Blender found no object of that name at report time (worker_child)."""
    found = (worker.manifest or {}).get("missing")
    return [str(name) for name in found] if isinstance(found, list) else []


_TRI_KEYS = {"tris", "triangles", "tri_count", "triangle_count", "evaluated_tris"}


def _claims(worker: Worker) -> str:
    """The worker's own words for facts.flags: its summary, and the triangle counts in its numbers."""
    counts = []

    def walk(value):
        for key, item in (value.items() if isinstance(value, dict) else ()):
            if str(key).lower() in _TRI_KEYS and isinstance(item, int) and not isinstance(item, bool):
                counts.append(f"{item} tris")
            walk(item)
    walk(_numbers(worker))
    return " ".join([worker.summary(), *counts])


def _flags(worker: Worker) -> list[str]:
    """Where its words contradict its measured facts (facts.flags), for the palette's materials and the
    ones its checks name."""
    named = list((worker.wave.plan.get("shared") or {}).get("palette") or ())
    named += [c.get("material") for c in [*worker.brief["checks"], *(worker.brief.get("advisory") or ())]
              if isinstance(c, dict) and c.get("kind") == "material_on"]
    return facts.flags(_claims(worker), _facts(worker), named, _missing(worker))


def report_of(worker: Worker, merge_report=None) -> dict:
    """The structured report (design §2.8)."""
    fail_lines = [f"{c['id']} {_title(c)}: {r.get('detail') or r.get('status')}" for c, r in _paired(worker)
                  if r.get("status") not in ("pass", "deferred")]
    passed, failing = _check_counts(worker.checks)
    quarantined = [q for q in _field(merge_report, "quarantined") if _belongs(q, worker.id)]
    admitted = [a for a in _field(merge_report, "admitted") if _belongs(a, worker.id)]
    report = worker.report or {}
    return {"worker": worker.id, "deliverable": worker.brief["deliverable"], "role": worker.brief["role"],
            "status": worker.status,
            "outcome": state.worker_outcome(worker.row), "merged": bool(worker.row.get("merged")),
            "merged_changes": list(worker.row.get("merged_changes") or []),
            "checks": {"passed": passed, "failed": failing, "fail_lines": fail_lines,
                       "deferred": [c["id"] for c, r in _paired(worker) if r.get("status") == "deferred"]},
            "created": _created(worker, merge_report),
            # What the merge actually decided, not the child's own advisory guess (worker.outside_role):
            # held back, with its reason; let through outside the role, with the brief's words that admitted it.
            "outside_role": [f"{_short(q)} → quarantined: {_reason(q, 'outside its role')}" for q in quarantined]
                           + [f"{_short(a)} → admitted: {_reason(a, 'the brief asked for it')}" for a in admitted],
            "facts": _facts(worker), "flags": _flags(worker), "numbers": _numbers(worker), "summary": worker.summary(),
            "change_requests": report.get("change_requests") or [], "question": report.get("question"),
            "synthesised": bool(report.get("synthesised")),
            # Advisory: the brief's own targets, measured; a miss is for the orchestrator, never a failure.
            "advisory": [f"{r.get('id')} {r.get('title') or r.get('kind')}: {r.get('detail') or r.get('status')}"
                         for r in worker.advice if r.get("status") not in ("pass", "deferred")],
            "steps": worker.steps, "cap_hit": worker.stopped_without_report,
            "unfinished": _unfinished(worker) if worker.stopped_without_report else [],
            "rollbacks": worker.rollbacks, "note": worker.note, "support": _support(worker, merge_report),
            "merge": {key: [e for e in _field(merge_report, key) if _belongs(e, worker.id)]
                      for key in ("replaced", "conflicts", "dropped_edits")}}


def _paired(worker: Worker) -> list[tuple[dict, dict]]:
    """The brief's checks with the child's results, by id (by position for results without one)."""
    by_id = {r.get("id"): r for r in worker.checks if r.get("id")}
    return [(check, by_id.get(check["id"]) or (worker.checks[i] if i < len(worker.checks) else {"status": "unavailable"}))
            for i, check in enumerate(worker.brief["checks"])]


def _title(check: dict) -> str:
    from . import orchestrator
    return orchestrator.check_title(check)


def _field(report, key: str) -> list:
    value = report.get(key) if isinstance(report, dict) else getattr(report, key, None)
    return list(value or [])


def _belongs(entry, worker: str) -> bool:
    """Whether a merge report entry is this worker's: entries that do not say belong to none."""
    if isinstance(entry, dict):
        return entry.get("worker") == worker
    return f"/{worker}" in str(entry) or str(entry).startswith(f"{worker}:")


def _short(entry) -> str:
    return str(entry.get("name") or entry.get("key") or entry.get("pointer")
               or ", ".join(entry.get("keys") or [])) if isinstance(entry, dict) else str(entry)


def _reason(entry, default: str) -> str:
    return (entry.get("reason") or default) if isinstance(entry, dict) else default


_clip = workers.clip  # Between items, never inside a value.


def render_worker(report: dict) -> str:
    """One worker's block for the orchestrator, at most REPORT_TOKENS: lines in order of what the
    orchestrator acts on, and a line that would not fit is left out."""
    checks = report["checks"]
    total = checks["passed"] + checks["failed"]
    head = (f"[{report['worker']}] {report['deliverable']} · {report['role']} · {report['status']} · "
            f"{report['steps']} step{'s' if report['steps'] != 1 else ''}"
            + (f" · checks {checks['passed']}/{total}" if total else "" if checks.get("deferred") else " · no checks")
            + (f" · {', '.join(checks['deferred'])} deferred to the live run" if checks.get("deferred") else "")
            + (f" · {report['note']}" if report.get("note") else ""))
    lines = [f"  fail {_clip(line, 130)}" for line in checks["fail_lines"][:MAX_FAIL_LINES]]
    if len(checks["fail_lines"]) > MAX_FAIL_LINES:
        lines.append(f"  +{len(checks['fail_lines']) - MAX_FAIL_LINES} more failing")
    if report["cap_hit"]:
        open_ = report.get("unfinished") or []
        lines.append("  open when stopped (no report_back): " + (
            workers.clip_list(open_, 160, "; ") if open_ else "nothing its checks see, but it did not say it was done"))
    if report["rollbacks"]:
        lines.append(f"  Rolled back: {report['rollbacks']} call(s) that touched what it does not own")
    if report["question"]:
        lines.append(f"  question: {_clip(report['question'], 160)}")
    for request in report["change_requests"]:
        lines.append("  asks: " + _clip(f"{request['target']}: {request['change']}"
                                        + (f" ({request['reason']})" if request.get("reason") else ""), 120))
    for name, fact in (report.get("facts") or {}).items():
        lines.append(f"  measured {_clip(facts.text(name, fact), 260)}")
        uv = facts.uv_text(name, fact.get("uv")) if isinstance(fact, dict) else ""
        if uv:
            lines.append(f"  measured {_clip(uv, 200)}")
        paint = paint_stats.text_of(name, fact.get("paint")) if isinstance(fact, dict) else ""
        if paint:
            lines.append(f"  measured {_clip(paint, 260)}")
    if report.get("flags"):
        lines.append("  flags (its words vs measured): " + workers.clip_list(report["flags"], 300, "; "))
    if report.get("advisory"):
        lines.append("  advisory misses: " + workers.clip_list([_clip(a, 110) for a in report["advisory"]], 300, "; "))
    if report["summary"]:
        lines.append(f"  {'report' if report.get('synthesised') else 'said'}: {report['summary']}")
    if report["outside_role"]:
        lines.append("  outside its role: " + workers.clip_list(report["outside_role"], 120))
    for key in ("conflicts", "dropped_edits"):
        if report["merge"][key]:
            lines.append(f"  merge {key.replace('_', ' ')}: " + workers.clip_list(list(map(_short, report["merge"][key])), 120))
    if report.get("support"):
        lines.append("  texts and images: " + workers.clip_list([support_text(e) for e in report["support"]], 260, "; "))
    if report["created"]:
        lines.append("  created: " + workers.clip_list(report["created"], 140))
    if report["numbers"]:
        lines.append("  its own numbers (unmeasured): " + workers.clip_items(report["numbers"], 140))
    budget = int(REPORT_TOKENS * context.CHARS_PER_TOKEN)
    block = [_clip(head, 200)]
    if report.get("outcome"):
        block.append("  outcome: " + report["outcome"])
    if report.get("merged"):
        block.append("  merged: " + workers.clip_list(report.get("merged_changes") or ["no recorded datablock changes"], 240))
    left = budget - len("\n".join(block))
    for line in lines:
        if len(line) + 1 <= left:
            block.append(line)
            left -= len(line) + 1
    return "\n".join(block)


def _merge_line(merge_report, error: str, wave: Wave, *, unchanged: bool = False) -> tuple[str, list[str]]:
    if error:
        if unchanged:
            return f"Merge: not merged ({error}). The live scene is as before this merge.", []
        return f"Merge: merge failed; what reached the scene is unconfirmed ({error}).", []
    if merge_report is None:
        return "Merge: nothing to merge.", []
    created, quarantined = _field(merge_report, "created"), _field(merge_report, "quarantined")
    parts = [merge.created_text(created), f"{len(_field(merge_report, 'replaced'))} replaced"]
    for key in ("renamed", "conflicts", "dropped_edits", "errors"):
        if _field(merge_report, key):
            parts.append(f"{len(_field(merge_report, key))} {key.replace('_', ' ')}")
    line = "Merge: " + ", ".join(parts) + "."
    reused = _field(merge_report, "reused")
    if reused:
        line += (" Made again by a worker, so the live one is used instead of a copy: "
                 + ", ".join(f"{_short(r)} ({r.get('worker')})" for r in reused[:6]) + ".")
    if quarantined:
        line += (f" Kept aside (outside a worker's role): {', '.join(map(_short, quarantined[:6]))}; "
                 f"accept_quarantine wave \"{wave.id}\" only if the user wants them.")
        held = [f"{_short(e)} ({', '.join(e['held'])})" for key in ("created", "replaced")
                for e in _field(merge_report, key) if isinstance(e, dict) and e.get("held")]
        if held:
            line += f" Merged objects that hang on them: {', '.join(held[:4])}."
    return line, card_lines(merge_report)


# The merge card's line prefixes (ui/layout.py draws them): + created, ~ replaced or renamed, - an edit
# dropped, ! kept aside or a conflict or an error.
_CARD = (("created", "+ ", ""), ("replaced", "~ ", ""), ("renamed", "~ ", " (renamed)"),
         ("reused", "~ ", " (made again: the live one used)"),
         ("dropped_edits", "- ", " (edit not kept)"), ("quarantined", "! ", " (kept aside)"),
         ("conflicts", "! ", " (conflict)"), ("errors", "! ", ""))
MAX_CARD_LINES = 12


def _card_entries(merge_report, key: str) -> list[str]:
    """A report field's entries as the card names them: an object by its name, other datablocks with
    their type ("Armchair_Fabric (material)"); a created object's own data, which by Blender's habit
    shares its name, goes without saying; an object that hangs on something kept aside says so
    ("Lit_Spot (parent Lit_Spot_root held back)")."""
    entries = _field(merge_report, key)
    objects = {(e.get("worker"), e.get("name")) for e in entries if isinstance(e, dict) and e.get("id_type") == "OBJECT"}
    names = []
    for entry in entries:
        kind = entry.get("id_type") if isinstance(entry, dict) else None
        held = f" ({', '.join(entry['held'])} held back)" if isinstance(entry, dict) and entry.get("held") else ""
        if kind and kind != "OBJECT":
            if key == "created" and (entry.get("worker"), entry.get("name")) in objects:
                continue
            names.append(f"{_short(entry)} ({kind.lower()})")
        else:
            names.append(_short(entry) + held)
    return names


def card_lines(merge_report) -> list[str]:
    lines = [f"{mark}{name}{suffix}" for key, mark, suffix in _CARD for name in _card_entries(merge_report, key)]
    return lines[:MAX_CARD_LINES] + ([f"  +{len(lines) - MAX_CARD_LINES} more"] if len(lines) > MAX_CARD_LINES else [])


SHEET_SIDE = 1024   # The contact sheet's long side at most: a worker's own look's (worker_capture.WIDTH).
SHEET_ASPECT = 0.75  # A cell's height to its width: a named angle's (worker_capture.ASPECT).


def sheet_layout(count: int) -> tuple[int, tuple[int, int]]:
    """(columns, (cell width, cell height)) for a contact sheet of count cells: one row up to three,
    two rows of two or three past that, cells at worker_capture.CELL or narrower so the sheet stays
    within SHEET_SIDE."""
    from .asset_common import SHEET_GAP
    from .worker_capture import CELL
    columns = min(count, 2 if count == 4 else 3)
    width = min(CELL, (SHEET_SIDE - (columns - 1) * SHEET_GAP) // max(1, columns))
    return columns, (width, round(width * SHEET_ASPECT))


def contact_sheet(folder: Path, crew: list, on_main, name: str = "sheet.png") -> tuple[Path | None, str]:
    """(image, its line) for a delegate result: one picture of the deliverables it reports, a cell per
    worker that finished with one (worker_capture.cell: its deliverable in colour, its id in the
    corner), in the crew's order, instead of an image per worker for the orchestrator to open.
    (None, "") when no worker left a cell or the sheet could not be made."""
    cells = [(w.id, (w.manifest or {}).get("look")) for w in crew]
    cells = [(worker, path) for worker, path in cells if path and Path(path).is_file()]
    if not cells:
        return None, ""
    # The user's reference a brief showed its worker, as one more cell: the orchestrator compares them at a glance.
    reference = next((w.reference for w in crew if getattr(w, "reference", None)), None)

    def make():
        from . import asset_common, worker_capture
        paths = [p for _, p in cells]
        columns, cell = sheet_layout(len(cells) + (reference is not None))
        extra = reference and worker_capture.reference_cell(Path(reference["path"]), cell, Path(folder) / f"ref-{name}")
        if not extra:
            columns, cell = sheet_layout(len(cells))
        return asset_common.sheet(paths + ([str(extra)] if extra else []), name, cell, columns, folder), bool(extra)
    try:
        image, shown = on_main(make)
    except llm.Cancelled:
        raise
    except Exception as ex:  # A missing picture never costs the report.
        print(f"Loopcut: no contact sheet in {folder}: {ex!r}")
        return None, ""
    if image is None:
        return None, ""
    return image, (f"Image attached: the contact sheet of these deliverables, each as it finished, in the "
                   f"scene's own light (a studio light if it has none), labelled {', '.join(w for w, _ in cells)}"
                   + (f"; last, labelled reference, the user's {reference['name']}." if shown else "."))


def unavailable_note(session: dict) -> str:
    """Keep collection and ownership visible until every existing worker has been reported."""
    from .worker_host import UNAVAILABLE_NOTE
    job = getattr(session.get("turn"), "job", None)
    return _still_running(_busy(job), job) if job is not None else UNAVAILABLE_NOTE + " Continue in the main Blender."


def resolve_quarantine(session: dict, wave: str, entries: list[dict]) -> None:
    """Reconcile confirmed scene changes with saved rows (also the live workers' rows)."""
    cards = [item for item in session.get("items", [])
             if item.get("kind") == "wave" and item.get("wave") == wave]
    changed_jobs = {}
    for card in cards:
        for row in card.get("rows", []):
            resolved = [entry for entry in entries if _belongs(entry, row["id"])
                        and _short(entry) in row.get("quarantined", [])]
            if not resolved:
                continue
            names = {_short(entry) for entry in resolved}
            row["quarantined"] = [name for name in row["quarantined"] if name not in names]
            row["merged"] = True
            row["merge_unchanged"] = False
            row["merged_changes"] = list(dict.fromkeys([
                *row.get("merged_changes", []), *card_lines({"created": resolved})]))
            # Job logs and continuation metadata must agree with the panel and later waits.
            for job in session.get("jobs", []):
                if job.get("job") != card.get("job"):
                    continue
                for logged_wave in job.get("waves", []):
                    if logged_wave.get("n") == card.get("number"):
                        for worker in logged_wave.get("workers", []):
                            if worker.get("id") == row["id"]:
                                worker.update(delivery=state.worker_outcome(row), merged=bool(row.get("merged")),
                                              merged_changes=list(row.get("merged_changes", [])))
                                changed_jobs[job["job"]] = job
            thread = session.get("worker_threads", {}).get(row["id"])
            if thread and thread.get("wave") == card.get("number"):
                thread["merged"] = bool(row.get("merged"))
                thread["created"] = list(dict.fromkeys([*thread.get("created", []), *map(_short, resolved)]))
    names = {_short(entry) for entry in entries}
    for item in session.get("items", []):
        if item.get("kind") != "merge" or item.get("wave") != wave:
            continue
        held = item.get("quarantined", [])
        changed = [name for name in held if name in names]
        if not changed:
            continue
        remaining = [name for name in held if name not in names]
        item["accepted"] = list(dict.fromkeys([*item.get("accepted", []), *changed]))
        item["quarantined"] = remaining
        if not remaining:
            item["resolved"] = "accepted"
    for job in changed_jobs.values():
        if job.get("folder"):
            _write_job_log(Path(job["folder"]), job)


def accept_quarantine(session: dict, wave: str, names: list[str] | None = None) -> str:
    """Main thread: acceptance and its delivery receipts are one operation for both UI and tool."""
    resolved = []
    text = merge.accept_quarantine(wave, names, resolved=resolved)
    resolve_quarantine(session, wave, resolved)
    return text


def wait_result(session: dict, job: Job | None = None, lead: str = ""):
    """Repeat delivery receipts even after the turn's Job is gone. Wave rows survive save/reload.
    Use the latest job's cards, including earlier waves of that job, never a worker's promises."""
    cards = [w.item for w in job.waves] if job is not None else []
    if not cards:
        saved = [i for i in session.get("items", []) if i.get("kind") == "wave"]
        if saved:
            def job_key(card):
                return card.get("job") or str(card.get("wave", "")).rsplit("-w", 1)[0]
            cards = [i for i in saved if job_key(i) == job_key(saved[-1])]
    lines = [lead] if lead else []
    rows = [row for card in cards for row in card.get("rows", [])]
    if not rows:
        lines.append("Nothing to wait for: no worker results are available.")
    else:
        complete = all(state.worker_complete(row) for row in rows)
        lines.append("Nothing to wait for: every worker has reported." if complete
                     else "Nothing to wait for. Worker outcomes:")
        for card in cards:
            for row in card.get("rows", []):
                outcome = state.worker_outcome(row) or "unavailable (no merge confirmed)"
                lines.append(f"[{row['id']}] {row.get('deliverable', row['id'])} "
                             f"(wave {card['wave']}): {outcome}.")
                if row.get("merged"):
                    changes = row.get("merged_changes")
                    lines.append("  merged: " + ("; ".join(changes) if changes else
                                 "no recorded datablock changes" if changes == [] else "details unavailable in this saved result"))
                elif row.get("note"):
                    lines.append("  " + row["note"])
    return tools.ToolResult("\n".join(lines), ok=not any(
        row.get(key) for row in rows for key in ("merge_error", "quarantined", "conflicts")))


def _still_running(busy: list, job: Job | None = None) -> str:
    """Every worker of the turn as done (reported), running or queued, with why the queued ones wait,
    and what to do meanwhile."""
    unavailable = job is not None and job.session.get("workers_unavailable")
    if not busy and not unavailable and (job is None or all(
            state.worker_complete(w.row) for w in job.crew)):
        return "Every worker has reported."
    running = [w for w in busy if _state(w) == "running"]
    queued = [w for w in busy if _state(w) == "queued"]
    done = [w.id for w in job.crew if w.reported and state.worker_complete(w.row)] if job else []
    lines = [f"Done: {', '.join(done)}."] if done else []
    if job is not None and any(w.reported and not state.worker_complete(w.row) for w in job.crew):
        lines = []
        for worker in job.crew:
            if worker.reported:
                lines.append(f"[{worker.id}] wave {worker.wave.number}: {state.worker_outcome(worker.row)}.")
                if worker.row.get("merged_changes"):
                    lines.append("Merged: " + "; ".join(worker.row["merged_changes"]) + ".")
    if running:
        lines.append("Still running: " + ", ".join(f"{w.id} ({w.steps} step{'s' if w.steps != 1 else ''}, wave "
                                                   f"{w.wave.number})" for w in running) + ".")
    if queued:
        names = ', '.join(f'{w.id} (wave {w.wave.number})' for w in queued)
        if unavailable:
            lines.append(f"Queued: {names}; these workers will be stopped without starting.")
        else:
            slots = job.max_workers if job is not None else len(running)
            lines.append(f"Queued: {names}, waiting for a free slot ({slots} run at once); each starts, "
                         "in that order, as a running worker finishes.")
    if unavailable:
        from .worker_host import UNAVAILABLE_NOTE
        ready = _ready(job)
        if ready:
            lines.append("Ready to collect: " + ", ".join(w.id for w in ready) + ".")
        if busy or ready:
            return " ".join([UNAVAILABLE_NOTE, *lines, "Use delegate wait true to collect the remaining results "
                             "before editing their objects in the main Blender."])
        return " ".join([UNAVAILABLE_NOTE, *lines, "No workers are running; continue in the main Blender."])
    if not busy:
        return " ".join([*lines, "No workers are running."])
    return " ".join(lines + ["Review the results above now and send fixes back with continues; delegate wait "
                             "true returns the next results."])


def render_report(job: Job, sections: list[list[str]], live: list, held: list, busy: list, sheet_line: str = "",
                  lead: str = "") -> str:
    """What delegate returns: what it started (lead), a section per wave (its workers' blocks and its
    merge), the live checks and those waiting for a worker still running, who is still running, the
    budget, then the contact sheet's line."""
    lines = ([lead] if lead else []) + ["Status comes from each brief's checks, run in the worker's copy."]
    for section in sections:
        lines += section
    if live:
        passed = sum(1 for _, _, r in live if r.get("status") == "pass")
        lines.append(f"Live checks after the merge: {passed} of {len(live)} pass.")
        from . import orchestrator
        lines += [f"  fail {_clip(orchestrator.result_line(*row), 180)}" for row in live if row[2].get("status") != "pass"]
    if held:
        waiting = sorted({w for _, _, ids in held for w in ids})
        lines.append(f"Checks that wait for {', '.join(waiting)} to merge: {', '.join(label for label, _, _ in held)}.")
    lines.append(_still_running(busy, job))
    budget = job.budget
    lines.append(f"Job budget used: {budget.steps} worker step{'s' if budget.steps != 1 else ''}"
                 + (f", ${budget.usd:.3f}/${budget.max_usd:.2f}" if budget.max_usd else "") + ".")
    return "\n".join(lines + ([sheet_line] if sheet_line else []))


def _dedupe_requests(job: Job, worker: Worker) -> None:
    from . import orchestrator
    if not worker.report:
        return
    kept = []
    for request in worker.report["change_requests"]:
        key = (request["target"], orchestrator.normalise(request["change"]))
        if key not in job.requests:
            job.requests.add(key)
            kept.append(request)
    worker.report["change_requests"] = kept


def _log_worker(worker: Worker, tokens: dict) -> dict:
    return {"id": worker.id, "role": worker.brief["role"], "status": worker.status, "steps": worker.steps,
            "delivery": state.worker_outcome(worker.row), "merged": bool(worker.row.get("merged")),
            "merged_changes": list(worker.row.get("merged_changes") or []),
            "cap_hit": worker.stopped_without_report, "outcome": worker.turn.outcome,
            "unfinished": _unfinished(worker) if worker.stopped_without_report else [],
            "retries": worker.retries, "rollbacks": worker.rollbacks,
            "checks": dict(zip(("passed", "failed"), _check_counts(worker.checks)),
                           deferred=sum(1 for r in worker.checks if r.get("status") == "deferred")),
            "change_requests": (worker.report or {}).get("change_requests") or [],
            "cr_targets_token_at_report": tokens, "model": worker.model, "usage": dict(worker.usage),
            "usd": pricing.usd(worker.model, worker.usage),
            "peak_rss_mb": getattr(worker.runtime, "peak_rss_mb", None), "t": dict(worker.t),
            "note": worker.note, "crashes": list(worker.crashes), "missing": _missing(worker),
            "first_call_cached": worker.first_call_cached, "facts": _facts(worker), "flags": _flags(worker),
            "advisory": {"met": sum(1 for r in worker.advice if r.get("status") == "pass"),
                         "missed": sum(1 for r in worker.advice if r.get("status") not in ("pass", "deferred"))}}


def _log_merge(merge_report, error: str) -> dict:
    if error or merge_report is None:
        return {"created": 0, "replaced": [], "quarantined": [], "conflicts": [], "dropped_edits": [], "seconds": 0.0,
                **({"error": error} if error else {})}
    seconds = merge_report.get("seconds") if isinstance(merge_report, dict) else getattr(merge_report, "seconds", 0.0)
    return {"created": len(_field(merge_report, "created")),
            "objects": sum(1 for e in _field(merge_report, "created")
                           if (e.get("id_type") if isinstance(e, dict) else str(e).split(":", 1)[0]) == "OBJECT"),
            **{key: [_short(e) for e in _field(merge_report, key)] for key in ("replaced", "quarantined", "conflicts",
                                                                            "dropped_edits")},
            "seconds": seconds}


def _merges_total(merges: list[dict]) -> dict:
    """A wave's merge calls (one per report that merged some of its workers) as one entry, the shape
    _log_merge gives one: counts added, lists joined, an error kept."""
    total = _log_merge(None, "")
    for entry in merges:
        for key, value in entry.items():
            if key == "workers":
                continue
            if isinstance(value, list):
                total[key] = list(total.get(key) or []) + value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] = round((total.get(key) or 0) + value, 3)
            else:
                total[key] = value
    return total


# ------------------------------------------------------------------ running: start, collect, report

def _defaults(wave: Wave) -> None:
    wave.make_runtime = wave.make_runtime or _HostRuntime
    wave.merge = wave.merge or _merge
    wave.run_checks = wave.run_checks or _run_checks
    wave.snapshot = wave.snapshot or _snapshot
    wave.token_of = wave.token_of or _token_of


def _keep_check(wave: Wave) -> dict | None:
    """shared.keep as a check. Kept names, their data, materials and children compare with what they
    were when the wave that first kept them began (_kept), found through the job's baseline. No
    quote (orchestrator.repair_wave clears one it could not find in the user's words) means no check:
    the names still stay kept (nobody may own them), just not verified."""
    keep = wave.plan["shared"]["keep"]
    if not keep.get("names") or not keep.get("quote"):
        return None
    return {"kind": "unchanged", "names": list(keep["names"]), "quote": keep["quote"], "id": "k1"}


def _pinned(check: dict, wave: Wave) -> dict:
    """An unchanged check against its own wave's spawn tokens, as the worker's copy measured it (the
    child's stamp_all tokens are that copy's), not the job's first wave's: what earlier waves built
    or changed is not a change to it. A copy; the brief keeps its own."""
    tokens = getattr(wave.spawn, "tokens", None)
    if check.get("kind") != "unchanged" or check.get("tokens") or not isinstance(tokens, dict):
        return check
    return {**check, "tokens": tokens}


def _kept(job: Job, wave: Wave) -> None:
    """The wave's shared.keep into the job's keep checks: names no earlier wave kept become a check of
    their own ("keep", then "keep <wave>"), compared with this wave's spawn (_pinned). A look pass
    that keeps the props only from its lighting wave on no longer counts the painting as a change."""
    keep = _keep_check(wave)
    kept = {name for check in job.keep.values() for name in check["names"]}
    names = [name for name in (keep or {}).get("names", ()) if name not in kept]
    if names:
        job.keep["keep" if not job.keep else f"keep {wave.number}"] = _pinned({**keep, "names": names}, wave)


def _stop_all(workers_: list[Worker]) -> None:
    for worker in workers_:
        worker.turn.cancel.set()
        if worker.thread is None and worker.finished is None:
            worker.never_started()
    killers = [threading.Thread(target=worker.kill, name=f"loopcut-kill-{worker.id}", daemon=True)
               for worker in workers_]  # Together: each kill may wait out its SIGTERM grace.
    for killer in killers:
        killer.start()
    for killer in killers:
        killer.join()
    deadline = time.monotonic() + JOIN_SECONDS
    for worker in workers_:
        if worker.thread is not None:
            worker.thread.join(max(0.0, deadline - time.monotonic()))


def _busy(job: Job) -> list[Worker]:
    """Queued or running."""
    with job.lock:
        return [w for w in job.crew if w.finished is None]


def _ready(job: Job) -> list[Worker]:
    """Finished and not reported yet."""
    with job.lock:
        return [w for w in job.crew if w.finished is not None and not w.reported]


def pending(job: Job) -> bool:
    """Whether a worker's result is still to come: queued, running, or finished and not reported."""
    with job.lock:
        return any(not w.reported for w in job.crew)


def _note_delivery(item: dict | None, worker: Worker, step: int | None = None) -> None:
    """Under job.lock: publish a per-helper receipt without changing the parent's pending reply.
    Replace the list so the panel and conversation saver can safely snapshot it while helpers read.
    Missing or unusable metadata may omit a receipt, but must never stop a helper or a correction."""
    try:
        if not isinstance(item, dict):
            return
        delivery = {"wave": worker.wave.id, "worker": worker.id, "deliverable": worker.brief["deliverable"]}
        if step is not None:
            delivery.update(step=step, read_at=time.time())
        saved = item.get("helper_deliveries")
        if not isinstance(saved, (list, tuple)):
            saved = ()
        receipts = [r for r in saved if isinstance(r, dict) and all(k in r for k in ("wave", "worker", "deliverable"))]
        for n, receipt in enumerate(receipts):
            if (receipt["wave"], receipt["worker"]) == (worker.wave.id, worker.id):
                if "step" not in receipt:
                    receipts[n] = delivery
                break
        else:
            receipts.append(delivery)
        item["helper_deliveries"] = receipts
    except Exception:  # A stale parent item or partial worker must not affect delivery to other helpers.
        pass


def steer(job: Job, text: str, item: dict | None = None) -> None:
    """Broadcast without draining the orchestrator's notes. Workers read before their next request;
    their cursor survives a child retry, whose conversation already contains earlier corrections."""
    with job.lock:
        if not job.stopped:
            job.steers.append((text, item))
            try:
                for worker in job.crew:
                    if getattr(worker, "finished", None) is None:
                        _note_delivery(item, worker)
            except Exception:  # The correction is queued even when its optional receipts are unavailable.
                pass


def _state(worker: Worker) -> str:
    return "finished" if worker.finished is not None else "queued" if worker.thread is None else "running"


def busy(job: Job | None) -> dict:
    """{worker id: {deliverable, prefix, owns, aspects, role, bounds, wave, state}} of the workers whose
    results are not in yet, for delegate: a new brief may not continue one, nor take its id, prefix,
    deliverable or owned names (orchestrator.validate_wave), nor plan a box into its planned bounds
    (layout); state (queued, running, finished) says where the new briefs queue (schedule_line)."""
    if job is None:
        return {}
    with job.lock:
        return {w.id: {"deliverable": w.brief["deliverable"], "prefix": w.brief["prefix"],
                       "owns": list(w.brief.get("owns") or ()), "aspects": list(w.brief.get("aspects") or ()),
                       "role": w.brief["role"], "bounds": w.brief.get("bounds"), "wave": w.wave.number,
                       "state": _state(w)}
                for w in job.crew if not w.reported}


def _schedule(job: Job, is_cancelled) -> None:
    """The job's thread while any worker is queued or running: starts queued workers in order as slots
    free (at most max_workers at once, across the turn's waves; one the budget has no room for never
    starts), stops a worker past its seconds, and on Stop stops them all (the agent thread, which may be
    merging, closes the waves: collect or end_turn)."""
    while True:
        if is_cancelled():
            stop(job, close=False)
        with job.lock:
            running = [w for w in job.crew if w.thread is not None and w.thread.is_alive()]
            queued = [w for w in job.crew if w.thread is None and w.finished is None]
            if job.stopped or not (running or queued):
                job.scheduler = None
                return
            for worker in queued:
                if worker.wave.session.get("workers_unavailable"):
                    from .worker_host import UNAVAILABLE_NOTE
                    worker.row["unavailable"] = True
                    worker.never_started(UNAVAILABLE_NOTE)
                    continue
                if len(running) >= job.max_workers:
                    break
                reason = "cancelled" if worker.cancelled() else job.budget.spent()
                if reason:
                    worker.never_started(f"not started: {reason}")
                    continue
                worker.start()
                running.append(worker)
        late = [w for w in running if not w.cancelled()
                and job.budget.clock() - w.started > job.budget.seconds + HARD_GRACE]
        for worker in late:
            worker.set_status("stopped", "it ran out of time")
        if late:
            _stop_all(late)
        time.sleep(POLL)


def start(wave: Wave, is_cancelled, on_main) -> None:
    """The agent thread: the wave's workers join the job's crew, queued, and the job's thread runs them
    (_schedule), started here unless it runs already."""
    _defaults(wave)
    job = wave.job
    wave.on_main, wave.started = on_main, time.monotonic()
    job.on_main = on_main
    job.checker = lambda checks: on_main(lambda: wave.run_checks(checks, job.baseline))
    _kept(job, wave)
    wave.crew = [Worker(wave, brief, index) for index, brief in enumerate(wave.plan["workers"])]
    wave.entry = {"n": wave.number, "seconds": 0.0, "merge": _log_merge(None, ""), "merges": [], "workers": []}
    job.log["waves"].append(wave.entry)
    job.waves.append(wave)
    with job.lock:
        for worker in wave.crew:
            _running[(wave.id, worker.id)] = worker
        job.crew += wave.crew
        if job.scheduler is None and not job.stopped:
            job.scheduler = threading.Thread(target=_schedule, args=(job, is_cancelled), name=f"loopcut-job-{job.id}",
                                             daemon=True)
            job.scheduler.start()


def _close(job: Job, wave: Wave, cancelled: bool = False) -> None:
    """A wave all of whose workers have reported, or every wave after a Stop: off the rows' Cancel
    list, its JobLog entry complete, its temporary copies gone."""
    for worker in wave.crew:
        _running.pop((wave.id, worker.id), None)
    if cancelled:
        wave.entry.update(workers=[w.log if w.log and w.row.get("merged") else _log_worker(w, {})
                                  for w in wave.crew], cancelled=True,
                          seconds=round(time.monotonic() - wave.started, 1))
    reap_wave(wave.folder)


def stop(job: Job, close: bool = True) -> None:
    """Stop: every worker still queued or running stops, and nothing more starts or merges; then
    (close) every wave's temporary copies go, the transcripts stay. Safe to call again (the job's
    thread, collect, the finish gate and end_turn may all get here)."""
    with job.lock:
        job.stopped = True
        busy_ = [w for w in job.crew if w.finished is None]
    _stop_all(busy_)
    for worker in job.crew:
        row = getattr(worker, "row", None)
        if row is None:  # Helpers without a wave card still complete cancellation and note teardown.
            continue
        if not row.get("merged") and not row.get("merge_error") and worker.status != "failed":
            row["cancelled_before_merge"] = True
            worker._stopped()
    if close:
        for wave in job.waves:
            _close(job, wave, cancelled=any(not w.row.get("merged") for w in wave.crew))
        job.write()


def _wait(job: Job, is_cancelled, done) -> None:
    """Blocks the agent thread until done(); counts it as the orchestrator's wait (JobLog "waited").
    Raises llm.Cancelled after a Stop, with every worker stopped."""
    t0 = time.monotonic()
    try:
        while True:  # Stop first: the job's thread may have stopped every worker already, making done() true.
            if is_cancelled() or job.stopped:
                stop(job)
                raise llm.Cancelled()
            if done():
                break
            time.sleep(POLL)
    finally:
        job.log["waited"] = round(job.log.get("waited", 0.0) + time.monotonic() - t0, 1)


def collect(job: Job, is_cancelled, on_main, lead: str = "", scene_before=None):
    """The agent thread, as delegate's `then` (run_wave, or wait with no briefs): waits until a worker
    has finished, then up to BATCH_SECONDS more while others still run, and reports every one finished
    by then (report). Raises llm.Cancelled after a Stop."""
    if job.stopped and not is_cancelled():
        return wait_result(job.session, job, lead)

    def done():
        ready, busy_ = _ready(job), _busy(job)
        if not ready:
            return not busy_
        return not busy_ or time.monotonic() >= min(w.finished for w in ready) + BATCH_SECONDS
    _wait(job, is_cancelled, done)
    ready = _ready(job)
    if not ready:
        return wait_result(job.session, job, lead)
    return report(job, ready, on_main, lead, scene_before)


def run_wave(wave: Wave, is_cancelled, on_main, lead: str = ""):
    """The agent thread, as the delegate result's `then`: the wave's workers join the job (start), and
    delegate returns as soon as one of the job's workers has finished and merged (collect), with lead,
    what delegate said on starting it, first. Raises llm.Cancelled after a Stop."""
    start(wave, is_cancelled, on_main)
    return collect(wave.job, is_cancelled, on_main, lead, wave.scene_before)


def drain(job: Job, is_cancelled):
    """The finish gate (and end_turn after an error): waits for every worker still queued or running and
    reports every result not reported yet (report); None when there is none, or after a Stop."""
    if not job.crew or job.on_main is None or not pending(job):
        return None
    try:
        _wait(job, is_cancelled, lambda: not _busy(job))
    except llm.Cancelled:
        return None
    ready = _ready(job)
    return report(job, ready, job.on_main) if ready else None


def end_turn(session: dict, turn) -> None:
    """The orchestrator's turn is over (LoopSpec.on_end): after a Stop, whatever still runs stops;
    after an error, it is waited for and merged (a notice says so), as the finish gate would have."""
    job = getattr(turn, "job", None)
    if job is None or not job.crew:
        return
    if turn.cancel.is_set():
        stop(job)
        return
    late = drain(job, turn.cancel.is_set)
    if late is not None:
        session["items"].append(state.item_notice(
            "Workers still running when the turn ended were waited for; see each worker's outcome in its wave card. "
            "The agent did not review them."))
        if getattr(late, "scene_after", None) is not None:
            turn.scene_before = turn.scene_before or late.scene_before
            turn.scene_after = late.scene_after


def _waits_on(check: dict, busy_: list[Worker]) -> list[str]:
    """The workers still running whose objects a check names (acceptance._named): the deliverable, an
    owned name, or a name under the prefix."""
    names = acceptance._named(check)
    return [w.id for w in busy_ if any(name == w.brief["deliverable"] or name in (w.brief.get("owns") or ())
                                       or ownership.prefixed(name, w.brief["prefix"]) for name in names)]


def _settle(wave: Wave, crew: list[Worker], on_main) -> tuple[list[str], list, bool, str]:
    """Some of a wave's finished workers, into the scene and the JobLog: their checks join the finish
    gate's, the ones that built something merge (one call, its own undo step; the spawn copy stays
    while more of the wave is to come), their threads are remembered. Returns (the wave's section of
    the report, the merged checks as (label, check), whether it merged, the merge error)."""
    job, session = wave.job, wave.session
    for worker in crew:
        worker.reported = True
        worker.t["reported"] = worker._now()
        if not worker.user_cancelled:  # A worker the user cancelled is not asked for at the finish gate.
            for check in worker.brief["checks"]:
                job.checks[(worker.id, check["id"])] = _pinned(check, wave)
    last = all(w.reported for w in wave.crew)
    mergeable = [w for w in crew if w.status in ("done", "partial", "question") and w.manifest is not None
                 and not job.stopped and not w.user_cancelled]
    merge_report, error, unchanged = None, "", False
    if mergeable:
        for worker in mergeable:
            worker.row["status"] = "merging"
        agent._redraw()
        try:
            merge_report = on_main(lambda: wave.merge(wave.spawn, [{"brief": w.brief, "manifest": w.manifest,
                                                                     "shared": wave.plan["shared"]}
                                                                    for w in mergeable], lambda: wave.checkpoint,
                                                      keep_spawn=not last))
        except llm.Cancelled:
            raise
        except Exception as ex:  # Only MergeError can confirm rollback or failure before any edits.
            error = f"{ex}"[:200] or type(ex).__name__
            unchanged = isinstance(ex, merge.MergeError) and ex.scene_unchanged
        if not error:
            job.follow(wave.spawn)
        for worker in mergeable:
            worker.row["status"] = worker.status
            worker.row["merged"] = not error
            if error:
                worker.row["merge_error"] = error
                worker.row["merge_unchanged"] = unchanged
            else:
                changes = {
                    key: [e for e in _field(merge_report, key) if _belongs(e, worker.id)]
                    for key in ("created", "replaced", "reused")}
                worker.row["merged_changes"] = card_lines(changes)
                for key in ("quarantined", "conflicts"):
                    worker.row[key] = [_short(e) for e in _field(merge_report, key) if _belongs(e, worker.id)]
                errors = [str(e) for e in _field(merge_report, "errors") if _belongs(e, worker.id)]
                if errors:
                    worker.row["merge_error"] = "; ".join(errors)
                if errors or worker.row["quarantined"] or worker.row["conflicts"]:
                    worker.row["merged"] = any(changes.values())
                    worker.row["merge_unchanged"] = not worker.row["merged"]
    headline, merge_lines = _merge_line(merge_report, error, wave, unchanged=unchanged)
    if mergeable:
        checkpoint = str(_field_value(merge_report, "checkpoint") or wave.checkpoint or "")
        session["items"].append(state.item_merge(wave.id, headline, merge_lines, checkpoint,
                                                 [_short(q) for q in _field(merge_report, "quarantined")]))
    labelled = [(f"{w.id} {c['id']}", _pinned(c, wave)) for w in mergeable if w.row.get("merged")
                for c in w.brief["checks"]]
    tokens = {}
    for worker in crew:
        _dedupe_requests(job, worker)
        tokens[worker.id] = {}
        for request in (worker.report or {}).get("change_requests") or []:
            try:
                tokens[worker.id][request["target"]] = on_main(lambda: wave.token_of(request["target"]))
            except llm.Cancelled:
                raise
            except Exception:
                pass
    reports = [report_of(w, merge_report) for w in crew]
    remember(wave, crew, merge_report if not error else None)
    for worker in crew:
        worker.log = _log_worker(worker, tokens[worker.id])
    if mergeable:
        wave.entry["merges"].append({**_log_merge(merge_report, error), "workers": [w.id for w in mergeable]})
    seconds = time.monotonic() - wave.started
    wave.entry.update(seconds=round(seconds, 1), merge=_merges_total(wave.entry["merges"]),
                      workers=[w.log for w in wave.crew if w.log])  # Worker 1 first, as the wave has them.
    if last:
        _close(job, wave)
    completed = all(state.worker_complete(w.row) for w in crew)
    title = (f"Wave {wave.number} ({wave.id}) finished in {seconds:.0f} s."
             if completed and last and len(crew) == len(wave.crew)
             else f"Wave {wave.number} ({wave.id}), {seconds:.0f} s in: {', '.join(w.id for w in crew)} "
                  + ("finished." if completed else "results."))
    merge_error = error or "; ".join(state.worker_outcome(w.row) for w in crew
                                    if any(w.row.get(key) for key in ("merge_error", "quarantined", "conflicts")))
    return ([title, *(render_worker(r) for r in reports), headline], labelled,
            any(w.row.get("merged") for w in crew), merge_error)


def report(job: Job, batch: list[Worker], on_main, lead: str = "", scene_before=None):
    """The agent thread: finished workers merged, wave by wave (_settle), their checks run on the live
    scene with the checks held for them and keep, and one result for the orchestrator: a block per
    worker, each wave's merge, the live checks, who is still running, and a contact sheet of these
    deliverables."""
    waves_ = list(dict.fromkeys(w.wave for w in batch))
    first = waves_[0]
    if scene_before is None and any(w.manifest is not None for w in batch):
        scene_before = on_main(first.snapshot)
    sections, labelled, merged, errors = [], [], False, []
    for wave in waves_:
        section, checks_, did, error = _settle(wave, [w for w in batch if w.wave is wave], on_main)
        sections.append(section)
        labelled += checks_
        merged = merged or did
        errors += [error] if error else []
    busy_ = _busy(job)
    held, run = [], []
    for label, check in job.held + labelled:  # A check naming a worker still running waits for its merge.
        waits = _waits_on(check, busy_)
        (held.append((label, check, waits)) if waits else run.append((label, check)))
    job.held = [(label, check) for label, check, _ in held]
    run += list(job.keep.items())
    ran = {label for label, _ in run}  # Preservation is measured after every merge, not only at the finish.
    run += [(label, check) for label, check in job.final_checks() if check.get("kind") == "preserved" and label not in ran]
    live = []
    if run:
        try:
            results = on_main(lambda: first.run_checks([c for _, c in run], job.baseline))
            live = [(label, check, result) for (label, check), result in zip(run, results)]
        except llm.Cancelled:
            raise
        except Exception as ex:
            sections[-1][-1] += f" The live checks could not run: {ex}"
    job.write()
    scene_after = on_main(first.snapshot) if merged else scene_before
    job.sheets += 1
    sheet, sheet_line = contact_sheet(first.folder, batch, on_main,
                                      "sheet.png" if job.sheets == 1 else f"sheet-{job.sheets}.png")
    return tools.ToolResult(render_report(job, sections, live, held, busy_, sheet_line, lead), image_path=sheet,
                            ok=merged and not errors, scene_before=scene_before, scene_after=scene_after)


def _field_value(report, key: str):
    return report.get(key) if isinstance(report, dict) else getattr(report, key, None)
