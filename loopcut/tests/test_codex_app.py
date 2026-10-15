"""codex_app.py against a scripted app-server (fake_codex_app_server.py), through agent.run_loop. No Blender,
no Codex, no network.

    python3 -m unittest discover -s tests -v
"""

import json
import os
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

from loopcut import agent, codex_app, config, context, conversations, state, tools, workspace  # noqa: E402

FAKE = Path(__file__).with_name("fake_codex_app_server.py")
ENV = {"LOOPCUT_API_KEY": "", "LOOPCUT_BASE_URL": "", "LOOPCUT_MODEL": "", "LOOPCUT_AUTO_RUN": "false",
       "LOOPCUT_ORCHESTRATE": "false", "LOOPCUT_ROUTER": "off", "LOOPCUT_CODEX_APP": "true",
       "LOOPCUT_CODEX_MODEL": "gpt-6.1-sol", "LOOPCUT_CODEX_EFFORT": "low"}


class CodexAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws, self.home, self.record = root / "ws", root / "codex-home", root / "record.jsonl"
        self.ws.mkdir()
        self.home.mkdir()
        (self.home / "config.toml").write_text('model = "x"\n[mcp_servers.kaggle]\ncommand = "k"\n'
                                               '[mcp_servers.kaggle.env]\nSECRET = "s"\n[mcp_servers."computer-use"]\n')
        binary = root / "codex"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        binary.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {**ENV, "LOOPCUT_DATA_DIR": str(root / "data"), "LOOPCUT_CODEX_BIN": str(binary),
                                                "CODEX_HOME": str(self.home), "FAKE_CODEX_RECORD": str(self.record),
                                                "OPENAI_API_KEY": "sk-should-not-pass", "FAKE_CODEX_SCENARIO": "tool"})
        self.env.start()
        self.patches = [mock.patch.object(agent, name, value) for name, value in (
            ("_redraw", lambda: None), ("_on_main", lambda fn, is_cancelled=None, name="": fn()),
            ("_workspace_turn", lambda session, turn: None), ("_keeps_context", lambda cfg: False),
            ("_tool_schemas", lambda: tools.SCHEMAS + [{"type": "function", "function": {
                "name": n, "description": f"{n} tool", "parameters": {"type": "object", "properties": {}}}}
                for n in ("search_assets", "import_asset", "add_object")]))]
        self.patches += [mock.patch.object(workspace, "ensure", lambda session: self.ws),
                         mock.patch.object(workspace, "_binary", lambda: "/usr/bin/true")]
        for patch in self.patches:
            patch.start()
        self.session = state.reset()
        self.ran: list = []

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def run_tool(self, name, arguments):
        self.ran.append((name, json.loads(arguments)))
        return types.SimpleNamespace(text="x is 1", ok=True, image_path=None)

    def start(self, text: str = "paint the cube red", scenario: str = "tool", auto_run: bool = True) -> agent.Turn:
        os.environ["FAKE_CODEX_SCENARIO"] = scenario
        os.environ["LOOPCUT_AUTO_RUN"] = "true" if auto_run else "false"
        session = self.session
        session["items"].append(state.item_user(text))
        session["messages"].append({"role": "user", "content": f"<scene_context>\nfile: unsaved\n</scene_context>\n\n{text}"})
        turn = agent.Turn()
        agent._configure(turn, session)
        session["busy"], session["turn"] = True, turn
        turn.thread = threading.Thread(target=agent._run, daemon=True,
                                       args=(session, turn, lambda *a: self.run_tool(*a), lambda s: None))
        turn.thread.start()
        return turn

    def received(self) -> list:
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def wait_for(self, predicate, what: str):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}; items={self.session['items']}")

    def test_configure_hands_every_turn_to_codex_without_an_api_key(self):
        turn = agent.Turn()
        agent._configure(turn, self.session)
        self.assertIs(turn.spec, agent.CODEX_SPEC)
        self.assertEqual((turn.codex.model, turn.codex.effort, turn.codex.summary), ("gpt-6.1-sol", "low", "detailed"))
        self.assertEqual(self.session["mode"], "workspace")
        with mock.patch.dict(os.environ, {"LOOPCUT_CODEX_APP": "false", "LOOPCUT_WORKSPACE": "true",
                                          "LOOPCUT_API_KEY": "k", "LOOPCUT_BASE_URL": "https://example.com/v1",
                                          "LOOPCUT_MODEL": "m"}):
            off = agent.Turn()
            agent._configure(off, state.reset())
            self.assertIs(off.spec, agent.WORKSPACE_SPEC, "off: the HTTP workspace loop as before")
            self.assertIsNone(config.codex_app())

    def test_a_turn_runs_in_a_codex_thread_with_loopcuts_live_tools(self):
        turn = self.start()
        turn.thread.join(15)
        self.assertEqual(turn.outcome, "completed")
        first, *messages = self.received()
        self.assertEqual(first["env"]["CODEX_HOME"], str(self.home))
        self.assertIsNone(first["env"]["OPENAI_API_KEY"], "Codex bills the plan, never an API key")
        self.assertTrue(first["path"].startswith(str(self.ws / workspace.OWN / "bin")), "the workspace's blender first")
        self.assertIn("mcp_servers.kaggle.enabled=false", first["argv"])
        self.assertIn("mcp_servers.computer-use.enabled=false", first["argv"])
        self.assertIn('web_search="disabled"', first["argv"])
        methods = [m.get("method") for m in messages]
        self.assertEqual(methods[:4], ["initialize", "initialized", "thread/start", "turn/start"])
        self.assertTrue(messages[0]["params"]["capabilities"]["experimentalApi"])
        thread = messages[2]["params"]
        self.assertEqual((thread["cwd"], thread["approvalPolicy"], thread["sandbox"], thread["model"]),
                         (str(self.ws), "never", "danger-full-access", "gpt-6.1-sol"))
        self.assertTrue(thread["developerInstructions"].startswith(codex_app.PROMPT))
        self.assertEqual([t["name"] for t in thread["dynamicTools"]], list(codex_app.DYNAMIC),
                         "apply_build, snapshot_scene and the live tools, never the cheaper agent's others")
        api = next(t for t in thread['dynamicTools'] if t['name'] == 'inspect_api')
        http_api = next(t['function'] for t in agent._workspace_schemas()
                        if t['function']['name'] == 'inspect_api')
        self.assertEqual(api['inputSchema'], http_api['parameters'])
        self.assertEqual(api['description'], http_api['description'])
        self.assertIn('inspect_api', thread['developerInstructions'])
        started = messages[3]["params"]
        self.assertEqual((started["effort"], started["summary"]), ("low", "detailed"))
        self.assertIn("paint the cube red", started["input"][0]["text"])
        self.assertIn("<scene_context>", started["input"][0]["text"])
        self.assertEqual(self.ran, [("run_python", {"code": "x = 1", "summary": "set x"})])
        answer = next(m for m in messages if m.get("id") == 900)
        self.assertEqual(answer["result"], {"success": True, "contentItems": [{"type": "inputText", "text": "x is 1"}]})
        kinds = [(i["kind"], i.get("name"), i.get("status")) for i in self.session["items"]]
        self.assertEqual(kinds, [("user", None, None), ("assistant", None, None), ("tool", "shell", "done"),
                                 ("tool", "run_python", "done"), ("assistant", None, None)])
        self.assertEqual(self.session["items"][2]["summary"], "$ blender -b source.blend --python build.py")
        self.assertEqual(self.session["items"][-1]["text"], "Painted it red.")
        self.assertEqual(self.session["codex_thread"], "thread-1")
        self.assertEqual(self.session["usage"], {"input": 1000, "cached": 400, "output": 100, "context": 1000})
        self.assertEqual(self.session["usage_by_role"]["single"]["requests"], 1)
        roles = [m["role"] for m in self.session["messages"]]
        self.assertEqual(roles, ["user", "assistant", "assistant", "tool", "assistant"])
        self.assertTrue(self.session["messages"][0][codex_app.SENT])
        logs = list((self.ws / workspace.OWN / codex_app.LOG_FOLDER).glob("turn-*.jsonl"))
        self.assertEqual(len(logs), 1)
        logged = [json.loads(line) for line in logs[0].read_text().splitlines()]
        self.assertEqual(logged[0]["dir"], "start")
        self.assertIn("item/tool/call", "".join(e["raw"] for e in logged if e["dir"] == "in"))
        self.assertEqual(conversations.load(self.session["id"])["codex_thread"], "thread-1")

    def test_the_next_turn_resumes_the_thread(self):
        self.start().thread.join(15)
        self.record.unlink()
        self.session["turn_number"] = 2
        self.start("now brighter").thread.join(15)
        methods = [m.get("method") for m in self.received()[1:]]
        self.assertEqual(methods[:4], ["initialize", "initialized", "thread/resume", "turn/start"])

    def test_a_thread_codex_lost_starts_again(self):
        self.session["codex_thread"] = "gone"
        turn = self.start()
        turn.thread.join(15)
        self.assertEqual(turn.outcome, "completed")
        self.assertIn("thread/start", [m.get("method") for m in self.received()[1:]])
        self.assertEqual(self.session["codex_thread"], "thread-1")
        self.assertTrue(any(i["kind"] == "notice" for i in self.session["items"]))

    def test_without_auto_run_codex_asks_through_loopcuts_approval_card(self):
        turn = self.start(scenario="approval", auto_run=False)
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "approval card")
        card = next(i for i in self.session["items"] if i.get("status") == "awaiting")
        self.assertEqual(card["summary"], "$ /bin/zsh -c 'rm -rf build'")
        agent.decide(False)
        turn.thread.join(15)
        answer = next(m for m in self.received()[1:] if m.get("id") == 901)
        self.assertEqual(answer["result"], {"decision": "decline"})
        self.assertEqual(card["status"], "rejected")
        thread = next(m for m in self.received()[1:] if m.get("method") == "thread/start")
        self.assertEqual(thread["params"]["approvalPolicy"], "untrusted")

    def test_with_auto_run_a_package_install_still_asks(self):
        turn = self.start(scenario="install", auto_run=True)
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "approval card")
        card = next(i for i in self.session["items"] if i.get("status") == "awaiting")
        self.assertTrue(card["install"])
        agent.decide(False)
        turn.thread.join(15)
        answer = next(m for m in self.received()[1:] if m.get("id") == 901)
        self.assertEqual(answer["result"], {"decision": "decline"})

    def test_stop_interrupts_the_turn(self):
        turn = self.start(scenario="interrupt")
        self.wait_for(lambda: any(i.get("name") == "shell" for i in self.session["items"]), "the command")
        agent.stop()
        turn.thread.join(15)
        self.assertFalse(turn.thread.is_alive())
        self.assertEqual(turn.outcome, "cancelled")
        self.assertIn("turn/interrupt", [m.get("method") for m in self.received()[1:]])
        self.assertEqual(self.session["items"][-1]["text"], "Stopped.")
        self.assertEqual(next(i for i in self.session["items"] if i.get("name") == "shell")["status"], "rejected")

    def test_a_failed_turn_says_why(self):
        turn = self.start(scenario="fail")
        turn.thread.join(15)
        self.assertEqual(turn.outcome, "error")
        self.assertEqual(self.session["items"][-1], state.item_error("Codex: usage limit reached"))

    def test_a_note_sent_mid_turn_steers_it(self):
        turn = self.start(scenario="steer")
        self.wait_for(lambda: self.record.is_file() and "turn/start" in self.record.read_text(), "the turn")
        self.assertTrue(agent.send("make it blue too"))
        turn.thread.join(15)
        steer = next(m for m in self.received()[1:] if m.get("method") == "turn/steer")
        self.assertEqual(steer["params"]["input"][0]["text"], "make it blue too")
        self.assertEqual(steer["params"]["expectedTurnId"], "turn-1")
        self.assertTrue(any(m.get(context.STEER) for m in self.session["messages"]))
        self.assertFalse(any(i.get("queued") for i in self.session["items"]))

    def test_attached_images_go_in_as_the_workspace_files(self):
        picture = self.ws / "ref1.jpg"
        picture.write_bytes(b"jpeg")
        self.session["references"] = [{"ref": "loopcut-image:a.png", "full": "loopcut-image:b.png", "name": "ref1.jpg"}]
        self.session["workspace_refs"] = {"loopcut-image:b.png": "ref1.jpg"}
        self.session["messages"].append({"role": "user", "content": [
            {"type": "text", "text": "<scene_context>x</scene_context>\n\nlike this"},
            {"type": "image_url", "image_url": {"url": "loopcut-image:a.png"}}]})
        sent = codex_app.turn_input(self.session, self.ws)
        self.assertEqual(sent[1], {"type": "localImage", "path": str(picture)})
        self.session["messages"][-1][codex_app.SENT] = True
        self.assertEqual(codex_app.turn_input(self.session, self.ws)[0]["text"], codex_app.RESUME)

    def test_tool_images_keep_their_origin(self):
        image = Path(self.tmp.name) / "preview.png"
        image.write_bytes(b"\x89PNG preview")
        self.session["auto_run"] = True
        self.session["auto_heavy"] = True
        dynamic = mock.patch.object(codex_app, "DYNAMIC", (*codex_app.DYNAMIC, "view_image", "inspect_scene"))
        dynamic.start()
        self.addCleanup(dynamic.stop)

        def run_tool(name, arguments):
            if name == "view_image":
                return tools.ToolResult(text="Image attached: image.", image_path=image, file_image=True)
            return types.SimpleNamespace(text="Image attached: image.", ok=True, image_path=image)

        driver = codex_app.CodexTurn(self.session, agent.Turn(), config.load(), None, self.ws, None,
                                    run_tool, lambda session: None)
        for name, arguments, prefix in (("view_image", {"path": str(image)}, "File preview: "),
                                        ("search_assets", {}, "Viewport capture: "),
                                        ("inspect_scene", {}, "Viewport capture: "),
                                        ("capture_viewport", {}, "Viewport capture: "),
                                        ("run_python", {}, "Viewport capture: "),
                                        ("run_python", {"capture": "front"}, "Viewport capture: ")):
            with self.subTest(tool=name):
                reply = driver.tool_call({"tool": name, "arguments": arguments, "callId": name})
                self.assertTrue(reply["success"])
                self.assertTrue(reply["contentItems"][1]["imageUrl"].startswith("data:image/png;base64,"))
                message = self.session["messages"][-1]
                self.assertTrue(message[context.CAPTURE])
                self.assertTrue(message["content"][0]["text"].startswith(prefix))

    def test_a_tool_image_goes_back_as_a_data_url(self):
        image = Path(self.tmp.name) / "capture.png"
        image.write_bytes(b"\x89PNG\r\n")
        reply = codex_app._reply("Image attached: the scene.", True, image)
        self.assertTrue(reply["contentItems"][1]["imageUrl"].startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
