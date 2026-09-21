# Running a project task in the ChatGPT app (the manual arm)

The comparison needs three things from the other tool, in the arm folder next to the start file:
`<task>.final.blend` (the finished scene), `<task>.run.json` (effort: time, prompts, hand edits) and,
if you can get it, `<task>.transcript.md` (everything it ran and said). The grader never looks at
how the scene was made, only at the saved file, so any agent that can run Python in Blender, live
or headless, can produce all three itself.

"ChatGPT" here is the unified desktop app (ChatGPT.app, which absorbed Codex in July 2026). It is
an agent with a shell, reads `AGENTS.md` from the project folder, and shares `~/.codex/config.toml`
with the Codex CLI. Checked 2026-09-21.

## Setup, once

Export the start scene (headless, seconds):
```
tools/Blender.app/Contents/MacOS/Blender -b --factory-startup --python blender/loopcut/harness/evals/start_scene.py -- out/projects/chatgpt perfume_ad
```

**Route A, recommended: shell only, no plugin.** Open `out/projects/chatgpt` as the project in the
app, put the instruction block below in `out/projects/chatgpt/AGENTS.md`, pick Astra with medium
reasoning in the model picker (or in `~/.codex/config.toml`: `model = "gpt-6-astra"`,
`model_reasoning_effort = "medium"`), and paste the brief as the first message. The agent works by
writing scripts and running Blender headless on the .blend, and checks its work with real renders
(EEVEE renders fine headless; only viewport screenshots need a window). Blender does not need to
be open. It uses the same Blender binary as Loopcut, which keeps the arms fair.

**Route B: live Blender through MCP.** Adds viewport screenshots and a running session, closer
to how Loopcut works. Install ahujasid's MCP for Blender:
```
brew install uv
uvx mcp-for-blender install-addon        # the add-on, blender_mcp.py (package formerly "blender-mcp")
```
Enable "Interface: MCP for Blender" in Blender's add-on preferences, open the start .blend, press N,
MCP for Blender tab, Start MCP Server (socket 9876). In `~/.codex/config.toml`:
```
[mcp_servers.blender]
command = "uvx"
args = ["mcp-for-blender"]

[mcp_servers.blender.env]
BLENDER_HOST = "localhost"
BLENDER_PORT = "9876"
```
The instruction block covers both routes; the agent uses the MCP tools when they are there.

**Route C: no agent.** Plain chat writes scripts you paste into Blender's Text Editor yourself.
Every paste is a manual fix and is counted as one. A fair third arm ("ChatGPT without tools"),
not a stand-in for A.

Whichever route, the brief goes in as the first user message, verbatim, nothing else; everything
else the model is told lives in AGENTS.md, like Loopcut's system prompt.

## AGENTS.md for the arm folder (paste verbatim)

```
You are doing a Blender job on the .blend file in this folder whose name ends in .start.blend; the
task id is the part of that name before the first dot. Work until the brief is fully done, then stop.
Some tasks have a second part: if <task>.followup.txt exists, the person will paste it as a second
message once you have finished the first brief. When you finish the first brief, save the scene as
<task>.stage1.blend before you say you are done; after the follow-up, save <task>.final.blend. If
there is no followup file, <task>.final.blend is the end.

- If Blender MCP tools are available, use them on the open file. Otherwise work headless with the
  shell: Blender is at ../../../tools/Blender.app/Contents/MacOS/Blender relative to this folder
  (fall back to /Applications/Blender.app). Run scripts with
  `Blender -b <file>.blend --python <script>.py`, and have every script save its result to
  <task>.work.blend so the next script starts from it (copy the start file to work.blend first).
- Never ask the person to click, select or run anything in Blender themselves. If you cannot do a
  step, say so in the log and move on.
- Keep every script you run as <task>.steps/NN_<why>.py, numbered in order, with a first-line
  comment saying why you ran it and, after it ran, a second comment saying what happened.
- Check your own work before declaring done: render the scene camera at the first, middle and last
  frame of the animation (bpy.ops.render.render with write_still, EEVEE, low samples, 640 px) to
  <task>.steps/, look at the images, and fix what looks wrong. Note what you concluded in the log.
- When done: save the scene as <task>.final.blend in this folder; write <task>.transcript.md with
  one section per step (why, the code in a ```python block, what happened) plus your notes on the
  renders; write <task>.run.json with "tool" (this app and route: shell or MCP), "model" (the
  model and reasoning setting you are running as), "seconds" (from the first message to now; ask
  the person if unsure), "prompts_sent" (messages the person typed, the brief included),
  "manual_fixes" (things the person did in Blender by hand; 0 if none), "notes" (anything you
  could not do). Then tell the person the three paths.
```

## Running one task

1. Start a timer. Paste the contents of `<task>.prompt.txt` as the first message, verbatim.
2. Answer questions if it asks, and say "continue" if it stalls, but count every message you type.
   Anything you do in Blender yourself counts as a manual fix. Give up after 20 minutes per brief.
3. Two-turn tasks: when it says the first brief is done, make sure `<task>.stage1.blend` exists,
   then paste `<task>.followup.txt` as the next message, verbatim. The follow-up is one of the two
   prompts the task expects, so it does not count against the tool.
4. When it says it is done, check that `<task>.final.blend`, `<task>.run.json` and (two-turn) the
   stage1 file are in the arm folder. If it did not write `run.json`, fill the template that
   start_scene.py left there. If it did not save, save the scene yourself and note it.
5. Run the Loopcut arm and the comparison (see PROJECTS.md).

The transcript is copied into the report folder and linked from comparison.md when it exists. The
app keeps its own session logs under `~/.codex/sessions/` if you want the raw record.
