"""waves.run_wave against a scripted model server and in-process fake children: the spawn rule,
concurrency, budgets, retries, cancel, reports and the JobLog. No Blender.

    python3 -m unittest discover -s loopcut/tests
"""

import ast
import base64
import dataclasses
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import (agent, checkpoints, config, context, conversations, llm, merge, orchestrator,  # noqa: E402
                     ownership, state, tools, waves, workers)

_BRIEF = re.compile(r"deliverable: (\w+)")


def sse(*chunks: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


USAGE = {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                                  "prompt_tokens_details": {"cached_tokens": 400}}}


def text(content: str) -> bytes:
    return sse({"choices": [{"delta": {"content": content}}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}]}, USAGE)


def calls(*pairs) -> bytes:
    return sse(*({"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"call_{i}", "type": "function",
                                                         "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
                 for i, (name, args) in enumerate(pairs)),
               {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}, USAGE)


def build(code: str = "make()") -> bytes:
    return calls(("run_python", {"code": code, "summary": "build"}))


def finish(summary: str = "Built it.", **report) -> bytes:
    return calls(("check_brief", {}), ("report_back", {"summary": summary, **report}))


class Models:
    """Replies per worker, found by the deliverable in its brief; arrival times per request."""

    def __init__(self):
        self.lock = threading.Lock()
        self.scripts: dict[str, list] = {}
        self.arrivals: list[tuple[str, float]] = []
        self.delays: dict[tuple[str, int], float] = {}  # (worker, request number) -> seconds before replying
        self.bodies: list[tuple[str, dict]] = []  # (who, request body) in arrival order

    def reply_for(self, body: dict) -> bytes:
        first = next(m["content"] for m in body["messages"] if m["role"] == "user")
        if isinstance(first, list):  # A brief with the user's images (references): its text part.
            first = "".join(p.get("text", "") for p in first if p.get("type") == "text")
        found = _BRIEF.search(first)
        worker = "router" if body.get("model") == "router" else found.group(1).lower() if found else "orchestrator"
        with self.lock:
            self.bodies.append((worker, body))
        with self.lock:
            number = sum(1 for w, _ in self.arrivals if w == worker)
            self.arrivals.append((worker, time.monotonic()))
            script = self.scripts[worker]
            reply = script.pop(0) if script else text("Nothing left to say.")
        time.sleep(self.delays.get((worker, number), 0))
        return reply


MODELS = Models()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        reply = MODELS.reply_for(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


class Crashed(RuntimeError):
    """worker_host.ChildCrashed's shape: a dead child, and where its crash kit was kept."""

    def __init__(self, text: str, kept: str = ""):
        super().__init__(text)
        self.kept = kept


def kit(worker) -> str:
    """The next crash kit's folder in a worker's folder, as worker_host.Child._keep makes it."""
    folder = worker.folder / f"crash-{1 + len(list(worker.folder.glob('crash-*')))}"
    folder.mkdir(parents=True)
    return str(folder)


class FakeRuntime:
    """A child that runs tools against a dict scene. crash: tool name whose call dies ("kept:" first:
    and leaves a crash kit)."""

    def __init__(self, registry: "Registry", worker, crash: str = "", checks=None, facts=None, look=None, missing=None):
        self.registry, self.worker, self.crash = registry, worker, crash
        self.checks, self.facts, self.look, self.missing = checks, facts, look, missing
        self.killed = threading.Event()
        self.closed = False
        self.peak_rss_mb = 300
        self.scene = registry.scene

    def start(self):
        with self.registry.lock:
            self.registry.started.append(self.worker.id)
            self.registry.live += 1
            self.registry.peak = max(self.registry.peak, self.registry.live)

    def _gone(self):
        with self.registry.lock:
            self.registry.live -= 1

    def run_tool(self, name, arguments, is_cancelled):
        args = json.loads(arguments or "{}")
        kept = kit(self.worker) if self.crash.startswith("kept:") else ""
        crash = self.crash.removeprefix("kept:")
        if name == crash:
            raise Crashed("the child exited (signal 11)", kept)
        if f"result:{name}" == crash:  # worker_host's way: a failed result marked crashed.
            result = tools.ToolResult(f"Your Blender stopped during {name} (exited: exited with code -11).", ok=False)
            result.crashed, result.crash_kept = "exited", kept
            return result
        if name == "run_python" and args.get("code") == "slow":
            self.killed.wait(5)
            raise RuntimeError("killed")
        if name == "run_python":
            self.scene[f"{self.worker.brief['prefix']}_Part"] = self.worker.id
            result = tools.ToolResult("Scene changes: +1 object")
            result.changed = True
            if args.get("code") == "touch rug":
                result.text += "\nRolled back: this call changed Rug, which your brief doesn't own."
                result.rolled_back = {"keys": ["OBJECT:Rug"], "note": "..."}
            return result
        if name == "check_brief":
            return tools.ToolResult("c1 pass")
        return tools.ToolResult(f"{name} ok")

    def check(self, is_cancelled):
        brief = self.worker.brief["checks"]
        return self.checks if self.checks is not None else [{"id": c["id"], "status": "pass", "detail": "ok"} for c in brief]

    def finish(self, is_cancelled):
        prefix = self.worker.brief["prefix"]
        return {"result": "/tmp/result.blend", "manifest": {"created": [f"OBJECT:{prefix}", f"MATERIAL:{prefix}_Mat"],
                                                            "facts": self.facts or {}, "look": self.look,
                                                            "missing": self.missing or []}}

    def close(self):
        if not self.closed:
            self.closed = True
            self._gone()

    def kill(self):
        with self.registry.lock:
            self.registry.kills.append(self.worker.id)
        self.killed.set()
        self.close()


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.started, self.kills = [], []
        self.live = self.peak = 0
        self.scene = {}


class KeepCheckTest(unittest.TestCase):
    """M4: repair_wave clears shared.keep.quote it could not find in the user's words, rather than
    refusing the wave; the names still stay kept, just with no unchanged check to verify it."""

    def fake_wave(self, keep: dict) -> types.SimpleNamespace:
        plan = orchestrator.normalise_wave({"shared": {"keep": keep}, "workers": []})
        return types.SimpleNamespace(plan=plan)

    def test_a_quote_makes_the_check(self):
        check = waves._keep_check(self.fake_wave({"names": ["Bottle"], "quote": "keep the bottle"}))
        self.assertEqual(check, {"kind": "unchanged", "names": ["Bottle"], "quote": "keep the bottle", "id": "k1"})

    def test_no_quote_still_no_check(self):
        self.assertIsNone(waves._keep_check(self.fake_wave({"names": ["Bottle"], "quote": ""})))
        self.assertIsNone(waves._keep_check(self.fake_wave({"names": [], "quote": "keep the bottle"})))
        self.assertIsNone(waves._keep_check(self.fake_wave({})))


class BudgetTest(unittest.TestCase):
    def estimate(self, **overrides):
        sample = dict(steps=4, elapsed=40, seconds=100, job_steps=12, usd=0.12,
                      max_usd=0.4, running=3, priced=True)
        return waves.budget_steps_left(**(sample | overrides))

    def test_time_bound(self):
        self.assertEqual(self.estimate(), 6)

    def test_dollars_are_shared_by_the_workers_currently_running(self):
        self.assertAlmostEqual(self.estimate(max_usd=0.18), 2)  # $0.06 / ($0.01 * 3).
        self.assertAlmostEqual(self.estimate(max_usd=0.18, running=1), 6)

    def test_unpriced_model_estimates_from_time_only(self):
        self.assertEqual(self.estimate(max_usd=0.18, priced=False), 6)

    def test_first_request_has_no_estimate_even_if_other_workers_have_spent_money(self):
        self.assertIsNone(self.estimate(steps=0, elapsed=0))

    def test_disabled_dollars_or_zero_cost_samples_use_time(self):
        for overrides in ({"max_usd": 0}, {"usd": 0}, {"job_steps": 0}):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.estimate(**overrides), 6)

    def test_zero_elapsed_never_divides_by_zero_and_spent_budgets_estimate_zero(self):
        self.assertIsNone(self.estimate(elapsed=0, usd=0))
        self.assertAlmostEqual(self.estimate(elapsed=0, max_usd=0.18), 2)
        self.assertEqual(self.estimate(elapsed=101), 0)
        self.assertEqual(self.estimate(usd=0.5), 0)

    def test_job_counts_steps_past_160_without_stopping(self):
        budget = waves.JobBudget(usd=0.4, seconds=1800, clock=lambda: 1)
        for _ in range(200):
            budget.take_step(started=0)
        self.assertEqual(budget.steps, 200)
        self.assertEqual(budget.spent(0), "")
        self.assertFalse(hasattr(budget, "max_steps"))


class WorkerSteerTest(unittest.TestCase):
    """Real worker hooks and loop, with requests replaced in process; no server or Blender."""

    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        from prompt_index_fixture import index_patch
        for patcher in (index_patch(),
                        mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": self.data.name}),
                        mock.patch.object(ownership, "expand_owns", return_value=set()),
                        mock.patch.object(checkpoints, "ensure_for_wave", return_value="cp"),
                        mock.patch.object(agent, "_redraw")):
            patcher.start()
            self.addCleanup(patcher.stop)
        cfg = config.Config(api_key="k", base_url=config.SERVICE_BASE_URL, model="fast", reasoning_effort="low",
                            auto_run=True, auto_look=False, max_steps=0, context_budget=40_000)
        self.session = state.reset()
        self.turn = agent.Turn()
        self.turn.roles = config.Roles(orchestrator=cfg, worker=cfg, orchestrate=True)
        self.session.update(busy=True, turn=self.turn)
        self.session["items"].append(state.item_user("make a small object"))
        self.wave = self.prepare()
        self.job = self.wave.job
        self.addCleanup(lambda: [conversations.FOLDERS.pop(w.session["id"], None) for w in self.crew])
        self.crew = []

    def prepare(self, continues=False):
        plan = orchestrator.normalise_wave({"workers": [
            {"id": name, "deliverable": name.upper(), "role": "modeling", "prefix": name.upper(),
             "continues": name if continues else "",
             "requirements": [{"quote": "small", "do": "small"}], "checks": []} for name in ("a", "b")]})
        return waves.prepare(self.session, self.turn, plan, message_index=0,
                             save_spawn=lambda folder: types.SimpleNamespace(wave=str(folder), tokens={}),
                             scene_context=lambda text: "scene")

    def worker(self, index=0, wave=None):
        worker = waves.Worker(wave or self.wave, (wave or self.wave).plan["workers"][index], index)
        worker.started = worker.wave.job.budget.clock()
        worker.wave.first_reply.set()
        self.crew.append(worker)
        return worker

    def take_step(self, worker, step):
        worker.before_request(step)
        agent._fold_notes(worker.session, worker.turn)

    def steers(self, session):
        return [m["content"] for m in session["messages"] if m.get(context.STEER)]

    def worker_steer(self, text):
        return f"<steer>{workers.STEER_INSTRUCTION}\n\n{text}</steer>"

    def test_running_and_queued_workers_each_read_corrections_once_and_parent_keeps_them(self):
        first, queued = self.worker(0), self.worker(1)
        self.take_step(first, 1)
        for text in ("make it smaller", "also red"):
            self.assertTrue(agent.send(text))
        for worker in (first, queued):
            self.take_step(worker, 2)
            self.take_step(worker, 3)
            self.assertEqual(self.steers(worker.session),
                             [self.worker_steer("make it smaller"), self.worker_steer("also red")])
        self.assertEqual(len(self.turn.notes), 2, "worker delivery does not consume the orchestrator's notes")
        self.assertTrue(all(item.get("queued") for _, item in self.turn.notes))
        agent._fold_notes(self.session, self.turn)
        self.assertEqual(self.steers(self.session), ["<steer>make it smaller</steer>", "<steer>also red</steer>"])
        self.assertFalse(any(i.get("queued") for i in self.session["items"]))

    def test_receipts_identify_each_helper_and_its_consuming_step_and_survive_reload(self):
        first, queued = self.worker(0), self.worker(1)
        self.job.crew = [first, queued]
        first.steps = 25
        self.assertTrue(agent.send("same note"))
        note = self.turn.notes[0][1]
        self.assertEqual([(r["worker"], r["deliverable"]) for r in note["helper_deliveries"]],
                         [("a", "A"), ("b", "B")])
        self.assertTrue(all("step" not in r for r in note["helper_deliveries"]))
        self.take_step(first, 26)
        read = dict(note["helper_deliveries"][0])
        self.assertEqual((read["wave"], read["step"]), (self.wave.id, 26))
        self.assertIsInstance(read["read_at"], float)
        self.assertNotIn("step", note["helper_deliveries"][1])
        self.take_step(queued, 1)
        self.take_step(first, 27)
        self.assertEqual([r["step"] for r in note["helper_deliveries"]], [26, 1])
        self.assertEqual(note["helper_deliveries"][0], read, "later steps must not rewrite the receipt")
        self.assertTrue(note["queued"], "the main agent has not consumed this note")
        agent.send("same note")
        second_note = self.turn.notes[1][1]
        self.assertTrue(all("step" not in r for r in second_note["helper_deliveries"]))
        self.take_step(first, 28)
        self.assertEqual(second_note["helper_deliveries"][0]["step"], 28)
        self.assertEqual(note["helper_deliveries"][0], read, "identical text still has separate receipts")
        conversations.save(self.session)
        loaded = conversations.load(self.session["id"])
        saved = [i for i in loaded["items"] if i.get("text") == "same note"]
        self.assertEqual(saved[0]["helper_deliveries"], note["helper_deliveries"])
        agent._fold_notes(self.session, self.turn)
        self.assertNotIn("queued", note)
        self.assertEqual(note["helper_deliveries"][0], read)

    def test_a_note_arriving_during_the_first_reply_wait_reaches_that_request(self):
        worker = self.worker(1)
        self.wave.first_reply = mock.Mock()
        self.wave.first_reply.is_set.return_value = False
        self.wave.first_reply.wait.side_effect = lambda timeout: agent.send("smaller")
        self.take_step(worker, 1)
        self.assertEqual(self.steers(worker.session), [self.worker_steer("smaller")])

    def test_receipt_failures_do_not_interrupt_notes_or_crash_retries(self):
        class UnreadableNote(dict):
            def get(self, key, default=None):
                raise RuntimeError("note unavailable")

        class UnwritableNote(dict):
            def __setitem__(self, key, value):
                raise RuntimeError("receipt unavailable")

        for failure, note in (("no item", None), ("invalid item", []),
                              ("null receipts", {"helper_deliveries": None}),
                              ("invalid receipts", {"helper_deliveries": 1}),
                              ("invalid entries", {"helper_deliveries": [None, {}, "old"]}),
                              ("read", UnreadableNote()), ("write", UnwritableNote()),
                              ("recorder", {})):
            with self.subTest(failure=failure):
                self.job.steers = []
                worker = self.worker()
                self.job.crew = [worker]
                registry = Registry()
                self.wave.make_runtime = lambda wave, w: FakeRuntime(
                    registry, w, crash="run_python" if not w.retries else "")
                received = []

                def request(session, *args):
                    received.append(self.steers(session))
                    if not worker.retries:
                        waves.steer(self.job, "red", note)
                        return llm.Completion(tool_calls=[llm.ToolCall("build", "run_python", '{"code":"make()"}')])
                    return llm.Completion(tool_calls=[llm.ToolCall("done", "report_back", '{"summary":"Built it."}')])

                error = RuntimeError("receipt recorder unavailable") if failure == "recorder" else None
                with mock.patch.object(waves, "_note_delivery", wraps=waves._note_delivery, side_effect=error), \
                        mock.patch.object(agent, "_request", side_effect=request) as requested:
                    waves.steer(self.job, "smaller", note)
                    worker._work()
                self.assertEqual((registry.started, worker.retries, requested.call_count), (["a", "a"], 1, 2))
                self.assertEqual(worker.status, "done")
                self.assertEqual(worker.turn.outcome, "completed")
                self.assertEqual(worker.report["summary"], "Built it.")
                self.assertEqual(worker.steer_index, 2)
                self.assertEqual(received, [[self.worker_steer("smaller")],
                                            [self.worker_steer("smaller"), waves.CRASH_NOTE.format(tool="run_python"),
                                             self.worker_steer("red")]])

    def test_legacy_string_notes_and_partial_workers_keep_delivery_working(self):
        worker = self.worker()
        self.job.crew = [types.SimpleNamespace(), worker]
        self.job.steers.append("smaller")
        note = {"helper_deliveries": (None, {}, {"wave": "old", "worker": "a", "deliverable": "A", "step": 3})}
        waves.steer(self.job, "red", note)
        self.job.crew = [worker]
        self.take_step(worker, 1)
        self.take_step(worker, 2)
        self.assertEqual(self.steers(worker.session), [self.worker_steer("smaller"), self.worker_steer("red")])
        self.assertEqual([(r["wave"], r["worker"], r["step"]) for r in note["helper_deliveries"]],
                         [("old", "a", 3), (self.wave.id, "a", 1)])

    def test_cancel_as_first_reply_wait_ends_does_not_consume_notes_or_request(self):
        worker = self.worker(1)
        agent.send("smaller")
        self.wave.first_reply = mock.Mock()
        self.wave.first_reply.is_set.return_value = False

        def released(timeout):
            worker.turn.cancel.set()
            return True  # The first worker's failed startup releases the wait concurrently with Stop.

        self.wave.first_reply.wait.side_effect = released
        with mock.patch.object(agent, "_request", return_value=llm.Completion(text="done")) as requested:
            agent.run_loop(worker.session, worker.turn, worker.spec)
        requested.assert_not_called()
        self.assertEqual(worker.turn.outcome, "cancelled")
        self.assertEqual((worker.steps, worker.steer_index, self.job.budget.steps), (0, 0, 0))
        self.assertEqual(self.steers(worker.session), [])
        self.assertEqual([text for text, _ in self.turn.notes], ["smaller"])
        self.assertFalse(any("step" in r for r in self.turn.notes[0][1].get("helper_deliveries", [])))

    def test_retry_keeps_its_cursor_and_reads_new_notes(self):
        worker = self.worker()
        agent.send("smaller")
        self.take_step(worker, 1)
        cancel = worker.turn.cancel
        worker.turn = worker._new_turn(cancel)
        agent.send("red")
        self.take_step(worker, 1)
        self.assertIs(worker.turn.cancel, cancel)
        self.assertEqual(self.steers(worker.session), [self.worker_steer("smaller"), self.worker_steer("red")])
        self.assertEqual([item["helper_deliveries"][0]["step"] for _, item in self.turn.notes], [1, 2])

    def test_new_wave_has_earlier_notes_in_its_opening_and_only_receives_later_steers(self):
        agent.send("smaller")
        first = self.worker()
        self.take_step(first, 1)
        later = self.prepare()
        worker = self.worker(wave=later)
        self.assertIn("smaller", later.openings[worker.id])
        self.take_step(worker, 1)
        self.assertEqual(self.steers(worker.session), [])
        earlier, receipt = self.turn.notes[0][1]["helper_deliveries"]
        self.assertEqual(earlier["wave"], self.wave.id)
        self.assertEqual((receipt["wave"], receipt["worker"], receipt["step"]), (later.id, worker.id, 1))
        agent.send("red")
        self.take_step(worker, 2)
        self.assertEqual(self.steers(worker.session), [self.worker_steer("red")])

    def test_new_wave_receipts_only_credit_notes_kept_in_the_opening(self):
        # Identical text must not make an omitted, older note look delivered.
        agent.send("make it red")
        omitted = self.turn.notes[-1][1]
        agent.send("filler " * 500)
        agent.send("make it red")
        included = self.turn.notes[-1][1]
        self.session["worker_threads"] = {
            name: {"role": "modeling", "messages": [], "folder": self.data.name} for name in ("a", "b")}
        for continues in (False, True):
            with self.subTest(continues=continues):
                later = self.prepare(continues=continues)
                worker = self.worker(wave=later)
                self.assertEqual(later.openings[worker.id].count('"make it red"'), 1)
                self.assertEqual(later.openings[worker.id].startswith(workers.CONTINUE), continues)

                def request(session, turn, cfg, spec):
                    self.assertEqual(session["messages"][0]["content"], later.openings[worker.id])
                    self.assertFalse(any(r["wave"] == later.id for r in omitted.get("helper_deliveries", [])))
                    receipt, = [r for r in included["helper_deliveries"] if r["wave"] == later.id]
                    self.assertEqual((receipt["worker"], receipt["step"]), (worker.id, 1))
                    return llm.Completion(text="done")

                with mock.patch.object(agent, "_request", side_effect=request) as requested:
                    agent.run_loop(worker.session, worker.turn, worker.spec)
                requested.assert_called_once()
                self.assertEqual(worker.turn.outcome, "completed")

    def test_loop_folds_a_workers_note_before_requesting(self):
        worker = self.worker()
        agent.send("smaller")

        def request(session, turn, cfg, spec):
            self.assertEqual(self.steers(session), [self.worker_steer("smaller")])
            self.assertFalse(turn.notes)
            self.assertEqual(self.turn.notes[0][1]["helper_deliveries"][0]["step"], 1)
            return llm.Completion(text="done")

        with mock.patch.object(agent, "_request", side_effect=request) as requested:
            agent.run_loop(worker.session, worker.turn, worker.spec)
        self.assertEqual(worker.turn.outcome, "completed")
        requested.assert_called_once()
        self.assertEqual(len(self.turn.notes), 1)

    def test_ordinary_worker_requests_have_no_correction_instruction(self):
        worker = self.worker()

        def request(session, *args):
            self.assertEqual(self.steers(session), [])
            self.assertNotIn(workers.STEER_INSTRUCTION, json.dumps(session["messages"]))
            return llm.Completion(text="done")

        with mock.patch.object(agent, "_request", side_effect=request) as requested:
            agent.run_loop(worker.session, worker.turn, worker.spec)
        requested.assert_called_once()

    def run_worker(self, worker, request):
        registry = Registry()
        self.wave.make_runtime = lambda wave, worker: FakeRuntime(registry, worker)
        with mock.patch.object(agent, "_request", side_effect=request):
            worker._work()
        return registry

    def stop_workers(self):
        waves.stop(self.job)
        scheduler = self.job.scheduler
        if scheduler is not None:
            scheduler.join(2)
            self.assertFalse(scheduler.is_alive())

    def outcome_runtime(self):
        """Real scheduling/collection with in-process children and an attributable merge receipt."""
        registry = Registry()
        self.wave.make_runtime = lambda wave, worker: FakeRuntime(registry, worker)
        self.wave.merge = mock.Mock(side_effect=lambda spawn, results, *args, **kwargs: {
            "created": [{"worker": r["brief"]["id"], "name": r["brief"]["prefix"] + "_Part",
                         "id_type": "OBJECT"} for r in results], "replaced": []})
        self.wave.snapshot = lambda: {}
        self.wave.run_checks = lambda checks, spawn: []
        self.addCleanup(self.stop_workers)

    def test_stop_mid_wave_then_wait_keeps_outcomes_across_turns_and_reload(self):
        self.outcome_runtime()
        entered = threading.Event()

        def loop(session, turn, *args):
            worker = next(w for w in self.wave.crew if w.session is session)
            worker.steps = worker.row["steps"] = 6
            if worker.id == "b":
                session["messages"].append({"role": "assistant", "content": "I will add the new parts."})
                entered.set()
                if not turn.cancel.wait(3):
                    raise AssertionError("test did not stop the helper")
            else:
                worker.report = workers.parse_report({"summary": "Built A."})

        with mock.patch.object(agent, "run_loop", side_effect=loop), mock.patch.object(waves, "BATCH_SECONDS", 0):
            waves.start(self.wave, self.turn.cancel.is_set, lambda fn: fn())
            self.crew += self.wave.crew
            self.assertTrue(entered.wait(2))
            self.crew[0].thread.join(2)
            first = waves.collect(self.job, lambda: False, lambda fn: fn())
            self.assertIn("completed and merged", first.text)
            self.assertIn("merged: + A_Part", first.text)
            self.turn.cancel.set()
            with self.assertRaises(llm.Cancelled):
                waves.collect(self.job, self.turn.cancel.is_set, lambda fn: fn())
        self.assertFalse(self.crew[1].row["merged"])
        self.assertEqual(self.crew[1].row["steps"], 6)
        self.assertEqual(self.crew[1].row["status"], "stopped")
        self.wave.merge.assert_called_once()
        logged = self.job.log["waves"][0]["workers"][1]
        self.assertEqual((logged["delivery"], logged["merged"]),
                         ("stopped by the user before finishing (nothing merged)", False))
        same_job = waves.collect(self.job, lambda: False, lambda fn: fn())
        # A new turn has no live Job. Save/reload must retain the very same receipts.
        self.session["turn"] = agent.Turn()
        conversations.save(self.session)
        loaded = conversations.load(self.session["id"])
        for session in (self.session, loaded):
            with mock.patch.object(state, "session", return_value=session):
                for _ in range(2):
                    result = orchestrator.delegate(wait=True)
                    self.assertIsNone(result.then)
                    self.assertEqual(result.text, same_job.text)
                    self.assertIn("[a] A", result.text)
                    self.assertIn("completed and merged", result.text)
                    self.assertIn("merged: + A_Part", result.text)
                    self.assertIn("[b] B", result.text)
                    self.assertIn("stopped by the user before finishing (nothing merged)", result.text)
                    self.assertNotIn("every worker has reported", result.text.lower())
                    self.assertNotIn("I will add", result.text)
        self.wave.merge.assert_called_once()

    def test_row_cancel_after_finish_before_collection_never_merges_that_worker(self):
        self.outcome_runtime()
        with mock.patch.object(agent, "run_loop"):
            waves.start(self.wave, lambda: False, lambda fn: fn())
            self.crew += self.wave.crew
            for worker in self.crew:
                self.assertTrue(worker.ready.wait(2))
                worker.thread.join(2)
                self.assertEqual(worker.status, "done")
            self.assertTrue(waves.cancel_worker(self.wave.id, "b"))
            result = waves.collect(self.job, lambda: False, lambda fn: fn())
        self.assertIn("stopped by the user before finishing (nothing merged)", result.text)
        self.assertNotIn("every worker has reported", result.text.lower())
        self.assertNotIn("b finished", result.text)
        self.assertEqual([r["brief"]["id"] for r in self.wave.merge.call_args.args[1]], ["a"])
        self.assertFalse(waves.cancel_worker(self.wave.id, "a"))
        self.assertIn("stopped by the user before finishing (nothing merged)", orchestrator.delegate(wait=True).text)

    def test_normal_completion_and_repeated_wait_report_what_actually_merged(self):
        self.outcome_runtime()
        with mock.patch.object(agent, "run_loop"):
            result = waves.run_wave(self.wave, lambda: False, lambda fn: fn())
            self.crew += self.wave.crew
        self.assertTrue(result.ok, result.text)
        self.assertIn("Every worker has reported.", result.text)
        self.assertEqual(result.text.count("outcome: completed and merged"), 2)
        self.wave.merge.assert_called_once()
        for worker in self.crew:
            self.assertTrue(worker.row["merged"])
            self.assertEqual(worker.row["merged_changes"], ["+ " + worker.id.upper() + "_Part"])
        repeated = orchestrator.delegate(wait=True)
        self.assertEqual(repeated.text.count("completed and merged"), 2)
        self.assertIn("+ A_Part", repeated.text)
        self.assertIn("+ B_Part", repeated.text)

    def test_generic_merge_exception_does_not_confirm_rollback(self):
        self.outcome_runtime()
        self.wave.merge.side_effect = RuntimeError("merge rolled back")
        with mock.patch.object(agent, "run_loop"):
            result = waves.run_wave(self.wave, lambda: False, lambda fn: fn())
            self.crew += self.wave.crew
        self.assertFalse(result.ok)
        for text in (result.text, orchestrator.delegate(wait=True).text):
            self.assertIn("merge failed; what reached the scene is unconfirmed: merge rolled back", text)
            self.assertNotIn("nothing merged", text)
            self.assertNotIn("every worker has reported", text.lower())
            self.assertNotIn("completed and merged", text)

    def assert_delivery_receipt(self, result, expected, *, merged=False):
        """The first receipt, later waits, saved rows and panel must tell the same story."""
        from loopcut.ui import layout
        self.assertFalse(result.ok)
        self.assertEqual(self.crew[0].row["merged"], merged)
        self.assertEqual(state.worker_outcome(self.crew[0].row), expected)
        repeated = orchestrator.delegate(wait=True)
        self.session["turn"] = agent.Turn()
        conversations.save(self.session)
        loaded = conversations.load(self.session["id"])
        receipts = [result, repeated, waves.wait_result(loaded)]
        for receipt in receipts:
            self.assertFalse(receipt.ok)
            self.assertIn(expected, receipt.text)
            self.assertNotIn("every worker has reported", receipt.text.lower())
        self.assertIn(expected, self.job.log["waves"][0]["workers"][0]["delivery"])
        for session in (self.session, loaded):
            for width in (260, 420):
                display = layout.build(session, width, 4000, 1.0,
                                       lambda font, size, text: len(text) * size * 0.6, "fast", None, {})
                outcome = " ".join(p["text"] for p in display["prims"]
                                   if ".wave.row0.outcome.l" in p["id"])
                self.assertEqual(outcome, expected)

    def collect_merge_receipt(self, report):
        self.outcome_runtime()
        self.wave.merge.side_effect = None
        self.wave.merge.return_value = report
        with mock.patch.object(agent, "run_loop"):
            result = waves.run_wave(self.wave, lambda: False, lambda fn: fn())
            self.crew += self.wave.crew
        return result

    def test_quarantine_only_is_held_back_in_receipts_and_saved_cards(self):
        result = self.collect_merge_receipt({
            "quarantined": [{"worker": "a", "name": "A_Light", "id_type": "OBJECT"}],
            "created": [{"worker": "b", "name": "B_Part", "id_type": "OBJECT"}]})
        self.assert_delivery_receipt(result, "held back (quarantined, waiting for the user's accept): A_Light")
        self.assertEqual(state.worker_outcome(self.crew[1].row), "completed and merged")

    def held_results(self, *names):
        entries = [{"worker": "a", "name": name, "id_type": "OBJECT"} for name in names]
        self.collect_merge_receipt({"quarantined": entries,
                                    "created": [{"worker": "b", "name": "B_Part", "id_type": "OBJECT"}]})
        # Exercise the real acceptance selection and tag removal against small Blender stand-ins.
        class HeldID(dict):
            def __eq__(held, other):
                return held is other

            def __init__(held, name, kind="OBJECT"):
                super().__init__({ownership.QUARANTINE: self.wave.id + "/a"})
                held.name, held.id_type, held.type, held.data = name, kind, "LIGHT", None

        ids = [HeldID(name) for name in names]
        data = HeldID("A_LightData", "LIGHT")
        ids[0].data = data
        ids.append(data)
        self.enterContext(mock.patch.object(ownership, "all_ids", side_effect=lambda: iter(ids)))
        self.enterContext(mock.patch.dict(sys.modules, bpy=types.SimpleNamespace(
            data=types.SimpleNamespace(collections={}))))
        self.enterContext(mock.patch.object(merge, "_accepted_home", return_value=types.SimpleNamespace(
            name="Collection", objects=mock.MagicMock())))
        self.enterContext(mock.patch.object(merge, "_undo"))
        return ids

    def assert_resolved_receipt(self, expected, *, ok):
        row = self.crew[0].row
        self.assertEqual(state.worker_outcome(row), expected)
        self.assertEqual(self.job.log["waves"][0]["workers"][0]["delivery"], expected)
        on_disk = json.loads((self.job.folder / "job.json").read_text())
        self.assertEqual(on_disk, self.job.log)
        same_job = orchestrator.delegate(wait=True)
        self.session["turn"] = agent.Turn()
        conversations.save(self.session)
        loaded = conversations.load(self.session["id"])
        for result in (same_job, orchestrator.delegate(wait=True), waves.wait_result(loaded)):
            self.assertEqual(result.ok, ok, result.text)
            self.assertIn(expected, result.text)
        from loopcut.ui import layout
        for session in (self.session, loaded):
            display = layout.build(session, 420, 4000, 1.0,
                                   lambda font, size, text: len(text) * size * 0.6, "fast", None, {})
            index = next(n for n, item in enumerate(session["items"])
                         if item.get("kind") == "wave" and item.get("wave") == self.wave.id)
            outcome = " ".join(p["text"] for p in display["prims"]
                               if p["id"].startswith(f"item{index}.wave.row0.outcome.l"))
            self.assertEqual(outcome, expected)
        return row

    def resolve_from_panel(self):
        # Import just the callback, as in test_host_area_guard, without Blender's drawing machinery.
        source = Path(waves.__file__).parent / "ui" / "host.py"
        nodes = [node for node in ast.parse(source.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name == "_accept_quarantine"]
        scope = {"__package__": "loopcut.ui", "state": state, "conversations": conversations}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), scope)
        card = next(i for i in self.session["items"] if i["kind"] == "merge")
        scope["_accept_quarantine"](self.session, card)
        return card

    def test_panel_accept_updates_worker_receipt(self):
        self.held_results("A_Light")
        card = self.resolve_from_panel()
        self.assertEqual(card["resolved"], "accepted")
        self.assert_resolved_receipt("completed and merged", ok=True)

    def test_accept_quarantine_updates_worker_and_wait_after_reload(self):
        ids = self.held_results("A_Light")
        result = orchestrator.accept_quarantine(self.wave.id)
        self.assertTrue(result.ok, result.text)
        self.assertTrue(all(ownership.QUARANTINE not in idb for idb in ids))
        row = self.assert_resolved_receipt("completed and merged", ok=True)
        self.assertTrue(row["merged"])
        self.assertFalse(row["merge_unchanged"])
        self.assertEqual(row["quarantined"], [])
        self.assertEqual(row["merged_changes"], ["+ A_Light"])
        card = next(i for i in self.session["items"] if i["kind"] == "merge")
        self.assertEqual(card["resolved"], "accepted")
        self.assertEqual(card["quarantined"], [])

    def test_selective_accept_keeps_other_names_held_then_finishes(self):
        ids = self.held_results("A_Light", "A_Other")
        result = orchestrator.accept_quarantine(self.wave.id, ["A_Light"])
        self.assertTrue(result.ok, result.text)
        self.assertNotIn(ownership.QUARANTINE, ids[0])
        self.assertNotIn(ownership.QUARANTINE, ids[-1])  # The object's data goes with it.
        self.assertIn(ownership.QUARANTINE, ids[1])
        self.assert_resolved_receipt("partly merged: + A_Light; missing: held back "
                                     "(quarantined, waiting for the user's accept): A_Other", ok=False)
        card = next(i for i in self.session["items"] if i["kind"] == "merge")
        self.assertEqual(card["quarantined"], ["A_Other"])
        self.assertEqual(card["resolved"], "")
        self.assertTrue(orchestrator.accept_quarantine(self.wave.id, ["A_Other"]).ok)
        self.assert_resolved_receipt("completed and merged", ok=True)
        self.assertEqual(self.crew[0].row["merged_changes"], ["+ A_Light", "+ A_Other"])
        self.assertEqual(card["resolved"], "accepted")

    def test_accept_after_turn_ends_persists_reconciled_job_log(self):
        self.held_results("A_Light", "A_Other")
        self.session["turn"] = agent.Turn()
        self.assertIsNone(self.session["turn"].job)
        path = self.job.folder / "job.json"
        before = json.loads(path.read_text())
        other_worker = before["waves"][0]["workers"][1]
        self.assertIn("held back", before["waves"][0]["workers"][0]["delivery"])

        for names, expected in ((["A_Light"], "partly merged: + A_Light; missing: held back "
                                  "(quarantined, waiting for the user's accept): A_Other"),
                                (None, "completed and merged")):
            with self.subTest(names=names):
                result = orchestrator.accept_quarantine(self.wave.id, names)
                self.assertTrue(result.ok, result.text)
                on_disk = json.loads(path.read_text())
                worker = on_disk["waves"][0]["workers"][0]
                self.assertEqual(on_disk, self.session["jobs"][0])
                self.assertEqual(worker["delivery"], expected)
                self.assertTrue(worker["merged"])
                self.assertIn("+ A_Light", worker["merged_changes"])
                self.assertEqual(on_disk["waves"][0]["workers"][1], other_worker)
        self.assertEqual(worker["merged_changes"], ["+ A_Light", "+ A_Other"])

    def test_failed_or_unmatched_accept_does_not_clear_held_names(self):
        self.held_results("A_Light")
        before = json.loads(json.dumps(self.wave.item))
        self.assertTrue(orchestrator.accept_quarantine(self.wave.id, ["missing"]).ok)
        self.assertEqual(self.wave.item, before)
        with mock.patch.object(merge, "_accepted_home", side_effect=RuntimeError("cannot link")):
            self.assertFalse(orchestrator.accept_quarantine(self.wave.id).ok)
        self.assertEqual(self.wave.item, before)
        card = next(i for i in self.session["items"] if i["kind"] == "merge")
        self.assertEqual(card["resolved"], "")

    def test_accept_preserves_conflicts_errors_and_other_waves(self):
        self.held_results("A_Light")
        row = self.crew[0].row
        row.update(conflicts=["scene.camera"], merge_error="a: target did not arrive")
        other = state.wave_row("a", "Other", "modeling")
        other.update(status="done", quarantined=["A_Light"])
        other_card = state.item_wave("another-wave", 99, [other])
        self.session["items"].insert(0, other_card)
        self.assertTrue(orchestrator.accept_quarantine(self.wave.id).ok)
        self.assertEqual(other["quarantined"], ["A_Light"])
        self.assertFalse(other["merged"])
        self.assert_resolved_receipt("partly merged: + A_Light; missing: conflicted: scene.camera; "
                                     "a: target did not arrive", ok=False)

    def test_conflict_only_is_not_completion_in_receipts_and_saved_cards(self):
        result = self.collect_merge_receipt({
            "conflicts": [{"worker": "a", "pointer": "scene.camera", "reason": "live value changed"}],
            "created": [{"worker": "b", "name": "B_Part", "id_type": "OBJECT"}]})
        self.assert_delivery_receipt(result, "conflicted (nothing merged): scene.camera")
        self.assertEqual(state.worker_outcome(self.crew[1].row), "completed and merged")

    def test_partial_delivery_keeps_errors_alongside_what_merged(self):
        error = "a: scene.camera target A_Camera did not arrive"
        result = self.collect_merge_receipt({
            "created": [{"worker": "a", "name": "A_Part", "id_type": "OBJECT"},
                        {"worker": "b", "name": "B_Part", "id_type": "OBJECT"}],
            "errors": [error]})
        self.assert_delivery_receipt(result, "partly merged: + A_Part; missing: " + error, merged=True)
        self.assertIn(error, self.crew[0].row["merge_error"])
        self.assertNotIn("merge_error", self.crew[1].row)
        self.assertEqual(state.worker_outcome(self.crew[1].row), "completed and merged")
        self.assertFalse(orchestrator.delegate(wait=True).ok)

    def test_partial_delivery_names_quarantine_and_conflicts_as_missing(self):
        result = self.collect_merge_receipt({
            "created": [{"worker": "a", "name": "A_Part", "id_type": "OBJECT"}],
            "quarantined": [{"worker": "a", "name": "A_Light", "id_type": "OBJECT"}],
            "conflicts": [{"worker": "a", "keys": ["OBJECT:A_Camera"], "reason": "live value changed"}]})
        self.assert_delivery_receipt(
            result, "partly merged: + A_Part; missing: held back (quarantined, waiting for the user's accept): "
                    "A_Light; conflicted: OBJECT:A_Camera", merged=True)

    def failed_merge_receipt(self, *, push=True, undo=False):
        """Exercise merge_wave's real exception path after an object reached the scene."""
        self.outcome_runtime()
        self.wave.merge.side_effect = merge.merge_wave
        self.wave.spawn.uid2spawn = lambda: {}
        self.wave.spawn.path = self.wave.folder / "spawn.blend"
        scene = []

        def apply(*args):
            scene.append("A_Part")
            raise RuntimeError("pointer update failed")

        def rollback(op, message=""):
            if op == "push":
                return push
            if undo:
                scene.clear()
            return undo

        with mock.patch.object(agent, "run_loop"), mock.patch.object(merge, "_undo", side_effect=rollback), \
                mock.patch.object(merge, "_live_pointers", return_value={}), \
                mock.patch.object(merge, "_merge_worker", side_effect=apply), mock.patch.object(merge, "_cleanup"):
            result = waves.run_wave(self.wave, lambda: False, lambda fn: fn())
            self.crew += self.wave.crew
        self.assertEqual(scene, [] if undo else ["A_Part"])
        return result

    def assert_unconfirmed_merge(self, result):
        expected = "merge failed; what reached the scene is unconfirmed"
        for receipt in (result, orchestrator.delegate(wait=True)):
            self.assertIn(expected, receipt.text)
            self.assertNotIn("nothing merged", receipt.text)
            self.assertNotIn("The live scene is as before", receipt.text)
        merge_card = next(i for i in self.session["items"] if i["kind"] == "merge")
        self.assertIn(expected, merge_card["text"])
        self.assert_delivery_receipt(result, state.worker_outcome(self.crew[0].row))

    def test_failed_merge_without_undo_never_claims_nothing_merged(self):
        self.assert_unconfirmed_merge(self.failed_merge_receipt(push=False))

    def test_failed_merge_with_failed_undo_never_claims_nothing_merged(self):
        self.assert_unconfirmed_merge(self.failed_merge_receipt())

    def test_failed_merge_with_confirmed_rollback_can_say_nothing_merged(self):
        result = self.failed_merge_receipt(undo=True)
        for text in (result.text, orchestrator.delegate(wait=True).text):
            self.assertIn("failed (nothing merged)", text)
            self.assertNotIn("what reached the scene is unconfirmed", text)
        self.assert_delivery_receipt(result, state.worker_outcome(self.crew[0].row))

    def test_stop_after_workers_finish_but_before_collection_discards_their_results(self):
        self.outcome_runtime()
        with mock.patch.object(agent, "run_loop"):
            waves.start(self.wave, lambda: False, lambda fn: fn())
            self.crew += self.wave.crew
            for worker in self.crew:
                self.assertTrue(worker.ready.wait(2))
                worker.thread.join(2)
                self.assertEqual(worker.status, "done")
            self.stop_workers()
        result = orchestrator.delegate(wait=True)
        self.assertEqual(result.text.count("stopped before finishing (nothing merged)"), 2)
        self.wave.merge.assert_not_called()

    def test_unavailable_boot_and_queued_worker_have_explicit_outcomes(self):
        from loopcut.worker_host import WorkerUnavailable
        self.outcome_runtime()
        self.job.max_workers = 1
        self.wave.make_runtime = mock.Mock(side_effect=WorkerUnavailable("cannot connect"))
        with mock.patch.object(agent, "_request") as requested:
            result = waves.run_wave(self.wave, lambda: False, lambda fn: fn())
            self.crew += self.wave.crew
        requested.assert_not_called()
        self.wave.merge.assert_not_called()
        for worker in self.crew:
            self.assertEqual(state.worker_outcome(worker.row), "unavailable (nothing merged)")
        for text in (result.text, orchestrator.delegate(wait=True).text):
            self.assertIn("[a]", text)
            self.assertIn("[b]", text)
            self.assertIn("unavailable (nothing merged)", text)
            self.assertNotIn("every worker has reported", text.lower())

    def test_missing_result_file_is_a_failed_worker_even_when_sibling_merged(self):
        self.outcome_runtime()
        self.wave.merge.side_effect = None
        self.wave.merge.return_value = {
            "created": [{"worker": "a", "name": "A_Part", "id_type": "OBJECT"}],
            "errors": ["b: no result file, nothing merged"]}
        with mock.patch.object(agent, "run_loop"):
            result = waves.run_wave(self.wave, lambda: False, lambda fn: fn())
            self.crew += self.wave.crew
        self.assertFalse(result.ok)
        self.assertTrue(self.crew[0].row["merged"])
        self.assertFalse(self.crew[1].row["merged"])
        for text in (result.text, orchestrator.delegate(wait=True).text):
            self.assertIn("failed (nothing merged): b: no result file", text)
            self.assertIn("completed and merged", text)
            self.assertIn("A_Part", text)
            self.assertNotIn("every worker has reported", text.lower())

    def test_prepare_links_the_running_attempt_and_failed_boot_does_not_mark_it_started(self):
        from loopcut.worker_host import WorkerUnavailable
        card = state.item_tool("delegate", "Start helpers", "")
        self.session["items"].append(card)
        later = self.prepare()
        self.assertIs(later.plan_card, card)
        worker = self.worker(wave=later)
        runtime = mock.Mock()
        runtime.start.side_effect = WorkerUnavailable("cannot connect")
        later.make_runtime = lambda wave, worker: runtime
        with mock.patch.object(agent, "_request") as requested:
            worker._work()
        requested.assert_not_called()
        self.assertNotIn("plan_started", card)
        self.assertEqual(worker.status, "failed")

    def test_blocking_startup_keeps_steering_available_and_notes_precede_first_requests(self):
        registry = Registry()
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()

        def runtime(wave, worker):
            child = FakeRuntime(registry, worker)

            def start():
                FakeRuntime.start(child)
                entered[worker.index].set()
                if not release.wait(3):
                    raise AssertionError("test did not release startup")
            child.start = start
            return child

        def request(session, *args):
            self.assertEqual(self.steers(session), [self.worker_steer("smaller")])
            self.assertTrue(self.wave.plan_card["plan_started"])
            self.assertEqual(self.wave.plan_card["status"], "running")
            return llm.Completion(tool_calls=[llm.ToolCall("r1", "report_back", '{"summary":"Built it."}')])

        self.wave.make_runtime = runtime
        self.wave.plan_card = {"status": "running"}
        sent = []
        sender = threading.Thread(target=lambda: sent.append(agent.send("smaller")), daemon=True)
        with mock.patch.object(agent, "_request", side_effect=request) as requested:
            try:
                waves.start(self.wave, self.turn.cancel.is_set, lambda fn: fn())
                self.assertNotIn("plan_started", self.wave.plan_card)
                self.crew += self.wave.crew
                self.assertTrue(all(event.wait(2) for event in entered), "both children prewarm concurrently")
                requested.assert_not_called()
                sender.start()
                sender.join(1)
                self.assertFalse(sender.is_alive(), "startup must not hold job.lock against steering")
                self.assertEqual(sent, [True])
                requested.assert_not_called()
                release.set()
                for worker in self.crew:
                    worker.thread.join(2)
                    self.assertFalse(worker.thread.is_alive())
                    self.assertEqual(worker.status, "done")
                    self.assertEqual(worker.steer_index, 1)
                self.assertEqual(requested.call_count, 2)
                self.assertEqual([text for text, _ in self.turn.notes], ["smaller"])
            finally:
                release.set()
                if sender.ident is not None:
                    sender.join(2)
                self.stop_workers()

    def test_send_now_during_startup_waits_for_cleanup_before_resending(self):
        registry = Registry()
        entered, cleaning, release = threading.Event(), threading.Event(), threading.Event()

        def runtime(wave, worker):
            child = FakeRuntime(registry, worker)

            def start():
                FakeRuntime.start(child)
                entered.set()
                if not worker.turn.cancel.wait(3):
                    raise AssertionError("test did not cancel startup")
                cleaning.set()
                if not release.wait(3):
                    raise AssertionError("test did not release startup cleanup")
                raise llm.Cancelled()
            child.start = start
            return child

        self.wave.make_runtime = runtime
        self.turn.config = self.turn.roles.orchestrator
        spec = agent.LoopSpec(role="orchestrator", system=lambda session: "", schemas=lambda: [],
                              save=lambda session: None, asks=False, checkpoints=False, changes_card=False,
                              scene_changed=lambda result: False, on_end=waves.end_turn)
        parent = threading.Thread(target=agent.run_loop,
                                  args=(self.session, self.turn, spec,
                                        lambda *args: waves.run_wave(self.wave, self.turn.cancel.is_set, lambda fn: fn())),
                                  daemon=True)
        with mock.patch.object(agent, "_request", return_value=llm.Completion(
                tool_calls=[llm.ToolCall("d1", "delegate", "{}")])) as requested, \
                mock.patch.object(agent, "_resend_on_main") as resent, \
                mock.patch.object(agent, "_restore_input_on_main") as restored:
            parent.start()
            try:
                self.assertTrue(entered.wait(2))
                self.crew += self.wave.crew
                self.assertTrue(agent.send("smaller"))
                note = self.turn.notes[-1][1]
                self.assertTrue(agent.send_queued_now())
                self.assertTrue(cleaning.wait(2))
                parent.join(0.2)
                self.assertTrue(parent.is_alive(), "send now must wait for startup to unwind")
                resent.assert_not_called()
                release.set()
                parent.join(2)
                self.assertFalse(parent.is_alive())
                self.assertEqual(self.turn.outcome, "cancelled")
                requested.assert_called_once()  # Only the orchestrator's delegate; no worker model spend.
                resent.assert_called_once_with("smaller", [note])
                restored.assert_not_called()
                self.assertTrue(all(w.finished is not None and w.status == "stopped" for w in self.crew))
                self.assertFalse(self.session.get("workers_unavailable"))
            finally:
                self.turn.cancel.set()
                release.set()
                parent.join(2)
                self.stop_workers()

    def test_failed_boot_keeps_healthy_worker_steerable_and_wait_only_until_collected(self):
        from loopcut.worker_host import WorkerUnavailable
        registry = Registry()
        entered, release = threading.Event(), threading.Event()

        def runtime(wave, worker):
            child = FakeRuntime(registry, worker)

            def start():
                FakeRuntime.start(child)
                if worker.index == 0:
                    if not entered.wait(2):
                        raise AssertionError("healthy sibling did not start")
                    raise WorkerUnavailable("cannot connect")
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test did not release healthy startup")
            child.start = start
            return child

        def request(session, *args):
            self.assertIs(session, healthy.session)
            self.assertEqual(self.steers(session), [self.worker_steer("smaller")])
            return llm.Completion(tool_calls=[llm.ToolCall("r1", "report_back", '{"summary":"Built it."}')])

        spec = types.SimpleNamespace(schemas=lambda: orchestrator.SCHEMAS)
        self.wave.make_runtime = runtime
        with mock.patch.object(agent, "_request", side_effect=request) as requested:
            try:
                waves.start(self.wave, self.turn.cancel.is_set, lambda fn: fn())
                self.crew += self.wave.crew
                failed, healthy = self.crew
                self.assertTrue(entered.wait(2))
                failed.thread.join(2)
                self.assertFalse(failed.thread.is_alive())
                self.assertEqual(failed.status, "failed")
                self.assertEqual(failed.retries, 0)
                self.assertTrue(self.session["workers_unavailable"])
                failed.reported = True  # The first delegate result has already reported this failure.
                self.assertIn(orchestrator.WAIT_SCHEMA, agent._session_schemas(self.session, spec))
                self.assertIn("Still running: b", waves.unavailable_note(self.session))
                requested.assert_not_called()
                self.assertTrue(agent.send("smaller"))
                release.set()
                healthy.thread.join(2)
                self.assertFalse(healthy.thread.is_alive())
                self.assertEqual(healthy.status, "done")
                requested.assert_called_once()
                self.assertEqual(failed.steer_index, 0)
                self.assertEqual(healthy.steer_index, 1)
                self.assertEqual([text for text, _ in self.turn.notes], ["smaller"])
                self.assertIn(orchestrator.WAIT_SCHEMA, agent._session_schemas(self.session, spec))
                self.assertIn("Ready to collect: b", waves.unavailable_note(self.session))

                def report(job, batch, *args):
                    self.assertIs(job, self.job)
                    self.assertEqual(batch, [healthy])
                    healthy.reported = True
                    return tools.ToolResult("Healthy result merged.")

                waiting = orchestrator.delegate(wait=True)
                with mock.patch.object(waves, "report", side_effect=report):
                    self.assertEqual(waiting.then(lambda: False, lambda fn: fn()).text, "Healthy result merged.")
                self.assertNotIn("delegate", {s["function"]["name"] for s in agent._session_schemas(self.session, spec)})
            finally:
                release.set()
                self.stop_workers()

    def test_worker_finishing_its_outstanding_reply_does_not_consume_a_late_correction(self):
        worker = self.worker()

        def request(session, turn, cfg, spec):
            self.assertEqual(worker.steps, 1)
            self.assertTrue(agent.send("Make A smaller"))
            self.assertEqual(self.steers(session), [])
            return llm.Completion(tool_calls=[llm.ToolCall("r1", "report_back", '{"summary":"A built."}')])

        with mock.patch.object(agent, "_resend_on_main") as resent, \
                mock.patch.object(agent, "_restore_input_on_main") as restored:
            self.run_worker(worker, request)
        self.assertEqual(worker.steps, 1, "the final reply needs no further request")
        self.assertEqual(worker.status, "done")
        self.assertIsNotNone(worker.finished)
        self.assertEqual(worker.steer_index, 0)
        self.assertEqual(self.steers(worker.session), [])
        self.assertEqual([text for text, _ in self.job.steers], ["Make A smaller"])
        self.assertFalse(any("step" in r for r in self.turn.notes[0][1].get("helper_deliveries", [])))
        self.assertEqual([text for text, _ in self.turn.notes], ["Make A smaller"])
        self.assertTrue(self.turn.notes[0][1]["queued"])
        resent.assert_not_called()
        restored.assert_not_called()

    def test_targeted_correction_keeps_both_deliverables_scoped_and_reports_the_unrelated_request(self):
        first, other = self.worker(0), self.worker(1)
        self.assertTrue(agent.send("Make A smaller"))

        def request(session, turn, cfg, spec):
            # A scripted model exercises the delivery contract and tool/report paths, not language understanding.
            note, = self.steers(session)
            self.assertIn("Reconsider requirements that conflict", note)
            self.assertIn("Change only what clearly applies to your deliverable", note)
            self.assertIn("Report unclear or unrelated requests to the orchestrator", note)
            self.assertIn("Your ownership and merge restrictions still apply", note)
            self.assertIn("Make A smaller", note)
            if session is first.session:
                self.assertIn("deliverable: A", session["messages"][0]["content"])
                if first.steps == 1:
                    return llm.Completion(tool_calls=[llm.ToolCall("c1", "run_python",
                                          '{"code":"resize own object", "summary":"Make A smaller"}')])
                report = {"summary": "A made smaller."}
            else:
                self.assertIn("deliverable: B", session["messages"][0]["content"])
                report = {"summary": "B unchanged; the correction names A.", "question": "Please route this to A."}
            return llm.Completion(tool_calls=[llm.ToolCall("r1", "report_back", json.dumps(report))])

        built = self.run_worker(first, request)
        unchanged = self.run_worker(other, request)
        self.assertEqual(built.scene, {"A_Part": "a"})
        self.assertEqual(unchanged.scene, {})
        self.assertEqual((first.status, other.status), ("done", "question"))
        self.assertEqual(first.row["summary"], "A made smaller.")
        self.assertIn("B unchanged", other.row["summary"])
        self.assertEqual(other.report["question"], "Please route this to A.")
        self.assertEqual(len(self.steers(first.session)), 1)
        self.assertEqual(len(self.steers(other.session)), 1)
        self.assertEqual(len(self.turn.notes), 1, "the parent still reviews the correction and both reports")

    def test_cancelled_worker_never_restores_its_copy_of_a_note_into_the_users_input(self):
        worker = self.worker()
        agent.send("smaller")
        worker.before_request(1)
        worker.turn.cancel.set()
        with mock.patch.object(agent, "_restore_input_on_main") as restored, \
                mock.patch.object(agent, "_resend_on_main") as resent:
            agent.run_loop(worker.session, worker.turn, worker.spec)
        self.assertEqual(worker.turn.outcome, "cancelled")
        restored.assert_not_called()
        resent.assert_not_called()
        self.assertEqual(len(self.turn.notes), 1, "the orchestrator still owns delivery")

    def test_steps_wait_for_a_reply_and_latest_action_uses_the_tool_summary(self):
        from loopcut.ui import layout
        worker = self.worker()
        worker.set_status("working")
        self.assertFalse(layout._waiting_first_reply(worker.row))
        worker.before_request(1)
        self.assertEqual(worker.row["steps"], 0)
        self.assertTrue(layout._waiting_first_reply(worker.row))
        self.assertNotIn("0 steps", layout._worker_detail(worker.row))
        worker.on_usage({})
        self.assertEqual(worker.row["steps"], 1)
        self.assertFalse(layout._waiting_first_reply(worker.row))
        worker.ready.set()
        worker.runtime = mock.Mock()
        worker.runtime.run_tool.return_value = tools.ToolResult("changed")
        worker.run_tool("run_python", json.dumps({"code": "pass", "summary": "Adjust the shape"}))
        self.assertEqual(worker.row["summary"], "Adjust the shape")
        worker.run_tool("capture_viewport", "{}")
        self.assertEqual(worker.row["summary"], "Checking the model’s appearance")
        self.assertNotIn("role", worker.row["summary"])
        worker.before_request(2)
        self.assertEqual(worker.row["steps"], 1, "the next pending request has not progressed yet")
        self.assertFalse(layout._waiting_first_reply(worker.row))

    def test_latest_stored_scene_preview_is_kept_without_another_capture(self):
        worker = self.worker()
        for name, path, expected in (("capture_viewport", "/saved/first.png", "/saved/first.png"),
                                     ("run_python", "/saved/second.png", "/saved/second.png"),
                                     ("look_at_reference", "/saved/reference.png", "/saved/second.png")):
            worker.session["items"].append({"kind": "tool", "name": name, "image_path": path})
            self.assertFalse(worker.after_step(worker.session, worker.turn))
            self.assertEqual(worker.row["image_path"], expected)
        worker.session["items"].append({"kind": "tool", "name": "capture_viewport", "output": "No image"})
        worker.report = {"summary": "Done"}
        self.assertTrue(worker.after_step(worker.session, worker.turn))
        self.assertEqual(worker.row["image_path"], "/saved/second.png")

    def test_crash_restart_clears_preview_until_the_new_attempt_captures(self):
        worker = self.worker()
        registry = Registry()
        previews = []

        def runtime(wave, worker):
            previews.append(worker.row.get("image_path"))
            return FakeRuntime(registry, worker)

        def run_loop(session, turn, spec, run_tool, *args):
            if not worker.retries:
                session["items"].append({"kind": "tool", "name": "capture_viewport",
                                          "image_path": "/saved/discarded.png"})
                self.assertFalse(worker.after_step(session, turn))
                worker._crashed("run_python", "child exited")
            else:
                session["items"].append({"kind": "tool", "name": "run_python", "output": "Changed"})
                self.assertFalse(worker.after_step(session, turn))
                previews.append(worker.row.get("image_path"))
                session["items"].append({"kind": "tool", "name": "capture_viewport",
                                          "image_path": "/saved/restarted.png"})
                worker.after_step(session, turn)
                previews.append(worker.row.get("image_path"))

        self.wave.make_runtime = runtime
        with mock.patch.object(agent, "run_loop", side_effect=run_loop), mock.patch.object(worker, "_end"):
            worker._work()
        self.assertEqual(worker.retries, 1)
        self.assertEqual(previews, [None, None, None, "/saved/restarted.png"])
        self.assertTrue(any(i.get("image_path") == "/saved/discarded.png" for i in worker.session["items"]),
                        "keep the old attempt in history without reusing its preview")

    def test_preview_update_errors_do_not_hide_crashes_or_reports(self):
        class UnwritablePreview(dict):
            def __setitem__(self, key, value):
                if key == "image_path":
                    raise RuntimeError("preview unavailable")
                super().__setitem__(key, value)

        worker = self.worker()
        worker.row = UnwritablePreview()
        worker.session["items"].append({"name": "capture_viewport", "image_path": "/saved/preview.png"})
        self.assertFalse(worker.after_step(worker.session, worker.turn))
        worker._crashed("run_python", "child exited")
        self.assertTrue(worker.after_step(worker.session, worker.turn))
        worker.crashed = ""
        worker.report = {"summary": "Done"}
        self.assertTrue(worker.after_step(worker.session, worker.turn))

    def test_stop_prevents_delivery_and_elapsed_time_is_kept_when_the_worker_ends(self):
        worker = self.worker()
        clock_time = [50]
        runtime = mock.Mock()
        runtime.kill.side_effect = lambda: clock_time.__setitem__(0, 75)
        worker.wave.make_runtime = mock.Mock(return_value=runtime)
        worker.started = 10
        with mock.patch.object(worker, "_end"), mock.patch.object(agent, "run_loop"), \
                mock.patch.object(self.job.budget, "clock", side_effect=lambda: clock_time[0]):
            worker.report = {"summary": "Finished the shape."}
            worker._work()
        self.assertEqual(worker.row["elapsed"], 65, "the saved duration includes child cleanup")
        self.assertEqual(worker.row["summary"], "Finished the shape.")
        conversations.save(self.session)
        loaded = conversations.load(self.session["id"])
        from loopcut.ui import layout
        for now in (999, 9999):
            self.assertEqual(layout._worker_detail(worker.row, now), "1m 05s · modeling · 0 steps")
            self.assertEqual(layout._worker_detail(loaded["items"][1]["rows"][0], now),
                             "1m 05s · modeling · 0 steps")
        self.assertEqual(loaded["items"][1]["rows"][0]["summary"], "Finished the shape.")
        waves.stop(self.job)
        waves.steer(self.job, "smaller")
        self.assertEqual(self.job.steers, [])


class WaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        from prompt_index_fixture import index_patch
        patcher = index_patch()
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": self.data.name,
                                               "LOOPCUT_SERVICE_URLS": f"http://127.0.0.1:{self.server.server_port}"})
        patcher.start()
        self.addCleanup(patcher.stop)
        MODELS.scripts.clear()
        MODELS.arrivals.clear()
        MODELS.delays.clear()
        MODELS.bodies.clear()
        wave_checkpoint = mock.patch.object(checkpoints, "ensure_for_wave", create=True,  # W2's; merge calls it.
                                            side_effect=lambda session, label, message_index, item_index: "cp")
        wave_checkpoint.start()
        self.addCleanup(wave_checkpoint.stop)
        owns = mock.patch.object(ownership, "expand_owns",  # Reads bpy: the owned names as object keys.
                                 lambda owns, aspects: {f"OBJECT:{name}" for name in owns})
        owns.start()
        self.addCleanup(owns.stop)
        self.registry = Registry()
        self.merges, self.checkpoint_calls, self.kept = [], [], []
        self.quarantined = []
        self.crashes: dict[str, list[str]] = {}  # worker -> crash tool per attempt
        self.child_checks: dict[str, list] = {}
        self.facts: dict[str, dict] = {}  # worker -> what its child's finish measured (facts.deliverables)
        self.looks: dict[str, str] = {}   # worker -> its contact sheet cell (worker_capture.cell)
        self.missing: dict[str, list] = {}  # worker -> its deliverable, when its child found none at report time

    def roles(self, **kw) -> config.Roles:
        cfg = config.Config(api_key="k", base_url=f"http://127.0.0.1:{self.server.server_port}", model="glm-5.3-flash",
                            reasoning_effort="low", auto_run=True, auto_look=False, max_steps=0, context_budget=40_000)
        return config.Roles(orchestrator=cfg, worker=cfg, orchestrate=True, **kw)

    def runtime(self, wave, worker):
        attempts = self.crashes.get(worker.id, [])
        crash = attempts.pop(0) if attempts else ""
        return FakeRuntime(self.registry, worker, crash, self.child_checks.get(worker.id), self.facts.get(worker.id),
                           self.looks.get(worker.id), self.missing.get(worker.id))

    def merge(self, spawn, results, checkpoint, keep_spawn=False):
        self.checkpoint_calls.append(checkpoint())
        self.merges.append([r["brief"]["id"] for r in results])
        self.shared = [r.get("shared") for r in results]  # The notes may name a collection (merge._asked).
        self.kept.append(keep_spawn)
        return types.SimpleNamespace(created=[{"worker": r["brief"]["id"], "name": r["brief"]["prefix"],
                                               "id_type": "OBJECT"} for r in results], replaced=[], renamed=[],
                                     quarantined=self.quarantined, dropped_edits=[], conflicts=[], errors=[],
                                     checkpoint="cp-1", seconds=0.2)

    def wave(self, briefs: list[dict], keep=None, references=None, **roles) -> waves.Wave:
        """references: {worker: [names]}; every name is attached first, as attachments.add stores them."""
        self.session = state.new_session()
        for name in dict.fromkeys(n for names in (references or {}).values() for n in names):
            self.attach(name)
        self.session["turn_number"] = 1
        self.session["usage_by_role"]["orchestrator"] = {"input": 5000, "cached": 0, "output": 100, "requests": 2}
        for key in ("input", "cached", "output"):
            self.session["usage"][key] += self.session["usage_by_role"]["orchestrator"][key]
        self.session["items"].append(state.item_user("a chair and a table, keep the bottle"))
        self.turn = agent.Turn()
        self.turn.roles = self.roles(**roles)
        workers = [{"id": b, "deliverable": b.capitalize(), "role": "modeling", "prefix": b.capitalize(),
                    "requirements": [{"quote": "a chair", "do": "x"}],
                    "checks": [{"kind": "exists", "name": b.capitalize(), "quote": "a chair"}]} for b in briefs]
        for worker in workers:
            worker["references"] = (references or {}).get(worker["id"], [])
        plan = orchestrator.normalise_wave({"shared": {"keep": keep or {}}, "workers": workers})
        spawn = types.SimpleNamespace(wave="w1-test", path=Path(self.data.name) / "spawn.blend",
                                      tokens={"OBJECT:Bottle": "t-bottle", "OBJECT:Rug": "t-rug"})
        wave = waves.prepare(self.session, self.turn, plan, message_index=3, save_spawn=lambda folder: spawn,
                             scene_context=lambda text: "<scene_context>\nfile: test\n</scene_context>")
        wave.make_runtime, wave.merge = self.runtime, self.merge
        wave.run_checks = lambda checks, spawn: [{"status": "pass", "detail": "measured"} for _ in checks]
        wave.snapshot, wave.token_of = (lambda: {"after": True}), (lambda name: f"token-{name}")
        wave.scene_before = {"before": True}
        return wave

    def attach(self, name: str) -> str:
        """An image the user attached: stored in their conversation, listed in their references."""
        source = Path(self.data.name) / f"source-{name}"
        source.write_bytes(b"\x89PNG " + name.encode())
        ref = conversations.store_image(self.session, source)
        self.session["references"].append({"ref": ref, "full": ref, "name": name, "pinned": True})
        return ref

    def run_wave(self, wave, cancel: threading.Event | None = None):
        cancel = cancel or threading.Event()
        return waves.run_wave(wave, cancel.is_set, lambda fn: fn())

    def more(self, briefs: list[dict], message_index: int = 9, keep=None, tokens=None) -> waves.Wave:
        """Another wave of the same turn, as a later delegate call makes it."""
        plan = orchestrator.normalise_wave({"shared": {"keep": keep or {}}, "workers": briefs})
        spawn = types.SimpleNamespace(wave=f"w{self.session['wave_count'] + 1}-test", tokens=tokens or {},
                                      path=Path(self.data.name) / f"spawn{message_index}.blend")
        wave = waves.prepare(self.session, self.turn, plan, message_index=message_index, save_spawn=lambda folder: spawn,
                             scene_context=lambda text: "<scene_context>\nfile: test\n</scene_context>")
        wave.make_runtime, wave.merge = self.runtime, self.merge
        wave.run_checks = lambda checks, spawn: [{"status": "pass", "detail": "measured"} for _ in checks]
        wave.snapshot, wave.token_of = (lambda: {"after": True}), (lambda name: f"token-{name}")
        return wave

    def collect(self, job, cancel: threading.Event | None = None):
        """delegate wait: the next results."""
        return waves.collect(job, (cancel or threading.Event()).is_set, lambda fn: fn())

    def rows(self, wave) -> dict:
        return {row["id"]: row["status"] for row in wave.item["rows"]}

    def logged(self) -> dict:
        return {w["id"]: w for w in self.session["jobs"][-1]["waves"][-1]["workers"]}

    # ---------------------------------------------------------------- tests

    def test_a_wave_merges_what_its_workers_built_and_reports_compactly(self):
        self.quarantined = ["LIGHT:A_Test /a"]
        MODELS.scripts.update(a=[build(), finish("Chair built.", change_requests=[
            {"target": "Rug", "change": "raise 2 cm", "reason": "legs sink"}])], b=[build(), finish("Table built.")])
        with mock.patch.object(checkpoints, "ensure_for_wave", create=True,
                               side_effect=lambda session, label, message_index, item_index:
                               f"{label}|{message_index}|{item_index}"):
            wave = self.wave(["a", "b"])
            result = self.run_wave(wave)
        self.assertFalse(result.ok, "a's extra light still needs the user's accept")
        self.assertEqual(self.merges, [["a", "b"]])
        self.assertEqual(self.checkpoint_calls, ["Before wave 1|3|1"], "the wave card's index, the delegate message's")
        self.assertEqual(self.rows(wave), {"a": "done", "b": "done"})
        self.assertEqual((result.scene_before, result.scene_after), ({"before": True}, {"after": True}))
        self.assertIn("[a] A · modeling · done · 2 steps · checks 1/1", result.text)
        self.assertIn("asks: Rug: raise 2 cm (legs sink)", result.text)
        self.assertIn("Live checks after the merge: 2 of 2 pass.", result.text)
        merge = self.session["items"][-1]
        self.assertEqual((merge["kind"], merge["checkpoint"]), ("merge", "cp-1"))
        self.assertEqual(merge["lines"], ["+ A", "+ B", "! LIGHT:A_Test /a (kept aside)"])
        created = [{"worker": "a", "name": "Chair", "id_type": "OBJECT"}, {"worker": "a", "name": "Chair", "id_type": "MESH"},
                   {"worker": "a", "name": "Chair_Fabric", "id_type": "MATERIAL"}]
        self.assertEqual(waves.card_lines(types.SimpleNamespace(created=created, quarantined=[
            {"worker": "a", "name": "Chair_Test", "id_type": "OBJECT"}])),
            ["+ Chair", "+ Chair_Fabric (material)", "! Chair_Test (kept aside)"], "an object's own mesh goes unsaid")
        self.assertEqual(sorted(self.registry.started), ["a", "b"], "children prewarm on their own threads, in any order")
        self.assertEqual(self.registry.live, 0, "every child is closed")
        log = self.session["jobs"][-1]
        self.assertEqual((log["turn"], log["job"]), (1, "1-1"))
        self.assertTrue((Path(log["folder"]) / "job.json").is_file())
        worker = self.logged()["a"]
        self.assertEqual(worker["cr_targets_token_at_report"], {"Rug": "token-Rug"})
        self.assertEqual(worker["usage"], {"input": 2000, "cached": 800, "output": 100, "requests": 2})
        self.assertAlmostEqual(worker["usd"], (1200 * 0.15 + 800 * 0.03 + 100 * 0.5) / 1e6)
        self.assertEqual(worker["peak_rss_mb"], 300)
        for row in wave.item["rows"]:
            row["elapsed"] = 65.6  # A fixed saved duration; the fake requests' runtime varies between runs.
        from loopcut.ui import layout  # The panel draws the cards the wave made, as they are.
        display = layout.build(self.session, 420, 2000, 1.0, lambda font, size, text: len(text) * size * 0.6, "fast",
                               None, {"selection": [], "context_budget": 24_000, "account": None})
        drawn = {p["id"]: p.get("text") for p in display["prims"]}
        self.assertEqual((drawn["item1.wave.row0.status"], drawn["item1.wave.row1.detail"]),
                         ("partly merged", "1m 06s · modeling · 2 steps · checks 1/1"))
        self.assertEqual(drawn["item1.wave.row1.summary.l0"], "Table built.")
        conversations.save(self.session)
        loaded = conversations.load(self.session["id"])
        for session in (self.session, loaded):
            redrawn = layout.build(session, 420, 2000, 1.0, lambda font, size, text: len(text) * size * 0.6,
                                    "fast", None, {"now": time.time() + 9999})
            text = {p["id"]: p.get("text") for p in redrawn["prims"]}
            self.assertEqual(text["item1.wave.row0.status"], "partly merged")
            self.assertEqual(text["item1.wave.row1.detail"], "1m 06s · modeling · 2 steps · checks 1/1")
            self.assertEqual(text["item1.wave.row1.summary.l0"], "Table built.")
        self.assertEqual(drawn["item2.merge.l2.l0"], "LIGHT:A_Test /a (kept aside)")
        self.assertIn("item2.merge.accept", {h["id"] for h in display["hits"]})

    # ---------------------------------------------------------------- results as workers finish

    def test_delegate_returns_once_a_worker_finishes_and_says_who_still_runs(self):
        """Eval 20260929-130617: the potions worker finished at 3 min, then sat idle 19 min for its wave."""
        MODELS.scripts.update(a=[build(), finish("Chair built.")], b=[build(), finish("Table built.")])
        MODELS.delays[("b", 1)] = 1.0
        wave = self.wave(["a", "b"])
        with mock.patch.object(waves, "BATCH_SECONDS", 0.2):
            first = self.run_wave(wave)
            self.assertEqual(self.merges, [["a"]])
            self.assertEqual((self.rows(wave)["a"], wave.item["rows"][0]["merged"]), ("done", True))
            self.assertIn(self.rows(wave)["b"], ("starting", "working"))
            self.assertIn("Wave 1 (w1-test), 0 s in: a finished.", first.text)
            self.assertIn("[a] A · modeling · done", first.text)
            self.assertNotIn("[b]", first.text)
            self.assertRegex(first.text, r"Still running: b \(\d+ steps?, wave 1\)\. Review the results above now and "
                                         r"send fixes back with continues; delegate wait true returns the next results\.")
            rest = self.collect(wave.job)
        self.assertEqual(self.merges, [["a"], ["b"]])
        self.assertEqual(self.kept, [True, False], "the wave's copy stays while b may still need it")
        self.assertEqual(self.checkpoint_calls, ["cp", "cp"], "both merges name the checkpoint taken at the start")
        self.assertIn("s in: b finished.", rest.text)
        self.assertIn("[b] B · modeling · done", rest.text)
        self.assertIn("Every worker has reported.", rest.text)
        self.assertEqual(len([i for i in self.session["items"] if i["kind"] == "merge"]), 2, "a merge card each")
        log = self.session["jobs"][-1]
        entry = log["waves"][-1]
        self.assertEqual([m["workers"] for m in entry["merges"]], [["a"], ["b"]])
        self.assertEqual((entry["merge"]["created"], entry["merge"]["seconds"]), (2, 0.4), "one entry, both merges")
        self.assertEqual([w["id"] for w in entry["workers"]], ["a", "b"], "worker 1 first, as the wave has them")
        self.assertLess(entry["workers"][0]["t"]["reported"], entry["workers"][1]["t"]["end"], "a reported while b ran")
        self.assertGreater(log["waited"], 0)
        repeated = self.collect(wave.job).text
        self.assertIn("Nothing to wait for: every worker has reported.", repeated)
        self.assertEqual(repeated.count("completed and merged"), 2)

    def test_workers_finishing_about_together_come_back_together(self):
        MODELS.scripts.update(a=[build(), finish()], b=[build(), finish()])
        MODELS.delays[("b", 1)] = 0.3
        wave = self.wave(["a", "b"])
        with mock.patch.object(waves, "BATCH_SECONDS", 3.0):
            result = self.run_wave(wave)
        self.assertEqual(self.merges, [["a", "b"]])
        self.assertEqual(self.shared, [wave.plan["shared"]] * 2, "the merge reads the shared notes too")
        self.assertRegex(result.text, r"Wave 1 \(w1-test\) finished in \d+ s\.")
        self.assertEqual(self.kept, [False])

    def test_a_fix_goes_back_to_a_finished_worker_while_another_still_runs(self):
        MODELS.scripts.update(a=[build(), finish("Chair built.")], b=[build(), finish("Table built.")])
        MODELS.delays[("b", 1)] = 1.5
        wave = self.wave(["a", "b"], max_workers=2)
        with mock.patch.object(waves, "BATCH_SECONDS", 0.2):
            self.run_wave(wave)
            MODELS.scripts["a"] = [build(), finish("Arms rolled.")]
            again = self.more([{"id": "a", "deliverable": "A", "role": "modeling", "prefix": "A", "owns": ["A"],
                                "continues": "a", "style": "roll the arms", "requirements": [{"quote": "a chair", "do": "x"}],
                                "checks": [{"kind": "exists", "name": "A", "quote": "a chair"}]}])
            fixed = waves.run_wave(again, threading.Event().is_set, lambda fn: fn(), lead="Wave 2 started: A.")
            self.assertEqual(self.merges, [["a"], ["a"]], "the fix merged while b still built")
            self.assertTrue(fixed.text.startswith("Wave 2 started: A.\nStatus comes from"), fixed.text[:80])
            self.assertIn("Wave 2 (w2-test) finished in", fixed.text)
            self.assertIn("  said: Arms rolled.", fixed.text)
            self.assertIn("Still running: b", fixed.text)
            rest = self.collect(wave.job)
        self.assertEqual(self.merges, [["a"], ["a"], ["b"]])
        self.assertIn("[b] B · modeling · done", rest.text)
        self.assertLessEqual(self.registry.peak, 2, "the fix waited for a free slot like any brief")
        self.assertEqual(self.session["worker_threads"]["a"]["wave"], 2)
        self.assertEqual([len(w["workers"]) for w in self.session["jobs"][-1]["waves"]], [2, 1])

    def test_a_check_naming_a_running_workers_object_waits_for_its_merge(self):
        MODELS.scripts.update(a=[build(), finish("Lamp built.")], b=[build(), finish("Table built.")])
        MODELS.delays[("b", 1)] = 1.0
        self.child_checks["a"] = [{"id": "c1", "status": "pass", "detail": "ok"},
                                  {"id": "c2", "status": "deferred", "detail": "B is not in this copy"}]
        wave = self.wave(["a", "b"])
        wave.plan["workers"][0]["checks"].append({"kind": "contact", "a": "A", "relation": "rests_on", "b": "B",
                                                  "quote": "a chair", "id": "c2"})
        ran = []
        wave.run_checks = lambda checks, spawn: ran.append([c["id"] for c in checks]) or [
            {"status": "pass", "detail": "measured"} for _ in checks]
        with mock.patch.object(waves, "BATCH_SECONDS", 0.2):
            first = self.run_wave(wave)
            rest = self.collect(wave.job)
        self.assertIn("Live checks after the merge: 1 of 1 pass.", first.text)
        self.assertIn("Checks that wait for b to merge: a c2.", first.text)
        self.assertIn("Live checks after the merge: 2 of 2 pass.", rest.text)
        self.assertEqual(ran, [["c1"], ["c2", "c1"]], "a's c2 ran once B was in the scene")

    def test_a_turn_that_ended_on_an_error_still_merges_its_workers(self):
        """end_turn (LoopSpec.on_end): the orchestrator's request failed while b still built."""
        MODELS.scripts.update(a=[build(), finish()], b=[build(), finish()])
        MODELS.delays[("b", 1)] = 0.5
        wave = self.wave(["a", "b"])
        with mock.patch.object(waves, "BATCH_SECONDS", 0.1):
            self.run_wave(wave)
            waves.end_turn(self.session, self.turn)
        self.assertEqual(self.merges, [["a"], ["b"]])
        self.assertIn("see each worker's outcome in its wave card", self.session["items"][-1]["text"])
        self.assertIn("did not review them", self.session["items"][-1]["text"])
        self.assertEqual((self.turn.scene_before, self.turn.scene_after), ({"after": True}, {"after": True}))

    def test_the_seconds_budget_is_each_worker_runs_own(self):
        budget = waves.JobBudget(usd=0, seconds=5, clock=lambda: 10)
        self.assertEqual(budget.spent(9), "")
        self.assertEqual(budget.spent(4), "its 5 s ran out")
        self.assertEqual(budget.spent(), "", "a queued worker has all its seconds still")

    def test_stop_after_an_early_return_stops_the_rest_and_leaves_no_copies(self):
        MODELS.scripts.update(a=[build(), finish()], b=[build("slow")])
        wave = self.wave(["a", "b"])
        (wave.folder / "spawn.blend").write_bytes(b"BLENDER")
        with mock.patch.object(waves, "BATCH_SECONDS", 0.2):
            self.run_wave(wave)
        self.assertTrue((wave.folder / "spawn.blend").is_file(), "b may still need the copy")
        self.turn.cancel.set()
        started = time.monotonic()
        waves.end_turn(self.session, self.turn)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.rows(wave), {"a": "done", "b": "stopped"})
        self.assertEqual((self.merges, self.registry.live), ([["a"]], 0))
        self.assertIn("b", self.registry.kills)
        self.assertEqual(sorted(p.name for p in wave.folder.rglob("*.blend*")), [], "no copy is left behind")
        self.assertTrue(self.session["jobs"][-1]["waves"][-1]["cancelled"])
        self.assertEqual([w["status"] for w in self.session["jobs"][-1]["waves"][-1]["workers"]], ["done", "stopped"])

    def test_every_worker_reads_the_users_own_messages(self):
        MODELS.scripts.update(a=[finish()])
        wave = self.wave(["a"])
        self.assertIn('- "a chair and a table, keep the bottle"', wave.openings["a"].split("</shared>")[0])
        self.run_wave(wave)
        first = next(m["content"] for m in MODELS.bodies[0][1]["messages"] if m["role"] == "user")
        self.assertIn("the user's words", first)

    def test_a_brief_sent_back_to_a_worker_continues_its_conversation(self):
        """Audit §5: a correction could only go to a fresh worker with no memory of the first try."""
        MODELS.scripts.update(a=[build(), finish("Chair built.")])
        first = self.wave(["a"])
        self.run_wave(first)
        thread = self.session["worker_threads"]["a"]
        saved = json.loads((first.folder / "a" / "conversation.json").read_text())
        for messages in (saved['messages'], thread['messages']):
            self.assertIn('<loopcut-skills ids="', messages[0]['content'])
            self.assertFalse(any(m["role"] == "system" for m in messages))
        self.assertEqual((thread["deliverable"], thread["prefix"], thread["wave"], thread["created"]),
                         ("A", "A", 1, ["A"]), "the continuation records what actually merged")
        (Path(thread["folder"]) / "images").mkdir(parents=True, exist_ok=True)
        (Path(thread["folder"]) / "images" / "look.png").write_bytes(b"png")
        earlier = len(thread["messages"])

        MODELS.scripts["a"] = [build(), finish("Arms rolled.")]
        brief = {"id": "a", "deliverable": "A", "role": "modeling", "prefix": "A", "owns": ["A"], "continues": "a",
                 "style": "roll the arms", "requirements": [{"quote": "a chair", "do": "x"}],
                 "checks": [{"kind": "exists", "name": "A", "quote": "a chair"}]}
        plan = orchestrator.normalise_wave({"shared": {}, "workers": [brief]})
        spawn = types.SimpleNamespace(wave="w2-test", path=Path(self.data.name) / "spawn2.blend", tokens={})
        again = waves.prepare(self.session, self.turn, plan, message_index=9, save_spawn=lambda folder: spawn,
                              scene_context=lambda text: "<scene_context>\nfile: test\n</scene_context>")
        again.make_runtime, again.merge = self.runtime, self.merge
        again.run_checks = lambda checks, spawn: [{"status": "pass", "detail": "measured"} for _ in checks]
        again.snapshot, again.token_of = (lambda: {"after": True}), (lambda name: f"token-{name}")
        result = self.run_wave(again)
        self.assertIn("[a] A · modeling · done", result.text)
        request = [body for who, body in MODELS.bodies if who == "a"][2]  # The first of the second wave.
        users = [m["content"] for m in request["messages"] if m["role"] == "user" and isinstance(m["content"], str)]
        self.assertTrue(users[0].startswith("<shared>"), "the earlier brief is still its first message")
        self.assertTrue(users[-1].startswith(workers.CONTINUE))
        self.assertIn("roll the arms", users[-1])
        self.assertNotIn("<role_notes>", users[-1], "same role: its notes are already in the conversation")
        self.assertEqual([m["role"] for m in request["messages"]], ["user", "assistant", "user"],
                         "the earlier conversation folded into its record, as any earlier turn is (context.py)")
        self.assertIn('report_back({"summary":"Chair built."})', request["messages"][1]["content"])
        self.assertGreater(earlier, 3)
        now = self.session["worker_threads"]["a"]
        self.assertEqual(now["wave"], 2)
        self.assertTrue((Path(now["folder"]) / "images" / "look.png").is_file(), "its earlier captures came along")

    def test_a_brief_sent_back_to_a_worker_whose_work_never_merged_says_so(self):
        """The TreasureChest retry: its first run crashed twice and nothing merged, yet the re-brief said
        its earlier work was in the scene; it reported a chest after one step that built nothing."""
        MODELS.scripts.update(a=[build(), build()])
        self.crashes["a"] = ["run_python", "run_python"]
        self.run_wave(self.wave(["a"]))
        thread = self.session["worker_threads"]["a"]
        self.assertEqual((thread["status"], thread["merged"]), ("failed", False))

        MODELS.scripts["a"] = [build(), finish("A rebuilt.")]
        brief = {"id": "a_fix1", "deliverable": "A", "role": "modeling", "prefix": "A", "continues": "a",
                 "requirements": [{"quote": "a chair", "do": "x"}],
                 "checks": [{"kind": "exists", "name": "A", "quote": "a chair"}]}
        again = self.more([brief])
        opening = again.openings["a_fix1"]
        self.assertTrue(opening.startswith("[Loopcut] More work on your deliverable. Nothing you built earlier reached "
                                           "the user's scene (your earlier work ended failed and was not merged)"),
                        opening[:200])
        self.assertNotIn("was merged into the user's scene", opening)
        self.run_wave(again)
        self.assertTrue(self.session["worker_threads"]["a_fix1"]["merged"])

    def test_the_report_names_what_came_in_as_it_is_called_live(self):
        """The merge renames what a worker left unprefixed (Leg -> A_Leg); the orchestrator reads the live names."""
        MODELS.scripts.update(a=[build(), finish()])
        wave = self.wave(["a"])
        wave.merge = lambda spawn, results, checkpoint, keep_spawn=False: types.SimpleNamespace(
            created=[{"worker": "a", "name": "A", "id_type": "OBJECT"}, {"worker": "a", "name": "A", "id_type": "MESH"},
                     {"worker": "a", "name": "A_Leg", "id_type": "OBJECT"}, {"worker": "a", "name": "A_Mat", "id_type": "MATERIAL"}],
            replaced=[], renamed=["a: OBJECT Leg -> A_Leg"], quarantined=[], dropped_edits=[], conflicts=[], errors=[],
            checkpoint="cp", seconds=0.1)
        result = self.run_wave(wave)
        self.assertIn("  created: A, A_Leg, MAT A_Mat", result.text)

    def test_the_merge_line_counts_objects_and_the_rest_apart(self):
        """"Merge: 14 created" for 7 props: their meshes counted too. The user counts objects."""
        MODELS.scripts.update(a=[build(), finish()])
        wave = self.wave(["a"])
        wave.merge = lambda spawn, results, checkpoint, keep_spawn=False: types.SimpleNamespace(
            created=[{"worker": "a", "name": "A", "id_type": "OBJECT"}, {"worker": "a", "name": "A", "id_type": "MESH"},
                     {"worker": "a", "name": "A_Leg", "id_type": "OBJECT"}, {"worker": "a", "name": "A_Leg", "id_type": "MESH"},
                     {"worker": "a", "name": "A_Mat", "id_type": "MATERIAL"}],
            replaced=[], renamed=[], quarantined=[], dropped_edits=[], conflicts=[], errors=[], checkpoint="cp", seconds=0.1)
        result = self.run_wave(wave)
        self.assertIn("Merge: 2 objects and 3 other datablocks created, 0 replaced.", result.text)
        self.assertEqual(merge.created_text(["OBJECT:A"]), "1 object created")
        self.assertEqual(self.session["jobs"][-1]["waves"][-1]["merge"]["objects"], 2)

    def test_a_material_made_again_as_the_live_one_is_said_in_the_merge_line_and_card(self):
        """The palette's safety net (merge._reuse_live_materials): the orchestrator and the user read that
        the worker's copy was not merged and which live material its objects wear instead."""
        MODELS.scripts.update(a=[build(), finish()])
        wave = self.wave(["a"])
        wave.merge = lambda spawn, results, checkpoint, keep_spawn=False: types.SimpleNamespace(
            created=[{"worker": "a", "name": "A", "id_type": "OBJECT"}], replaced=[], renamed=[],
            reused=[{"worker": "a", "name": "Pal_Glass", "made": "Pal_Glass.001", "id_type": "MATERIAL"}],
            quarantined=[], dropped_edits=[], conflicts=[], errors=[], checkpoint="cp", seconds=0.1)
        result = self.run_wave(wave)
        self.assertIn("Made again by a worker, so the live one is used instead of a copy: Pal_Glass (a).", result.text)
        self.assertIn("~ Pal_Glass (material) (made again: the live one used)", self.session["items"][-1]["lines"])

    def test_an_object_hanging_on_what_was_kept_aside_says_so(self):
        """perfume_ad: Lit_Spot merged, its parent Lit_Spot_root quarantined; the orchestrator went
        looking for where the spot really was. The card and the merge line say why."""
        MODELS.scripts.update(a=[build(), finish()])
        wave = self.wave(["a"])
        wave.merge = lambda spawn, results, checkpoint, keep_spawn=False: types.SimpleNamespace(
            created=[{"worker": "a", "name": "Lit_Spot", "id_type": "OBJECT", "held": ["parent Lit_Spot_root"]},
                     {"worker": "a", "name": "Lit_Spot_root", "id_type": "OBJECT"}],
            replaced=[{"worker": "a", "key": "OBJECT:Light", "name": "Light", "held": ["Track To target Lit_Aim"]}],
            renamed=[], quarantined=[{"worker": "a", "name": "Lit_Spot_root", "id_type": "OBJECT"},
                                     {"worker": "a", "name": "Lit_Aim", "id_type": "OBJECT"}],
            dropped_edits=[], conflicts=[], errors=[], checkpoint="cp", seconds=0.1)
        result = self.run_wave(wave)
        self.assertIn("Merged objects that hang on them: Lit_Spot (parent Lit_Spot_root), "
                      "Light (Track To target Lit_Aim).", result.text)
        self.assertEqual(self.session["items"][-1]["lines"][:3], [
            "+ Lit_Spot (parent Lit_Spot_root held back)", "+ Lit_Spot_root", "~ Light (Track To target Lit_Aim held back)"])

    def test_later_workers_wait_for_the_first_workers_first_reply(self):
        """The spawn rule: a prefix is cached only once a request with it has completed."""
        MODELS.scripts.update(a=[build(), finish()], b=[build(), finish()], c=[build(), finish()])
        MODELS.delays[("a", 0)] = 0.4
        wave = self.wave(["a", "b", "c"])
        self.run_wave(wave)
        first_reply = wave.started + self.logged()["a"]["t"]["first_reply"]
        arrivals = {}
        for worker, at in MODELS.arrivals:
            arrivals.setdefault(worker, at)
        self.assertLess(arrivals["a"], first_reply)
        for later in ("b", "c"):
            self.assertGreaterEqual(arrivals[later], first_reply - 0.005, f"{later} asked before a's first reply")
        self.assertEqual(sorted(self.registry.started), ["a", "b", "c"], "every child prewarms at once")

    def test_no_more_than_max_workers_run_at_once(self):
        MODELS.scripts.update(a=[build(), finish()], b=[build(), finish()], c=[build(), finish()])
        MODELS.delays.update({("a", 1): 0.3, ("b", 1): 0.3})
        wave = self.wave(["a", "b", "c"], max_workers=2)
        self.run_wave(wave)
        self.assertEqual(self.registry.peak, 2)
        self.assertEqual(self.rows(wave), {"a": "done", "b": "done", "c": "done"})

    def test_the_report_says_who_is_done_running_and_queued_and_why_they_wait(self):
        """GPT-6.1 Sol's wish (20260929-230207): queued, running and done, and the reason each waits."""
        MODELS.scripts.update(a=[build(), finish()], b=[build(), finish()], c=[build(), finish()])
        MODELS.delays[("b", 1)] = 1.0
        wave = self.wave(["a", "b", "c"], max_workers=1)
        with mock.patch.object(waves, "BATCH_SECONDS", 0.2):
            first = self.run_wave(wave)
            self.assertIn("Done: a. Still running: b (", first.text)
            self.assertIn("Queued: c (wave 1), waiting for a free slot (1 run at once); each starts, in that order, as "
                          "a running worker finishes. Review the results above now", first.text)
            self.assertEqual({k: v["state"] for k, v in waves.busy(wave.job).items()}, {"b": "running", "c": "queued"})
            while waves.pending(wave.job):
                last = self.collect(wave.job)
        self.assertIn("Every worker has reported.", last.text)
        self.assertEqual(self.rows(wave), {"a": "done", "b": "done", "c": "done"})

    def test_a_worker_passes_35_steps_and_reports_back_without_a_cap(self):
        MODELS.scripts.update(a=[build()] * 36 + [finish()])
        wave = self.wave(["a"])
        # Even an explicit cap for the main loop cannot bound an orchestrated worker.
        wave.turn.roles = dataclasses.replace(wave.turn.roles, worker=dataclasses.replace(
            wave.turn.roles.worker, max_steps=1))
        result = self.run_wave(wave)
        worker, = wave.job.crew
        self.assertEqual((worker.turn.config.max_steps, worker.turn.outcome), (0, "completed"))
        self.assertEqual(worker.report["summary"], "Built it.")
        self.assertEqual((worker.steps, worker.row["steps"], self.logged()["a"]["steps"]), (37, 37, 37))
        self.assertNotIn("cap", worker.row)
        self.assertFalse(self.logged()["a"]["cap_hit"])
        self.assertEqual(self.logged()["a"]["unfinished"], [])
        self.assertIn("[a] A · modeling · done · 37 steps", result.text)
        self.assertIn("Job budget used: 37 worker steps, $", result.text)
        self.assertFalse(any("Stopped" in i.get("text", "") for i in worker.session["items"]))

    def costly_script(self, summary="Built most of it; the legs are not bevelled."):
        # $0.225 per reply: the default $0.40 is exhausted after two regular requests.
        with mock.patch.dict(USAGE["usage"], {"prompt_tokens": 1_000_000, "completion_tokens": 150_000,
                                             "prompt_tokens_details": {"cached_tokens": 0}}):
            return [build(), build(), text(summary)]

    def assert_budget_stop(self, wave, result, reason, steps):
        worker, = wave.job.crew
        self.assertEqual(worker.turn.outcome, "budget")
        self.assertEqual(worker.turn.stop_reason, reason)
        self.assertIn("Stopped by the budget: " + reason, [i.get("text") for i in worker.session["items"]])
        self.assertIn(agent.BUDGET_PROMPT.format(reason=reason),
                      [m.get("content") for m in MODELS.bodies[-1][1]["messages"]])
        self.assertIn(f"[a] A · modeling · partial · {steps} steps · checks 1/1 · stopped: {reason}", result.text)
        self.assertIn("open when stopped (no report_back): nothing its checks see, but it did not say it was done",
                      result.text)
        self.assertIn(f"report: Loopcut's report: stopped before report_back ({reason}); checks 1 of 1 pass", result.text)
        self.assertIn("Its last words: Built most of it; the legs are not bevelled.", result.text)
        self.assertTrue(self.logged()["a"]["cap_hit"])
        self.assertEqual(self.logged()["a"]["outcome"], "budget")
        self.assertEqual(self.merges, [["a"]], "partial work still merges")
        self.assertEqual(self.registry.live, 0)

    def test_dollar_exhaustion_wraps_up_and_reports_partial_work_even_when_checks_pass(self):
        MODELS.scripts.update(a=self.costly_script())
        wave = self.wave(["a"])
        result = self.run_wave(wave)
        self.assert_budget_stop(wave, result, "the job's $0.40 is used", 2)
        self.assertEqual(self.logged()["a"]["usage"]["requests"], 3, "two steps plus the text-only wrap-up")
        self.assertAlmostEqual(wave.job.budget.usd, 0.675, msg="the wrap-up is still charged")
        self.assertIn("Job budget used: 2 worker steps, $0.675/$0.40.", result.text)

    def test_time_exhaustion_uses_an_injected_clock_and_reports_partial_work(self):
        MODELS.scripts.update(a=[build()] * 3 + [text("Built most of it; the legs are not bevelled.")])
        wave = self.wave(["a"], job_max_usd=0)
        now = [0.0]
        wave.job.budget.clock = lambda: now[0]
        run_tool = FakeRuntime.run_tool

        def timed(runtime, name, arguments, is_cancelled):
            result = run_tool(runtime, name, arguments, is_cancelled)
            if name == "run_python":
                now[0] += 600
            return result

        with mock.patch.object(FakeRuntime, "run_tool", timed):
            result = self.run_wave(wave)
        self.assert_budget_stop(wave, result, "its 1800 s ran out", 3)
        self.assertIn("Job budget used: 3 worker steps.", result.text)
        self.assert_budget_warning_once(wave, first_request=2)

    def assert_budget_warning_once(self, wave, first_request):
        note = agent.WORKER_STEPS_LEFT_NOTE.format(left=3).strip()
        worker, = wave.job.crew
        messages = worker.session["messages"]
        self.assertEqual(sum(m.get("content") == note for m in messages), 1)
        warning, = [m for m in messages if m.get("content") == note]
        self.assertTrue(warning[context.STEP_LIMIT], "a continuation, not a new turn that folds the brief")
        bodies = [b for who, b in MODELS.bodies if who == "a"]
        for i, body in enumerate(bodies, 1):
            self.assertEqual(sum(m.get("content") == note for m in body["messages"]), int(i >= first_request))
        self.assertIn("3 steps left", note, "today's server-held prompt still recognises the warning")
        self.assertIn("call check_brief, then report_back", note)

    def test_the_dollar_steps_left_notice_is_appended_once_before_the_next_request(self):
        # Cost $0.000127 per request; after step 2 the $0.00063 leaves just under 3 steps.
        MODELS.scripts.update(a=[build()] * 3 + [finish()])
        wave = self.wave(["a"], job_max_usd=0.00063)
        self.run_wave(wave)
        self.assert_budget_warning_once(wave, first_request=3)
        self.assertEqual(wave.crew[0].turn.outcome, "completed")
        self.assertFalse(self.logged()["a"]["cap_hit"], "the warning itself never stops a worker")

    def test_identical_worker_failures_end_with_the_no_progress_guard_and_a_report(self):
        MODELS.scripts.update(a=[build()] * 8 + [text("The build is blocked.")])
        wave = self.wave(["a"])

        def failed(*args):
            result = tools.ToolResult("Failed", ok=False)
            result.changed = False  # WorkerRuntime carries the child's flag, not scene snapshots.
            return result

        with mock.patch.object(FakeRuntime, "run_tool", side_effect=failed):
            result = self.run_wave(wave)
        worker, = wave.crew
        self.assertEqual((worker.turn.outcome, worker.steps), ("no_progress", 8))
        self.assertIn(agent.NO_PROGRESS_NOTICE, [i.get("text") for i in worker.session["items"]])
        self.assertIn(agent.NO_PROGRESS_PROMPT, [m.get("content") for m in MODELS.bodies[-1][1]["messages"]])
        self.assertTrue(self.logged()["a"]["cap_hit"])
        self.assertEqual(self.rows(wave), {"a": "partial"})
        self.assertIn("stopped: the no-progress guard", result.text)
        self.assertIn("open when stopped (no report_back)", result.text)
        self.assertIn("The build is blocked.", worker.summary())
        self.assertEqual(self.merges, [["a"]])

    def test_a_scene_change_resets_worker_failures(self):
        MODELS.scripts.update(a=[build("fail")] * 7 + [build()] + [build("fail")] * 7 + [finish()])
        wave = self.wave(["a"])
        run_tool = FakeRuntime.run_tool

        def failing(runtime, name, arguments, is_cancelled):
            if name == "run_python" and json.loads(arguments)["code"] == "fail":
                result = tools.ToolResult("Failed", ok=False)
                result.changed = False
                return result
            return run_tool(runtime, name, arguments, is_cancelled)

        with mock.patch.object(FakeRuntime, "run_tool", failing):
            self.run_wave(wave)
        self.assertEqual(wave.crew[0].turn.outcome, "completed")
        self.assertEqual(self.rows(wave), {"a": "done"})
        self.assertFalse(self.logged()["a"]["cap_hit"])

    def test_checks_and_looks_repeated_after_scene_changes_do_not_trip_the_guard(self):
        MODELS.scripts.update(a=[build(), calls(("check_brief", {})), calls(("capture_viewport", {}))] * 12 + [finish()])
        wave = self.wave(["a"])
        self.run_wave(wave)
        self.assertEqual((wave.crew[0].turn.outcome, wave.crew[0].steps), ("completed", 37))
        self.assertEqual(self.rows(wave), {"a": "done"})
        self.assertFalse(any(i.get("status") in ("failed", "rejected") for i in wave.crew[0].session["items"]))

    def test_looping_worker_looks_without_scene_changes_trip_the_guard(self):
        MODELS.scripts.update(a=[calls(("capture_viewport", {}))] * (1 + agent.MAX_IDLE_CAPTURES + 8)
                             + [text("Kept looking without changing anything.")])
        wave = self.wave(["a"])
        self.run_wave(wave)
        self.assertEqual(wave.crew[0].turn.outcome, "no_progress")
        self.assertTrue(self.logged()["a"]["cap_hit"])

    def test_a_budget_stop_lists_what_its_checks_and_brief_leave_open(self):
        MODELS.scripts.update(a=self.costly_script())
        self.child_checks["a"] = [{"id": "c1", "status": "fail", "detail": "A is 12 mm above the rug"}]
        wave = self.wave(["a"])
        wave.plan["workers"][0]["requirements"].append({"quote": "bevelled legs", "do": "bevel the legs 3 mm"})
        result = self.run_wave(wave)
        self.assertIn("open when stopped (no report_back): c1 fails; unchecked: bevel the legs 3 mm", result.text)
        self.assertEqual(self.logged()["a"]["unfinished"], ["c1 fails", "unchecked: bevel the legs 3 mm"])

    def test_a_worker_queued_after_the_budget_is_spent_never_starts(self):
        MODELS.scripts.update(a=self.costly_script(), b=[finish()])
        wave = self.wave(["a", "b"], max_workers=1)
        started = time.monotonic()
        result = self.run_wave(wave)
        self.assertLess(time.monotonic() - started, 5, "no spawn-rule wait for a worker that cannot run")
        self.assertEqual(self.rows(wave), {"a": "partial", "b": "stopped"})
        self.assertEqual(self.registry.started, ["a"])
        self.assertIn("[b] B · modeling · stopped · 0 steps", result.text)
        self.assertIn("not started: the job's $0.40 is used", result.text)

    def test_status_comes_from_checks_never_from_what_the_worker_says(self):
        MODELS.scripts.update(a=[build(), finish("Everything is perfect.")])
        self.child_checks["a"] = [{"id": "c1", "status": "fail", "detail": "A is 12 mm above the rug (fix -0.012 z)"}]
        result = self.run_wave(self.wave(["a"]))
        self.assertIn("[a] A · modeling · partial", result.text)
        self.assertIn("fail c1 exists A: A is 12 mm above the rug (fix -0.012 z)", result.text)

    def test_a_check_on_another_workers_objects_is_deferred_not_failed(self):
        MODELS.scripts.update(a=[build(), finish("Lamp built.")])
        self.child_checks["a"] = [{"id": "c1", "status": "pass", "detail": "ok"},
                                  {"id": "c2", "status": "deferred", "detail": "Armchair is not in this copy (another "
                                   "worker's): runs on the live scene after the merge"}]
        wave = self.wave(["a"])
        wave.plan["workers"][0]["checks"].append({"kind": "no_overlap", "names": ["A", "Armchair"], "quote": "a chair",
                                                  "id": "c2"})
        result = self.run_wave(wave)
        self.assertEqual(self.rows(wave), {"a": "done"}, "a deferred check is not a failing one")
        self.assertIn("[a] A · modeling · done · 2 steps · checks 1/1 · c2 deferred to the live run", result.text)
        self.assertNotIn("fail c2", result.text)
        self.assertEqual(self.logged()["a"]["checks"], {"passed": 1, "failed": 0, "deferred": 1})
        self.assertIn("Live checks after the merge: 2 of 2 pass.", result.text, "the live run measures it")

    def test_a_report_stays_under_its_cap_whatever_the_worker_writes(self):
        huge = {"summary": "word " * 1000, "numbers": {f"Part{i}": {"size": [1.123456] * 3} for i in range(50)},
                "change_requests": [{"target": f"T{i}", "change": "x" * 300, "reason": "y" * 300} for i in range(9)],
                "question": "q" * 900}
        MODELS.scripts.update(a=[build("touch rug"), calls(("report_back", huge))])
        self.child_checks["a"] = [{"id": f"c{i}", "status": "fail", "detail": "d" * 400} for i in range(1, 9)]
        wave = self.wave(["a"])
        wave.plan["workers"][0]["checks"] = [{"kind": "exists", "name": "A", "quote": "a chair", "id": f"c{i}"}
                                             for i in range(1, 9)]
        result = self.run_wave(wave)
        block = result.text.split("\n[a]")[1].split("\nMerge:")[0]
        self.assertLessEqual(len(block) / context.CHARS_PER_TOKEN, waves.REPORT_TOKENS + 10)
        self.assertIn("Rolled back: 1 call(s)", block)
        self.assertEqual(len(self.logged()["a"]["change_requests"]), 3)

    def test_measured_facts_come_before_what_the_worker_says(self):
        """Evidence, not claims (analysis §5.8): the child's facts per deliverable, then its words; the
        numbers it wrote itself are labelled as unmeasured."""
        barrel = {"tris": 1548, "base_tris": 1548, "modifiers": [], "faces": 700, "smooth": 1.0, "non_manifold": 0,
                  "materials": {"Shop_WoodLight": 500, "Shop_Iron": 200}, "size": [0.58, 0.58, 0.86],
                  "bounds": [[-1.34, -0.19, 0], [-0.76, 0.39, 0.86]], "origin": [-1.05, 0.1, 0], "origin_off": 0.0,
                  "planned": {"box": 1, "count": 1, "out": {}}}
        self.facts["a"] = {"Barrel": barrel}
        MODELS.scripts.update(a=[build(), finish("A flat-shaded barrel.", numbers={"Barrel": {"tris": 600}})])
        result = self.run_wave(self.wave(["a"]))
        block = result.text.split("\n[a]")[1].split("\nMerge:")[0]
        self.assertIn("  measured Barrel: 1,548 tris; no modifiers; 100% smooth; 0 non-manifold edges; 0.58 x 0.58 x "
                      "0.86 m; inside its box; origin 0 mm from its base centre; faces Shop_WoodLight 500, Shop_Iron 200",
                      block)
        self.assertLess(block.index("measured Barrel"), block.index("said: A flat-shaded barrel."))
        self.assertIn('its own numbers (unmeasured): {"Barrel":{"tris":600}}', block)
        self.assertIn("  flags (its words vs measured): says flat shading; smooth faces measured: Barrel 100%; says 600 "
                      "triangles; measured 1,548", block)
        self.assertEqual(self.logged()["a"]["flags"], ["says flat shading; smooth faces measured: Barrel 100%",
                                                       "says 600 triangles; measured 1,548"])
        self.assertNotIn("  numbers:", block)
        self.assertEqual(self.logged()["a"]["facts"], {"Barrel": barrel})

    def test_measured_paint_is_one_advisory_line_per_deliverable(self):
        """Paint stats (paint_stats.summary): how the parts read and the worst ones' numbers, never a gate."""
        barrel = {"tris": 1548, "base_tris": 1548, "modifiers": [], "faces": 700, "smooth": 1.0, "non_manifold": 0,
                  "materials": {"Barrel_Amber": 500, "Barrel_Ink": 200},
                  "paint": [{"part": "Barrel_Amber", "px": 0.35, "texels": 75000, "levels": [0.27, 0.56, 0.71],
                             "ratio": 3.0, "patch": 88.0, "noise": 0.44, "noise_size": 0.104, "accents": 0.19,
                             "hue": 45, "accent_dl": 0.37, "accent_chroma": 0.3, "accent_dark": 0.0},
                            {"part": "Barrel_Ink", "px": 0.35, "texels": 9000, "levels": [0.2], "ratio": 1.0,
                             "patch": 200.0, "noise": 0.02, "noise_size": 0.004, "accents": 0.0}]}
        self.facts["a"] = {"Barrel": barrel}
        MODELS.scripts.update(a=[build(), finish("Bold painterly barrel.")])
        result = self.run_wave(self.wave(["a"]))
        block = result.text.split("\n[a]")[1].split("\nMerge:")[0]
        self.assertIn("  measured Barrel paint (2 parts, advisory): noise-like 1, values far apart 1, accents pop 1, "
                      "restrained 1; Barrel_Amber 1:3.0 noise 44% ΔL 0.37 C x0.3", block)

    def test_advisory_misses_are_shown_and_never_decide(self):
        """E2 (owner decision 3): a miss of the brief's own targets is for the orchestrator to read; the
        worker stays done, and neither the live checks nor the finish gate run it."""
        MODELS.scripts.update(a=[build(), finish()])
        self.child_checks["a"] = [
            {"id": "c1", "status": "pass", "detail": "ok"},
            {"id": "a1", "advisory": True, "status": "fail", "title": "flat_shaded A",
             "detail": "62% of 700 faces smooth shaded; flat wants at most 5%"},
            {"id": "a2", "advisory": True, "status": "pass", "title": "no_modifiers A", "detail": "none"}]
        wave = self.wave(["a"])
        wave.plan["workers"][0]["advisory"] = [{"kind": "flat_shaded", "name": "A", "id": "a1"},
                                               {"kind": "no_modifiers", "name": "A", "id": "a2"}]
        result = self.run_wave(wave)
        self.assertEqual(self.rows(wave), {"a": "done"})
        self.assertIn("[a] A · modeling · done · 2 steps · checks 1/1", result.text)
        self.assertIn("  advisory misses: a1 flat_shaded A: 62% of 700 faces smooth shaded; flat wants at most 5%",
                      result.text)
        self.assertNotIn("a2", result.text)
        self.assertIn("Live checks after the merge: 1 of 1 pass.", result.text)
        self.assertEqual([label for label, _ in wave.job.final_checks()], ["a c1"])
        self.assertEqual(self.logged()["a"]["advisory"], {"met": 1, "missed": 1})
        self.assertEqual(self.logged()["a"]["checks"], {"passed": 1, "failed": 0, "deferred": 0})

    def test_one_contact_sheet_of_the_workers_cells_comes_with_the_report(self):
        """E4: one image per wave, a cell per worker that left one, in crew order; no image ids to open."""
        MODELS.scripts.update(a=[build(), finish()], b=[build(), finish()], c=[build(), finish()])
        wave = self.wave(["a", "b", "c"])
        cells = {w: Path(self.data.name) / f"{w}.png" for w in ("a", "c")}
        for path in cells.values():
            path.write_bytes(b"\x89PNG cell")
        self.looks.update({w: str(p) for w, p in cells.items()})
        made = []

        def sheet(paths, name, cell, columns, folder):
            made.append((paths, name, cell, columns, folder))
            out = Path(folder) / name
            out.write_bytes(b"\x89PNG sheet")
            return out
        with mock.patch("loopcut.asset_common.sheet", sheet):
            result = self.run_wave(wave)
        self.assertEqual(made, [([str(cells["a"]), str(cells["c"])], "sheet.png", (384, 288), 2, wave.folder)])
        self.assertEqual(result.image_path, wave.folder / "sheet.png")
        self.assertTrue(result.text.endswith("Image attached: the contact sheet of these deliverables, each as it "
                                             "finished, in the scene's own light (a studio light if it has none), "
                                             "labelled a, c."), result.text[-200:])
        self.assertNotIn(" · image ", result.text)

    def test_a_brief_with_references_shows_its_worker_the_users_images(self):
        """The owner's Arcane stills reached the orchestrator only. A brief's references are attached to its
        worker's first message as the user's own attachments are (768 px copies), with no reference card
        asked for, and the first is the tile its looks show beside the render (worker_capture)."""
        MODELS.scripts.update(a=[build(), finish()], b=[build(), finish()])
        wave = self.wave(["a", "b"], references={"a": ["ref1.jpg", "ref2.jpg"]})
        self.run_wave(wave)
        first = {who: body for who, body in reversed(MODELS.bodies)}  # Each worker's first request.
        content = first["a"]["messages"][0]["content"]
        self.assertEqual([p["type"] for p in content], ["text", "image_url", "image_url"])
        self.assertIn("references (the user's images, attached: the look your style asks for; look_at_reference for "
                      "detail): ref1.jpg, ref2.jpg", content[0]["text"])
        sent = [base64.b64decode(p["image_url"]["url"].split(",", 1)[1]) for p in content[1:]]
        self.assertEqual(sent, [b"\x89PNG ref1.jpg", b"\x89PNG ref2.jpg"])
        self.assertIsInstance(first["b"]["messages"][0]["content"], str, "a brief without references: text only")
        self.assertNotIn(agent.REFERENCE_PROMPT, [b["messages"][0]["content"] for _, b in MODELS.bodies])
        crew = {w.id: w for w in wave.job.crew}
        self.assertEqual(crew["a"].reference["name"], "ref1.jpg")
        self.assertTrue(Path(crew["a"].reference["path"]).is_relative_to(crew["a"].folder))
        self.assertEqual(Path(crew["a"].reference["path"]).read_bytes(), b"\x89PNG ref1.jpg")
        self.assertIsNone(crew["b"].reference)
        self.assertEqual([r["name"] for r in self.session["worker_threads"]["a"]["references"]], ["ref1.jpg", "ref2.jpg"])

    def test_a_brief_sent_back_keeps_its_references_or_changes_them(self):
        """Kept: not sent again, the first brief's images still are. Changed: the new one is attached to the
        new brief and the one no longer named stops being sent."""
        MODELS.scripts.update(a=[build(), finish()])
        self.run_wave(self.wave(["a"], references={"a": ["ref1.jpg"]}))
        self.attach("ref2.jpg")

        def again(names: list[str]) -> dict:
            MODELS.bodies.clear()
            MODELS.scripts["a"] = [finish("Refined.")]
            brief = {"id": "a", "deliverable": "A", "role": "modeling", "prefix": "A", "owns": ["A"], "continues": "a",
                     "requirements": [{"quote": "a chair", "do": "x"}], "references": names,
                     "checks": [{"kind": "exists", "name": "A", "quote": "a chair"}]}
            self.run_wave(self.more([brief]))
            return MODELS.bodies[0][1]

        def images(message: dict) -> list:
            content = message["content"]
            return [p.get("text") or base64.b64decode(p["image_url"]["url"].split(",", 1)[1]) for p in content
                    if isinstance(content, list) and p != content[0]]

        request = again(["ref1.jpg"])
        users = [m for m in request["messages"] if m["role"] == "user"]
        self.assertEqual(images(users[0]), [b"\x89PNG ref1.jpg"], "still sent with the first brief")
        self.assertIsInstance(users[-1]["content"], str, "not attached twice")
        request = again(["ref2.jpg"])
        users = [m for m in request["messages"] if m["role"] == "user"]
        self.assertEqual(images(users[0]), ["[an earlier image, no longer attached]"])
        self.assertEqual(images(users[-1]), [b"\x89PNG ref2.jpg"])
        self.assertEqual([(r["name"], r["pinned"]) for r in self.session["worker_threads"]["a"]["references"]],
                         [("ref1.jpg", False), ("ref2.jpg", True)])

    def test_the_contact_sheet_ends_with_the_reference_its_workers_saw(self):
        MODELS.scripts.update(a=[build(), finish()], b=[build(), finish()])
        wave = self.wave(["a", "b"], references={"b": ["ref1.jpg"]})
        cells = {w: Path(self.data.name) / f"{w}.png" for w in ("a", "b")}
        for path in cells.values():
            path.write_bytes(b"\x89PNG cell")
        self.looks.update({w: str(p) for w, p in cells.items()})
        made, fitted = [], []

        def reference_cell(path, size, out):
            fitted.append((Path(path).read_bytes(), size))
            out.write_bytes(b"\x89PNG reference")
            return out

        def sheet(paths, name, cell, columns, folder):
            made.append((paths, cell, columns))
            return Path(folder) / name
        with mock.patch("loopcut.asset_common.sheet", sheet), \
                mock.patch("loopcut.worker_capture.reference_cell", reference_cell):
            result = self.run_wave(wave)
        (paths, cell, columns), = made
        self.assertEqual(paths[:2], [str(cells["a"]), str(cells["b"])])
        self.assertEqual(Path(paths[2]).read_bytes(), b"\x89PNG reference")
        self.assertEqual((columns, cell), waves.sheet_layout(3))
        self.assertEqual(fitted, [(b"\x89PNG ref1.jpg", cell)])
        self.assertTrue(result.text.endswith("labelled a, b; last, labelled reference, the user's ref1.jpg."),
                        result.text[-160:])

    def test_no_cells_no_image(self):
        MODELS.scripts.update(a=[build(), finish()])
        result = self.run_wave(self.wave(["a"]))
        self.assertIsNone(result.image_path)
        self.assertNotIn("Image attached", result.text)

    def test_the_sheet_stays_within_a_worker_looks_size(self):
        self.assertEqual(waves.sheet_layout(1), (1, (384, 288)))
        self.assertEqual(waves.sheet_layout(2), (2, (384, 288)))
        self.assertEqual(waves.sheet_layout(4), (2, (384, 288)))
        for count in (3, 5, 6):
            columns, (width, height) = waves.sheet_layout(count)
            self.assertEqual(columns, 3)
            self.assertLessEqual(columns * width + 2 * 4, waves.SHEET_SIDE)

    def test_change_requests_are_filed_once_per_job(self):
        request = {"target": "Rug", "change": "Raise it 2 cm.", "reason": "r"}
        MODELS.scripts.update(a=[finish(change_requests=[request])], b=[finish(change_requests=[
            {**request, "change": "raise it 2 cm"}])])
        self.run_wave(self.wave(["a", "b"]))
        self.assertEqual([len(w["change_requests"]) for w in self.logged().values()], [1, 0])

    def test_stop_kills_every_child_and_nothing_merges(self):
        MODELS.scripts.update(a=[build("slow")], b=[build("slow")])
        cancel = threading.Event()
        wave = self.wave(["a", "b"])
        threading.Timer(0.5, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(llm.Cancelled):
            self.run_wave(wave, cancel)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(sorted(set(self.registry.kills)), ["a", "b"])
        self.assertEqual(self.merges, [])
        self.assertEqual(self.rows(wave), {"a": "stopped", "b": "stopped"})
        self.assertTrue(self.session["jobs"][-1]["waves"][-1]["cancelled"])
        self.assertFalse(any(i["kind"] == "merge" for i in self.session["items"]))
        self.assertEqual(sorted(p.name for p in wave.folder.rglob("*.blend*")), [], "no copy is left behind")
        self.assertTrue((wave.folder / "a" / "conversation.json").is_file(), "the transcripts stay")

    def test_stop_right_after_the_copy_leaves_no_temporary_files(self):
        MODELS.scripts.update(a=[build("slow")])
        wave = self.wave(["a"])
        for path in (wave.folder / "spawn.blend", wave.folder / "a" / "result.blend", wave.folder / "a" / "tmp" / "x"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"BLENDER")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(llm.Cancelled):
            self.run_wave(wave, cancel)
        self.assertEqual([p.name for p in wave.folder.rglob("*") if p.is_file() and p.suffix != ".json"], [])

    def test_reap_clears_what_a_dead_blender_left_and_never_touches_a_live_ones(self):
        import subprocess
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        root = waves.jobs_root()

        def job(conversation, name, owner, days_old=0.0):
            folder = root / conversation / name
            (folder / "w1" / "a" / "tmp").mkdir(parents=True)
            for part in ("w1/spawn.blend", "w1/a/result.blend", "w1/a/tmp/t", "w1/a/conversation.json"):
                (folder / part).write_text("x")
            (folder / "job.json").write_text(json.dumps({"owner": owner}))
            stamp = time.time() - days_old * 86400
            os.utime(folder, (stamp, stamp))
            return folder
        dead = job("c1", "1-1", gone.pid)
        old = job("c1", "2-1", gone.pid, days_old=waves.KEEP_DAYS + 1)
        live = job("c2", "1-1", os.getpid(), days_old=waves.KEEP_DAYS + 1)
        waves.reap()
        self.assertEqual(sorted(p.relative_to(dead).as_posix() for p in dead.rglob("*") if p.is_file()),
                         ["job.json", "w1/a/conversation.json"], "a dead Blender's copies go, its logs stay")
        self.assertFalse(old.exists(), f"older than {waves.KEEP_DAYS} days")
        self.assertTrue((live / "w1" / "spawn.blend").is_file(), "a running Blender's job is left alone")

    def test_later_waves_extend_the_jobs_baseline_without_moving_it(self):
        job = waves.Job({"id": "c", "turn_number": 1, "jobs": []}, self.roles())
        job.absorb(types.SimpleNamespace(ids={"OBJECT:Bottle": 1}, tokens={"OBJECT:Bottle": "first"}))
        job.absorb(types.SimpleNamespace(ids={"OBJECT:Bottle": 1, "OBJECT:Box": 2},
                                         tokens={"OBJECT:Bottle": "changed", "OBJECT:Box": "box"}))
        self.assertEqual(job.baseline.tokens, {"OBJECT:Bottle": "first", "OBJECT:Box": "box"})
        self.assertEqual(job.baseline.ids, {"OBJECT:Bottle": 1, "OBJECT:Box": 2})

    def test_a_rows_cancel_stops_that_worker_only(self):
        MODELS.scripts.update(a=[build(), finish()], b=[build("slow")])
        wave = self.wave(["a", "b"])
        threading.Timer(0.5, lambda: waves.cancel_worker("w1-test", "b")).start()
        result = self.run_wave(wave)
        self.assertEqual(self.rows(wave), {"a": "done", "b": "cancelled"})
        self.assertEqual(self.merges, [["a"]])
        self.assertIn("[b] B · modeling · cancelled", result.text)
        self.assertNotIn(("b", "c1"), wave.job.checks, "the finish gate does not ask for what the user cancelled")

    def test_a_crashed_child_is_retried_once_in_the_same_conversation(self):
        MODELS.scripts.update(a=[build(), build(), finish()])
        self.crashes["a"] = ["run_python"]
        wave = self.wave(["a"])
        self.run_wave(wave)
        self.assertEqual(self.rows(wave), {"a": "done"})
        self.assertEqual(self.logged()["a"]["retries"], 1)
        self.assertEqual(self.registry.started, ["a", "a"], "a fresh child from the copy")
        last_request = [m for m in MODELS.arrivals if m[0] == "a"]
        self.assertEqual(len(last_request), 3)

    def test_a_crash_reported_as_a_result_is_retried_too(self):
        MODELS.scripts.update(a=[build(), build(), finish()])
        self.crashes["a"] = ["result:run_python"]
        wave = self.wave(["a"])
        self.run_wave(wave)
        self.assertEqual(self.rows(wave), {"a": "done"})
        self.assertEqual(self.logged()["a"]["retries"], 1)
        self.assertEqual(self.registry.started, ["a", "a"])
        notes = [m["content"] for m in json.loads((wave.folder / "a" / "conversation.json").read_text())["messages"]
                 if m["role"] == "user" and "crashed during run_python" in str(m["content"])]
        self.assertEqual(len(notes), 1, "the same conversation is told once")

    def test_a_child_that_crashes_twice_fails_and_is_not_merged(self):
        MODELS.scripts.update(a=[build(), build()], b=[build(), finish()])
        self.crashes["a"] = ["run_python", "run_python"]
        wave = self.wave(["a", "b"])
        result = self.run_wave(wave)
        self.assertEqual(self.rows(wave), {"a": "failed", "b": "done"})
        self.assertIn("its Blender crashed twice in run_python (no crash report kept): the child exited (signal 11)",
                      result.text)
        self.assertEqual(self.merges, [["b"]])

    def test_where_each_crash_was_kept_is_in_the_status_the_report_and_job_json(self):
        """A crash report is in the child's TMPDIR, deleted with the job's copies: worker_host keeps a
        kit per crash, and the report and job.json say where (the eval copies the job folder)."""
        MODELS.scripts.update(a=[build(), build()])
        self.crashes["a"] = ["kept:result:run_python", "kept:run_python"]
        wave = self.wave(["a"])
        result = self.run_wave(wave)
        note = ("its Blender crashed twice in run_python (crash reports kept at w1/a/crash-1, w1/a/crash-2): "
                "the child exited (signal 11)")
        self.assertIn(f"[a] A · modeling · failed · 2 steps · no checks · {note}", result.text)
        logged = self.logged()["a"]
        self.assertEqual((logged["status"], logged["note"]), ("failed", note))
        self.assertEqual([(c["tool"], c["kept"]) for c in logged["crashes"]],
                         [("run_python", "w1/a/crash-1"), ("run_python", "w1/a/crash-2")])
        on_disk = json.loads((Path(self.session["jobs"][-1]["folder"]) / "job.json").read_text())
        self.assertEqual(on_disk["waves"][-1]["workers"][0]["crashes"], logged["crashes"])
        self.assertTrue((wave.job.folder / "w1" / "a" / "crash-2").is_dir())

    def test_a_worker_that_crashed_once_and_finished_says_where_the_crash_was_kept(self):
        MODELS.scripts.update(a=[build(), build(), finish()])
        self.crashes["a"] = ["kept:result:run_python"]
        wave = self.wave(["a"])
        result = self.run_wave(wave)
        self.assertEqual(self.rows(wave), {"a": "done"})
        self.assertIn("· done · 3 steps · checks 1/1 · its Blender crashed in run_python "
                      "(crash report kept at w1/a/crash-1), restarted", result.text)
        self.assertEqual(self.logged()["a"]["retries"], 1)

    def test_a_deliverable_missing_at_report_time_is_never_done(self):
        """A continued brief that starts from a scene without its earlier work (nothing merged) reported
        a chest after one step with no tool call: its checks may not see it, the facts do."""
        MODELS.scripts.update(a=[finish("Rebuilt A complete: 1566 tris, flat shaded.")])
        self.missing["a"] = ["A"]
        wave = self.wave(["a"])
        result = self.run_wave(wave)
        self.assertEqual(self.rows(wave), {"a": "partial"}, "its checks pass, its report says done: still partial")
        self.assertIn("[a] A · modeling · partial · 1 step · checks 1/1 · deliverable missing: no A in its copy "
                      "at report time", result.text)
        self.assertIn("flags (its words vs measured): deliverable missing: no object named A in its copy", result.text)
        logged = self.logged()["a"]
        self.assertEqual((logged["missing"], logged["flags"][:1]),
                         (["A"], ["deliverable missing: no object named A in its copy at report time"]))
        self.assertEqual(self.merges, [["a"]], "a status, not a gate: what it did make still merges")

    def test_usage_by_role_adds_up_to_the_conversations_usage(self):
        MODELS.scripts.update(a=[build(), finish()], b=[finish()])
        self.run_wave(self.wave(["a", "b"]))
        by_role = self.session["usage_by_role"]
        self.assertEqual(by_role["worker"], {"input": 3000, "cached": 1200, "output": 150, "requests": 3})
        for key in ("input", "cached", "output"):
            self.assertEqual(self.session["usage"][key], sum(bucket[key] for bucket in by_role.values()))

    def test_workers_see_only_their_tools_and_kept_names_become_a_live_check(self):
        MODELS.scripts.update(a=[calls(("delegate", {"wave": {}})), finish()])
        seen, baselines = [], []
        wave = self.wave(["a"], keep={"names": ["Bottle"], "quote": "keep the bottle"})
        wave.run_checks = lambda checks, spawn: seen.extend(checks) or baselines.append(spawn) or [
            {"status": "pass"} for _ in checks]
        self.run_wave(wave)
        transcript = json.loads((wave.folder / "a" / "conversation.json").read_text())
        self.assertIn("delegate is not one of your tools.", [m["content"] for m in transcript["messages"]
                                                              if m["role"] == "tool"])
        keep = seen[-1]
        self.assertEqual((keep["kind"], keep["names"]), ("unchanged", ["Bottle"]))
        self.assertIs(keep["tokens"], wave.spawn.tokens, "the whole spawn's, so the bottle's data and materials count")
        self.assertIs(baselines[-1], wave.job.baseline, "found through the job's baseline")
        self.assertEqual([label for label, _ in wave.job.final_checks()], ["a c1", "keep"])

    def test_preserved_checks_run_after_every_merge(self):
        """A snapshot's comparison after each merge, not only at the finish (GPT-6.1 Sol's wish)."""
        MODELS.scripts.update(a=[build(), finish()])
        wave = self.wave(["a"])
        wave.job.own["o1"] = {"kind": "preserved", "names": ["Bottle"], "snapshot": "s1", "quote": "keep", "id": "o1"}
        wave.job.own["o2"] = {"kind": "file", "path": "out.glb", "quote": "export", "id": "o2"}
        seen = []
        wave.run_checks = lambda checks, spawn: seen.append([c["id"] for c in checks]) or [{"status": "pass"} for _ in checks]
        self.run_wave(wave)
        self.assertEqual(seen[-1], ["c1", "o1"], "a's check, then the preservation; the export waits for the finish")

    def test_the_baseline_follows_what_a_merge_replaced(self):
        """The Arcane look pass (20260929-235231): the material workers owned the seven props, so their
        merges replaced them; the lighting wave's keep then found every prop "gone" by its first-wave
        session_uid, and the orchestrator retired it. The job's baseline follows each merge's
        replacements; the spawn's own map, which the merge itself reads, stays as it was."""
        job = waves.Job({"id": "c", "turn_number": 1, "jobs": []}, self.roles())
        first = types.SimpleNamespace(ids={"OBJECT:Bottle": 1, "MESH:Bottle": 2}, tokens={"OBJECT:Bottle": "first"})
        job.absorb(first)
        job.follow(types.SimpleNamespace(moved={"OBJECT:Bottle": 7, "MESH:Bottle": 8}))
        self.assertEqual(job.baseline.ids, {"OBJECT:Bottle": 7, "MESH:Bottle": 8})
        self.assertEqual(first.ids, {"OBJECT:Bottle": 1, "MESH:Bottle": 2})
        job.absorb(types.SimpleNamespace(ids={"OBJECT:Bottle": 7, "OBJECT:Lamp": 9}, tokens={"OBJECT:Bottle": "painted"}))
        self.assertEqual(job.baseline.ids, {"OBJECT:Bottle": 7, "MESH:Bottle": 8, "OBJECT:Lamp": 9})
        self.assertEqual(job.baseline.tokens, {"OBJECT:Bottle": "first"})
        job.follow(types.SimpleNamespace(wave="no moves"))  # A spawn with nothing replaced.
        self.assertEqual(job.baseline.ids["OBJECT:Bottle"], 7)

    def test_a_keep_from_a_later_wave_compares_with_that_wave(self):
        """The same look pass: waves 1 and 2 painted the props (their owners), then the lighting wave
        kept them. Its keep, and the lighting worker's own unchanged check, compare with the lighting
        wave's spawn, as the worker's copy did, and find the props by the ids the merges left."""
        MODELS.scripts.update(a=[build(), finish()], b=[build(), finish()])
        wave = self.wave(["a"])
        wave.job.baseline.ids = {"OBJECT:Bottle": 1}

        def replacing(spawn, results, checkpoint, keep_spawn=False):
            spawn.moved = {"OBJECT:Bottle": 7}  # Worker a owned the bottle: its merge replaced it.
            return self.merge(spawn, results, checkpoint, keep_spawn)
        wave.merge = replacing
        self.run_wave(wave)
        self.assertEqual(wave.job.baseline.ids, {"OBJECT:Bottle": 7})
        self.assertEqual(wave.job.keep, {}, "wave 1 kept nothing")
        brief = {"id": "b", "deliverable": "B", "role": "lighting", "prefix": "B", "requirements": [{"quote": "a chair", "do": "x"}],
                 "checks": [{"kind": "unchanged", "names": ["Bottle"], "quote": "keep the bottle"}]}
        again = self.more([brief], keep={"names": ["Bottle"], "quote": "keep the bottle"}, tokens={"OBJECT:Bottle": "painted"})
        seen = []
        again.run_checks = lambda checks, spawn: seen.append((checks, spawn)) or [{"status": "pass"} for _ in checks]
        self.run_wave(again)
        checks, spawn = seen[-1]
        self.assertEqual([c["kind"] for c in checks], ["unchanged", "unchanged"], "b's own check, then keep")
        self.assertTrue(all(c["tokens"] is again.spawn.tokens for c in checks), "wave 2's spawn, not wave 1's")
        self.assertEqual(spawn.ids, {"OBJECT:Bottle": 7})
        self.assertNotIn("tokens", brief["checks"][0], "the brief keeps its own check")
        final = dict(again.job.final_checks())
        self.assertEqual(sorted(final), ["a c1", "b c1", "keep"])
        self.assertIs(final["b c1"]["tokens"], again.spawn.tokens)
        third = self.more([{**brief, "id": "c", "prefix": "C", "checks": []}], message_index=11,
                          keep={"names": ["Bottle", "Rug"], "quote": "keep the bottle"}, tokens={"OBJECT:Rug": "rug"})
        self.run_wave(third)
        final = dict(third.job.final_checks())
        self.assertEqual((final["keep"]["names"], final["keep 3"]["names"]), (["Bottle"], ["Rug"]),
                         "a name kept later compares with its own wave; the bottle still with wave 2")
        self.assertIs(final["keep 3"]["tokens"], third.spawn.tokens)

    def test_a_delegated_turn_ends_with_the_verified_block(self):
        """The orchestrator's loop, delegate through tools.execute, the wave, the merge and the finish
        gate, with only Blender, the child (W1) and the merge and checks (W2) faked."""
        self.delegated_turn()

    def test_a_routed_turn_that_tries_to_finish_alone_is_nudged_once_then_delegates(self):
        """The router says level 3; the orchestrator first answers without delegating, is nudged, then
        delegates. The note rides on the user's message, the system prompt is untouched, and the
        router's call is counted and recorded."""
        from loopcut import router
        routed = json.dumps(dict(level=3, deliverables=[dict(name='Armchair', quote='an armchair'),
                                                        dict(name='Lamp', quote='a lamp')], why='two pieces'))
        self.delegated_turn(router_reply=routed, first=text("I put the armchair on the rug myself."))
        session = self.session
        who = [w for w, _ in MODELS.bodies]
        self.assertEqual(who[:3], ["router", "orchestrator", "orchestrator"], who)
        router_body, first, second = (body for _, body in MODELS.bodies[:3])
        self.assertEqual([m["role"] for m in router_body["messages"]], ["user"])
        self.assertNotIn("tools", router_body)
        note = ('<routing>level 3 — deliverables: Armchair ("an armchair"), Lamp ("a lamp"). Delegate these, one '
                'worker each, with a style (look, parts, detail, finish), planned bounds and a dry_run first; do '
                'shared setup (palette, supports, render '
                'and output settings) yourself. Skeleton: '
                'workers: [{"id": "armchair", "deliverable": "Armchair", "role": "modeling", "prefix": "Armchair", '
                '"quote": "an armchair"}, {"id": "lamp", "deliverable": "Lamp", "role": "modeling", '
                '"prefix": "Lamp", "quote": "a lamp"}]</routing>')
        self.assertTrue(first["messages"][0]["content"].endswith(f"Put an armchair on the rug.\n\n{note}"))
        self.assertEqual(first["messages"][0], second["messages"][0], "the first user message stays unchanged")
        self.assertEqual(second["messages"][-1]["content"], "[Loopcut] Routing planned workers for Armchair, Lamp; "
                         "delegate them, or say in one line why you did it yourself.")
        self.assertEqual(session["turn_levels"], [{"turn": 1, "first": 2, "final": 2, "routed": 3, "nudged": True}])
        self.assertEqual(session["usage_by_role"]["router"], {"input": 1000, "cached": 400, "output": 50, "requests": 1})
        self.assertEqual(session["usage_by_role"]["orchestrator"]["requests"], 4)
        self.assertEqual(session["usage"]["input"], sum(u["input"] for u in session["usage_by_role"].values()))

    def test_a_routers_bad_reply_leaves_the_turn_as_it_was(self):
        self.delegated_turn(router_reply=text("Level three, I think."))
        first = next(body for who, body in MODELS.bodies if who == "orchestrator")
        self.assertNotIn("<routing>", first["messages"][0]["content"])
        self.assertEqual(self.session["turn_levels"], [{"turn": 1, "first": 2, "final": 2, "routed": None}])

    @staticmethod
    def brief_on_rug(name: str, x0: float) -> dict:
        return {"id": name.lower(), "deliverable": name, "role": "modeling", "prefix": name,
                "requirements": [{"quote": f"an {name.lower()}", "do": name.lower()}],
                "bounds": [[x0, -0.5, 0.015], [x0 + 0.9, 0.5, 0.9]], "rests_on": "Rug",
                "checks": [{"kind": "contact", "a": name, "relation": "rests_on", "b": "Rug",
                            "quote": f"an {name.lower()} on the rug"}]}

    def test_a_reply_while_a_worker_runs_waits_for_it_and_bounces_once_with_its_result(self):
        """No work is merged unseen: the orchestrator's final reply came while the ottoman still built;
        Loopcut waits, merges it and sends its result back once; the next reply ends the turn."""
        wave = {"shared": {"supports": [{"name": "Rug", "top_z": 0.015}]},
                "workers": [self.brief_on_rug("Armchair", -1.0), self.brief_on_rug("Ottoman", 0.2)]}
        MODELS.delays[("ottoman", 1)] = 1.0
        with mock.patch.object(waves, "BATCH_SECONDS", 0.2):
            session, turn = self.orchestrated_turn(
                "Put an armchair on the rug and an ottoman on the rug.",
                [calls(("delegate", {"wave": wave})), text("Both are on the rug."), text("Checked: both rest on the rug.")],
                armchair=[build(), finish("Armchair built.")], ottoman=[build(), finish("Ottoman built.")])
        self.assertEqual(turn.outcome, "completed")
        self.assertEqual(self.merges, [["armchair"], ["ottoman"]])
        results = [m["content"] for m in session["messages"] if m.get("role") == "tool"]
        self.assertIn("Still running: ottoman", results[0])
        late = [m["content"] for m in session["messages"] if m.get("role") == "user" and isinstance(m.get("content"), str)
                and m["content"].startswith("[Loopcut] You replied while workers were still running")]
        self.assertEqual(len(late), 1, "one bounce for this set of results")
        self.assertIn("[ottoman] Ottoman · modeling · done", late[0])
        self.assertIn("Every worker has reported.", late[0])
        self.assertTrue(session["items"][-1]["text"].endswith(
            "Checked: both rest on the rug.\n\nVerified by Loopcut on this scene: 2 of 2 checks.\n"
            "Requirements (the user's words): 2 of 2 covered."), session["items"][-1]["text"])
        self.assertIn("Loopcut collected their outcomes", " ".join(i["text"] for i in session["items"] if i["kind"] == "notice"))
        self.assertEqual((session["jobs"][0]["bounces"], session["jobs"][0]["late_bounces"]), (0, 1),
                         "the checks passed; the late workers are counted apart")

    def orchestrated_turn(self, user: str, replies: list, router_reply=None, **worker_scripts):
        """One orchestrator turn through agent._run: delegate through tools.execute, the workers, the
        merges and the finish gate, with only Blender, the child (W1) and the merge and checks (W2) faked."""
        MODELS.scripts.update(orchestrator=list(replies), **worker_scripts)
        if router_reply is not None:
            MODELS.scripts["router"] = [router_reply if isinstance(router_reply, bytes) else text(router_reply)]
        live = {"objects": {"Rug": {"type": "MESH", "box": [[-2, -2, 0], [2, 2, 0.015]]}}, "materials": [], "names": ["Rug"]}
        spawn = types.SimpleNamespace(wave="w1-e2e", path=Path(self.data.name) / "spawn.blend", tokens={})
        seen = []
        patches = [mock.patch.object(orchestrator, "live_facts", lambda plan: live),
                   mock.patch.object(orchestrator, "_snapshot", lambda: {"scene": "before"}),
                   mock.patch.object(agent, "_changes_item", lambda session, turn: None),
                   mock.patch.object(waves, "_save_spawn", lambda folder, palette=(): spawn),
                   mock.patch.object(waves, "_scene_context", lambda text: "<scene_context>\nf\n</scene_context>"),
                   mock.patch.object(waves, "_HostRuntime", self.runtime), mock.patch.object(waves, "_merge", self.merge),
                   mock.patch.object(waves, "_run_checks", lambda checks, spawn: seen.append(len(checks)) or
                                     [{"status": "pass", "detail": "gap 0.4 mm"} for _ in checks]),
                   mock.patch.object(waves, "_snapshot", lambda: {"scene": "after"}),
                   mock.patch.object(waves, "_token_of", lambda n: "t")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        session = state.reset()
        session.update(auto_run=True, turn_number=1)
        session["items"].append(state.item_user(user))
        session["messages"].append({"role": "user", "content": f"<scene_context>\nRug\n</scene_context>\n\n{user}"})
        turn = agent.Turn()
        turn.roles = self.roles()
        if router_reply is not None:
            turn.roles = dataclasses.replace(turn.roles, router=dataclasses.replace(turn.roles.worker, model="router"))
        turn.config, turn.spec = turn.roles.orchestrator, agent.ORCH_SPEC
        session["busy"], session["turn"] = True, turn

        def run_tool(name, arguments):
            result = tools.execute(name, arguments)
            return result.then(turn.cancel.is_set, lambda fn: fn()) if result.then else result
        agent._run(session, turn, run_tool, lambda s: None, lambda a, b: False)
        self.session = session
        self.seen = seen
        return session, turn

    def delegated_turn(self, router_reply=None, first=None):
        user = "Put an armchair on the rug."
        wave = {"shared": {"supports": [{"name": "Rug", "top_z": 0.015}]}, "workers": [self.brief_on_rug("Armchair", -0.5)]}
        session, turn = self.orchestrated_turn(
            user, [*([first] if first else []), calls(("delegate", {"wave": wave, "dry_run": True})),
                   calls(("delegate", {"wave": wave})), text("The armchair is on the rug.")],
            router_reply=router_reply, armchair=[build(), finish("Armchair on the rug.")])
        seen = self.seen
        self.assertEqual(turn.outcome, "completed")
        self.assertEqual((turn.scene_before, turn.scene_after), ({"scene": "before"}, {"scene": "after"}),
                         "the turn's changes card covers the merge")
        items = session["items"]
        if first:  # The reply that was nudged: its card stays, before the work.
            self.assertEqual(items[1]["text"], "I put the armchair on the rug myself.")
            items = items[:1] + items[2:]
        kinds = [item["kind"] for item in items]
        self.assertEqual(kinds, ["user", "tool", "tool", "wave", "merge", "assistant"], kinds)
        dry, wave_card = items[1], items[3]
        self.assertTrue(dry["output"].startswith("Dry run, nothing started: a real delegate would start."), dry["output"])
        self.assertEqual(wave_card["rows"][0]["status"], "done")
        self.assertIn("[armchair] Armchair · modeling · done", items[2]["output"])
        self.assertTrue(session["items"][-1]["text"].endswith(
            "The armchair is on the rug.\n\nVerified by Loopcut on this scene: 1 of 1 checks.\n"
            "Requirements (the user's words): 1 of 1 covered."), session["items"][-1]["text"])
        self.assertEqual([e["status"] for e in session["jobs"][0]["coverage"]], ["covered"], "the JobLog has it too")
        self.assertEqual(seen, [1, 1], "once after the merge, once at the finish gate")
        log = session["jobs"][0]
        self.assertEqual((log["level"], log["bounces"], log["final_checks"]["passed"]), (2, 0, 1))
        if router_reply is None:
            self.assertEqual(session["turn_levels"], [{"turn": 1, "first": 2, "final": 2}])
            self.assertEqual(set(session["usage_by_role"]), {"orchestrator", "worker"})
            self.assertEqual(session["usage_by_role"]["orchestrator"]["requests"], 3)


if __name__ == "__main__":
    unittest.main()
