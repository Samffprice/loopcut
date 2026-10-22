"""worker_ipc.py, the protocol between the user's Blender and a worker's child Blender: framing over a
real socket (newlines inside strings, a 1 MB payload), messages, the child's command line and its
scrubbed environment. No Blender; the real child is covered by loopcut/harness/worker_host_check.py."""

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import config, worker_ipc as ipc  # noqa: E402


def socket_channels() -> tuple:
    """(parent, child) channels over a socket pair; works on Windows as well as POSIX."""
    parent, child = socket.socketpair()
    return ipc.Channel.from_socket(parent), ipc.Channel.from_socket(child)


class Framing(unittest.TestCase):
    def setUp(self):
        self.parent, self.child = socket_channels()

    def tearDown(self):
        self.parent.close()
        self.child.close()

    def test_round_trip_keeps_newlines_and_unicode(self):
        code = "for i in range(3):\n    print('a\\nb', i)\n\n# ünïcødé ✓   end\r\n"
        message = {"id": 2, "op": "tool", "name": "run_python", "arguments": {"code": code}, "step": 3}
        self.parent.send(message)
        self.assertEqual(self.child.receive(), message)
        self.child.send({"id": 2, "ok": True, "text": "line 1\nline 2\n"})
        self.assertEqual(self.parent.receive()["text"], "line 1\nline 2\n")

    def test_one_megabyte_payload_is_one_message(self):
        text = ("x" * 1023 + "\n") * 1024  # 1 MB with a newline every KB: more than any pipe buffer.
        writer = threading.Thread(target=self.child.send, args=({"id": 7, "ok": True, "text": text},))
        writer.start()
        received = self.parent.receive()
        writer.join(timeout=10)
        self.assertEqual(len(received["text"]), 1024 * 1024)
        self.assertEqual(received["text"], text)
        self.parent.send({"id": 8, "op": "check"})  # The stream is still in step after it.
        self.assertEqual(self.child.receive(), {"id": 8, "op": "check"})

    def test_several_messages_in_order(self):
        for n in range(1, 6):
            self.parent.send({"id": n, "op": "tool", "name": "get_scene_info", "arguments": {}})
        self.assertEqual([self.child.receive()["id"] for _ in range(5)], [1, 2, 3, 4, 5])

    def test_eof_is_none(self):
        self.parent.close()
        self.assertIsNone(self.child.receive())

    def test_encoded_message_is_one_line(self):
        line = ipc.encode({"text": "a\nb\r\nc", "nested": {"list": ["\n"]}})
        self.assertEqual(line.count(b"\n"), 1)
        self.assertTrue(line.endswith(b"\n"))


class Messages(unittest.TestCase):
    def test_decode_refuses_what_is_not_one_object(self):
        for line in (b"[1, 2]\n", b"not json\n", b"\xff\xfe\n", b"42\n"):
            with self.assertRaises(ipc.ProtocolError):
                ipc.decode(line)

    def test_request_round_trip(self):
        request = ipc.Request(4, "tool", {"name": "check_contact", "arguments": {"a": "Chair"}, "step": 2})
        message = request.to_message()
        self.assertEqual(message["op"], "tool")
        self.assertEqual(ipc.Request.from_message(ipc.decode(ipc.encode(message))), request)

    def test_request_needs_known_op_and_int_id(self):
        with self.assertRaises(ipc.ProtocolError):
            ipc.Request.from_message({"id": 1, "op": "format_disk"})
        with self.assertRaises(ipc.ProtocolError):
            ipc.Request.from_message({"id": "1", "op": "check"})

    def test_payload_cannot_overwrite_id_or_op(self):
        self.assertEqual(ipc.Request(3, "check", {"id": 99, "op": "exit"}).to_message(), {"id": 3, "op": "check"})

    def test_reply_round_trip_and_error(self):
        reply = ipc.Reply(5, True, {"text": "OK", "changed": True, "rolled_back": None})
        self.assertEqual(ipc.Reply.from_message(reply.to_message()), reply)
        failed = ipc.Reply(6, False, error="KeyError: 'Rug'").to_message()
        self.assertEqual(failed, {"id": 6, "ok": False, "error": "KeyError: 'Rug'"})
        self.assertEqual(ipc.Reply.from_message(failed).error, "KeyError: 'Rug'")
        with self.assertRaises(ipc.ProtocolError):
            ipc.Reply.from_message({"id": 6, "ok": "yes"})

    def test_timeouts(self):
        self.assertEqual(ipc.timeout_for("init"), 60)
        self.assertEqual(ipc.timeout_for("check"), 60)
        self.assertEqual(ipc.timeout_for("finish"), 120)
        self.assertEqual(ipc.timeout_for("tool", "run_python", {}, run_timeout=60), 90)
        self.assertEqual(ipc.timeout_for("tool", "run_python", {"capture": "front"}, run_timeout=60), 180)
        self.assertEqual(ipc.timeout_for("tool", "capture_viewport"), 90)
        self.assertEqual(ipc.timeout_for("tool", "import_asset"), 120)
        self.assertEqual(ipc.timeout_for("tool", "get_scene_info", run_timeout=10), 40)


class Authentication(unittest.TestCase):
    """Real socket I/O with the listening address mocked, so these also run without network permission."""

    def test_bad_token_rejected_before_valid_child_and_listener_closed(self):
        bad_client, bad_server = socket.socketpair()
        good_client, good_server = socket.socketpair()
        for connection in (bad_client, bad_server, good_client, good_server):
            self.addCleanup(connection.close)
        with mock.patch.object(ipc.socket, "socket") as factory:
            listener = ipc.Listener()
        factory.return_value.bind.assert_called_once_with(("127.0.0.1", 0))
        listener.socket.accept.side_effect = [(bad_server, ("127.0.0.1", 1)), (good_server, ("127.0.0.1", 2))]
        bad_client.sendall(b"x" * 64)
        good_client.sendall(listener.token.encode("ascii"))
        parent = listener.accept(mock.Mock(poll=lambda: None), timeout=2)
        self.addCleanup(parent.close)
        self.assertEqual(bad_client.recv(1), b"")
        self.assertEqual(good_client.recv(2), b"OK")
        listener.socket.close.assert_called_once()
        self.assertFalse(parent.connection.get_inheritable())
        child = ipc.Channel.from_socket(good_client)
        self.addCleanup(child.close)
        child.send({"id": 1, "op": "init"})
        self.assertEqual(parent.receive(), {"id": 1, "op": "init"})

    def test_connect_authenticates_then_returns_blocking_channel(self):
        client, server = socket.socketpair()
        self.addCleanup(server.close)
        server.sendall(b"OK")
        with mock.patch.object(ipc.socket, "create_connection", return_value=client) as connect:
            channel = ipc.connect(12345, "a" * 64)
        self.addCleanup(channel.close)
        self.assertEqual(server.recv(64), b"a" * 64)
        connect.assert_called_once_with(("127.0.0.1", 12345), timeout=60.0)
        self.assertIsNone(client.gettimeout())
        self.assertFalse(client.get_inheritable())

    def test_failed_authentication_closes_connection(self):
        client, server = socket.socketpair()
        self.addCleanup(server.close)
        server.sendall(b"NO")
        with mock.patch.object(ipc.socket, "create_connection", return_value=client), \
                self.assertRaises(ipc.ProtocolError):
            ipc.connect(12345, "a" * 64)
        self.assertEqual(client.fileno(), -1)

    def test_dead_child_and_timeout_do_not_wait_forever(self):
        with mock.patch.object(ipc.socket, "socket"):
            listener = ipc.Listener()
        self.addCleanup(listener.close)
        with self.assertRaisesRegex(ipc.ProtocolError, "exited before connecting"):
            listener.accept(mock.Mock(poll=lambda: 7, returncode=7), timeout=1)
        with self.assertRaises(TimeoutError):
            listener.accept(mock.Mock(poll=lambda: None), timeout=0)

    def test_slow_authentication_is_bounded(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        server.settimeout(0.02)
        client.sendall(b"a")
        with self.assertRaises(TimeoutError):
            ipc._receive_exact(server, 64)

    def test_close_unblocks_reader(self):
        parent, child = socket_channels()
        self.addCleanup(child.close)
        replies = []
        thread = threading.Thread(target=lambda: replies.append(parent.receive()), daemon=True)
        thread.start()
        parent.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(replies, [None])

    def test_windows_read_errors_read_as_eof(self):
        """Windows fails reads with a reset when the peer dies, and with WinError 10060 across our own close."""
        class Failing:
            def __init__(self, error):
                self.error = error

            def readline(self):
                raise self.error

        for error in (ConnectionResetError(10054, "reset"), ConnectionAbortedError(10053, "aborted")):
            self.assertIsNone(ipc.Channel(Failing(error), None).receive())
        timed_out = ipc.Channel(Failing(TimeoutError(10060, "timed out")), None)
        with self.assertRaises(TimeoutError):
            timed_out.receive()  # Not closing: a real failure stays one.
        timed_out.closing = True
        self.assertIsNone(timed_out.receive())


class ChildCommand(unittest.TestCase):
    def test_command_line(self):
        command = ipc.child_command("/B/Blender", "/j/w1/spawn.blend", "/a/worker_child.py", "/j", "armchair", 1234)
        self.assertEqual(command[:4], ["/B/Blender", "-b", "--factory-startup", "--disable-autoexec"])
        self.assertGreater(command.index("/j/w1/spawn.blend"), command.index("--"))
        self.assertNotIn("--token", command)
        self.assertEqual(command[command.index("--python-exit-code") + 1], "1")
        self.assertEqual(command[command.index("--python") + 1], "/a/worker_child.py")
        self.assertEqual(ipc.parse_child_args(command), {"job": "/j", "worker": "armchair", "port": 1234,
                                                        "spawn": "/j/w1/spawn.blend", "parent": os.getpid()})

    def test_stdin_token_requires_exactly_one_valid_secret(self):
        self.assertEqual(ipc.read_token(io.BytesIO(b"a" * 64 + b"\n")), "a" * 64)
        for invalid in (b"", b"a" * 64, b"a" * 63 + b"\n", b"a" * 65 + b"\n", b"g" * 64 + b"\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ipc.ProtocolError):
                ipc.read_token(io.BytesIO(invalid))

    def test_addon_and_scene_loading_follow_authentication(self):
        from loopcut import worker_child
        events = []
        args = {"parent": 123, "spawn": "/j/w1/spawn.blend"}
        channel = mock.Mock()
        blender = mock.Mock()
        package = types.SimpleNamespace()
        blender.ops.wm.open_mainfile.side_effect = lambda **kw: events.append("scene") or {"FINISHED"}
        with mock.patch.object(worker_child, "_connect", side_effect=lambda: (
                events.append("connect") or args, channel, ipc)), \
                mock.patch.object(worker_child, "_load_package", side_effect=lambda: events.append("addon") or package), \
                mock.patch.object(worker_child, "_watch_parent"), mock.patch.object(worker_child, "bpy", blender), \
                mock.patch.object(worker_child, "Worker") as worker:
            worker.return_value.serve.side_effect = lambda ch: events.append("serve")
            worker_child.main()
        self.assertIs(package.worker_ipc, ipc)
        self.assertIs(sys.modules["loopcut.worker_ipc"], ipc)
        self.assertEqual(events, ["connect", "addon", "scene", "serve"])
        blender.ops.wm.open_mainfile.assert_called_once_with(filepath=args["spawn"], use_scripts=False)
        channel.close.assert_called_once()

    def test_addon_and_scene_failures_log_before_closing_authenticated_channel(self):
        from loopcut import worker_child
        for stage in ("addon", "scene"):
            with self.subTest(stage=stage):
                channel, blender = mock.Mock(), mock.Mock()
                log = io.StringIO()
                channel.close.side_effect = lambda: self.assertIn("RuntimeError", log.getvalue())
                blender.ops.wm.open_mainfile.return_value = {"CANCELLED"}
                with mock.patch.object(worker_child, "_connect", return_value=(
                        {"parent": 123, "spawn": "/missing.blend"}, channel, ipc)), \
                        mock.patch.object(worker_child, "_load_package",
                                          side_effect=RuntimeError("bad addon") if stage == "addon" else None), \
                        mock.patch.object(worker_child, "_watch_parent"), \
                        mock.patch.object(worker_child, "bpy", blender), \
                        mock.patch.object(worker_child.sys, "stderr", log), \
                        mock.patch.object(worker_child, "Worker") as worker, self.assertRaises(RuntimeError):
                    worker_child.main()
                channel.close.assert_called_once()
                worker.assert_not_called()

    def test_bootstrap_uses_stdin_without_loading_addon(self):
        from loopcut import worker_child
        command = ipc.child_command("blender", "/j/w1/spawn.blend", "worker_child.py", "/j", "w", 1234)
        stream = io.BytesIO(b"a" * 64 + b"\n")
        self.addCleanup(sys.modules.pop, "_loopcut_worker_ipc", None)
        with mock.patch.object(worker_child.sys, "argv", command), \
                mock.patch.object(worker_child.sys, "stdin", types.SimpleNamespace(buffer=stream)), \
                mock.patch.object(ipc.socket, "create_connection") as connection, \
                mock.patch.object(worker_child, "_load_package") as load:
            connection.return_value.recv.return_value = b"OK"
            connection.return_value.gettimeout.return_value = None
            args, channel, protocol = worker_child._connect()
        load.assert_not_called()
        self.assertEqual(args["port"], 1234)
        self.assertIs(type(channel), protocol.Channel)
        connection.return_value.sendall.assert_called_once_with(b"a" * 64)
        self.assertTrue(stream.closed)
        channel.close()

    def test_parse_refuses_missing_arguments(self):
        with self.assertRaises(ipc.ProtocolError):
            ipc.parse_child_args(["blender", "--", "--job", "/j", "--in", "3"])
        with self.assertRaises(ipc.ProtocolError):
            ipc.parse_child_args(["blender"])

    def test_worker_ids_are_folder_names(self):
        for good in ("armchair", "Side_Table-2", "w1"):
            self.assertTrue(ipc.valid_worker_id(good), good)
        for bad in ("", "../x", "a/b", ".hidden", "x" * 41, "tab\t", "line\n", "con", "NUL", "COM1", "lpt9", None):
            self.assertFalse(ipc.valid_worker_id(bad), bad)
        self.assertEqual(ipc.worker_dir(Path("/j/w2/spawn.blend"), "lamp"), Path("/j/w2/lamp"))


PARENT_ENV = {
    "PATH": "/usr/bin:/bin", "HOME": "/Users/me", "LANG": "en_US.UTF-8", "TMPDIR": "/var/folders/x",
    "LOOPCUT_API_KEY": "lc-fake-api-key", "LOOPCUT_BASE_URL": "https://api.example", "LOOPCUT_MODEL": "fast",
    "LOOPCUT_WORKER_API_KEY": "lc-fake-worker-key", "LOOPCUT_BLENDERKIT_KEY": "bk", "LOOPCUT_POLYPIZZA_KEY": "pp",
    "OPENAI_API_KEY": "sk-fake", "GITHUB_TOKEN": "ghp_fake", "AWS_SECRET_ACCESS_KEY": "aws", "ZAI_SECRET": "z",
    "LOOPCUT_ENV_FILE": "/dev/.env", "LOOPCUT_DATA_DIR": "/data", "PYTHONPATH": "/evil", "BLENDER_USER_SCRIPTS": "/s",
}


class ChildEnv(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.job = Path(self.folder.name) / "job"
        self.env = ipc.child_env(self.job, self.job / "w1" / "armchair", PARENT_ENV)

    def tearDown(self):
        self.folder.cleanup()

    def test_only_the_allowed_variables(self):
        self.assertEqual(set(self.env), {"PATH", "HOME", "LANG", "TMPDIR", "TMP", "TEMP",
                                         "LOOPCUT_WORKER", "LOOPCUT_ENV_FILE"})
        self.assertEqual(self.env["PATH"], PARENT_ENV["PATH"])
        self.assertEqual(self.env["LOOPCUT_WORKER"], "1")
        self.assertEqual(self.env["TMPDIR"], str(self.job / "w1" / "armchair" / "tmp"))
        self.assertEqual(self.env["LOOPCUT_ENV_FILE"], str(self.job / "child.env"))

    def test_no_key_token_or_secret(self):
        self.assertNotIn("LOOPCUT_API_KEY", self.env)
        leaked = [name for name in self.env if ipc.is_secret(name)]
        self.assertEqual(leaked, [])
        for value in ("lc-fake-api-key", "lc-fake-worker-key", "sk-fake", "ghp_fake"):
            self.assertNotIn(value, self.env.values())
        for name in ("LOOPCUT_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "ZAI_SECRET",
                     "db_password", "API_KEYS"):
            self.assertTrue(ipc.is_secret(name), name)
        for name in ("PATH", "HOME", "KEYBOARD", "LOOPCUT_WORKER", "MONKEY_BUSINESS"):
            self.assertFalse(ipc.is_secret(name), name)

    def test_missing_parent_variables_are_left_out(self):
        env = ipc.child_env(self.job, self.job / "w1" / "a", {"PATH": "/bin"})
        self.assertNotIn("HOME", env)
        self.assertNotIn("LANG", env)

    def test_windows_environment_preserves_os_paths_and_isolates_temp(self):
        parent = {"SystemRoot": r"C:\Windows", "windir": r"C:\Windows", "Path": r"C:\Blender",
                  "USERPROFILE": r"C:\Users\Person", "LOCALAPPDATA": r"C:\Users\Person\AppData\Local",
                  "APPDATA": r"C:\Users\Person\AppData\Roaming", "TEMP": "wrong", "TMP": "wrong",
                  "API_TOKEN": "private", "PYTHONPATH": "wrong"}
        env = ipc.child_env(self.job, self.job / "w1" / "a", parent)
        for key in ("SYSTEMROOT", "WINDIR", "PATH", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            self.assertIn(key, env)
        self.assertEqual(env["SYSTEMROOT"], r"C:\Windows")
        self.assertEqual({env[k] for k in ("TMPDIR", "TMP", "TEMP")}, {str(self.job / "w1" / "a" / "tmp")})
        self.assertNotIn("API_TOKEN", env)
        self.assertNotIn("PYTHONPATH", env)

    def test_env_file_holds_only_the_run_timeout(self):
        path = ipc.write_env_file(self.job, 45)
        self.assertEqual(path, self.job / "child.env")
        self.assertEqual(path.read_text(), "LOOPCUT_RUN_TIMEOUT=45\n")

    def test_config_in_the_child_finds_no_key(self):
        """With the child's environment, config reads child.env and never walks up to a .env with keys."""
        ipc.write_env_file(self.job, 30)
        saved = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(self.env)
            env_file, get = config._getter()
            self.assertEqual(env_file, self.job / "child.env")
            self.assertEqual(get("LOOPCUT_API_KEY"), "")
            self.assertEqual(config.run_timeout(), 30.0)
        finally:
            os.environ.clear()
            os.environ.update(saved)


# A stand-in for Blender that speaks the protocol: started with child_command's arguments, it serves
# the ops from worker_child with canned replies. `tool` names ask for behaviour: slow sleeps, die exits,
# segv writes a crash report where Blender does (its TMPDIR) and exits; worker "rude" leaves unasked at exit.
FAKE_CHILD = """
import json, os, sys, time
sys.path.insert(0, {addons!r})
from loopcut import worker_ipc as ipc
opts = ipc.parse_child_args(sys.argv)
if opts["worker"] == "no_connect":
    time.sleep(30)
if opts["worker"] == "early_exit":
    sys.exit(7)
with sys.stdin.buffer as credentials:
    token = ipc.read_token(credentials)
channel = ipc.connect(opts["port"], token)
while True:
    request = channel.receive()
    if request is None:
        break
    op, name = request["op"], request.get("name")
    reply = {{"id": request["id"], "ok": True, "peak_rss_mb": 123.0}}
    if op == "init":
        if opts["worker"] == "bad_init":
            reply.update(ok=False, error="init failed")
        if opts["worker"] == "dead_init":
            os._exit(6)
        if opts["worker"] == "slow_init":
            time.sleep(30)
        reply.update(pid=os.getpid(), undo="undo", stamped=3, env=sorted(os.environ),
                     secret_in_argv=token in sys.argv, secret_in_env=token in os.environ.values(),
                     stdin_closed=sys.stdin.closed)
    elif op == "tool" and name == "slow":
        time.sleep(30)
    elif op == "tool" and request["arguments"].get("die"):
        os._exit(5)
    elif op == "tool" and request["arguments"].get("segv"):
        print("Saved: capture " + str(request["step"]))
        print("Writing: spawn.crash.txt", flush=True)
        with open(os.path.join(os.environ["TMPDIR"], "spawn.crash.txt"), "w") as report:
            report.write("# backtrace " + str(request["step"]))
        os._exit(11)
    elif op == "exit" and opts["worker"] == "rude":
        os._exit(0)
    elif op == "tool":
        reply.update(text="ran " + name + " " + json.dumps(request["arguments"], sort_keys=True),
                     image=None, tool_ok=name != "failing", changed=name == "run_python",
                     rolled_back={{"keys": ["OBJECT:Rug"], "note": "Rolled back"}} if request["arguments"].get("rug") else None,
                     advisory={{"outside_role": []}}, fetched=request.get("fetched"), step=request["step"])
    elif op == "check":
        reply.update(results=[])
    elif op == "broken":
        pass
    channel.send(reply)
    if op == "exit":
        os._exit(0)
"""


class FakeChildren(unittest.TestCase):
    """worker_host.Child and WorkerRuntime against FAKE_CHILD: sockets, routing, crashes and cancel."""

    @classmethod
    def setUpClass(cls):
        from loopcut import tools, worker_host
        cls.tools, cls.host = tools, worker_host
        cls.folder = tempfile.TemporaryDirectory(prefix="loopcut space ü ")
        script = Path(cls.folder.name) / "fake_blender.py"
        script.write_text(FAKE_CHILD.format(addons=str(Path(ipc.__file__).parent.parent)), encoding="utf-8")
        cls.binary = sys.executable
        command = ipc.child_command
        cls.command_patch = mock.patch.object(ipc, "child_command", side_effect=lambda *args:
                                              [sys.executable, str(script), *command(*args)[1:]])
        cls.command_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.host.shutdown_all()
        cls.command_patch.stop()
        cls.folder.cleanup()

    def child(self, worker="w", run_timeout=60.0):
        job = Path(self.folder.name) / "job"
        spawn = job / "w1" / "spawn.blend"
        spawn.parent.mkdir(parents=True, exist_ok=True)
        child = self.host.Child(job, worker, spawn, "modeling", binary=str(self.binary), run_timeout=run_timeout)
        self.addCleanup(child.kill)
        return child.start()

    def test_init_and_tools_in_the_child(self):
        child = self.child("chair")
        ready = child.call("init", {"prefix": "Chair"})
        self.assertTrue(ready["ok"])
        self.assertEqual(ready["pid"], child.pid)
        self.assertEqual(child.init_payload["role"], "modeling")  # Filled in from the Child.
        self.assertIn("ready", child.t)
        self.assertEqual(child.peak_rss_mb, 123.0)
        self.assertFalse([name for name in ready["env"] if ipc.is_secret(name)])
        self.assertFalse(ready["secret_in_argv"])
        self.assertFalse(ready["secret_in_env"])
        self.assertTrue(ready["stdin_closed"])
        record = json.loads((child.folder / self.host.RECORD).read_text())
        self.assertEqual((record["pid"], record["owner"]), (child.pid, os.getpid()))
        runtime = self.host.WorkerRuntime(child, {})
        result = runtime.run_tool("run_python", json.dumps({"code": "print('a\\nb')"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.text, 'ran run_python {"code": "print(\'a\\\\nb\')"}')
        self.assertTrue(result.changed)
        self.assertIsNone(result.rolled_back)
        self.assertEqual(result.crashed, "")
        failed = runtime.run_tool("failing", "{}")
        self.assertIn("Unknown tool", failed.text)  # Not a worker tool: never sent.
        rolled = runtime.run_tool("frame_camera", json.dumps({"rug": True}))
        self.assertEqual(rolled.rolled_back["keys"], ["OBJECT:Rug"])
        self.assertEqual(runtime.rollbacks, 1)
        child.exit()
        self.assertEqual(child.process.returncode, 0)
        self.assertFalse(child.alive())

    def test_parent_tools_and_the_import_split(self):
        from loopcut import tools
        child = self.child("imports")
        child.call("init")
        asked = []

        def search(arguments_json):
            asked.append(("search", json.loads(arguments_json)))
            return tools.ToolResult("3 matches")

        def fetch(arguments):
            asked.append(("fetch", arguments))
            return {"source": arguments["source"], "files": ["/cache/chair.blend"]}

        runtime = self.host.WorkerRuntime(child, {"search_assets": search, "fetch_asset": fetch})
        found = runtime.run_tool("search_assets", json.dumps({"source": "polyhaven", "query": "chair"}))
        self.assertEqual((found.text, found.changed), ("3 matches", False))
        imported = runtime.run_tool("import_asset", json.dumps({"source": "polyhaven", "id": "chair_01"}))
        self.assertTrue(imported.ok)
        self.assertEqual([a[0] for a in asked], ["search", "fetch"])
        self.assertIn("ran import_asset", imported.text)
        missing = runtime.run_tool("report_back", "{}")
        self.assertFalse(missing.ok)
        self.assertIn("not available", missing.text)

        def refuse(arguments):
            raise tools.ToolError("No such asset")
        refused = self.host.WorkerRuntime(child, {"fetch_asset": refuse}).run_tool(
            "import_asset", json.dumps({"source": "polyhaven", "id": "nope"}))
        self.assertEqual((refused.ok, refused.text), (False, "No such asset"))

    def test_timeout_kills_and_marks_a_crash(self):
        saved = ipc.TOOL_GRACE
        ipc.TOOL_GRACE = 0.3
        try:
            child = self.child("slow", run_timeout=0.2)
            child.call("init")
            runtime = self.host.WorkerRuntime(child, {})
            result = runtime.run_tool("get_scene_info", json.dumps({"name": "slow"}))  # Canned: echo.
            self.assertTrue(result.ok)
            stuck = runtime._in_child("slow", {}, {}, None)
            self.assertFalse(stuck.ok)
            self.assertEqual(stuck.crashed, "timeout")
            self.assertEqual(runtime.crashed, "timeout")
            self.assertIsNotNone(child.process.poll())
            again = runtime.run_tool("get_scene_info", "{}")
            self.assertEqual((again.ok, again.crashed), (False, "timeout"))
            with self.assertRaises(self.host.ChildCrashed):
                child.call("check")
            record = json.loads((Path(stuck.crash_kept) / "crash.json").read_text())
            self.assertEqual((record["reason"], record["tool"], record["report"]), ("timeout", "slow", []))
            self.assertIn("killed, it did not crash", record["report_note"])
        finally:
            ipc.TOOL_GRACE = saved

    def test_a_crash_keeps_blenders_report_the_last_blend_and_the_log(self):
        """asset_pack's chest: Blender wrote its crash report to the child's TMPDIR, and the eval's
        cleanup took it, the spawn copy and the scene with them. Each crash now has a kit."""
        child = self.child("chest")
        child.spawn_path.write_bytes(b"BLENDER spawn")
        child.call("init")
        runtime = self.host.WorkerRuntime(child, {})
        self.addCleanup(lambda: runtime.child.kill())
        runtime.step = 7
        died = runtime.run_tool("run_python", json.dumps({"segv": True}))
        kit = child.folder / "crash-1"
        self.assertEqual((died.crashed, died.crash_kept), ("exited", str(kit)))
        self.assertEqual((kit / "spawn.crash.txt").read_text(), "# backtrace 7")
        self.assertFalse(list((child.folder / "tmp").glob("*.crash.txt")), "moved: the next crash writes its own")
        self.assertEqual((kit / "spawn.blend").read_bytes(), b"BLENDER spawn")
        self.assertTrue((kit / "worker.log.tail.txt").read_text().endswith("Saved: capture 7\nWriting: spawn.crash.txt\n"))
        record = json.loads((kit / "crash.json").read_text())
        self.assertEqual({k: record[k] for k in ("worker", "op", "tool", "reason", "exit_code", "report", "blend")},
                         {"worker": "chest", "op": "tool", "tool": "run_python", "reason": "exited", "exit_code": 11,
                          "report": ["spawn.crash.txt"], "blend": "spawn.blend"})
        self.assertIn("the start of its brief", record["blend_note"])

        runtime.restart()
        (runtime.child.folder / "percall.blend").write_bytes(b"BLENDER before the last call")
        runtime.step = 9
        again = runtime.run_tool("run_python", json.dumps({"segv": True}))
        second = child.folder / "crash-2"
        self.assertEqual(again.crash_kept, str(second))
        self.assertEqual((second / "spawn.crash.txt").read_text(), "# backtrace 9")
        self.assertEqual((second / "percall.blend").read_bytes(), b"BLENDER before the last call")
        self.assertIn("before its last run_python", json.loads((second / "crash.json").read_text())["blend_note"])
        self.assertEqual((kit / "spawn.crash.txt").read_text(), "# backtrace 7", "the first kit stays as it was")

    def test_a_crash_kit_names_a_blend_too_big_to_copy(self):
        child = self.child("huge")
        child.spawn_path.write_bytes(b"BLENDER spawn")
        child.call("init")
        with mock.patch.object(self.host, "CRASH_BLEND_MB", 0):
            died = self.host.WorkerRuntime(child, {}).run_tool("run_python", json.dumps({"segv": True}))
        record = json.loads((Path(died.crash_kept) / "crash.json").read_text())
        self.assertIsNone(record["blend"])
        self.assertIn("not copied", record["blend_note"])
        self.assertFalse(list(Path(died.crash_kept).glob("*.blend")))

    def test_a_child_we_stop_or_ask_to_leave_leaves_no_crash_kit(self):
        child = self.child("rude")
        child.call("init")
        child.exit()
        self.assertFalse(list(child.folder.glob("crash-*")))
        stopped = self.child("stopped")
        stopped.call("init")
        errors = []

        def busy():
            try:
                stopped.call("tool", {"name": "slow", "arguments": {}})
            except self.host.ChildCrashed as ex:
                errors.append(ex)
        thread = threading.Thread(target=busy)
        thread.start()
        threading.Timer(0.3, stopped.kill).start()  # Stop: the job's thread kills the children mid-call.
        thread.join(10)
        self.assertEqual([ex.kept for ex in errors], [""])
        self.assertFalse(list(stopped.folder.glob("crash-*")))

    def test_restart_after_the_child_dies(self):
        child = self.child("dies")
        child.call("init", {"prefix": "Lamp"})
        runtime = self.host.WorkerRuntime(child, {})
        # restart replaces runtime.child; child() only registered cleanup for the original.
        self.addCleanup(lambda: runtime.child.kill())
        died = runtime.run_tool("run_python", json.dumps({"die": True}))
        self.assertEqual(died.crashed, "exited")
        self.assertIn("code 5", died.text)
        reply = runtime.restart()
        self.assertTrue(reply["ok"])
        self.assertIsNot(runtime.child, child)
        self.assertEqual(runtime.child.init_payload["prefix"], "Lamp")
        self.assertEqual((runtime.crashed, runtime.restarts), ("", 1))
        self.assertTrue(runtime.run_tool("get_scene_info", "{}").ok)

    def test_cancel_kills_the_child(self):
        from loopcut import llm
        child = self.child("cancel")
        child.call("init")
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        runtime = self.host.WorkerRuntime(child, {})
        with self.assertRaises(llm.Cancelled):
            runtime._in_child("slow", {}, {}, cancel.is_set)
        self.assertIsNotNone(child.process.poll())
        self.assertEqual(child.dead, "cancelled")

    def test_restart_cancel_reaps_real_child_that_never_connects(self):
        from loopcut import llm
        child = self.child("restart_cancel")
        child.call("init")
        runtime = self.host.WorkerRuntime(child, {})
        self.addCleanup(lambda: runtime.child.kill())
        command, popen = ipc.child_command, self.host.subprocess.Popen
        cancel = threading.Event()
        replacements = []

        def no_connect(*args):
            argv = command(*args)
            argv[argv.index("--worker") + 1] = "no_connect"
            return argv

        def spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            replacements.append(process)
            self.addCleanup(process.wait, timeout=5)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            # Cancel after a real process exists, before start enters Listener.accept.
            cancel.set()
            return process

        with mock.patch.object(ipc, "child_command", side_effect=no_connect), \
                mock.patch.object(self.host.subprocess, "Popen", side_effect=spawn), self.assertRaises(llm.Cancelled):
            runtime.restart(is_cancelled=cancel.is_set)
        self.assertEqual(len(replacements), 1)
        self.assertIsNotNone(replacements[0].poll())
        self.assertTrue(replacements[0].stdin.closed)
        self.assertFalse(self.host._live)

    def test_bad_ids_are_refused(self):
        with self.assertRaises(ValueError):
            self.host.Child("/j", "../escape", "/j/w1/spawn.blend", "modeling", binary="/bin/false", run_timeout=1)

    def test_connection_failure_is_startup_failure_and_reaps_child(self):
        for worker in ("early_exit", "no_connect"):
            with self.subTest(worker=worker), mock.patch.dict(ipc.TIMEOUTS, init=0.2), \
                    self.assertRaises(self.host.WorkerUnavailable):
                self.child(worker)
        self.assertFalse(self.host._live)

    def test_init_error_exit_and_timeout_keep_crash_kits(self):
        for worker in ("bad_init", "dead_init", "slow_init"):
            with self.subTest(worker=worker):
                child = self.child(worker)
                with self.assertRaises(self.host.ChildCrashed) as crash:
                    child.call("init", timeout=0.2)
                self.assertIsNotNone(child.process.poll())
                record = json.loads((Path(crash.exception.kept) / "crash.json").read_text())
                self.assertEqual(record["op"], "init")


class StartupFailures(unittest.TestCase):
    """Spawn cleanup and pre-tool failures without needing Blender or a network listener."""

    def setUp(self):
        from loopcut import worker_host
        self.host = worker_host
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.child = worker_host.Child(folder.name, "w", Path(folder.name) / "w1" / "spawn.blend",
                                       "modeling", binary=sys.executable, run_timeout=1)
        self.addCleanup(self.child.kill)

    def test_spawn_oserror_and_valueerror_close_listener(self):
        for error in (OSError("cannot spawn"), ValueError("unsupported option")):
            with self.subTest(error=type(error).__name__), mock.patch.object(ipc, "Listener") as listener, \
                    mock.patch.object(self.host.subprocess, "Popen", side_effect=error) as popen:
                listener.return_value.port, listener.return_value.token = 12345, "a" * 64
                with self.assertRaises(self.host.WorkerUnavailable):
                    self.child.start()
                self.assertNotIn("pass_fds", popen.call_args.kwargs)
                self.assertNotIn("start_new_session", popen.call_args.kwargs)
                self.assertTrue(popen.call_args.kwargs["close_fds"])
                listener.return_value.close.assert_called_once()
                self.assertNotIn(self.child, self.host._live)

    def test_failed_connection_terminates_and_reaps_process(self):
        process = mock.Mock(pid=123, poll=lambda: None, stdin=io.BytesIO())
        with mock.patch.object(ipc, "Listener") as listener, \
                mock.patch.object(self.host.subprocess, "Popen", return_value=process):
            listener.return_value.port, listener.return_value.token = 12345, "a" * 64
            listener.return_value.accept.side_effect = TimeoutError("no connection")
            with self.assertRaises(self.host.WorkerUnavailable):
                self.child.start()
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        listener.return_value.close.assert_called_once()
        self.assertNotIn(self.child, self.host._live)
        self.child.process = None

    def test_windows_start_records_identity_and_passes_secret_only_on_stdin(self):
        from loopcut import worker_process
        process = mock.Mock(pid=123, poll=lambda: None, stdin=io.BytesIO())
        windows = types.SimpleNamespace(**(vars(os) | {"name": "nt"}))
        with mock.patch.object(self.host, "os", windows), mock.patch.object(ipc, "Listener") as listener, \
                mock.patch.object(worker_process, "windows_creation", return_value=42) as creation, \
                mock.patch.object(self.host.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(self.host.threading, "Thread"):
            listener.return_value.port, listener.return_value.token = 12345, "a" * 64

            def accept(*args):
                self.assertTrue(process.stdin.closed)
                return mock.Mock()

            listener.return_value.accept.side_effect = accept
            # Observe the bytes before the parent's close, without changing the pipe protocol.
            original_close = process.stdin.close

            def close():
                if not process.stdin.closed:
                    self.assertEqual(process.stdin.getvalue(), b"a" * 64 + b"\n")
                    original_close()

            with mock.patch.object(process.stdin, "close", side_effect=close):
                self.child.start()
        creation.assert_called_once_with(123)
        record = json.loads((self.child.folder / self.host.RECORD).read_text(encoding="utf-8"))
        self.assertEqual(record["windows_created"], 42)
        self.assertNotIn("pass_fds", popen.call_args.kwargs)
        self.assertNotIn("startupinfo", popen.call_args.kwargs)
        self.assertNotIn("start_new_session", popen.call_args.kwargs)
        self.assertEqual(popen.call_args.kwargs["stdin"], self.host.subprocess.PIPE)
        self.assertNotIn("a" * 64, str(popen.call_args))
        self.assertNotIn("--token", popen.call_args.args[0])

    def test_cancel_before_spawn_does_not_become_unavailable(self):
        from loopcut import llm
        with mock.patch.object(self.host.subprocess, "Popen") as popen, self.assertRaises(llm.Cancelled):
            self.child.start(is_cancelled=lambda: True)
        popen.assert_not_called()

    def test_init_eof_protocol_error_timeout_and_refusal_keep_diagnostics(self):
        for message in (None, ipc.ProtocolError("garbled"), {"id": 99, "ok": True},
                        {"id": 1, "ok": False, "error": "init failed"}, "timeout"):
            with self.subTest(message=message):
                self.child.dead = self.child.stopping = ""
                self.child.next_id = 1
                self.child.process = mock.Mock(pid=123, poll=lambda: None, wait=lambda **kw: 7)
                self.child.channel = mock.Mock()
                if message != "timeout":
                    self.child.replies.put(message)
                with self.assertRaises(self.host.ChildCrashed) as crash:
                    self.child.call("init", timeout=0.01)
                kit = Path(crash.exception.kept)
                record = json.loads((kit / "crash.json").read_text())
                self.assertEqual(record["op"], "init")
                self.assertTrue((kit / "worker.log.tail.txt").is_file())
                self.child.process = None

    def test_parent_programming_error_does_not_become_unavailable(self):
        with mock.patch.object(ipc, "child_command", side_effect=ValueError("bad command builder")), \
                mock.patch.object(ipc, "Listener"), self.assertRaisesRegex(ValueError, "bad command builder"):
            self.child.start()

    def test_spawn_connection_and_init_share_one_deadline(self):
        process = mock.Mock(pid=123, poll=lambda: None, stdin=io.BytesIO())
        with mock.patch.object(ipc, "Listener") as listener, \
                mock.patch.object(self.host.subprocess, "Popen", return_value=process), \
                mock.patch.object(self.host.threading, "Thread"), \
                mock.patch.object(self.host.time, "monotonic", return_value=100.0) as clock:
            listener.return_value.token = "a" * 64
            self.child.start()
            self.assertEqual(self.child.startup_deadline, 100.0 + ipc.TIMEOUTS["init"])
            self.assertEqual(listener.return_value.accept.call_args.args[1], ipc.TIMEOUTS["init"])
            clock.return_value = self.child.startup_deadline + 1
            with self.assertRaises(self.host.ChildCrashed) as crash:
                self.child.call("init", timeout=60)
            self.assertEqual(crash.exception.reason, "timeout")
        self.child.process = None

    def test_restart_cancel_while_replacement_is_connecting(self):
        from loopcut import llm
        cancel = threading.Event()
        process = mock.Mock(pid=123, poll=lambda: None, stdin=io.BytesIO())
        listener = ipc.Listener.__new__(ipc.Listener)
        listener.token, listener.port, listener.socket = "a" * 64, 12345, mock.Mock()

        def connecting():
            cancel.set()
            raise socket.timeout()

        listener.socket.accept.side_effect = connecting
        runtime = self.host.WorkerRuntime(self.child, {})
        started = time.monotonic()
        with mock.patch.object(ipc, "Listener", return_value=listener), \
                mock.patch.object(self.host.subprocess, "Popen", return_value=process), \
                mock.patch.object(self.host.Child, "call") as init, self.assertRaises(llm.Cancelled):
            runtime.restart(is_cancelled=cancel.is_set)
        self.assertLess(time.monotonic() - started, 2)
        init.assert_not_called()
        listener.socket.accept.assert_called_once()
        listener.socket.close.assert_called_once()
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertTrue(process.stdin.closed)
        self.assertFalse(self.host._live)

    def test_windows_liveness_uses_safe_probe_and_does_not_run_ps(self):
        from loopcut import jobs
        with mock.patch.object(self.host, "os", types.SimpleNamespace(name="nt")), \
                mock.patch.object(jobs, "_alive", return_value=True) as alive, \
                mock.patch.object(self.host.subprocess, "run") as run:
            self.assertTrue(self.host._alive(123))
            alive.assert_called_once_with(123)
            self.assertEqual(self.host._command_of(123), "")
            self.assertIsNone(self.child.rss_mb())
            run.assert_not_called()

    def test_missing_resource_is_optional_even_for_init_reply(self):
        from loopcut import worker_child
        worker = worker_child.Worker.__new__(worker_child.Worker)
        for name in ("init", "tool", "check", "finish"):
            setattr(worker, name, lambda payload: {})
        channel = mock.Mock()
        channel.receive.side_effect = [{"id": 1, "op": "init"}, {"id": 2, "op": "exit"}]
        with mock.patch.dict(sys.modules, resource=None), \
                mock.patch.object(worker_child, "_leave", side_effect=SystemExit), self.assertRaises(SystemExit):
            worker.serve(channel)
        self.assertEqual(channel.send.call_args_list[0].args[0], {"id": 1, "ok": True, "peak_rss_mb": None})

    def test_windows_watchdog_detects_dead_parent_with_unchanged_ppid(self):
        from loopcut import jobs, worker_child
        with mock.patch.object(worker_child, "os", mock.Mock(name="nt", getppid=lambda: 123)) as os_mock, \
                mock.patch.object(worker_child.threading, "Thread") as thread, \
                mock.patch.object(worker_child.time, "sleep"), mock.patch.object(jobs, "_alive", return_value=False):
            os_mock.name = "nt"
            os_mock._exit.side_effect = SystemExit
            worker_child._watch_parent(123)
            with self.assertRaises(SystemExit):
                thread.call_args.kwargs["target"]()
            os_mock._exit.assert_called_once_with(3)


class AssetSplit(unittest.TestCase):
    """import_asset for a worker: assets.fetch_asset in the parent, assets.load_fetched in the child."""

    def setUp(self):
        from unittest import mock
        from loopcut import asset_libraries, assets, blenderkit, indexed_assets, polyhaven, polypizza
        self.mock, self.assets = mock, assets
        self.modules = {"polyhaven": polyhaven, "blenderkit": blenderkit, "library": asset_libraries,
                        "polypizza": polypizza, "ambientcg": indexed_assets, "physicallybased": indexed_assets}

    def test_every_source_is_split(self):
        for source, (fetch, load) in self.assets.SPLIT.items():
            module = self.modules[source]
            self.assertTrue(callable(getattr(module, fetch)) and callable(getattr(module, load)), source)
            imported = self.assets.IMPORT_SOURCES[source][2]
            fetch_parameters = list(inspect_parameters(getattr(module, fetch)))
            self.assertEqual(fetch_parameters, list(inspect_parameters(getattr(module, imported))),
                             f"{source}: fetch takes import's arguments, so a worker's call can go to either")

    def test_fetch_routes_the_id_under_the_library_s_own_name(self):
        with self.mock.patch.object(self.modules["polyhaven"], "fetch_polyhaven", autospec=True,
                                    return_value={"id": "oak"}) as fetch:
            fetched = self.assets.fetch_asset(source="polyhaven", id="oak_01", resolution="2k", apply_to=["Seat"])
        fetch.assert_called_once_with(asset_id="oak_01", resolution="2k", apply_to=["Seat"])
        self.assertEqual(fetched, {"id": "oak", "source": "polyhaven"})
        from loopcut.tools import ToolError
        with self.assertRaises(ToolError) as refused:
            self.assets.fetch_asset(source="library", id="L::f.blend::WORLD::City", target_size=1)
        self.assertEqual(str(refused.exception), "source library does not take target_size")
        with self.assertRaises(ToolError):
            self.assets.fetch_asset(source="nope", id="x")

    def test_load_gets_only_what_it_takes(self):
        target = self.modules["polypizza"]
        with self.mock.patch.object(target, "load_polypizza", autospec=True, return_value="loaded") as load:
            result = self.assets.load_fetched({"source": "polypizza", "id": "abc", "path": "/c/abc.glb"},
                                              target_size=2.0, resolution="1k", apply_to=[])
        self.assertEqual(result, "loaded")
        load.assert_called_once_with({"source": "polypizza", "id": "abc", "path": "/c/abc.glb"}, target_size=2.0)

    def test_import_is_fetch_then_load(self):
        module = self.modules["polypizza"]
        with self.mock.patch.object(module, "fetch_polypizza", return_value={"id": "abc"}) as fetch, \
                self.mock.patch.object(module, "load_polypizza", return_value="done") as load:
            self.assertEqual(module.import_polypizza("abc", 1.5), "done")
        fetch.assert_called_once_with("abc", 1.5)
        load.assert_called_once_with({"id": "abc"}, 1.5)

    def test_the_fetched_record_is_json_and_holds_no_key(self):
        module = self.modules["polypizza"]
        with tempfile.TemporaryDirectory() as folder:
            glb = Path(folder) / "abc.glb"
            glb.write_bytes(b"glTF\x02\x00\x00\x00")
            model = {"ID": "abc", "Title": "Tree", "Licence": "CC0", "Attribution": "Someone", "Download": "https://x/abc.glb"}
            with self.mock.patch.object(module, "_headers", return_value={"x-auth-token": "lc-fake"}), \
                    self.mock.patch.object(module, "get_json", return_value=model), \
                    self.mock.patch.object(module, "download", return_value=glb), \
                    self.mock.patch.object(module, "cache_root", return_value=Path(folder)):
                fetched = module.fetch_polypizza("abc", 0.0)
        text = json.dumps(fetched)
        self.assertNotIn("lc-fake", text)
        self.assertEqual(fetched["path"], str(glb))
        self.assertEqual(fetched["model"]["Licence"], "CC0")


def inspect_parameters(fn) -> list:
    import inspect
    return list(inspect.signature(fn).parameters)


if __name__ == "__main__":
    unittest.main()
