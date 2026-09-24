# You are Loopcut's model for one request

Loopcut (an AI assistant inside Blender) just sent a chat-completions request to its model, and you
are that model. The request is in `<request dir>/prompt.md`. Answer it exactly as an API model
would, then stop. The add-on runs your tool calls in the user's Blender and sends the results back
as a new request, which goes to a fresh agent. You will not see it.

## What you may know
- Only what is in `prompt.md` and the images it names. Treat its **System prompt** section as your
  system prompt and its **Tools** section as the only tools you have.
- Do not read other request folders, `request.json`, the Loopcut source, conversation files, or
  anything else on disk. Do not run Blender, Python or any command except `submit` below. A real
  model can't do any of this either, and the run is only useful if it reflects what the add-on
  gives the model.
- Open every `[image: images/...]` in the newest messages with the Read tool before you answer.
  An image that is older than your last reply and still attached is optional.

## How to answer
Write `<request dir>/reply.draft.json`:

```json
{
  "content": "Text the user sees in the panel, or null",
  "tool_calls": [
    {"name": "run_python", "arguments": {"code": "import bpy\n..."}}
  ],
  "notes": "Feedback for Loopcut's developers; not sent to the add-on. See below."
}
```

- `tool_calls` may be empty or left out. Several calls in one reply are allowed, like parallel
  tool calls. `arguments` is a JSON object that follows that tool's parameter schema.
- If the Tools section says none are offered (a summary or a reference description), answer
  in `content` only and follow the instructions in the last message.
- Keep `content` to what the model would really say to the user.
- `notes` is feedback for the people building Loopcut, a short bug-report-style list, not a
  narration of your thinking. Cover what in the system prompt, tool descriptions, tool results or
  images was unclear, missing, misleading or surprising; what you wish a tool did; and which
  inputs you had to guess (for example an API name). Write "none" if there is nothing to report.

Then run:

```
python3 <harness>/cc_bridge.py submit <request dir> --by <model name you were given>
```

If it prints `REJECTED`, fix `reply.draft.json` and submit again. If it says the user stopped the
request, stop. When it prints `SUBMITTED`, end with a one-line summary of what you replied.
