"""Worker startup failure policy without Blender, HTTP or a model request."""

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))

from loopcut import agent, config, llm, orchestrator, state, tools, waves, worker_host  # noqa: E402


class WorkerStartup(unittest.TestCase):
    def setUp(self):
        redraw = mock.patch.object(agent, "_redraw")
        redraw.start()
        self.addCleanup(redraw.stop)

    def worker(self, session=None, error=None):
        runtime = mock.Mock()
        runtime.start.side_effect = error
        worker = waves.Worker.__new__(waves.Worker)
        worker.wave = types.SimpleNamespace(session={} if session is None else session,
                                           make_runtime=mock.Mock(return_value=runtime),
                                           first_reply=threading.Event(), started=time.monotonic(), number=1,
                                           job=types.SimpleNamespace(folder=Path("/job"),
                                                                     budget=waves.JobBudget(0, 1800)))
        worker.id, worker.index = "w", 0
        worker.session = {"id": "startup-test", "messages": []}
        worker.row, worker.note, worker.status = {}, "", "queued"
        worker.ready = threading.Event()
        worker.t = {"spawn": None}
        worker.runtime, worker.boot_error = None, None
        worker.user_cancelled, worker.crashed, worker.retries = False, "", 0
        worker.crashes, worker.steps, worker.reported = [], 0, False
        worker.thread, worker.finished = None, None
        worker.started, worker.report = time.monotonic(), None
        worker.turn = types.SimpleNamespace(cancel=threading.Event())
        worker.spec, worker.run_tool, worker._end = mock.Mock(), mock.Mock(), mock.Mock()
        worker._new_turn = lambda cancel: worker.turn
        return worker, runtime

    def test_spawn_and_handshake_failure_disable_session_without_model_spend_or_retry(self):
        for error in (worker_host.WorkerUnavailable("spawn failed"),
                      worker_host.WorkerUnavailable("unsupported platform option"),
                      worker_host.WorkerUnavailable("handshake failed")):
            with self.subTest(error=error), mock.patch.object(agent, "run_loop") as run:
                worker, runtime = self.worker(error=error)
                worker._work()
                self.assertTrue(worker.wave.session["workers_unavailable"])
                self.assertEqual((worker.status, worker.note), ("failed", worker_host.UNAVAILABLE_NOTE))
                self.assertEqual((worker.retries, worker.crashed), (0, ""))
                runtime.start.assert_called_once()
                runtime.kill.assert_called_once()
                run.assert_not_called()
                worker._end.assert_not_called()

    def test_next_worker_in_same_session_never_spawns(self):
        first, _ = self.worker(error=worker_host.WorkerUnavailable("cannot connect"))
        with mock.patch.object(agent, "run_loop") as run:
            first._work()
            second, runtime = self.worker(session=first.wave.session)
            second._work()
        second.wave.make_runtime.assert_not_called()
        runtime.start.assert_not_called()
        run.assert_not_called()
        self.assertEqual(second.note, worker_host.UNAVAILABLE_NOTE)

    def test_runtime_creation_failure_stays_local(self):
        worker, _ = self.worker()
        worker.wave.make_runtime.side_effect = OSError("scene metadata unavailable")
        with mock.patch.object(agent, "run_loop") as run, mock.patch("traceback.print_exc"):
            worker._work()
        self.assertFalse(worker.wave.session.get("workers_unavailable"))
        self.assertIn("scene metadata unavailable", worker.note)
        run.assert_not_called()

    def test_unclassified_boot_errors_do_not_disable_session(self):
        for error in (OSError("scene read failed"), ValueError("bad init code"), RuntimeError("warm-up failed")):
            with self.subTest(error=error), mock.patch.object(agent, "run_loop") as run, \
                    mock.patch("traceback.print_exc"):
                worker, _ = self.worker(error=error)
                worker._work()
                self.assertFalse(worker.wave.session.get("workers_unavailable"))
                self.assertIn(str(error), worker.note)
                run.assert_not_called()

    def test_init_crashes_keep_retry_and_crash_kit_paths_without_model_spend(self):
        worker, runtime = self.worker(error=[worker_host.ChildCrashed("init", "scene failed", "/job/w/crash-1"),
                                             worker_host.ChildCrashed("init", "warm-up failed", "/job/w/crash-2")])
        worker._end = lambda: waves.Worker._end(worker)
        with mock.patch.object(agent, "run_loop") as run:
            worker._work()
        run.assert_not_called()
        self.assertEqual(runtime.start.call_count, 2)
        self.assertEqual(worker.retries, 1)
        self.assertFalse(worker.wave.session.get("workers_unavailable"))
        self.assertEqual([crash["kept"] for crash in worker.crashes], ["w/crash-1", "w/crash-2"])
        self.assertEqual(worker.status, "failed")
        self.assertIn("crash report", worker.note)
        self.assertIn("warm-up failed", worker.note)

    def test_healthy_boot_finishes_despite_siblings_environment_failure(self):
        session = {}
        worker, runtime = self.worker(session=session)
        runtime.start.side_effect = lambda: session.update(workers_unavailable=True)
        with mock.patch.object(agent, "run_loop") as run:
            worker._work()
        run.assert_called_once()
        worker._end.assert_called_once()

    def test_cancelled_startup_does_not_disable_workers(self):
        worker, _ = self.worker(error=llm.Cancelled())
        with mock.patch.object(agent, "run_loop") as run:
            worker._work()
        self.assertFalse(worker.wave.session.get("workers_unavailable"))
        self.assertEqual(worker.status, "stopped")
        run.assert_not_called()

    def test_cancel_before_startup_never_creates_a_runtime(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable), mock.patch.object(agent, "run_loop") as run:
                worker, runtime = self.worker(session={"workers_unavailable": unavailable})
                worker.turn.cancel.set()
                worker._work()
                worker.wave.make_runtime.assert_not_called()
                runtime.start.assert_not_called()
                run.assert_not_called()
                self.assertEqual(worker.status, "stopped")

    def test_cancelled_startup_unwinds_before_worker_finishes(self):
        worker, runtime = self.worker()
        entered, release = threading.Event(), threading.Event()

        def start():
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release startup cleanup")
            raise llm.Cancelled()

        runtime.start.side_effect = start
        worker.thread = threading.Thread(target=worker._work, daemon=True)
        with mock.patch.object(agent, "run_loop") as run:
            worker.thread.start()
            try:
                self.assertTrue(entered.wait(2))
                worker.turn.cancel.set()
                worker.thread.join(0.2)
                self.assertIsNone(worker.finished, "startup still owns the child and its files")
                self.assertNotIn("elapsed", worker.row, "duration freezes only after startup cleanup")
                run.assert_not_called()
            finally:
                release.set()
                worker.thread.join(2)
            self.assertFalse(worker.thread.is_alive())
        self.assertEqual(worker.status, "stopped")
        self.assertIsNotNone(worker.finished)
        self.assertGreaterEqual(worker.row["elapsed"], 0.2)
        self.assertFalse(worker.wave.session.get("workers_unavailable"))
        runtime.kill.assert_called_once()

    def test_real_mid_task_crash_still_retries_once(self):
        worker, runtime = self.worker()

        def run(*args):
            worker.crashed = "run_python" if worker.retries == 0 else ""

        with mock.patch.object(agent, "run_loop", side_effect=run) as loop:
            worker._work()
        self.assertEqual(loop.call_count, 2)
        self.assertEqual(runtime.start.call_count, 2)
        self.assertEqual(worker.retries, 1)
        self.assertFalse(worker.wave.session.get("workers_unavailable"))
        self.assertIn("crashed during run_python", worker.session["messages"][0]["content"])
        worker._end.assert_called_once()

    def test_plan_bookkeeping_never_prevents_crash_retries(self):
        class UnwritableCard(dict):
            def __setitem__(self, key, value):
                raise RuntimeError("plan card unavailable")

        for during_init in (False, True):
            for card in (None, [], object(), {}, UnwritableCard()):
                with self.subTest(during_init=during_init, card=type(card).__name__):
                    error = [worker_host.ChildCrashed("init", "child exited", ""), None] if during_init else None
                    worker, runtime = self.worker(error=error)
                    worker.wave.plan_card = card

                    def run(*args):
                        if type(card) is dict:
                            self.assertTrue(card["plan_started"], "record successful boot before the request")
                        if not worker.retries:
                            worker._crashed("run_python", "child exited")

                    with mock.patch.object(agent, "run_loop", side_effect=run) as loop:
                        worker._work()
                    self.assertEqual(runtime.start.call_count, 2)
                    self.assertEqual(loop.call_count, 1 if during_init else 2)
                    self.assertEqual(worker.retries, 1)
                    self.assertFalse(worker.wave.session.get("workers_unavailable"))
                    worker._end.assert_called_once()

    def test_crash_retry_handles_empty_and_populated_transcripts(self):
        old = {"name": "capture_viewport", "image_path": "/saved/discarded.png"}
        for items in (None, [], [old], (old,)):
            with self.subTest(items=items):
                worker, runtime = self.worker()
                worker.session["items"] = items
                worker.row["image_path"] = old["image_path"]

                def run(session, turn, *args):
                    if not worker.retries:
                        worker._crashed("run_python", "child exited")
                        return
                    self.assertNotIn("image_path", worker.row)
                    self.assertFalse(worker.after_step(session, turn))
                    self.assertNotIn("image_path", worker.row, "the old attempt stays out of the preview")
                    session["items"] = list(items or []) + [
                        {"name": "capture_viewport", "image_path": "/saved/restarted.png"}]
                    self.assertFalse(worker.after_step(session, turn))
                    self.assertEqual(worker.row["image_path"], "/saved/restarted.png")

                with mock.patch.object(agent, "run_loop", side_effect=run) as loop:
                    worker._work()
                self.assertEqual((runtime.start.call_count, loop.call_count, worker.retries), (2, 2, 1))
                worker._end.assert_called_once()

    def test_preview_bookkeeping_errors_never_prevent_crash_retries(self):
        class UnreadableItems(list):
            def __len__(self):
                raise RuntimeError("transcript unavailable")

        class UnclearableRow(dict):
            def pop(self, key, default=None):
                raise RuntimeError("preview unavailable")

        old = {"name": "capture_viewport", "image_path": "/saved/discarded.png"}
        for during_init in (False, True):
            for failure in ("boundary", "row"):
                with self.subTest(during_init=during_init, failure=failure):
                    error = [worker_host.ChildCrashed("init", "child exited", ""), None] if during_init else None
                    worker, runtime = self.worker(error=error)
                    if failure == "boundary":
                        worker.session["items"] = UnreadableItems([old])
                        worker.row["image_path"] = old["image_path"]
                    else:
                        worker.row = UnclearableRow()

                    def run(session, turn, *args):
                        if not worker.retries:
                            worker._crashed("run_python", "child exited")
                            self.assertTrue(worker.after_step(session, turn))
                            return
                        # Even if the transcript becomes readable, an unknown boundary cannot reuse it.
                        session["items"] = [old]
                        self.assertFalse(worker.after_step(session, turn))
                        self.assertNotIn("image_path", worker.row)

                    with mock.patch.object(agent, "run_loop", side_effect=run) as loop:
                        worker._work()
                    self.assertEqual(runtime.start.call_count, 2)
                    self.assertEqual(loop.call_count, 1 if during_init else 2)
                    self.assertEqual(worker.retries, 1)
                    self.assertFalse(worker.wave.session.get("workers_unavailable"))
                    worker._end.assert_called_once()

    def test_delegate_removed_for_later_turns_and_new_sessions_unaffected(self):
        session = {"workers_unavailable": True}
        spec = types.SimpleNamespace(schemas=lambda: orchestrator.SCHEMAS)
        for turn in (1, 2):
            session["turn_number"] = turn
            names = {s["function"]["name"] for s in agent._session_schemas(session, spec)}
            self.assertNotIn("delegate", names)
            self.assertIn("checks", names)
        fresh = {s["function"]["name"] for s in agent._session_schemas({}, spec)}
        self.assertIn("delegate", fresh)

    def test_cached_delegate_call_is_refused_before_scene_or_model_work(self):
        session = {"workers_unavailable": True, "turn": types.SimpleNamespace(
            roles=types.SimpleNamespace(orchestrate=True))}
        with mock.patch.object(state, "session", return_value=session), \
                mock.patch.object(waves, "prepare") as prepare:
            for dry_run in (False, True):
                result = orchestrator.delegate({"workers": [{}]}, dry_run=dry_run)
                self.assertFalse(result.ok)
                self.assertEqual(result.text, waves.unavailable_note(session))
            prepare.assert_not_called()

    def request(self, session):
        """Capture the real request builder's system message and tool schema, without HTTP/model work."""
        spec = agent.LoopSpec(role="orchestrator", system=lambda _: "Build the scene.",
                              schemas=lambda: orchestrator.SCHEMAS, save=lambda _: None)
        cfg = config.Config("test", "http://127.0.0.1/v1", "test", "high", True, False, 10, 100000)
        with mock.patch.object(llm, "stream_chat", return_value=llm.Completion(text="Collect the workers first.")) as call:
            agent._request(session, session["turn"], cfg, spec)
        return call.call_args.kwargs

    def test_mixed_failure_keeps_wait_in_requests_until_healthy_result_is_collected(self):
        session = state.new_session()
        session["messages"] = [{"role": "user", "content": "Build two parts."}]
        session["turn"] = turn = agent.Turn()
        turn.roles = types.SimpleNamespace(orchestrate=True)
        healthy, _ = self.worker(session=session)
        healthy.id = "healthy"
        failed, _ = self.worker(session=session, error=worker_host.WorkerUnavailable("cannot connect"))
        failed.id = "failed"
        job = turn.job = types.SimpleNamespace(session=session, lock=threading.Lock(), crew=[healthy, failed],
                                              stopped=False, log={})
        working, release = threading.Event(), threading.Event()

        def run(*args):
            working.set()
            if not release.wait(5):
                raise AssertionError("test did not release healthy worker")

        with mock.patch.object(agent, "run_loop", side_effect=run), \
                mock.patch.object(state, "session", return_value=session):
            healthy.thread = threading.Thread(target=healthy._work)
            healthy.thread.start()
            try:
                self.assertTrue(working.wait(2))
                failed._work()
                failed.reported = True  # The first delegate result contains the failed worker.
                request = self.request(session)
                delegate = next(s["function"] for s in request["tools"] if s["function"]["name"] == "delegate")
                self.assertEqual(delegate["parameters"], orchestrator.WAIT_SCHEMA["function"]["parameters"])
                note = request["messages"][0]["content"]
                self.assertIn("Still running: healthy", note)
                self.assertIn("delegate wait true", note)
                self.assertNotIn("continue in the main Blender", note)
                self.assertFalse(orchestrator.delegate({"workers": [{}]}).ok)
                waiting = orchestrator.delegate(wait=True)
                self.assertIs(waiting.then.func, waves.collect)
                self.assertIs(waiting.then.args[0], job)
            finally:
                release.set()
                healthy.thread.join(2)
            self.assertFalse(healthy.thread.is_alive())
            self.assertFalse(healthy.reported)
            request = self.request(session)
            self.assertIn(orchestrator.WAIT_SCHEMA, request["tools"])
            self.assertIn("Ready to collect: healthy", request["messages"][0]["content"])

            def report(collected_job, batch, *args):
                self.assertEqual((collected_job, batch), (job, [healthy]))
                healthy.reported = True
                return tools.ToolResult("Healthy result merged.")

            with mock.patch.object(waves, "report", side_effect=report) as merge:
                self.assertEqual(waiting.then(lambda: False, lambda fn: fn()).text, "Healthy result merged.")
            merge.assert_called_once()
            request = self.request(session)
            self.assertNotIn("delegate", {s["function"]["name"] for s in request["tools"]})
            note = request["messages"][0]["content"]
            self.assertIn("No workers are running; continue in the main Blender", note)
            self.assertIn("[failed] wave 1: unavailable (nothing merged)", note)
            self.assertNotIn("delegate wait true", note)
            self.assertIn("wave", orchestrator.SCHEMAS[0]["function"]["parameters"]["properties"])

    def test_scheduler_stops_queued_workers(self):
        worker, runtime = self.worker(session={"workers_unavailable": True})
        worker.thread, worker.finished = None, None
        job = types.SimpleNamespace(lock=threading.Lock(), crew=[worker], stopped=False, max_workers=1,
                                    budget=mock.Mock(), scheduler=object())
        waves._schedule(job, lambda: False)
        self.assertEqual(worker.note, worker_host.UNAVAILABLE_NOTE)
        self.assertIsNotNone(worker.finished)
        self.assertIsNone(job.scheduler)
        runtime.start.assert_not_called()

    def test_report_and_finish_do_not_recommend_delegating_again(self):
        session = {"workers_unavailable": True}
        job = types.SimpleNamespace(session=session, crew=[], lock=threading.Lock())
        self.assertIn(worker_host.UNAVAILABLE_NOTE, waves._still_running([], job))
        with mock.patch("loopcut.router.nudge") as nudge, \
                mock.patch.object(agent, "_finish_gate", return_value=None) as finish:
            agent._orchestrator_final(session, object(), object())
        nudge.assert_not_called()
        finish.assert_called_once()

    def test_windows_folder_names_are_rejected_before_startup(self):
        for ids, expected in ((["con"], "reserved Windows filename"), (["A", "a"], "used twice")):
            plan = orchestrator.normalise_wave({"workers": [{"id": wid, "role": "modeling",
                                                           "deliverable": f"Part{n}", "prefix": f"Part{n}"}
                                                          for n, wid in enumerate(ids)]})
            errors = orchestrator.validate_wave(plan, "", {})
            self.assertTrue(any(expected in error for error in errors), errors)


def windows_os():
    """Patch a module's OS branch without changing pathlib's choice of path class on this Mac."""
    return types.SimpleNamespace(**(vars(os) | {"name": "nt"}))


class WindowsProcesses(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires native Win32 process handles")
    def test_real_windows_process_identity_and_termination(self):
        from loopcut import worker_process
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            stamp = worker_process.windows_creation(process.pid)
            self.assertIsNotNone(stamp)
            self.assertFalse(worker_process.windows_kill(process.pid, stamp + 1))
            self.assertIsNone(process.poll())
            self.assertEqual(worker_process.windows_creation(process.pid), stamp)
            self.assertTrue(worker_process.windows_kill(process.pid, stamp))
            self.assertNotEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def test_native_orphan_identity_checked_on_same_handle_before_kill(self):
        from loopcut import worker_process
        kernel = mock.Mock()
        kernel.OpenProcess.return_value = 1234567890123  # A handle must not be truncated to a 32-bit int.

        def process_times(handle, created, *unused):
            created._obj.dwHighDateTime, created._obj.dwLowDateTime = 2, 3
            return True

        kernel.GetProcessTimes.side_effect = process_times
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            stamp = worker_process.windows_creation(123)
            self.assertEqual(stamp, (2 << 32) | 3)
            self.assertFalse(worker_process.windows_kill(123, None))
            self.assertFalse(worker_process.windows_kill(123, stamp + 1))
            kernel.TerminateProcess.assert_not_called()
            self.assertTrue(worker_process.windows_kill(123, stamp))
        kernel.TerminateProcess.assert_called_once_with(1234567890123, 1)
        self.assertEqual(kernel.CloseHandle.call_count, 3)
        self.assertEqual(kernel.OpenProcess.call_args.args, (0x1001, False, 123))

    def test_native_orphan_query_failure_leaves_process_alone(self):
        from loopcut import worker_process
        kernel = mock.Mock()
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            kernel.OpenProcess.return_value = None
            self.assertIsNone(worker_process.windows_creation(123))
            self.assertFalse(worker_process.windows_kill(123, 42))
            kernel.OpenProcess.return_value = 100
            kernel.GetProcessTimes.return_value = False
            self.assertFalse(worker_process.windows_kill(123, 42))
        kernel.TerminateProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(100)

    def test_windows_reaper_never_sends_signals_or_kills_live_owners(self):
        from loopcut import worker_process
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "job" / "w1" / "w" / worker_host.RECORD
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"pid": 123, "owner": 456, "windows_created": 42}), encoding="utf-8")
            with mock.patch.object(worker_host, "os", windows_os()), \
                    mock.patch.object(worker_host, "_alive", return_value=True) as alive, \
                    mock.patch.object(worker_process, "windows_kill", return_value=True) as kill:
                self.assertEqual(worker_host.reap_orphans(directory), [])
                kill.assert_not_called()
                alive.return_value = False
                self.assertEqual(worker_host.reap_orphans(directory), [123])
                kill.assert_called_once_with(123, 42)

    def test_render_launcher_does_not_request_posix_session(self):
        from loopcut import jobs
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with mock.patch.object(jobs, "os", windows_os()), \
                    mock.patch.object(jobs, "spec_at", return_value={"binary": sys.executable}), \
                    mock.patch.object(jobs, "status", return_value={}), \
                    mock.patch.object(jobs.subprocess, "Popen", return_value=mock.Mock(pid=123)) as popen, \
                    mock.patch.object(jobs, "_children", []):
                jobs.launch(path)
            self.assertFalse(popen.call_args.kwargs["start_new_session"])
            self.assertNotIn("pass_fds", popen.call_args.kwargs)
            self.assertIn("job_supervisor.py", " ".join(popen.call_args.args[0]))

    def test_render_and_inspection_supervisor_does_not_request_posix_session(self):
        from loopcut import jobs
        with mock.patch.dict(sys.modules, jobs=jobs):
            from loopcut import job_supervisor
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "lease").mkdir()
            (path / "progress.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(job_supervisor, "os", windows_os()), \
                    mock.patch.object(jobs, "spec_at", return_value={"binary": sys.executable, "budget_seconds": 60}), \
                    mock.patch.object(jobs, "read_json", side_effect=[{}, {"verified_complete": True}]), \
                    mock.patch.object(jobs, "atomic_json"), \
                    mock.patch.object(job_supervisor.subprocess, "Popen",
                                      return_value=mock.Mock(pid=123, poll=lambda: 0)) as popen:
                self.assertEqual(job_supervisor.supervise(path), 0)
            self.assertFalse(popen.call_args.kwargs["start_new_session"])
            self.assertNotIn("pass_fds", popen.call_args.kwargs)

    def test_video_launcher_does_not_request_posix_session(self):
        from loopcut import conversations, video_attach
        session = state.new_session()
        fake = types.SimpleNamespace(app=types.SimpleNamespace(binary_path=sys.executable, timers=mock.Mock()))
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(video_attach, "os", windows_os()), mock.patch.object(video_attach, "bpy", fake), \
                mock.patch.object(video_attach, "probe", return_value={"fps": 30, "frames": 90, "size": [8, 8]}), \
                mock.patch.object(conversations, "_folder", return_value=Path(directory)), \
                mock.patch.object(video_attach.subprocess, "Popen", return_value=mock.Mock(pid=123)) as popen, \
                mock.patch.object(video_attach, "_running", {}):
            video_attach.start(session, Path(directory) / "clip.mp4", 0, 2)
        self.assertFalse(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("pass_fds", popen.call_args.kwargs)

    def test_workspace_shutdown_falls_back_to_process_handles(self):
        import subprocess
        from loopcut import workspace
        process = mock.Mock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired("child", 2), 0]
        with mock.patch.object(workspace, "os", types.SimpleNamespace(name="nt")), \
                mock.patch.object(workspace, "signal", types.SimpleNamespace(SIGTERM=15)):
            workspace._kill(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_movie_verification_releases_decoder_before_rename(self):
        from loopcut import jobs
        with mock.patch.dict(sys.modules, jobs=jobs):
            from loopcut import job_worker
        fake = mock.Mock()
        fake.app.ffmpeg.supported = True
        fake.data.movieclips.load.return_value = types.SimpleNamespace(size=(8, 8), frame_duration=1, fps=24)
        scene = fake.data.scenes.new.return_value
        rename = os.replace

        def replace(source, target):
            # Windows refuses this rename while the movie decoder holds the source open.
            fake.data.scenes.remove.assert_called_once_with(scene)
            rename(source, target)

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "movie.partial.mp4").write_bytes(b"movie")
            spec = {"width": 8, "height": 8, "fps": 24, "fps_base": 1}
            with mock.patch.object(job_worker, "bpy", fake), \
                    mock.patch.object(job_worker, "image_record", side_effect=lambda path, *args: path.write_bytes(b"png")), \
                    mock.patch.object(job_worker.os, "replace", side_effect=replace), \
                    mock.patch.object(jobs, "digest", return_value="hash"):
                result = job_worker.encode_movie(spec, folder, [{"file": "frame.png"}], folder)
            self.assertEqual((folder / "movie.mp4").read_bytes(), b"movie")
            self.assertEqual(result["decode_checked_frames"], [1])


if __name__ == "__main__":
    unittest.main()
