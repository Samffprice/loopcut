# Loopcut

An AI agent that works inside Blender: chat, it writes and runs `bpy`, looks at the viewport to
check its work, and every step is one undo away.

## Layout

| Path | What |
|---|---|
| `scripts/addons_core/loopcut/` | The product's brain and face, a core add-on. Python: agent loop, tools, and a UI drawn with `gpu`/`blf`. |
| `release/loopcut/` | The brand: icons, splash, the script that makes them. See "License and name". |
| `loopcut/harness/` | Scripts that drive Blender for screenshots and checks. Fixtures are canned conversations. |
| `loopcut/tests/` | Runs without Blender: agent loop against a scripted fake model server, layout. |
| `loopcut/harness/evals/` | The eval suite: real agent turns on scripted tasks, checked by reading the scene. |
| `loopcut/scripts/` | The dev loop, the first-run check, the Mac release. |
| everything else | Blender 5.2.2, changed in about forty files; see "The Loopcut build". |

This repository is a fork of Blender (`upstream` is projects.blender.org; our branch is `main`,
based on the `v5.2.2` tag). Next to the checkout, outside Git, sit `build/` (build folders, as
Blender's own `../build_*`), `tools/` (a stock Blender 5.2.2 for fast iteration), `out/` (harness
output) and `.env`. Blender's Git LFS files are not stored here: `make update` fetches them from
projects.blender.org, which it does for any fork hosted elsewhere. Push with `git push --no-verify`
so Git LFS does not try to upload Blender's files; Loopcut's own binaries in `release/loopcut/`
are ordinary Git files.

## Setup

Users: Preferences > Add-ons > Loopcut. Pick a provider (any OpenAI-compatible endpoint works,
including a local one), paste a key, pick a model. The key is kept in `credentials.json` in
Blender's config folder with user-only permissions, never in `userpref.blend` or a .blend.

Developers: copy `loopcut/.env.example` to `.env` next to the checkout and fill it in. Quote values containing `|`. Never commit
`.env`. Lookup order per setting: environment, preferences, `.env`.

Model-written code asks before it runs ("Always allow" on the card stops asking for that
conversation; the preference or `LOOPCUT_AUTO_RUN=true` turns asking off). A step that runs longer
than `LOOPCUT_RUN_TIMEOUT` (60 s) is stopped, so an endless loop cannot freeze Blender.

## The Loopcut build

Blender with six additions, kept small so rebasing onto a new release stays cheap:

- `SpaceLoopcut`, an editor type that is a shell: it clears its region, fires the add-on's draw
  callback, owns the "Loopcut" keymap and redraws when selection, mode or the open file change.
  All UI and logic stay in `scripts/addons_core/loopcut/` and hot-reload.
- `wm.loopcut_snapshot_write` / `wm.loopcut_snapshot_restore`: checkpoints as recovery files, so a
  restore happens in place (see Checkpoints).
- The add-on is a core add-on (`scripts/addons_core/loopcut`), enabled for new and existing
  preferences.
- Its own settings folder (`Loopcut/<version>` where Blender has `Blender/<version>`), so it
  never writes to stock Blender's preferences, and a first-run flow in the splash screen modelled
  on Cursor's: "Import Blender 5.2 Settings" copies preferences, add-ons, keymap, themes and the
  startup file from the newest stock Blender folder it can read (`blender_import.py`), or "Start
  Fresh" shows Blender's quick setup; then connect a model, privacy, done. The steps are drawn by
  `scripts/addons_core/loopcut/onboarding.py`; the fork's splash menus only call it. Add-ons that did not
  load after an import are named on the last step.
- The product's name and face: `Loopcut.app` (executable `Loopcut`, bundle id
  `io.github.samffprice.loopcut`), "Loopcut (Blender 5.2.2)" in the window title, its own app
  icon, splash and top-bar icon. "Blender" and its logo are the Blender Foundation's trademarks,
  so a fork may not ship under them. The files are in `release/loopcut/`, made by its
  `make_assets.py` from the mark's polygons in `scripts/addons_core/loopcut/ui/brand.py`.
  On Windows the icon, product name and install folder are Loopcut's; the executable is still
  `blender.exe`.
- At startup the panel is docked right of the 3D viewport if the layout has none. Cmd+L
  (Ctrl+Alt+L elsewhere) focuses it, opening it first if needed.

Build: `ninja -C ../build/lite install` (the build folder must stay on the external disk). After
changing Python only, the same command re-copies the add-on in seconds. The same extension still
runs in stock Blender, borrowing a Text Editor area; the harness uses whichever Blender
`LOOPCUT_BLENDER` names and always loads the checkout, not the bundled copy.

The dev build has `WITH_ASSERT_ABORT` on; a release build must turn it and `WITH_ASSERT_RELEASE` off.

## Releases

The Mac build is made at home, the Windows one on GitHub's runners (it cannot be built on a Mac):

    git tag v0.1.0 && git push --no-verify origin main v0.1.0
    loopcut/scripts/release_mac.sh v0.1.0 --upload               # .dmg, added to a draft release
    gh workflow run release.yml -f release_tag=v0.1.0            # Windows .zip/.msi, same release

`.github/workflows/release.yml` only ever starts by hand and builds straight from this repository.
A hosted build takes hours; in a private repository Windows minutes are billed at 2x and macOS at
10x. The tag is the release's source, which is what the GPL asks for: whoever gets the app can get
exactly that.

Both use Blender's release configuration. On a Mac that is everything: Cycles' Metal kernels are
compiled on the user's machine. On Windows the NVIDIA (CUDA, OptiX) and AMD (HIP) kernels are
taken from the official Blender release of the same version, checked against
`loopcut/official-builds.sha256`: Loopcut does not change Cycles, so they are the kernels this
source would produce, without the vendor SDKs and hours of compiling. Update that file when
moving to a new Blender version, and compile the kernels instead if Cycles is ever changed.
Intel's oneAPI is off. The builds are unsigned: macOS says the app
is damaged until `xattr -cr /Applications/Loopcut.app` is run, and Windows shows a SmartScreen
warning; signing needs an Apple Developer ID and a Windows code-signing certificate.
`.github/workflows/tests.yml` runs the Blender-free tests on every push.

## License and name

The code is Blender's and ours together, under the GNU General Public License v3 or later, like
Blender (`COPYING`, `doc/license/`). That includes the add-on: it is written against Blender's
Python API and ships inside the app. Anything that only talks to the app over the network, such as
a hosted model service, is separate work and does not belong in this repository.

The license covers the code, not the name. "Loopcut" and the Loopcut mark (`release/loopcut/`)
identify this product, and the GPL grants no right to use them; a fork of this repository needs
its own name and icons, exactly as this one does not use Blender's. Blender is a trademark
of the Blender Foundation; Loopcut is based on Blender and is not affiliated with or endorsed by
the Blender Foundation.

## What the agent can do

| Tool | For |
|---|---|
| `run_python` | Change the scene. Its result ends with "Scene changes": what was really added, removed and changed, measured from the scene, so the model reads ground truth instead of printing values to check itself. `capture` returns a viewport capture in the same result, one request instead of two. |
| `get_scene_info` | What is in the scene. Large scenes list names by collection and take `name_contains` / `type`. |
| `get_object_info` | How an object is set up: modifier settings, material and geometry node trees, constraints, animation. Read before editing someone's material. |
| `inspect_api` | The Python API of the running Blender: properties, enum values, operator arguments, node sockets, plus tested notes where recent versions differ from what models remember. |
| `capture_viewport` | A self-framed image (`three_quarter`, `front`, `side`, `top`, `camera`, `user`; style `material` or `distinct`) and which objects are nearest, because similar colors hide what is in front. |
| `compare_with_reference` | The attached reference on the left and a capture on the right in one image, for the compare-adjust loop of copying a picture. |
| `look_at_reference` | The attached reference at full resolution, or a region of it, to check a detail. |

Images can be attached to a message: drop image files on the panel or use "+ image" in the input
box (png, jpg, webp, bmp, tif, tga; up to 6 per message). Each is decoded by Blender and stored as
a PNG of at most 1568 px in the conversation's folder, so a dropped file is never sent as-is and
nothing is added to the .blend. An attached image is a reference: the model writes a
`<reference_card>` describing it before its first step (so the description survives any summary),
a 768 px copy rides every request until its chip above the input box is closed (the newest 3),
and the tools read details from the stored full-size copy. Attached images stay in the model's view
counted apart from viewport captures, so a reference photo is not pushed out by captures.

Every message also carries a `<scene_context>` block (file, mode, selection, and every object of a
scene up to twenty), so "make this shinier" works and small scenes need no get_scene_info call, and `@Name` pulls in that object, material or collection; the input box completes
names as you type. After a turn that changed something, a "Scene changes" card lists the diff with
Keep and Undo all (which restores that turn's checkpoint).

## The loops

| Command | Time | Use |
|---|---|---|
| `loopcut/scripts/dev.sh` | save → redraw <1s | Blender stays open with the panel; saving any file under `scripts/addons_core/loopcut/` hot-reloads it and keeps the conversation. |
| `loopcut/scripts/shot.sh <fixture>` | ~10s | Renders `loopcut/harness/fixtures/<fixture>.json` to `../out/`: window PNG, panel-only PNG, and the frame's display list as JSON. No network. |
| `python3 -m unittest discover -s loopcut/tests` | <1s | Agent loop and layout. |
| `Blender --factory-startup --python loopcut/harness/tools_check.py` | ~8s | The real tools against a real scene. |
| `Blender --factory-startup --enable-event-simulate --python loopcut/harness/input_check.py` | ~8s | Click, type, select, undo, complete an @mention, Esc through simulated events. |
| `Blender -b --factory-startup --python loopcut/harness/attachments_check.py` | ~3s | Attaching images: conversion to a bounded PNG, refusals, nothing left in `bpy.data`. `loopcut/scripts/shot.sh attachments` shows the chips. |
| `Blender -b --factory-startup --python loopcut/harness/api_docs_check.py` | ~3s | `inspect_api` against the real API, and that the code in its notes still runs. |
| `loopcut/scripts/onboarding_check.sh` | ~40s | First run of the build in a throwaway home folder: clicks through a fresh start, imports stock settings and checks they took effect and stock Blender's folder is byte-identical, screenshots every step to `../out/onboarding/`. |
| `Blender -b --factory-startup --python loopcut/harness/snapshot_check.py` | ~2s | The build's in-place restore (skipped in stock Blender). |
| `Blender -b --factory-startup --python loopcut/harness/evals/selfcheck.py` | ~5s | Every eval check fails on the untouched scene and passes on its reference solution. No model. |
| `python3 loopcut/harness/evals/run.py [tasks] [--tag t] [--label "what changed"]` | ~6 min, costs tokens | 22 real agent turns, checked by reading the scene; `../out/evals/<time>/summary.md` has pass rate, time, steps, failed calls and what was won or lost against the previous run. |

When a run goes badly, read the task's transcript in `../out/evals/<time>/<task>.md` before blaming
the model: it shows exactly what the tools told it. The two regressions found this way so far were
both tools lying (an enum listed as `['DEFAULT']`; two look-alike objects overlapping in a capture).

Debugging a frame: read `../out/<fixture>.json` before squinting at pixels. Every primitive has an
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

Check: `LOOPCUT_DATA_DIR=<tmp>/data Blender --factory-startup --enable-event-simulate --python loopcut/harness/checkpoint_check.py -- <tmp>/project`
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

Check: `loopcut/harness/persistence_check.py` (two launches sharing a data folder; see its docstring).
Harness scripts default `LOOPCUT_DATA_DIR` to a temp folder so they never touch real data.

## Context

Every request carries the conversation, so a long session pays for its history again on every
step; the cost of a conversation grows with the square of its length unless something is cut.
`context.py` keeps each request within `LOOPCUT_CONTEXT_BUDGET` tokens (preference "Context
budget", default 24000), in order:

1. Elision, on every request. The code of all but the newest two `run_python` calls is cut to
   a line, and all but the newest four tool results are cut to a headline (first line or the
   exception, plus the first scene change) and a note to call the tool again. Both edits land a
   few messages from the end and are never undone, so the prefix of a request stays stable and
   provider prompt caches keep hitting. At request time, runs of finished steps are folded into
   one "Earlier steps" message, one line each. Captures are limited to three per message by the
   agent loop, counting `run_python` steps that asked for one.
2. Compaction. If that is not enough, the model summarizes everything before the current turn
   (or before a later point, if the current turn alone is too big) into one
   `<conversation_summary>` message, and the chat shows a "Summarized N earlier messages" notice.
   The summary is stored on the first message it does not cover, so checkpoint restores that cut
   the conversation cut or keep it correctly.
3. Images: a capture is sent with the request right after it and dropped once the model has
   acted on it, since that step also changed what it showed. Attached references are pinned:
   sent with every request, at 768 px, until unpinned; they sit early in the prefix, so the
   provider cache pays for them. On the gateway one image costs as much as ten tool results on
   every request it rides in. Looks at an unchanged scene are refused after three in a turn,
   and any look after eight. A 960 px capture cost about 1700 tokens on the Loopcut gateway, ten times a
   typical tool result, so captures are taken at 640 px.

Sizes are estimated from characters (3.4 per token, 1700 per image, both measured against the
gateway's logs) and corrected with the token count the API reports for each request; the footer
shows that count ("12.3k context") next to the conversation's total. The
stored conversation keeps every message in place, so nothing above changes what the chat shows or
where a checkpoint cuts.

## UI architecture

`ui/layout.py` is pure Python: session state in, display list out. `ui/draw.py` renders that list
(one SDF shader for rounded rects, `blf` for text). `ui/host.py` binds it to a Blender area and
handles input (`ui/textedit.py` is the input box's editing rules, pure and tested). `host.py`
picks the build's own editor when it exists and a borrowed Text Editor area otherwise.
