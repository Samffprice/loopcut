# Loopcut

An AI agent that works inside Blender: chat, it writes and runs `bpy`, looks at the viewport to
check its work, and every step is one undo away.

## Layout

| Path | What |
|---|---|
| `extension/loopcut/` | The product. Python: agent loop, tools, and a UI drawn with `gpu`/`blf`. |
| `harness/` | Scripts that drive Blender for screenshots and checks. Fixtures are canned conversations. |
| `tests/` | Runs without Blender: agent loop against a scripted fake model server, layout. |
| `harness/evals/` | The eval suite: real agent turns on scripted tasks, checked by reading the scene. |
| `blender/` | The Blender fork (own git repo, branch `loopcut`, based on v5.2.2): the product people run. Holds only what Python cannot do; see "The Loopcut build". Gitignored here. |
| `tools/` | Stock Blender 5.2.2 for fast iteration. Gitignored. |

## Setup

Users: Preferences > Add-ons > Loopcut. Pick a provider (any OpenAI-compatible endpoint works,
including a local one), paste a key, pick a model. The key is kept in `credentials.json` in
Blender's config folder with user-only permissions, never in `userpref.blend` or a .blend.

Developers: copy `.env.example` to `.env` and fill it in. Quote values containing `|`. Never commit
`.env`. Lookup order per setting: environment, preferences, `.env`.

Model-written code asks before it runs ("Always allow" on the card stops asking for that
conversation; the preference or `LOOPCUT_AUTO_RUN=true` turns asking off). A step that runs longer
than `LOOPCUT_RUN_TIMEOUT` (60 s) is stopped, so an endless loop cannot freeze Blender.

## The Loopcut build

`blender/` is Blender with five additions, kept small so rebasing onto a new release stays cheap:

- `SpaceLoopcut`, an editor type that is a shell: it clears its region, fires the add-on's draw
  callback, owns the "Loopcut" keymap and redraws when selection, mode or the open file change.
  All UI and logic stay in `extension/loopcut/` and hot-reload.
- `wm.loopcut_snapshot_write` / `wm.loopcut_snapshot_restore`: checkpoints as recovery files, so a
  restore happens in place (see Checkpoints).
- The add-on is bundled (`scripts/addons_core/loopcut`, copied from `LOOPCUT_EXTENSION_DIR` at
  install time) and enabled for new and existing preferences.
- Its own settings folder (`Loopcut/<version>` where Blender has `Blender/<version>`), so it
  never writes to stock Blender's preferences, and a first-run flow in the splash screen modelled
  on Cursor's: "Import Blender 5.2 Settings" copies preferences, add-ons, keymap, themes and the
  startup file from the newest stock Blender folder it can read (`blender_import.py`), or "Start
  Fresh" shows Blender's quick setup; then connect a model, privacy, done. The steps are drawn by
  `extension/loopcut/onboarding.py`; the fork's splash menus only call it. Add-ons that did not
  load after an import are named on the last step.
- At startup the panel is docked right of the 3D viewport if the layout has none. Cmd+L
  (Ctrl+Alt+L elsewhere) focuses it, opening it first if needed.

Build: `ninja -C build/lite install` (the build folder must stay on the external disk). After
changing Python only, the same command re-copies the add-on in seconds. The same extension still
runs in stock Blender, borrowing a Text Editor area; the harness uses whichever Blender
`LOOPCUT_BLENDER` names and always loads the checkout, not the bundled copy.

The dev build has `WITH_ASSERT_ABORT` on; a release build must turn it and `WITH_ASSERT_RELEASE` off.

## Releases

The Mac build is made at home, the Windows one on GitHub's runners (it cannot be built on a Mac):

    scripts/make_patch.sh && git add patches && git commit -m "Fork patch for v0.1.0" && git push
    scripts/release_mac.sh v0.1.0 --upload                       # .dmg, added to a draft release
    gh workflow run release.yml -f release_tag=v0.1.0            # Windows .zip/.msi, same release

`.github/workflows/release.yml` only ever starts by hand. The fork is not in this repository, so
CI rebuilds it from upstream Blender at the tag in `patches/BASE` plus `patches/blender.patch`;
`scripts/make_patch.sh` writes both and checks the patch applies to the clean tag, so run it
whenever the fork changed. A hosted build takes hours; in a private repository Windows minutes
are billed at 2x and macOS at 10x.

Both use Blender's release configuration. On a Mac that is everything: Cycles' Metal kernels are
compiled on the user's machine. The Windows build leaves out the CUDA, OptiX, HIP and oneAPI
kernels for now, so Cycles renders on the CPU there. The builds are unsigned: macOS says the app
is damaged until `xattr -cr /Applications/Blender.app` is run, and Windows shows a SmartScreen
warning; signing needs an Apple Developer ID and a Windows code-signing certificate.
`.github/workflows/tests.yml` runs the Blender-free tests on every push.

## What the agent can do

| Tool | For |
|---|---|
| `run_python` | Change the scene. Its result ends with "Scene changes": what was really added, removed and changed, measured from the scene, so the model reads ground truth instead of printing values to check itself. |
| `get_scene_info` | What is in the scene. Large scenes list names by collection and take `name_contains` / `type`. |
| `get_object_info` | How an object is set up: modifier settings, material and geometry node trees, constraints, animation. Read before editing someone's material. |
| `inspect_api` | The Python API of the running Blender: properties, enum values, operator arguments, node sockets, plus tested notes where recent versions differ from what models remember. |
| `capture_viewport` | A self-framed image (`three_quarter`, `front`, `side`, `top`, `camera`, `user`; style `material` or `distinct`) and which objects are nearest, because similar colors hide what is in front. |

Images can be attached to a message: drop image files on the panel or use "+ image" in the input
box (png, jpg, webp, bmp, tif, tga; up to 6 per message). Each is decoded by Blender and stored as
a PNG of at most 1568 px in the conversation's folder, so a dropped file is never sent as-is and
nothing is added to the .blend. Attached images stay in the model's view for the newest 8,
counted apart from viewport captures, so a reference photo is not pushed out by captures.

Every message also carries a `<scene_context>` block (file, mode, selection), so "make this
shinier" works, and `@Name` pulls in that object, material or collection; the input box completes
names as you type. After a turn that changed something, a "Scene changes" card lists the diff with
Keep and Undo all (which restores that turn's checkpoint).

## The loops

| Command | Time | Use |
|---|---|---|
| `scripts/dev.sh` | save → redraw <1s | Blender stays open with the panel; saving any file under `extension/` hot-reloads it and keeps the conversation. |
| `scripts/shot.sh <fixture>` | ~10s | Renders `harness/fixtures/<fixture>.json` to `out/`: window PNG, panel-only PNG, and the frame's display list as JSON. No network. |
| `python3 -m unittest discover -s tests` | <1s | Agent loop and layout. |
| `Blender --factory-startup --python harness/tools_check.py` | ~8s | The real tools against a real scene. |
| `Blender --factory-startup --enable-event-simulate --python harness/input_check.py` | ~8s | Click, type, select, undo, complete an @mention, Esc through simulated events. |
| `Blender -b --factory-startup --python harness/attachments_check.py` | ~3s | Attaching images: conversion to a bounded PNG, refusals, nothing left in `bpy.data`. `scripts/shot.sh attachments` shows the chips. |
| `Blender -b --factory-startup --python harness/api_docs_check.py` | ~3s | `inspect_api` against the real API, and that the code in its notes still runs. |
| `scripts/onboarding_check.sh` | ~40s | First run of the build in a throwaway home folder: clicks through a fresh start, imports stock settings and checks they took effect and stock Blender's folder is byte-identical, screenshots every step to `out/onboarding/`. |
| `Blender -b --factory-startup --python harness/snapshot_check.py` | ~2s | The build's in-place restore (skipped in stock Blender). |
| `Blender -b --factory-startup --python harness/evals/selfcheck.py` | ~5s | Every eval check fails on the untouched scene and passes on its reference solution. No model. |
| `python3 harness/evals/run.py [tasks] [--tag t] [--label "what changed"]` | ~6 min, costs tokens | 22 real agent turns, checked by reading the scene; `out/evals/<time>/summary.md` has pass rate, time, steps, failed calls and what was won or lost against the previous run. |

When a run goes badly, read the task's transcript in `out/evals/<time>/<task>.md` before blaming
the model: it shows exactly what the tools told it. The two regressions found this way so far were
both tools lying (an enum listed as `['DEFAULT']`; two look-alike objects overlapping in a capture).

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
- The user's own .blend is never written. In the Loopcut build a restore happens in place: the
  open file keeps its path (also after a Save As, with relative paths rebased) and is marked as
  having unsaved changes. Stock Blender cannot do that from Python (the file path would point
  into our store and the next Ctrl+S would land there), so there the restored scene is saved at
  once as `<name>.restored-<time>.blend` beside the original. Index entries record which kind
  they are, so one store works with both.
- If a checkpoint cannot be written, the step is not run.
- Storage: compressed full copies under the Blender user data folder (`LOOPCUT_DATA_DIR`
  overrides), capped per conversation by `LOOPCUT_CHECKPOINT_BUDGET_MB` (default 2048). Over
  budget, the oldest expire first; the first checkpoint, the newest five and the latest
  pre-restore snapshot are protected. Expired ones show as "Checkpoint expired". Stores older
  than 30 days are removed at startup.
- Not covered: files the agent writes elsewhere on disk, preferences, add-ons, linked libraries.

Check: `LOOPCUT_DATA_DIR=<tmp>/data Blender --factory-startup --enable-event-simulate --python harness/checkpoint_check.py -- <tmp>/project`
clicks the real buttons and asserts the original file's hash never changes.

## Conversations

Conversations survive Blender restarts. Opening a .blend resumes the latest conversation that
worked in it; "History" in the header lists the others for that file, and "New chat" starts a
fresh one without losing the old.

- Stored in Loopcut's data folder (`conversations/<id>/`), never inside the .blend, so sharing a
  file does not share the chats that built it, and the user's file is never written.
- A conversation belongs to every file it has worked in: Save As, the first save of an untitled
  scene, and checkpoint restores all add to that list. An unsaved scene always starts fresh.
- Viewport captures are stored once as PNGs beside the conversation and referenced from
  messages; they are expanded to data URIs only when a request is sent.
- A turn cut short (Stop, or Blender closing) gets its unanswered tool calls answered, so the
  history stays valid for the API. Files on disk are validated on load; a damaged one is skipped
  and left in place.
- Checkpoints keep working across restarts. A snapshot store untouched for 30 days is dropped
  to reclaim space; its conversation stays and shows those checkpoints as expired.

Check: `harness/persistence_check.py` (two launches sharing a data folder; see its docstring).
Harness scripts default `LOOPCUT_DATA_DIR` to a temp folder so they never touch real data.

## UI architecture

`ui/layout.py` is pure Python: session state in, display list out. `ui/draw.py` renders that list
(one SDF shader for rounded rects, `blf` for text). `ui/host.py` binds it to a Blender area and
handles input (`ui/textedit.py` is the input box's editing rules, pure and tested). `host.py`
picks the build's own editor when it exists and a borrowed Text Editor area otherwise.
