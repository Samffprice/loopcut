#!/usr/bin/env python3
"""A scripted stand-in for `codex app-server` (test_codex_app.py): JSON-RPC lines on stdin and stdout, the
messages codex-cli 0.159.0 sent in the 2026-10-01 probes. Every message it receives, and its command line,
go to the JSONL file FAKE_CODEX_RECORD; FAKE_CODEX_SCENARIO picks what the turn does:
  tool       a commentary message, a command, a run_python dynamic tool call, usage, the final message
  approval   a command that asks for approval first; the decision is recorded
  install    the same with a pip install, which asks even with auto-run on
  interrupt  a command that runs until turn/interrupt arrives
  fail       the turn fails with an error
  steer      the turn waits for a turn/steer before it ends
"""

import json
import os
import sys

RECORD = os.environ.get("FAKE_CODEX_RECORD")
SCENARIO = os.environ.get("FAKE_CODEX_SCENARIO", "tool")
THREAD, TURN = "thread-1", "turn-1"


def record(entry) -> None:
    if RECORD:
        with open(RECORD, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def notify(method: str, params: dict) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


def receive() -> dict:
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    message = json.loads(line)
    record(message)
    return message


def wait_for(predicate) -> dict:
    while True:
        message = receive()
        if predicate(message):
            return message


def item(kind: str, key: str, **fields) -> dict:
    return {"type": kind, "id": key, **fields}


def started(entry: dict) -> None:
    notify("item/started", {"item": entry, "threadId": THREAD, "turnId": TURN})


def completed(entry: dict) -> None:
    notify("item/completed", {"item": entry, "threadId": THREAD, "turnId": TURN})


def message(key: str, text: str, phase: str) -> None:
    started(item("agentMessage", key, text="", phase=phase))
    notify("item/agentMessage/delta", {"threadId": THREAD, "turnId": TURN, "itemId": key, "delta": text})
    completed(item("agentMessage", key, text=text, phase=phase))


def command(key: str, text: str, output: str, status="completed", code=0) -> dict:
    return item("commandExecution", key, command=f"/bin/zsh -c '{text}'", cwd=os.getcwd(), status=status,
                commandActions=[{"type": "unknown", "command": text}], aggregatedOutput=output, exitCode=code,
                durationMs=120)


def usage() -> None:
    last = {"totalTokens": 1100, "inputTokens": 1000, "cachedInputTokens": 400, "cacheWriteInputTokens": 0,
            "outputTokens": 100, "reasoningOutputTokens": 30}
    notify("thread/tokenUsage/updated", {"threadId": THREAD, "turnId": TURN,
                                         "tokenUsage": {"total": last, "last": last, "modelContextWindow": 272000}})


def end(status: str = "completed", error: dict | None = None) -> None:
    notify("turn/completed", {"threadId": THREAD, "turn": {"id": TURN, "items": [], "status": status, "error": error}})


def turn() -> None:
    notify("turn/started", {"threadId": THREAD, "turn": {"id": TURN, "items": [], "status": "inProgress"}})
    if SCENARIO == "tool":
        message("m1", "Looking at the scene.", "commentary")
        started(command("c1", "blender -b source.blend --python build.py", "", status="inProgress"))
        completed(command("c1", "blender -b source.blend --python build.py", "wrote result.blend"))
        started(item("dynamicToolCall", "call-1", tool="run_python", arguments={"code": "x = 1", "summary": "set x"},
                     status="inProgress"))
        send({"jsonrpc": "2.0", "id": 900, "method": "item/tool/call", "params": {
            "threadId": THREAD, "turnId": TURN, "callId": "call-1", "namespace": None, "tool": "run_python",
            "arguments": {"code": "x = 1", "summary": "set x"}}})
        wait_for(lambda m: m.get("id") == 900)
        usage()
        message("m2", "Painted it red.", "final_answer")
        end()
    elif SCENARIO in ("approval", "install"):
        shell = "rm -rf build" if SCENARIO == "approval" else "python3 -m pip install --target ./deps imageio-ffmpeg"
        send({"jsonrpc": "2.0", "id": 901, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": THREAD, "turnId": TURN, "itemId": "c2", "command": f"/bin/zsh -c '{shell}'",
            "cwd": os.getcwd()}})
        answer = wait_for(lambda m: m.get("id") == 901)
        accepted = answer["result"]["decision"] == "accept"
        started(command("c2", shell, "", status="inProgress"))
        completed(command("c2", shell, "", status="completed" if accepted else "declined",
                          code=0 if accepted else None))
        message("m3", "Done.", "final_answer")
        end()
    elif SCENARIO == "interrupt":
        started(command("c3", "sleep 60", "", status="inProgress"))
        wait_for(lambda m: m.get("method") == "turn/interrupt")
        end("interrupted")
    elif SCENARIO == "fail":
        end("failed", {"message": "usage limit reached", "codexErrorInfo": None, "additionalDetails": None})
    elif SCENARIO == "steer":
        steer = wait_for(lambda m: m.get("method") == "turn/steer")
        send({"jsonrpc": "2.0", "id": steer["id"], "result": {"turnId": TURN}})
        message("m4", "Made it blue as well.", "final_answer")
        end()


def main() -> None:
    record({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in (
        "CODEX_HOME", "OPENAI_API_KEY", "CODEX_API_KEY", "LOOPCUT_API_KEY")}, "path": os.environ.get("PATH", "")})
    while True:
        request = receive()
        method, request_id = request.get("method"), request.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"userAgent": "fake", "codexHome": "/tmp",
                                                                  "platformFamily": "unix", "platformOs": "macos"}})
        elif method in ("thread/start", "thread/resume"):
            thread = request["params"].get("threadId") or THREAD
            if thread == "gone":
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "no rollout found"}})
                continue
            send({"jsonrpc": "2.0", "id": request_id, "result": {"thread": {"id": thread}, "model": "fake"}})
        elif method == "turn/start":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"turn": {"id": TURN, "items": [],
                                                                          "status": "inProgress"}}})
            turn()
        elif method == "turn/interrupt":
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})


if __name__ == "__main__":
    main()
