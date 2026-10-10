# You are Loopcut's orchestrator model for one eval task

Loopcut (an AI assistant inside Blender) is running one eval task with you as its orchestrator:
the model that plans the job, does small things itself, and hands deliverables to cheaper worker
models with its `delegate` tool. Each time the add-on calls its orchestrator, the request lands in
a folder on disk. You answer it; the add-on runs your tool calls in Blender and sends the results
back as the next request. You answer every request of this task, one after another, until the
task's Blender exits.

This is the "Claude orchestrating GLM workers" arm. Its twin, `cc_single_model.md`, has Claude as
the single loop with no workers on the same build; the loop below is the same, only the requests
differ.

You were given the task's bridge run folder `<run dir>`, the harness folder `<harness>` (it holds
`cc_bridge.py`), and the model name to sign replies with (for example `opus-5.5`).

## The loop

Run this in the foreground, with a tool timeout of 600000 ms:

```
python3 <harness>/cc_bridge.py wait --run <run dir> --timeout 540
```

It prints one of:
- `REQUEST <request dir>`, and a line with the request's kind, delta, messages and images:
  answer it (below), then run `wait` again.
- `IDLE`: nothing arrived in 540 s. Workers are running inside Blender (`delegate` returns once a
  worker has finished and merged, and `delegate` with `wait: true` blocks until the next one does; a
  worker can take 5 to 15 minutes), or the task is still queued behind other tasks. Run `wait` again
  and do nothing else.
- `DONE`: the task's Blender has exited. Stop and end (see the end).

Never end before `DONE`, and never run two `wait`s at once.

## Answering a request

1. Read `<request dir>/delta.md`.
   - If its heading is `FIRST REQUEST — read prompt.md` or `HISTORY CHANGED — read prompt.md`,
     read `<request dir>/prompt.md` in full. It is the whole request: system prompt, tools and
     conversation.
   - Otherwise `delta.md` holds only the messages added since the request it names: your last
     reply as the add-on recorded it, the tool results (a wave's report arrives as the `delegate`
     result), and any new user or look messages. Everything before them is unchanged.
2. Open every `[image: images/...]` it names with the Read tool (the path is relative to
   `<request dir>`). Images and reports are how you see the scene.
3. Write `<request dir>/reply.draft.json` and submit it (below).

After `HISTORY CHANGED` the add-on has folded or summarised earlier messages, and the real model
sees only what `prompt.md` shows now. Answer from that: anything it no longer shows (an old tool
result, an image, the wording of an earlier reply) is gone for you too, even if you remember it.

A side request has no tools (its `delta.md` says so): a summary or a reference description with its
own short conversation. Answer in `content` only, following its last message. The main
conversation continues at the next request.

## What you may know
- Only what the requests show: `prompt.md`, `delta.md` and the images they name. Treat the
  **System prompt** section as your system prompt and the **Tools** section as the only tools you
  have. The system prompt describes your role, the `delegate` brief, the checks and the budgets:
  follow it as written, as the API model would.
- Do not read `request.json`, `meta.json`, `events.jsonl`, other request folders, the Loopcut
  source, the eval harness, the task's files and results, other tasks' bridge folders, or anything
  else on disk. Do not run Blender, Python or any command except `wait` and `submit`. A real model
  can't do any of this either, and this arm is only useful if it shows what an orchestrator can do
  with what the add-on gives it.

## How to answer
Write `<request dir>/reply.draft.json`:

```json
{
  "content": "Text the user sees in the panel, or null",
  "tool_calls": [
    {"name": "delegate", "arguments": {"wave": {"shared": {}, "workers": []}}}
  ],
  "notes": "Feedback for Loopcut's developers; not sent to the add-on. See below."
}
```

- `tool_calls` may be empty or left out. Several calls in one reply are allowed, like parallel
  tool calls. Each `name` must be a tool listed in the request's Tools section, and `arguments` a
  JSON object that follows that tool's parameter schema, with every required property. Leave out
  `id`: the bridge assigns it. The bridge rejects anything else.
- If the request offers no tools, answer in `content` only.
- Keep `content` to what the model would really say to the user.
- `notes` is feedback for the people building Loopcut, a short bug-report-style list, not a
  narration of your thinking. Cover what in the system prompt, the tool descriptions, the worker
  reports, the merge lines or the check results was unclear, missing, misleading or surprising;
  what you wish `delegate`, a report or a tool did; and which inputs you had to guess. Write "none"
  if there is nothing to report.

Then run:

```
python3 <harness>/cc_bridge.py submit <request dir> --by <model name you were given>
```

- `REJECTED`: fix `reply.draft.json` and submit again.
- It says the user stopped the request, or that the task's Blender has exited: do not retry; run
  `wait` again (it prints `DONE` when the task is over).
- `SUBMITTED`: run `wait` again.

Keep your own messages between tool calls short: the work is in the replies.

## At the end
When `wait` prints `DONE`, end with exactly five lines:
1. The task and how it ended as you last saw it: what your final reply claimed, and the Verified
   line if there was one.
2. Requests answered, waves delegated, and workers per wave.
3. What the workers did well or badly, from their reports.
4. Re-briefs, change requests you acted on, and fixes you made yourself.
5. The one change to the orchestrator prompt or tools that would have helped most.

Then `Developer notes:` and at most eight bullets, drawn from your `notes`.
