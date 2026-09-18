# Loopcut

An AI agent that works inside Blender: chat, it writes and runs `bpy`, looks at the viewport to
check its work, and every step is one undo away.

## Layout

| Path | What |
|---|---|
| `extension/loopcut/` | The product. Python: agent loop, tools, and a UI drawn with `gpu`/`blf`. |
| `harness/` | Scripts that drive Blender for screenshots and checks. Fixtures are canned conversations. |
| `tests/` | Runs without Blender: agent loop against a scripted fake model server, layout. |
| `blender/` | The Blender fork (own git repo, branch `loopcut`, based on v5.2.2). Will hold only a thin C++ editor type for the panel to live in. Gitignored here. |
| `tools/` | Stock Blender 5.2.2 for fast iteration. Gitignored. |

## Setup

Copy `.env.example` to `.env` and fill it in. Quote values containing `|`. Never commit `.env`.

Model-written code asks before it runs. `LOOPCUT_AUTO_RUN=true` turns that off.

## The loops

| Command | Time | Use |
|---|---|---|
| `scripts/dev.sh` | save → redraw <1s | Blender stays open with the panel; saving any file under `extension/` hot-reloads it and keeps the conversation. |
| `scripts/shot.sh <fixture>` | ~10s | Renders `harness/fixtures/<fixture>.json` to `out/`: window PNG, panel-only PNG, and the frame's display list as JSON. No network. |
| `python3 -m unittest discover -s tests` | <1s | Agent loop and layout. |
| `Blender --factory-startup --python harness/tools_check.py` | ~8s | The real tools against a real scene. |
| `Blender --factory-startup --enable-event-simulate --python harness/input_check.py` | ~8s | Click, type, edit, Esc through simulated events. |

Debugging a frame: read `out/<fixture>.json` before squinting at pixels. Every primitive has an
id, rect and color, so "invisible" shows up as a color equal to its background or a rect outside
the region.

## Checkpoints

Every user message that changes the scene gets a checkpoint, taken right before the first
scene-changing step of that turn (chat-only turns cost nothing). "Restore checkpoint" under a
message returns the scene and the conversation to before it, and puts the message back in the
input box.

- A restore never destroys work: it first snapshots the current state, manual edits included,
  and leaves an "Undo restore" button that brings back both the scene and the cut conversation.
- The user's own .blend is never written. Python cannot restore in place (Blender's file path
  would point into our store and the next Ctrl+S would land there), so the restored scene is
  saved at once as `<name>.restored-<time>.blend` beside the original. The fork's in-place
  restore will replace that step; the store format stays.
- If a checkpoint cannot be written, the step is not run.
- Storage: compressed full copies under the Blender user data folder (`LOOPCUT_DATA_DIR`
  overrides), capped per conversation by `LOOPCUT_CHECKPOINT_BUDGET_MB` (default 2048). Over
  budget, the oldest expire first; the first checkpoint, the newest five and the latest
  pre-restore snapshot are protected. Expired ones show as "Checkpoint expired". Stores older
  than 30 days are removed at startup.
- Not covered: files the agent writes elsewhere on disk, preferences, add-ons, linked libraries.

Check: `LOOPCUT_DATA_DIR=<tmp>/data Blender --factory-startup --enable-event-simulate --python harness/checkpoint_check.py -- <tmp>/project`
clicks the real buttons and asserts the original file's hash never changes.

## UI architecture

`ui/layout.py` is pure Python: session state in, display list out. `ui/draw.py` renders that list
(one SDF shader for rounded rects, `blf` for text). `ui/host.py` binds it to a Blender area and
handles input. The panel currently borrows Text Editor areas; moving to the fork's own editor
type is a change to the `HOST_*` constants in `host.py`.
