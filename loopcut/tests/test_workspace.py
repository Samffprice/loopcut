"""The single agent's workspace (workspace.py): its tools' rules, the pure parts of apply_build, the shell
on a temp folder, and how the agent, the router and the config choose the workspace loop. No Blender:
harness/workspace_check.py runs the whole loop headless with real background Blenders and a real apply."""

import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, config, context, conversations, files, router, skills, state, tools, workspace  # noqa: E402


def png(path: Path, width: int, height: int) -> None:
    raw = b"".join(b"\x00" + b"\x80" * (3 * width) for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg_header(path: Path, width: int, height: int) -> None:
    """SOI, an APP0 segment, then SOF0 with the size: what image_size reads."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    path.write_bytes(b"\xff\xd8" + app0 + sof + b"\xff\xd9")


def windows_args(command: str) -> list[str]:
    """MS C-runtime argv rules for arguments (not argv[0]), also used by Python on Windows.

    https://learn.microsoft.com/en-us/cpp/c-language/parsing-c-command-line-arguments
    Backslashes escape quotes for a native process, NOT for cmd.exe. Tests remove cmd's outer
    /c quotes first, then parse the native child's command line here. No shell expansion is simulated.
    """
    args, index = [], 0
    while index < len(command):
        if command[index] in " \t":
            index += 1
            continue
        value, quoted = "", False
        while index < len(command) and (quoted or command[index] not in " \t"):
            slashes = 0
            while index < len(command) and command[index] == "\\":
                slashes += 1
                index += 1
            if index < len(command) and command[index] == '"':
                value += "\\" * (slashes // 2)
                if slashes % 2:
                    value += '"'
                elif quoted and index + 1 < len(command) and command[index + 1] == '"':
                    value += '"'
                    index += 1
                else:
                    quoted = not quoted
            else:
                value += "\\" * slashes
                if index == len(command) or (not quoted and command[index] in " \t"):
                    break
                value += command[index]
            index += 1
        args.append(value)
    return args


class Folder(unittest.TestCase):
    """A workspace on a temp folder, as the current conversation's, without Blender."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        os.environ["LOOPCUT_DATA_DIR"] = str(self.root / "data")
        self.addCleanup(os.environ.pop, "LOOPCUT_DATA_DIR", None)
        self.session = state.reset()
        self.session["mode"] = "workspace"
        self.addCleanup(state.reset)
        workspace._BLENDER[:] = ["/bin/echo"]
        self.addCleanup(workspace._BLENDER.clear)
        self.python = mock.patch.object(workspace, "_python", return_value=sys.executable)
        self.python.start()
        self.addCleanup(self.python.stop)
        self.ws = workspace.ensure(self.session)
        self.addCleanup(workspace._ROOTS.discard, self.ws)

    def then(self, result, cancelled=lambda: False):
        self.assertIsNotNone(result.then, result.text)
        return result.then(cancelled, lambda fn: fn())


class LayoutTest(Folder):
    def test_the_folder_its_shims_and_where_relative_paths_go(self):
        self.assertEqual(self.ws, (self.root / "data" / "workspaces" / self.session["id"]).resolve())
        self.assertEqual(workspace.current(), self.ws)
        for name in ("logs", ".loopcut/tmp", ".loopcut/bin/blender", ".loopcut/bin/python3"):
            self.assertTrue((self.ws / name).exists(), name)
        self.assertIn(f'--factory-startup --python-exit-code 1 --python "{workspace.SHAPE_KEY_GUARD}" '
                      f'--python "{workspace.OP_NOTES}" --python "{workspace.RENDER_PATH_GUARD}" "$@"',
                      (self.ws / ".loopcut/bin/blender").read_text())
        self.assertEqual(files.resolve("build.py"), self.ws / "build.py")
        with mock.patch.object(files, "blend_base", return_value=(self.root / "project", "")):
            self.assertEqual(files.resolve("//chest_hero.png"), self.root / "project" / "chest_hero.png",
                             "// still means the .blend's folder")

    def test_windows_blender_shim_sets_failure_exit_code_before_startup_and_user_scripts(self):
        with mock.patch.object(workspace.os, "name", "nt"):
            workspace._shims(self.ws)
        self.assertIn(f'--factory-startup --python-exit-code 1 --python "{workspace.SHAPE_KEY_GUARD}" '
                      f'--python "{workspace.OP_NOTES}" --python "{workspace.RENDER_PATH_GUARD}" %*',
                      (self.ws / ".loopcut/bin/blender.cmd").read_text())

    def test_outside_workspace_mode_relative_paths_are_as_before(self):
        self.session["mode"] = ""
        self.assertIsNone(workspace.current())
        with mock.patch.object(files, "blend_base", return_value=(self.root / "project", "")):
            self.assertEqual(files.relative_base()[0], self.root / "project")

    def test_the_workspace_is_open_though_it_is_in_loopcuts_data_but_its_hidden_names_are_not(self):
        self.assertEqual(files.check(self.ws / "build.py"), self.ws / "build.py")
        with self.assertRaisesRegex(tools.ToolError, "hidden"):
            files.check(self.ws / ".loopcut" / "state.json")
        with self.assertRaisesRegex(tools.ToolError, "Loopcut's own data"):
            files.check(self.root / "data" / "conversations" / "x.json")

    def test_write_file_makes_folders_in_the_workspace(self):
        files.write_file("maps/wood/notes.txt", "hi")
        self.assertEqual((self.ws / "maps" / "wood" / "notes.txt").read_text(), "hi")

    def test_attached_images_are_copied_once_under_their_names(self):
        original = self.root / "ref1.jpg"
        original.write_bytes(b"jpeg bytes")
        self.session["references"] = [{"ref": "img:a.png", "full": "img:b.png", "name": "ref1.jpg",
                                       "source": str(original)}]
        self.assertEqual(workspace._copy_references(self.session, self.ws), ["ref1.jpg"])
        self.assertEqual((self.ws / "ref1.jpg").read_bytes(), b"jpeg bytes")
        self.assertEqual(workspace._copy_references(self.session, self.ws), [], "once")

    def test_stored_copies_are_named_for_what_they_hold(self):
        source = self.root / "made"
        refs = []
        for data in (b"\xff\xd8\xff\xe0 a clip's sheet", b"\x89PNG\r\n\x1a\n a photo"):
            source.write_bytes(data)
            refs.append(conversations.store_image(self.session, source))
        self.session["references"] = [{"ref": refs[0], "full": refs[0], "name": "take.mp4 2.00-4.27s"},
                                      {"ref": refs[1], "full": refs[1], "name": "gone.webp", "source": "/gone.webp"}]
        self.assertEqual(workspace._copy_references(self.session, self.ws), ["take.mp4 2.00-4.27s.jpg", "gone.png"])
        self.assertEqual((self.ws / "take.mp4 2.00-4.27s.jpg").read_bytes(), b"\xff\xd8\xff\xe0 a clip's sheet")

    def test_the_shells_environment_has_no_keys_and_finds_blender_first(self):
        env = workspace.shell_env(self.ws, {"PATH": "/usr/bin", "HOME": "/h", "OPENAI_API_KEY": "sk",
                                            "LOOPCUT_BASE_URL": "x", "GITHUB_TOKEN": "t", "PYTHONPATH": "/p"})
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(self.ws / ".loopcut" / "bin"))
        self.assertEqual(env["HOME"], "/h")
        self.assertEqual(env["BLENDER"], "/bin/echo")
        self.assertEqual(env["TMPDIR"], str(self.ws / ".loopcut" / "tmp"))
        for name in ("OPENAI_API_KEY", "LOOPCUT_BASE_URL", "GITHUB_TOKEN", "PYTHONPATH"):
            self.assertNotIn(name, env)


class ProcessTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / "process.log"

    def run_process(self, is_cancelled, timeout=2, **kwargs):
        return workspace.run_process([sys.executable, "-c", "import time; time.sleep(0.4)"],
                                     self.root, os.environ.copy(), timeout, self.log, is_cancelled, **kwargs)

    def test_live_rows_show_process_until_exit(self):
        for kwargs, label in (({"label": "Python script"}, "Python script"), ({}, Path(sys.executable).name)):
            with self.subTest(kwargs=kwargs):
                seen = []

                def observe():
                    seen.append(workspace.live_rows())
                    return False

                code, seconds, stopped = self.run_process(observe, **kwargs)
                self.assertEqual((code, stopped), (0, ""))
                self.assertTrue(seen)
                first = seen[0][0]
                self.assertTrue(first["id"])
                for rows in seen:
                    self.assertEqual(len(rows), 1)
                    row = rows[0]
                    self.assertEqual(row["id"], first["id"])
                    self.assertEqual(row["label"], label)
                    self.assertEqual(row["log"], str(self.log))
                    self.assertEqual(row["started"], first["started"])
                    self.assertGreaterEqual(row["elapsed"], 0)
                    self.assertLessEqual(row["elapsed"], seconds)
                    json.dumps(row)  # UI snapshots contain only plain data.
                self.assertEqual(workspace.live_rows(), [])

    def test_live_rows_removed_after_cancel(self):
        from loopcut import llm
        seen = []

        def cancel():
            seen.extend(workspace.live_rows())
            return True

        with self.assertRaises(llm.Cancelled):
            self.run_process(cancel, label="Python script")
        self.assertEqual([row["label"] for row in seen], ["Python script"])
        self.assertEqual(workspace.live_rows(), [])

    def test_live_rows_removed_after_timeout(self):
        seen = []

        def observe():
            seen.extend(workspace.live_rows())
            return False

        code, _, stopped = self.run_process(observe, timeout=0, label="Python script")
        self.assertEqual((code, stopped), (None, "timeout"))
        self.assertEqual([row["label"] for row in seen], ["Python script"])
        self.assertEqual(workspace.live_rows(), [])

    def test_live_rows_removed_after_exception(self):
        seen = []

        def fail():
            seen.extend(workspace.live_rows())
            raise RuntimeError("callback failed")

        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            self.run_process(fail, label="Python script")
        self.assertEqual([row["label"] for row in seen], ["Python script"])
        self.assertEqual(workspace.live_rows(), [])


class ShellTest(Folder):
    def test_a_command_runs_in_the_workspace_and_says_its_exit_code(self):
        result = self.then(workspace.shell("pwd; echo out; echo err >&2"))
        self.assertTrue(result.ok, result.text)
        self.assertTrue(result.text.startswith("exit 0 in "), result.text)
        self.assertIn(str(self.ws), result.text)
        self.assertIn("out", result.text)
        self.assertIn("err", result.text)
        failed = self.then(workspace.shell("exit 3"))
        self.assertFalse(failed.ok)
        self.assertIn("exit 3", failed.text)
        self.assertIn("(no output)", failed.text)

    @unittest.skipIf(os.name == "nt", "POSIX shell syntax")
    def test_status_variable_preserves_exit_code_and_output(self):
        for code in (0, 7):
            with self.subTest(code=code):
                result = self.then(workspace.shell(
                    f"(exit {code}); status=$?; echo completed; exit $status"))
                self.assertIn("completed", result.text.splitlines())
                self.assertTrue(result.text.startswith(f"exit {code} in "), result.text)
                self.assertEqual(result.ok, code == 0, result.text)

    @unittest.skipIf(os.name == "nt", "POSIX shell glob expansion")
    def test_unmatched_glob_does_not_hide_matching_files(self):
        (self.ws / "matched.txt").write_text("match")
        result = self.then(workspace.shell("ls missing-*.txt matched*.txt 2>/dev/null"))
        self.assertIn("matched.txt", result.text.splitlines())

    def test_bash_is_preferred_when_zsh_is_also_available(self):
        with mock.patch.object(workspace.os, "name", "posix"), \
                mock.patch.object(workspace.os.path, "exists", side_effect=lambda path: path in {
                    "/bin/zsh", "/bin/bash", "/bin/sh"}):
            self.assertEqual(workspace._shell_command("echo completed"),
                             ["/bin/bash", "-c", "echo completed"])

    def test_python3_is_blenders_own(self):
        result = self.then(workspace.shell("python3 -c 'import sys; print(sys.executable)'"))
        self.assertIn(sys.executable, result.text)

    def test_blender_shim_forwards_version_and_background_expression_arguments(self):
        # /bin/echo stands in for Blender; the GUI driver owns checks against the actual binary.
        prefix = (f'--factory-startup --python-exit-code 1 --python {workspace.SHAPE_KEY_GUARD} '
                  f'--python {workspace.OP_NOTES} --python {workspace.RENDER_PATH_GUARD} ')
        for arguments, forwarded in (("--version", "--version"),
                                     ('-b --python-expr "print(1 + 1)"', '-b --python-expr print(1 + 1)')):
            with self.subTest(arguments=arguments):
                result = self.then(workspace.shell("blender " + arguments))
                self.assertTrue(result.ok, result.text)
                self.assertEqual(result.text.splitlines()[-1], prefix + forwarded)

    def test_long_output_is_cut_in_the_middle_and_kept_whole_in_logs(self):
        result = self.then(workspace.shell("python3 -c \"print('x' * 20000)\""))
        self.assertIn("chars omitted", result.text)
        kept = sorted((self.ws / "logs").glob("shell-*.txt"))
        self.assertEqual(len(kept), 1)
        self.assertGreater(kept[0].stat().st_size, 20000)
        self.assertIn(f"logs/{kept[0].name}", result.text)

    def test_the_time_limit_kills_it(self):
        result = self.then(workspace.shell("sleep 5", timeout=1))
        self.assertFalse(result.ok)
        self.assertIn("time limit", result.text)

    def test_a_stop_kills_it(self):
        from loopcut import llm
        with self.assertRaises(llm.Cancelled):
            self.then(workspace.shell("sleep 5"), cancelled=lambda: True)

    def test_bad_arguments(self):
        for kwargs, words in (({"command": " "}, "empty"), ({"command": "ls", "timeout": 0}, "1-3600"),
                              ({"command": "ls", "timeout": "soon"}, "number")):
            with self.assertRaisesRegex(tools.ToolError, words):
                workspace.shell(**kwargs)


class WindowsShellTest(unittest.TestCase):
    # The three launch-review failures, with project filenames replaced by synthetic fixtures.
    COMMANDS = (
        'python -c "from PIL import Image; print(\'Pillow ready\')"',
        'python3 -c "from pathlib import Path; print(Path(\'build helpers.py\').read_text(encoding=\'utf8\'));'
        'print(Path(\'hair and display.py\').read_text(encoding=\'utf8\'))"',
        'python3 -c "import os,sys,platform,importlib.util; print(\'OS:\',platform.platform()); '
        'print(\'HOME:\',os.path.expanduser(\'~\')); print(\'USERPROFILE:\',os.environ.get(\'USERPROFILE\')); '
        'print(\'Python:\',sys.executable); print(\'GIS modules:\',{m:bool(importlib.util.find_spec(m)) '
        'for m in [\'rasterio\',\'osgeo\',\'fiona\',\'shapefile\',\'pyproj\',\'tifffile\',\'numpy\']})"',
    )

    def test_old_list_serialization_reproduces_the_quoted_token_and_fixed_wrapper_preserves_code(self):
        for command in self.COMMANDS:
            with self.subTest(command=command):
                old = subprocess.list2cmdline(["cmd", "/c", command])
                # cmd strips its outer quotes, but does not consume the backslashes before inner quotes.
                old_child = old.partition(" /c ")[2][1:-1]
                broken = windows_args(old_child)[2]
                self.assertIn(broken, ('"from', '"import'))
                with self.assertRaises(SyntaxError):
                    compile(broken, "<string>", "exec")
                with mock.patch.object(workspace.os, "name", "nt"):
                    fixed = workspace._shell_command(command)
                child = fixed.partition(" /d /s /c ")[2][1:-1]
                self.assertEqual(child, command)
                args = windows_args(child)
                self.assertEqual(len(args), 3)
                compile(args[2], "<string>", "exec")

    def test_windows_inline_forms_are_refused_before_starting_any_process(self):
        code = '''print('quotes "inside"; %PATH% ^ & $HOME `whoami` 日本語')'''
        commands = [*self.COMMANDS,
                    f"py -3.13 -c '{code}'", f'python3 -Ic "{code}"',
                    f'python -X utf8 -W ignore -c "{code}"',
                    f'"C:\\Program Files\\Loopcut 日本語\\python.exe" -c "{code}"',
                    f"& 'C:\\Program Files\\Loopcut 日本語\\python.exe' -c '{code}'",
                    f'blender -b source.blend --python-expr "{code}"',
                    'cmd /c python -c "print(1)"',
                    'cmd /c "python -c \\"print(1)\\""',
                    '''powershell -NoProfile -Command "python -c 'print(1)'"''',
                    '''pwsh -Command 'python -c "print(1)"' ''',
                    'echo before && python -c "print(1)"',
                    'echo before\r\npython -c "print(1)"']
        with mock.patch.object(workspace.os, "name", "nt"), \
                mock.patch.object(workspace, "_workspace") as prepare, \
                mock.patch.object(workspace, "run_process") as run:
            for command in commands:
                with self.subTest(command=command):
                    result = workspace.shell(command)
                    self.assertFalse(result.ok)
                    self.assertIsNone(result.then)
                    self.assertEqual(len(result.text.splitlines()), 1)
                    self.assertIn("python3 file.py", result.text)
            prepare.assert_not_called()
            run.assert_not_called()

    def test_file_commands_and_other_shell_commands_are_not_inline_python(self):
        for command in ('python "build helpers.py" -c value', 'py -3 -m pip --version',
                        'python -W ignore::DeprecationWarning script.py', 'python -X dev script.py',
                        'blender -b source.blend --python "build helpers.py"',
                        'echo "python -c example"', 'git -c core.autocrlf=true status'):
            with self.subTest(command=command):
                self.assertFalse(workspace._inline_python(command))

    def test_cmd_payload_is_unchanged_for_paths_quotes_and_shell_metacharacters(self):
        command = ('"C:\\Program Files\\日本語\\tool.exe" "a b" %TEMP% ^& '
                   '&& powershell -NoProfile -Command "Write-Output $HOME; Write-Output `\"hello`\""')
        with mock.patch.object(workspace.os, "name", "nt"), \
                mock.patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}):
            wrapped = workspace._shell_command(command)
        self.assertEqual(wrapped, f'C:\\Windows\\System32\\cmd.exe /d /s /c "{command}"')

    def test_windows_native_argv_roundtrip(self):
        args = [r'C:\Program Files\Loopcut 日本語\blender.exe', '--python',
                r'C:\Users\日本語\workspace folder\build.py', '--',
                'quotes "inside"', '%TEMP%', '^&', '$HOME', '`whoami`', 'trailing slash \\', '']
        self.assertEqual(windows_args(subprocess.list2cmdline(args)), args)

    def test_native_parser_matches_the_documented_windows_examples(self):
        # Examples from Microsoft's argv documentation, independent of list2cmdline.
        for command, expected in ((r'"a b c" d e', ['a b c', 'd', 'e']),
                                  (r'"ab\"c" "\\" d', ['ab"c', '\\', 'd']),
                                  (r'a\\\b d"e f"g h', [r'a\\\b', 'de fg', 'h']),
                                  (r'a"b"" c d', ['ab" c d'])):
            with self.subTest(command=command):
                self.assertEqual(windows_args(command), expected)

    @unittest.skipUnless(os.name == "nt", "requires a real Windows cmd.exe")
    def test_real_cmd_preserves_a_quoted_native_executable(self):
        with tempfile.TemporaryDirectory(prefix="Loopcut 日本語 ") as tmp:
            script = Path(tmp) / "build helpers.py"
            script.write_text('import sys; print(sys.argv[1])', encoding="utf-8")
            # No .cmd shim: exercise cmd's outer quotes and the native executable's argv.
            command = subprocess.list2cmdline([sys.executable, str(script), "日本語 space"])
            result = subprocess.run(workspace._shell_command(command), capture_output=True,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.decode("utf-8").strip(), "日本語 space")


class ShellSummaryTest(Folder):
    def test_describe_uses_normalized_summary(self):
        command = "echo first\necho second\n"
        for summary in ("  List the installed fonts  ", " \tList  the\ninstalled\tfonts\r\n"):
            with self.subTest(summary=summary):
                self.assertEqual(workspace.describe("shell", {"command": command, "summary": summary}),
                                 ("List the installed fonts", command))

    def test_describe_without_summary_keeps_first_line(self):
        for command, title in (("  echo first\necho second\n", "$ echo first"),
                               ("echo " + "x" * 100 + "\necho second", "$ echo " + "x" * 75),
                               (" \n\t", "$ ")):
            for extra in ({}, {"summary": ""}, {"summary": " \n\t"}, {"summary": None}):
                with self.subTest(command=command, extra=extra):
                    self.assertEqual(workspace.describe("shell", {"command": command, **extra}), (title, command))

    def test_schema_requires_summary(self):
        schema = next(s["function"] for s in workspace.SCHEMAS if s["function"]["name"] == "shell")
        parameters = schema["parameters"]
        self.assertIn("summary", parameters["properties"])
        self.assertEqual(parameters["properties"]["summary"]["type"], "string")
        self.assertIn("summary", parameters["required"])
        self.assertIn("command", parameters["required"])

    def test_dispatch_preserves_command_and_timeout(self):
        command = "echo first\necho second\n"
        for timeout in ({}, {"timeout": 3}):
            for extra in ({}, {"summary": "Print two lines"}):
                with self.subTest(timeout=timeout, extra=extra), \
                        mock.patch.object(workspace, "run_process", wraps=workspace.run_process) as run:
                    result = self.then(tools.execute("shell", json.dumps({"command": command, **timeout, **extra})))
                    self.assertTrue(result.ok, result.text)
                    self.assertEqual(result.text.splitlines()[1:], ["first", "second"])
                    run.assert_called_once()
                    self.assertEqual(run.call_args.args[0], workspace._shell_command(command))
                    self.assertEqual(run.call_args.args[3], timeout.get("timeout", workspace.SHELL_TIMEOUT))

    def test_agent_card_uses_summary(self):
        from loopcut import llm
        command = "echo first\necho second\n"
        call = llm.ToolCall(id="call", name="shell", arguments=json.dumps({
            "command": command, "summary": "  List  the\ninstalled fonts  "}))
        card = state.item_tool(call.name, *agent._describe(call))
        self.assertEqual(card["summary"], "List the installed fonts")
        self.assertEqual(card["code"], command)

    def test_summary_preserves_approval(self):
        for command, approval in (("echo first", "gate"), ("python3 -m pip install --upgrade pip", "install")):
            for extra in ({}, {"summary": "List the installed fonts"}):
                with self.subTest(command=command, extra=extra):
                    arguments = {"command": command, **extra}
                    self.assertTrue(workspace.needs_approval("shell", arguments, (self.ws,)))
                    self.assertEqual(agent._approval("shell", json.dumps(arguments), (self.ws,)), approval)


class RunBlenderTest(Folder):
    def test_file_route_preserves_code_and_native_arguments_without_a_shell(self):
        script = self.ws / "日本語 build helpers.py"
        literal = 'quotes "inside" %TEMP% ^ & $HOME `whoami` 日本語'
        code = f'print({literal!r})\r\n'
        files.write_file(script.name, code)
        self.assertEqual(script.read_bytes(), code.encode("utf-8"))
        # Execute the generated file with real Python to check its text independently of Blender.
        output = subprocess.check_output([sys.executable, str(script)], cwd=self.ws,
                                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(output.decode("utf-8").strip(), literal)

        def run(command, cwd, env, timeout, log, cancelled, label=""):
            self.assertIsInstance(command, list)
            self.assertEqual(windows_args(subprocess.list2cmdline(command)), command)
            self.assertIn(str(script), command)
            self.assertEqual(command[-5:], [str(script), "--", literal, "space 日本語", '"quoted"'])
            log.write_text("completed", encoding="utf-8")
            return 0, 0.1, ""

        with mock.patch.object(workspace, "run_process", side_effect=run) as process:
            result = self.then(workspace.run_blender(script.name, blend="", args=[literal, "space 日本語", '"quoted"']))
        self.assertTrue(result.ok, result.text)
        process.assert_called_once()

    def test_validation(self):
        (self.ws / "build.py").write_text("print('hi')")
        (self.ws / "notes.txt").write_text("x")
        with self.assertRaisesRegex(tools.ToolError, "does not exist"):
            workspace.run_blender("missing.py")
        with self.assertRaisesRegex(tools.ToolError, "not a .blend"):
            workspace.run_blender("build.py", blend="notes.txt")
        with self.assertRaisesRegex(tools.ToolError, "list of strings"):
            workspace.run_blender("build.py", blend="", args="--final")

    def test_the_command_the_log_and_what_it_wrote(self):
        # /bin/echo stands in for Blender: it prints the command line it was given.
        (self.ws / "build.py").write_text("print('hi')")
        png(self.ws / "old.png", 4, 4)
        result = self.then(workspace.run_blender("build.py", blend="", args=["--final"]))
        self.assertTrue(result.ok, result.text)
        self.assertIn("build.py on an empty scene: exit 0", result.text)
        self.assertIn("-b --factory-startup --python-exit-code 1 --python", result.text)
        self.assertIn(f'--python-exit-code 1 --python {workspace.SHAPE_KEY_GUARD} '
                      f'--python {workspace.OP_NOTES} --python {workspace.RENDER_PATH_GUARD} '
                      f'--python {self.ws / "build.py"} -- --final', result.text)
        self.assertIn("-- --final", result.text)
        self.assertNotIn("Wrote:", result.text, "nothing was written")
        self.assertEqual(len(list((self.ws / "logs").glob("*-build.log"))), 1)

    def test_guard_notices_are_visible_once_even_outside_the_shown_tail(self):
        (self.ws / "build.py").write_text("print('hi')")
        (self.ws / "source.blend").write_bytes(b"fixture")
        notice = ('Loopcut: key block "smile" of shape keys "Key" (mesh "Face") was removed '
                  'and Blender removed its driver with it (key_blocks["smile"].value)')
        for output in (notice + "\n" + "output\n" * 80,
                       "output\n" + notice + "\n",
                       "x" * 5000 + "\n" + notice + "\n" + "x" * 5000):
            with self.subTest(length=len(output)):
                def run(command, ws, env, seconds, log, is_cancelled, label=""):
                    self.assertEqual(label, "build.py")
                    self.assertEqual(command, ["/bin/echo", "-b", "--factory-startup",
                                               str(self.ws / "source.blend"), "--python-exit-code", "1",
                                               "--python", str(workspace.SHAPE_KEY_GUARD),
                                               "--python", str(workspace.OP_NOTES),
                                               "--python", str(workspace.RENDER_PATH_GUARD),
                                               "--python", str(self.ws / "build.py"), "--"])
                    log.write_text(output)
                    return 0, 0.1, False

                with mock.patch.object(workspace, "run_process", side_effect=run):
                    result = self.then(workspace.run_blender("build.py"))
                self.assertEqual(result.text.count(notice), 1, result.text)


class EditFileTest(Folder):
    def setUp(self):
        super().setUp()
        self.file = self.ws / "build.py"
        self.file.write_text("A = 1\nB = 2\nA = 1\n")

    def test_one_exact_replacement(self):
        result = workspace.edit_file("build.py", "B = 2", "B = 3\nC = 4")
        self.assertIn("1 replacement(s) at line 2", result.text)
        self.assertEqual(self.file.read_text(), "A = 1\nB = 3\nC = 4\nA = 1\n")

    def test_ambiguous_missing_and_all(self):
        with self.assertRaisesRegex(tools.ToolError, r"occurs 2 times .*lines 1, 3"):
            workspace.edit_file("build.py", "A = 1", "A = 5")
        with self.assertRaisesRegex(tools.ToolError, "is not in build.py"):
            workspace.edit_file("build.py", "Z = 9", "Z = 1")
        with self.assertRaisesRegex(tools.ToolError, "must not be empty"):
            workspace.edit_file("build.py", "", "x")
        workspace.edit_file("build.py", "A = 1", "A = 5", replace_all=True)
        self.assertEqual(self.file.read_text(), "A = 5\nB = 2\nA = 5\n")
        self.assertEqual(sorted(p.name for p in self.ws.iterdir() if p.name.endswith("edit")), [], "no temp file left")


class ImageTest(Folder):
    def image(self, width, height):
        image = mock.Mock(size=(width, height))
        image.scale.side_effect = lambda w, h: setattr(image, "size", (w, h))
        image.save.side_effect = lambda: png(Path(image.filepath_raw), *image.size)
        return image

    def test_final_render_reaches_chatgpt_at_1920_with_high_detail(self):
        from loopcut import chatgpt_responses
        target = self.ws / "hero.png"
        png(target, 1920, 1080)
        pixels = mock.Mock(shape=(1080, 1920, 4))
        image = self.image(1920, 1080)
        images = mock.Mock()
        images.new.return_value = image
        with mock.patch.object(tools, "_pixels", return_value=pixels), \
                mock.patch.object(tools.bpy, "data", types.SimpleNamespace(images=images), create=True):
            result = workspace.view_image("hero.png")
        self.assertIn("1920x1080 px", result.text)
        self.assertTrue(result.file_image)
        image.scale.assert_not_called()
        reference = conversations.store_image(self.session, result.image_path)
        messages = [{"role": "user", "content": "Review this render."},
                    agent._image_message(reference, result.text, file_preview=result.file_image)]
        body = chatgpt_responses.request_body(
            model="gpt-6.1-sol", messages=conversations.wire_messages(self.session["id"], messages, keep=True),
            tools=[], reasoning_effort="xhigh", disabled=set())
        part = body["input"][-1]["content"][1]
        raw = base64.b64decode(part["image_url"].split(",", 1)[1])
        self.assertEqual(struct.unpack(">II", raw[16:24]), (1920, 1080))
        self.assertEqual(part["detail"], "high")

    def test_large_texture_is_bounded_and_small_crop_is_not_upscaled(self):
        (self.ws / "texture.png").write_bytes(b"image")
        for width, height, shown in ((4096, 4096, (2048, 2048)), (400, 200, (400, 200))):
            with self.subTest(size=(width, height)):
                pixels = mock.Mock(shape=(height, width, 4))
                image = self.image(width, height)
                images = mock.Mock()
                images.new.return_value = image
                with mock.patch.object(tools, "_pixels", return_value=pixels), \
                        mock.patch.object(tools, "crop", return_value=(pixels, "the selected region")), \
                        mock.patch.object(tools.bpy, "data", types.SimpleNamespace(images=images), create=True):
                    result = workspace.view_image("texture.png", region=[0, 0, 1, 1])
                self.assertEqual(workspace.image_size(result.image_path), shown)
                self.assertTrue(result.file_image)
                self.assertIn(f"{shown[0]}x{shown[1]} px", result.text)
                images.remove.assert_called_once_with(image)

    def test_new_references_keep_a_2048_copy_and_a_cheap_copy(self):
        from loopcut import attachments
        source = self.ws / "reference.jpg"
        source.write_bytes(b"jpeg")
        images = mock.Mock()
        made = [self.image(3575, 2011), self.image(3575, 2011)]
        images.load.side_effect = made
        with mock.patch.object(attachments.bpy, "data", types.SimpleNamespace(images=images), create=True):
            self.assertEqual(attachments.add(self.session, [str(source)]), [])
        reference = self.session["attachments"][0]
        self.assertEqual(workspace.image_size(conversations.image_path(self.session["id"], reference["full"])),
                         (2048, 1152))
        self.assertEqual(workspace.image_size(conversations.image_path(self.session["id"], reference["ref"])),
                         (768, 432))
        self.assertEqual(images.remove.call_count, 2)

    def test_workspace_request_sends_full_reference_without_rewriting_history(self):
        from loopcut import llm
        picture = self.ws / "reference.png"
        picture.write_bytes(b"small image")
        small = conversations.store_image(self.session, picture)
        picture.write_bytes(b"full image")
        full = conversations.store_image(self.session, picture)
        self.session["references"] = [{"ref": small, "full": full, "name": "reference.png", "pinned": True}]
        self.session["messages"] = [{"role": "user", context.ATTACHED: True, "content": [
            {"type": "text", "text": "Paint this."}, {"type": "image_url", "image_url": {"url": small}}]}]
        cfg = config.Config("", config.CHATGPT_PLAN_BASE_URL, "gpt-6.1-sol", "xhigh", True, True, 200, 200000,
                            keep_context=True)
        with mock.patch.object(llm, "stream_chat", return_value=llm.Completion(text="Done", finish_reason="stop")) as stream:
            agent._request(self.session, agent.Turn(), cfg, agent.WORKSPACE_SPEC)
        sent = stream.call_args.kwargs["messages"][1]["content"][1]["image_url"]["url"]
        self.assertEqual(base64.b64decode(sent.split(",", 1)[1]), b"full image")
        self.assertEqual(self.session["messages"][0]["content"][1]["image_url"]["url"], small)
        self.session["mode"] = ""
        with mock.patch.object(llm, "stream_chat", return_value=llm.Completion(text="Done", finish_reason="stop")) as stream:
            agent._request(self.session, agent.Turn(), cfg)
        sent = stream.call_args.kwargs["messages"][1]["content"][1]["image_url"]["url"]
        self.assertEqual(base64.b64decode(sent.split(",", 1)[1]), b"small image")


class ApplyArgumentsTest(Folder):
    def test_validation(self):
        (self.ws / "result.txt").write_text("x")
        (self.ws / "result.blend").write_bytes(b"BLENDER")
        with self.assertRaisesRegex(tools.ToolError, "not a .blend"):
            workspace.apply_build("result.txt")
        with self.assertRaisesRegex(tools.ToolError, "list of datablock names"):
            workspace.apply_build("result.blend", owns="TreasureChest")
        with self.assertRaisesRegex(tools.ToolError, "does not exist"):
            workspace.apply_build("missing.blend")


class PureTest(unittest.TestCase):
    def test_image_sizes_from_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            png(Path(tmp) / "a.png", 320, 180)
            jpeg_header(Path(tmp) / "b.jpg", 1920, 1080)
            (Path(tmp) / "c.png").write_bytes(b"not an image")
            self.assertEqual(workspace.image_size(Path(tmp) / "a.png"), (320, 180))
            self.assertEqual(workspace.image_size(Path(tmp) / "b.jpg"), (1920, 1080))
            self.assertIsNone(workspace.image_size(Path(tmp) / "c.png"))

    def test_what_a_run_wrote(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            before = workspace.files_state(ws)
            png(ws / "preview.png", 96, 54)
            (ws / "result.blend").write_bytes(b"x" * 2500)
            (ws / "maps").mkdir()
            for n in range(15):
                (ws / "maps" / f"m{n:02}.txt").write_text("m")
            (ws / "logs").mkdir()
            (ws / "logs" / "0001-build.log").write_text("log")
            text = workspace.written(ws, before, workspace.files_state(ws))
            self.assertTrue(text.startswith("Wrote: "), text)
            self.assertIn("preview.png (96x54)", text)
            self.assertIn("result.blend (2.5 KB)", text)
            self.assertIn("and 5 more in maps/", text)
            self.assertNotIn("logs", text)
            self.assertEqual(workspace.written(ws, workspace.files_state(ws), workspace.files_state(ws)), "")

    def test_the_log_tail_leaves_out_progress_and_start_up(self):
        log = "\n".join(["Blender 5.2.2 LTS (hash x)", "00:01.0  blend  | Read blend: \"/x/source.blend\"",
                         "00:00.0  color_management | Using OCIO=/x/config.ocio",
                         *[f"Fra:1 Mem:12M | Rendering {n} / 64 samples" for n in range(50)],
                         "00:13.682  render  | Saved: '/x/hero.png'", "BUILD OK", "Blender quit"])
        tail, noise = workspace.log_tail(log)
        self.assertEqual(tail, "00:13.682  render  | Saved: '/x/hero.png'\nBUILD OK")
        self.assertEqual(noise, 54)

    def test_settings_follow_the_build_unless_they_changed_live(self):
        source = {"view_settings.look": "None", "render.engine": "BLENDER_EEVEE", "render.resolution_x": 1920,
                  "eevee.taa_render_samples": 64}
        built = {**source, "view_settings.look": "AgX - Medium High Contrast", "render.engine": "CYCLES",
                 "eevee.taa_render_samples": 64}
        live = {**source, "render.engine": "BLENDER_WORKBENCH"}  # The user switched it meanwhile.
        wanted, kept = workspace.settings_changes(built, source, live, source)
        self.assertEqual(wanted, {"view_settings.look": "AgX - Medium High Contrast"})
        self.assertEqual(kept, {"render.engine": ("BLENDER_WORKBENCH", "CYCLES")})
        # After an apply set the engine, a rebuild that keeps it is no change, and one that changes it again goes.
        applied = {**source, "render.engine": "CYCLES"}
        wanted, kept = workspace.settings_changes({**source, "render.engine": "BLENDER_EEVEE_NEXT"}, source, applied, applied)
        self.assertEqual((wanted, kept), ({"render.engine": "BLENDER_EEVEE_NEXT"}, {}))

    def test_the_live_side_counts_applies_since_the_builds_snapshot(self):
        data = {"snapshots": [{"number": 1, "tokens": {"OBJECT:Chest": "s1", "OBJECT:Barrel": "b1"}},
                              {"number": 2, "tokens": {"OBJECT:Chest": "a1", "OBJECT:ChestKey": "k1"}}],
                "live": {"tokens": {"OBJECT:Chest": ["a1", 1], "OBJECT:ChestKey": ["k1", 1], "OBJECT:Rim": ["r2", 2]},
                         "names": {}, "pointers": {"snapshots": 1, "value": {"world": "WORLD:Arcane"}},
                         "settings": {"snapshots": 2, "value": {"render.engine": "CYCLES"}}}}
        first, second = data["snapshots"]
        tokens, pointers, settings = workspace.live_side(data, first)
        self.assertEqual(tokens, {"OBJECT:Chest": "a1", "OBJECT:ChestKey": "k1", "OBJECT:Rim": "r2"})
        self.assertEqual(pointers, {"world": "WORLD:Arcane"})
        self.assertEqual(settings, {"render.engine": "CYCLES"})
        tokens, pointers, _ = workspace.live_side(data, second)
        self.assertEqual(tokens, {"OBJECT:Rim": "r2"}, "snapshot 2 already has what applied before it")
        self.assertIsNone(pointers)
        self.assertEqual(workspace.recreated_keys(data, first), ["OBJECT:ChestKey", "OBJECT:Rim"])
        self.assertEqual(workspace.recreated_keys(data, second), ["OBJECT:Rim"])
        self.assertIs(workspace.snapshot_for(data, 1), first)
        self.assertIs(workspace.snapshot_for(data, None), second, "no number: the newest")
        self.assertIs(workspace.snapshot_for(data, 7), second)

    def test_the_tools_ask_like_run_python_and_view_image_reads_like_read_file(self):
        roots = (Path("/w"),)
        for name in ("shell", "run_blender", "edit_file"):
            self.assertTrue(workspace.needs_approval(name, {}, roots), name)
            self.assertEqual(agent._approval(name, json.dumps({}), roots), "gate", name)
        self.assertFalse(workspace.needs_approval("view_image", {"path": "/w/preview.png"}, roots))
        self.assertTrue(workspace.needs_approval("view_image", {"path": "/elsewhere/x.png"}, roots))
        self.assertEqual(agent._approval("apply_build", json.dumps({"blend": "r.blend"}), roots), "gate")
        self.assertEqual(agent._approval("snapshot_scene", "{}", roots), "")
        self.assertIn("apply_build", tools.CHANGES_SCENE)  # A checkpoint first (agent._changes_scene).
        self.assertFalse(set(workspace.TOOLS) - {"apply_build"} & tools.CHANGES_SCENE)
        for name in workspace.TOOLS:
            self.assertTrue(tools.changes_over_time(name, {}), f"{name} may run twice in one reply")
        self.assertLessEqual({"shell", "run_blender", "edit_file"}, agent._ACTS)

    def test_every_tool_is_offered_described_and_dispatched(self):
        names = [s["function"]["name"] for s in workspace.SCHEMAS]
        self.assertEqual(tuple(names), workspace.TOOLS)
        self.assertEqual(set(workspace.DISPATCH), set(names))
        for schema in workspace.SCHEMAS:
            function = schema["function"]
            for required in function["parameters"].get("required", []):
                self.assertIn(required, function["parameters"]["properties"], function["name"])
        offered = [s["function"]["name"] for s in agent._workspace_schemas()]
        self.assertEqual(len(offered), len(set(offered)), "a tool offered twice")
        self.assertFalse(set(names) & {s["function"]["name"] for s in agent._tool_schemas()},
                         "the plain single agent does not get them")
        self.assertEqual(workspace.describe("shell", {"command": "ls -la\necho hi"}), ("$ ls -la", "ls -la\necho hi"))
        self.assertEqual(workspace.describe("run_blender", {"script": "build.py"})[0], "Blender: build.py on source.blend")
        self.assertIn("owns TreasureChest", workspace.describe("apply_build", {"blend": "r.blend", "owns": ["TreasureChest"]})[0])

    def test_its_own_prompt_says_what_the_environment_is_and_no_workflow(self):
        text = workspace.system_prompt("Add-ons: x")
        self.assertTrue(text.startswith("You are Loopcut,"), "codex_answer.role_of reads it as the single agent")
        self.assertTrue(text.endswith("\n\nAdd-ons: x"))
        self.assertEqual(workspace.system_prompt(), workspace.PROMPT)
        for words in ("source.blend", "apply_build", "view_image", "owns", "run_python", "<scene_context>", "<steer>"):
            self.assertIn(words, workspace.PROMPT)
        for prescribed in ("small previews", "studio", "Render full size only", "Verify with a script",
                           "Do not rebuild materials"):  # The workflow and rules evals 20261001-000435/-092556 had.
            self.assertNotIn(prescribed, workspace.PROMPT)
        self.assertNotIn("## Workers", workspace.PROMPT)

    def test_the_live_tools_it_keeps_exist(self):
        live = {s["function"]["name"] for s in agent._tool_schemas()}
        self.assertLessEqual(set(workspace.LIVE_TOOLS), live)
        offered = [s["function"]["name"] for s in agent._workspace_schemas()]
        self.assertEqual(offered, list(workspace.LIVE_TOOLS) + list(workspace.TOOLS))

    def test_inspect_api_keeps_the_single_agents_schema_and_read_semantics(self):
        schema = next(s for s in tools.SCHEMAS if s['function']['name'] == 'inspect_api')
        self.assertIn(schema, agent._workspace_schemas())
        self.assertIn('inspect_api', workspace.LIVE_TOOLS)
        arguments = json.dumps({'path': 'looptools'})
        self.assertEqual(agent._approval('inspect_api', arguments, ()), '')
        self.assertFalse(agent._changes_scene('inspect_api', arguments))
        self.assertFalse(agent._acts('inspect_api', arguments))
        self.assertTrue(agent._same_while_unchanged('inspect_api', arguments))

    def test_the_fixed_cost_cap(self):
        measured = context.fixed_tokens(agent._workspace_prompt({}), agent._workspace_schemas())
        self.assertLessEqual(measured, context.WORKSPACE_FIXED_TOKENS)
        self.assertLessEqual(context.fixed_tokens(agent.SYSTEM_PROMPT, agent._tool_schemas()), context.FIXED_TOKENS,
                             "the plain single agent's cost is unchanged")


class ChoosingTheLoopTest(unittest.TestCase):
    def setUp(self):
        handle, env_file = tempfile.mkstemp(suffix=".env")
        os.close(handle)
        self.addCleanup(os.unlink, env_file)
        self.env = mock.patch.dict(os.environ, {"LOOPCUT_API_KEY": "k", "LOOPCUT_BASE_URL": config.SERVICE_BASE_URL,
                                                "LOOPCUT_MODEL": "m", "LOOPCUT_ENV_FILE": env_file})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.prefs = mock.patch.object(config, "_preference_values", return_value={})
        self.prefs.start()
        self.addCleanup(self.prefs.stop)

    def configure(self, session, **env):
        from loopcut import cloud
        with mock.patch.dict(os.environ, env), mock.patch.object(agent, "_keeps_context", return_value=False), \
                mock.patch.object(cloud, 'prompt_index_available', return_value=True):
            turn = agent.Turn()
            agent._configure(turn, session)
        return turn

    def test_signed_in_with_chatgpt_the_workspace_agent_is_the_default(self):
        """The ChatGPT plan gets eval test F's setup whatever the workers switch says: the workspace agent in keep
        mode with the 200k floor, no orchestrator, router or craft notes, high effort, no default step limit."""
        plan = {"LOOPCUT_BASE_URL": config.CHATGPT_PLAN_BASE_URL, "LOOPCUT_ORCHESTRATE": "true",
                "LOOPCUT_WORKSPACE": "false", "LOOPCUT_REASONING_EFFORT": ""}
        turn = self.configure(state.new_session(), **plan)
        self.assertIs(turn.spec, agent.WORKSPACE_SPEC)
        self.assertTrue(turn.config.keep_context)
        self.assertGreaterEqual(turn.config.context_budget, context.WORKSPACE_BUDGET)
        self.assertEqual((turn.config.reasoning_effort, turn.config.max_steps), ("high", 0))
        with mock.patch.dict(os.environ, plan):
            roles = config.load_roles()
        self.assertEqual((roles.orchestrate, roles.workspace, roles.router, roles.single_craft), (False, True, None, False))
        with mock.patch.dict(os.environ, {**plan, "LOOPCUT_SINGLE_CRAFT": "on", "LOOPCUT_REASONING_EFFORT": "xhigh"}):
            self.assertTrue(config.load_roles().single_craft, "craft notes only when asked for")
            self.assertEqual(config.load().reasoning_effort, "xhigh")
        other = self.configure(state.new_session(), LOOPCUT_ORCHESTRATE="true", LOOPCUT_WORKSPACE="false",
                               LOOPCUT_REASONING_EFFORT="")
        self.assertIs(other.spec, agent.ORCH_SPEC, "the gateway still runs the orchestrator")
        self.assertEqual(other.config.reasoning_effort, "medium")

    def test_off_by_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("LOOPCUT_WORKSPACE", None)
            self.assertFalse(config.load_roles().workspace)
        session = state.new_session()
        turn = self.configure(session, LOOPCUT_ORCHESTRATE="false", LOOPCUT_WORKSPACE="false")
        self.assertIs(turn.spec, agent.SINGLE_SPEC)
        self.assertEqual(session["mode"], "")

    def test_the_single_loop_with_the_workspace_on(self):
        session = state.new_session()
        turn = self.configure(session, LOOPCUT_ORCHESTRATE="false", LOOPCUT_WORKSPACE="on")
        self.assertIs(turn.spec, agent.WORKSPACE_SPEC)
        self.assertEqual(session["mode"], "workspace")
        self.assertEqual(turn.config.model, "m")
        self.assertTrue(turn.config.keep_context)
        self.assertEqual(turn.config.context_budget, context.WORKSPACE_BUDGET, "the 40k default is raised")
        turn = self.configure(state.new_session(), LOOPCUT_ORCHESTRATE="false", LOOPCUT_WORKSPACE="on",
                              LOOPCUT_CONTEXT_BUDGET="400000")
        self.assertEqual(turn.config.context_budget, 400_000, "a larger budget stands")
        self.assertFalse(agent.WORKSPACE_SPEC.reference_card)

    def test_orchestrated_a_fresh_conversation_may_go_to_the_workspace_and_one_that_delegated_may_not(self):
        session = state.new_session()
        turn = self.configure(session, LOOPCUT_ORCHESTRATE="true", LOOPCUT_WORKSPACE="on", LOOPCUT_ORCH_MODEL="orch")
        self.assertIs(turn.spec, agent.ORCH_SPEC)
        self.assertTrue(turn.may_divert)
        session["messages"] = [{"role": "assistant", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "delegate", "arguments": "{}"}}]}]
        self.assertFalse(self.configure(session, LOOPCUT_ORCHESTRATE="true", LOOPCUT_WORKSPACE="on").may_divert)
        self.assertFalse(self.configure(state.new_session(), LOOPCUT_ORCHESTRATE="true",
                                        LOOPCUT_WORKSPACE="off").may_divert, "workspace off")
        session = state.new_session()
        session["mode"] = "workspace"
        turn = self.configure(session, LOOPCUT_ORCHESTRATE="true", LOOPCUT_WORKSPACE="on", LOOPCUT_ORCH_MODEL="orch")
        self.assertIs(turn.spec, agent.WORKSPACE_SPEC, "a conversation sent there stays there")
        self.assertEqual(turn.config.model, "orch", "with the orchestrator's model")

    def test_the_mode_is_saved_with_the_conversation(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": tmp}):
            session = state.new_session()
            session["mode"] = "workspace"
            session["items"] = [state.item_user("hi")]
            conversations.save(session)
            self.assertEqual(conversations.load(session["id"])["mode"], "workspace")


class RouterTest(unittest.TestCase):
    def decision(self, level, *roles):
        return router.Decision(level=level, deliverables=[(f"D{n}", "q", role) for n, role in enumerate(roles)])

    def test_a_look_goes_to_the_workspace_and_building_stays_with_the_orchestrator(self):
        self.assertTrue(router.to_workspace(self.decision(2, "materials")))
        self.assertTrue(router.to_workspace(self.decision(3, "materials", "shot")))
        self.assertFalse(router.to_workspace(self.decision(2, "modeling")))
        self.assertFalse(router.to_workspace(self.decision(3, "materials", "modeling")))
        self.assertFalse(router.to_workspace(self.decision(3, "materials", "lighting", "camera")), "three is a job for workers")
        self.assertFalse(router.to_workspace(self.decision(1)))
        self.assertFalse(router.to_workspace(self.decision(2)), "no deliverable named")

    def test_a_diverted_turn_gets_no_note(self):
        reply = '{"level": 2, "deliverables": [{"name": "ChestLook", "quote": "the chest", "role": "materials"}]}'
        session = state.new_session()
        session["turn_number"] = 1
        message = {"role": "user", "content": "Restyle the chest"}
        session["messages"] = [message]
        turn = agent.Turn()

        def stream(**kwargs):
            from loopcut import llm
            return llm.Completion(text=reply, tool_calls=[], usage={}, finish_reason="stop")
        cfg = types.SimpleNamespace(base_url=config.SERVICE_BASE_URL, api_key="k", model="m", reasoning_effort="low",
                                    max_output_tokens=100, call_max_seconds=0)
        from prompt_index_fixture import index_patch
        with index_patch():
            router.start_turn(session, turn, cfg, lambda: False, stream=stream, divert=router.to_workspace)
        self.assertTrue(turn.diverted)
        self.assertEqual(message["content"], "Restyle the chest")
        self.assertEqual(session["routing"]["level"], 2)


class CraftNotesTest(unittest.TestCase):
    def setUp(self):
        from loopcut import cloud
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for patcher in (mock.patch.dict(os.environ, {'LOOPCUT_SKILLS_DIR': str(self.directory),
                                                     'LOOPCUT_SINGLE_CRAFT': 'on'}),
                        mock.patch.object(cloud, '_runtime', None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def skill(self, name, required):
        (self.directory / (name + '.md')).write_text(f'''---
id: workspace-test/{name}
version: 1
title: Workspace tool note
when: A workspace test request
roles: [workspace]
requires_tools: [{required}]
tier: free
kind: assist
words: probe
---
Note for {name}.
''')

    def notes(self, *, codex=False):
        turn = agent.Turn()
        turn.codex = object() if codex else None
        with mock.patch.object(workspace, 'start_turn') as start:
            agent._workspace_turn({}, turn)
        return start.call_args.kwargs['craft']('probe')

    def test_workspace_notes_use_live_tools_without_cloud(self):
        self.skill('python', 'run_python')
        self.skill('shell', 'shell')
        self.skill('uv', 'inspect_uv_layout')
        self.skill('image', 'loopcut_cloud.image_generate')
        self.assertEqual(skills.single_notes('probe', extra_dir=self.directory, role='workspace'), '')
        notes = self.notes()
        self.assertIn('Note for python.', notes)
        self.assertIn('Note for shell.', notes)
        self.assertNotIn('Note for uv.', notes)
        self.assertNotIn('Note for image.', notes)
        schemas = agent._workspace_schemas() + [{'function': {'name': 'inspect_uv_layout'}}]
        with mock.patch.object(agent, '_workspace_schemas', return_value=schemas):
            self.assertIn('Note for uv.', self.notes())

    def test_workspace_notes_keep_cloud_names_and_policy(self):
        from loopcut import cloud
        self.skill('python', 'run_python')
        self.skill('image', 'loopcut_cloud.image_generate')
        self.skill('image-wire', 'loopcut_cloud__image_generate')
        schema = {'type': 'function', 'function': {'name': 'loopcut_cloud__image_generate',
                  'description': 'Test image tool', 'parameters': {'type': 'object', 'properties': {}}}}
        fields = dict(id='workspace-test/served', version=1, title='Served note', when='A probe',
                      roles=['workspace'], requires_tools=['loopcut_cloud.image_generate'], tier='free',
                      kind='gen', words='probe')
        options = dict(server_skills=[dict(front_matter=fields, body='Served note.', sha256='a' * 64)],
                       available_tools=['loopcut_cloud.image_generate'], no_generation=False)
        with mock.patch.object(cloud, 'skill_options', return_value=options), \
                mock.patch.object(cloud, 'tool_schemas', return_value=[schema]):
            notes = self.notes()
            self.assertIn('Note for python.', notes)
            self.assertIn('Note for image.', notes)
            self.assertIn('Note for image-wire.', notes)
            self.assertIn('Served note.', notes)
            options['no_generation'] = True
            self.assertNotIn('Served note.', self.notes())

    def test_codex_notes_use_its_dynamic_tools_and_shell(self):
        from loopcut import cloud, codex_app
        options = dict(available_tools=['loopcut_cloud.image_generate'])
        for name, required in (('python', 'run_python'), ('shell', 'shell'), ('blender', 'run_blender'),
                               ('uv', 'inspect_uv_layout'), ('image', 'loopcut_cloud.image_generate')):
            self.skill(name, required)
        dynamic = codex_app.dynamic_tools() + [{'name': 'inspect_uv_layout'}]
        with mock.patch.object(cloud, 'skill_options', return_value=options), \
                mock.patch.object(codex_app, 'dynamic_tools', return_value=dynamic):
            notes = self.notes(codex=True)
        # run_blender is one of Codex's dynamic tools since the Windows file runner (0.2.10), so its note comes too.
        for name in ('python', 'shell', 'uv', 'image', 'blender'):
            self.assertIn(f'Note for {name}.', notes)

    def test_single_notes_select_fetched_index_entries(self):
        from prompt_index_fixture import built_index
        entries = []
        for entry in built_index()['skills']:
            if entry['id'].startswith('single/'):
                fields = {k: v for k, v in entry.items() if k not in ('chars', 'sha256')}
                entries.append(dict(front_matter=fields, body=entry['id'], sha256=entry['sha256']))
        for role in ('single', 'workspace'):
            with self.subTest(role=role):
                notes = skills.single_notes('Arcane-style, hand-painted and low-poly', role=role, server_skills=entries)
                self.assertIn('single/painterly', notes)
                self.assertIn('single/light', notes)
                self.assertIn('single/stylised', notes)
                self.assertNotIn('single/toon', notes)
                self.assertEqual(skills.single_notes('real oak with brass fittings', role=role, server_skills=entries), '')

    def test_single_craft_off_sends_none(self):
        # LOOPCUT_SINGLE_CRAFT=off: the workspace turn passes no craft function, so no <craft_notes>.
        seen = {}
        with mock.patch.dict(os.environ, {"LOOPCUT_SINGLE_CRAFT": "off"}), \
                mock.patch.object(workspace, "start_turn", lambda *a, **k: seen.update(k)):
            self.assertFalse(config.load_roles().single_craft)
            agent._workspace_turn({}, types.SimpleNamespace(cancel=types.SimpleNamespace(is_set=lambda: False)))
        self.assertIsNone(seen["craft"])
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(workspace, "start_turn", lambda *a, **k: seen.update(k)):
            os.environ.pop("LOOPCUT_SINGLE_CRAFT", None)
            agent._workspace_turn({}, types.SimpleNamespace(cancel=types.SimpleNamespace(is_set=lambda: False)))
        self.assertTrue(callable(seen['craft']))


if __name__ == "__main__":
    unittest.main()
