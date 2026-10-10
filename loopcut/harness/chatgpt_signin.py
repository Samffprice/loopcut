"""Sign in with ChatGPT outside Blender: the same flow and the same private credential file as the
add-on's "Sign in with ChatGPT" (chatgpt_auth.py), so one sign-in serves the add-on in every Blender
on this computer and the evals (run.py --chatgpt-plan).

    python3 loopcut/harness/chatgpt_signin.py sign-in        # opens the browser; waits for the callback
    python3 loopcut/harness/chatgpt_signin.py status         # plan, plan-use scope, token expiry
    python3 loopcut/harness/chatgpt_signin.py models         # the account's model picker
    python3 loopcut/harness/chatgpt_signin.py probe          # one tiny Responses call with a tool and an image
    python3 loopcut/harness/chatgpt_signin.py sign-out       # revoke, then forget the tokens

Prints no token and no email address. Uses only the official SIWC OAuth flow: it never reads Codex's
or ChatGPT's own credentials (~/.codex/auth.json and the like) and never takes pasted ones.
"""

import argparse
import base64
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import chatgpt_auth, chatgpt_responses, llm  # noqa: E402


def _open_browser(url: str) -> None:
    import subprocess
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", url])
        elif sys.platform == "win32":
            os.startfile(url)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", url])
    except OSError:
        import webbrowser
        webbrowser.open(url)


def _status_lines() -> list[str]:
    info = chatgpt_auth.status()
    if not info.get("registered"):
        return ["Not signed in (no ChatGPT registration on this computer yet)."]
    if not info.get("signed_in"):
        return ["Signed out (the registration is kept; sign-in reuses it)."]
    left = (info.get("expires_at") or 0) - time.time()
    return [f"Signed in. ChatGPT plan: {info.get('plan_type') or 'not named in the ID token'}.",
            f"Plan use allowed (chatgpt.tokens.use.direct): {'yes' if info.get('plan_usage') else 'NO'}.",
            f"Access token: {'valid for ' + str(int(left // 60)) + ' min' if left > 0 else 'expired (refreshes on use)'}; "
            f"saved {info.get('saved_at')}.",
            f"Credentials: {chatgpt_auth.folder() / chatgpt_auth.ACCOUNTS_FILE} (0600)."]


def sign_in(args) -> int:
    def on_url(url: str) -> None:
        if args.print_url:
            print(f"If the browser did not open, open this link:\n{url}", flush=True)
    print("Opening your browser to sign in with ChatGPT; waiting up to "
          f"{int(args.timeout)} s for it to come back to Loopcut...", flush=True)
    try:
        chatgpt_auth.sign_in(_open_browser, new_account=args.new_account, timeout=args.timeout, on_url=on_url)
    except chatgpt_auth.ChatGPTAuthError as ex:
        print(f"Sign-in failed: {ex}", file=sys.stderr)
        return 1
    print("\n".join(_status_lines()))
    return 0


def status(args) -> int:
    print("\n".join(_status_lines()))
    return 0 if chatgpt_auth.signed_in() else 1


def models(args) -> int:
    try:
        for row in chatgpt_auth.list_models():
            print(f"{row['slug']:28} {row['display_name']}")
    except chatgpt_auth.ChatGPTAuthError as ex:
        print(f"Could not list models: {ex}", file=sys.stderr)
        return 1
    return 0


def sign_out(args) -> int:
    confirmed = chatgpt_auth.sign_out()
    print("Signed out." if confirmed else "Signed out on this computer; ChatGPT did not confirm the revocation. "
          "You can disconnect Loopcut in ChatGPT's settings.")
    return 0


def _png(width: int, height: int, rgb: tuple) -> bytes:
    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


PROBE_TOOL = {"type": "function", "function": {
    "name": "capture_viewport", "description": "Capture the 3D viewport and return the image.",
    "parameters": {"type": "object", "properties": {"angle": {"type": "string", "description": "front, top or camera"}},
                   "required": []}}}


def probe(args) -> int:
    """Two requests the way the agent loop makes them: a tool call, then its result with an image."""
    if args.log_dir:
        os.environ["LOOPCUT_CHATGPT_LOG_DIR"] = str(Path(args.log_dir).resolve())
    messages = [{"role": "system", "content": "You are testing a tool loop. Use the tool when asked."},
                {"role": "user", "content": "Call capture_viewport once with angle 'front', then tell me in one "
                                            "word the dominant colour of the image you get back."}]
    headers = llm.correlation_headers(f"probe{int(time.time())}", "single")
    total = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    t0 = time.monotonic()
    for step in range(1, 4):
        completion = chatgpt_responses.stream(
            model=args.model, messages=messages, tools=[PROBE_TOOL], reasoning_effort=args.effort,
            on_text=lambda _: None, is_cancelled=lambda: False, headers=headers)
        for key in total:
            total[key] += (completion.usage or {}).get(key, 0)
        print(f"step {step}: finish={completion.finish_reason} tool_calls={[c.name for c in completion.tool_calls]} "
              f"text={completion.text[:120]!r} usage={completion.usage}", flush=True)
        messages.append(completion.as_message())
        if not completion.tool_calls:
            break
        image = "data:image/png;base64," + base64.b64encode(_png(64, 64, (220, 30, 30))).decode("ascii")
        for call in completion.tool_calls:
            messages.append({"role": "tool", "tool_call_id": call.id, "content": f"Captured ({call.arguments})."})
        messages.append({"role": "user", "content": [
            {"type": "text", "text": "Viewport capture: the front view (after the step above)."},
            {"type": "image_url", "image_url": {"url": image}}]})
    print(f"total usage {json.dumps(total)}, {time.monotonic() - t0:.1f} s, disabled features: "
          f"{sorted(chatgpt_responses._disabled) or 'none'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sign-in", help="sign in with ChatGPT in the browser")
    p.add_argument("--new-account", action="store_true", help="register another ChatGPT account or workspace")
    p.add_argument("--print-url", action="store_true", help="also print the sign-in link (never with an ID token hint)")
    p.add_argument("--timeout", type=float, default=chatgpt_auth.SIGN_IN_TIMEOUT, help="seconds to wait (default 600)")
    p.set_defaults(run=sign_in)
    sub.add_parser("status", help="the signed-in account, without tokens").set_defaults(run=status)
    sub.add_parser("models", help="list the account's models").set_defaults(run=models)
    sub.add_parser("sign-out", help="revoke and forget the tokens").set_defaults(run=sign_out)
    p = sub.add_parser("probe", help="one tool call and one image through the Responses adapter")
    p.add_argument("--model", default="gpt-6.1-sol")
    p.add_argument("--effort", default="low")
    p.add_argument("--log-dir", help="keep the request/response transcript here (LOOPCUT_CHATGPT_LOG_DIR)")
    p.set_defaults(run=probe)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
