# Developing Loopcut

How the repository is laid out, how the fork differs from Blender, how to build, test and release,
and how the agent, checkpoints, conversations and context work inside. The [README](../README.md)
is the product page; this is the manual.

## Layout

| Path | What |
|---|---|
| `scripts/addons_core/loopcut/` | The product's brain and face, a core add-on. Python: agent loop, tools, and a UI drawn with `gpu`/`blf`. |
| `release/loopcut/` | The brand: icons, splash, the script that makes them. See "License and name". |
| `loopcut/harness/` | Scripts that drive Blender for screenshots and checks. Fixtures are canned conversations; `fake_model.py` is a scripted model server. |
| `loopcut/tests/` | Runs without Blender: agent loop against a scripted fake model server, layout. |
| `loopcut/harness/evals/` | The eval suite: real agent turns on scripted tasks, checked by reading the scene. |
| `loopcut/scripts/` | The dev loop, the first-run check, the Mac release. |
| everything else | Blender 5.2.2, changed in about forty files; see "The Loopcut build" below. |

This repository is a fork of Blender (`upstream` is projects.blender.org; our branch is `main`,
based on the `v5.2.2` tag). Next to the checkout, outside Git, sit `build/` (build folders, as
Blender's own `../build_*`), `tools/` (a stock Blender 5.2.2 for fast iteration), `out/` (harness
output) and `.env`. Blender's Git LFS files are not stored here: `make update` fetches them from
projects.blender.org, which it does for any fork hosted elsewhere. Push with `git push --no-verify`
so Git LFS does not try to upload Blender's files; Loopcut's own binaries in `release/loopcut/`
are ordinary Git files.

## Setup

Keys entered in the app are kept in `credentials.json` in Blender's config folder with user-only
permissions, never in `userpref.blend` or a .blend.

Developers: copy `loopcut/.env.example` to `.env` next to the checkout and fill it in. Quote values containing `|`. Never commit
`.env`. Lookup order per setting: environment, preferences, `.env`.

Model providers (`settings.PROVIDERS`): Loopcut's own models through the gateway; the ChatGPT plan
(`chatgpt_responses.py`, the Responses API); own keys for OpenAI-compatible chat completions (OpenAI,
OpenRouter, Meta, Ollama, Custom; `llm.py`); and Anthropic (`anthropic_messages.py`, the Messages API). Two
providers are pseudo base URLs that `llm.stream_chat` hands to their own adapter: `chatgpt-plan://…` and
`anthropic://api.anthropic.com/v1` (sent as https). The Anthropic adapter puts `cache_control` on the system
prompt, the end of the request and the end of the previous request (3 of 4 breakpoints, 5-minute TTL); turns
Loopcut's reasoning effort into adaptive thinking with `output_config.effort` (Opus 5.5 by default;
`LOOPCUT_ANTHROPIC_THINKING=off` turns thinking off where the model allows); replays thinking blocks with
their signatures, asking the API to drop the ones a folded history invalidated (`drop_block`); and shows the
notes Opus/Sonnet 5.5 write between tool calls (`display: "updates"`). Usage carries `cache_write`, which
`pricing.py` prices at the cache-write rate. Keep mode (`LOOPCUT_KEEP_CONTEXT`, see Context) is on by default
for Anthropic. On own keys, Anthropic included, a turn runs the open single loop: workers and the router need
the gateway's prompts. Details and test list:
`loopcut/track/ANTHROPIC-PROVIDER.md`.

Model-written code asks before it runs ("Always allow" on the card stops asking for that
conversation; the preference or `LOOPCUT_AUTO_RUN=true` turns asking off). Renders and bakes
ask even then; "Always allow renders" on a render's card stops that for the conversation, though
code that bakes still needs the plain "Always allow" too. A final render in the code
(`render.render`) is not asked about as a render: the step asks as code, and the cost guard refuses
the call when the code reaches it (below). A step that runs longer
than `LOOPCUT_RUN_TIMEOUT` (60 s) is stopped at the next Python trace event. This cannot interrupt
a native render or bake. [Durable render jobs](RENDER_JOBS.md) run outside the editing process,
with a separate supervisor enforcing cancellation and time limits.

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
  Fresh" shows Blender's quick setup; then a required free Loopcut account (with a Claim for the
  trial credits the server granted), how Loopcut works (the ChatGPT plan as the thinking, the Loopcut account as the cloud tools, and with sales open Free and Pro side by side), one model, privacy, and three first projects. The steps are drawn by
  `scripts/addons_core/loopcut/onboarding.py`; the fork's splash menus only call it. Add-ons that did not
  load after an import are named on the last step. While a step shows, the add-on sets
  `WindowManager.splash_modal` (`wm_splash_screen.cc`): the window behind is dimmed and clicking
  outside, Escape, Enter, drops and the splash image do not close it (`BLOCK_MODAL`); the add-on closes
  it with `wm.splash_close`. The step is saved to `onboarding.json` in Loopcut's config folder, so
  quitting half way resumes there. The steps draw with the fork's layout additions:
  `UILayout.label_styled` (text at a size, weight and colour, drawn as an image), `box_padded` and
  `operator_link` (an operator as a text link in a colour), in loopcut.org's palette set on the
  splash block only (`block_theme_override_set`, never the user's theme). The outline icons in
  `media/icons` are Lucide's (ISC, the add-on's `THIRD-PARTY-NOTICES.txt`), made by
  `release/loopcut/make_setup_icons.py`.
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

## Bundled extensions

UniV, LoopTools, Node Arrange, MPFB and Bool Tool ship unmodified in
`release/extensions/system/`. CMake installs them into Blender's built-in read-only **System**
extension repository. `release/extensions/bundled.json` pins the archives, hashes, file counts
and credits; upstream licence texts are in `release/extensions/licenses/`. Generated
`__pycache__` directories are excluded from installation and source verification;
loose `*.pyc` and `*.pyo` files are also excluded from installation.

These add-ons start disabled. The model sees short capability hints; `inspect_api(path="looptools")`
or a bundled operator path enables the package for the session, then describes the live API.
Search is read-only. UniV registers only in interactive Blender. Node Arrange also needs a node
editor context to run its operators, including its batch operator.

Session enabling uses `default_set=True` for AddonPreferences and restores the previous
`preferences.is_dirty` value. Interactive enables are journaled in
`bundled_addons_session.json` beside Loopcut's `credentials.json`. At the next startup, after
saved add-ons load, any recorded enable that was incidentally saved is disabled and preferences
marked dirty for Blender to save the repair. The record stays until a later startup sees it off.
Packages already enabled by the user are never recorded or cleaned up; background processes
never write or consume the journal. This path never calls `save_userpref()`.

Verify the shipped tree offline, or retrieve the pinned source for comparison:

```sh
python3 loopcut/scripts/bundled_extensions.py verify
python3 loopcut/scripts/bundled_extensions.py fetch looptools
python3 loopcut/scripts/bundled_extensions.py fetch looptools --write
```

For an update, review the new upstream archive and licences, update that package's pin in
`bundled.json` (version, archive URL/SHA-256, file count/tree SHA-256 and other changed metadata),
then fetch it with `--write`, refresh its licence copies and credits, and run verification and
the checks below. `fetch --all` compares every pinned archive; `--write` explicitly replaces
the selected package directories only after both hashes pass. It never chooses a new version.

```sh
python3 -m unittest discover -s loopcut/tests
LOOPCUT_BLENDER=/path/to/Blender python3 loopcut/harness/bundled_addons_check.py
```

The Blender check defaults to `../tools/Blender.app/Contents/MacOS/Blender` and needs a desktop
session. It starts one Blender at a time with a read-only temporary extension copy and fresh
user resources, without changing `HOME`. LoopTools, Node Arrange's lifecycle, Bool Tool and
MPFB run headlessly; Node Arrange's measured arrangement and UniV's UV operation run in
interactive windows that close themselves. Each process has a 240-second watchdog (override
with `--seconds`); logs and isolated user folders are kept at the printed temporary path.

## Releases

Prepare a squashed public-source review before using the release commands below:

    python3 loopcut/scripts/public_release.py --dry-run --source HEAD --base origin/main --build-root "$PWD" --out /private/tmp/loopcut-public-review

The output directory must be new or empty. The script writes only Git objects and
`refs/loopcut-release/dry-run`; it preserves branches, worktree indexes and remotes.
It exports the whole snapshot fork to `tree/` and writes `report.md` and `report.json` with
locations and hashes, never matched text. Every dry run also requires the platform
leak checker on the whole snapshot tree, isolated complete snapshot history,
and the shipped add-on ZIP (checked by both scanners), using the same `--prompts-dir`
and `--skills-dir`; repeat `--zip /path/to/extension.zip` to check additional archives.
Select the platform script with `--leak-check PATH` or `LOOPCUT_RELEASE_LEAK_CHECK`
(otherwise the local platform/web script is discovered); missing scripts and scan errors
fail as incomplete, and `--no-leak-check` skips the platform checks for tests only.
All platform modes run non-strict: gitlinks and unresolved LFS pointers are reported
as unscanned SKIPs, separately from hits. A clean result does not certify skipped content.
Platform hits are classified as public-in-base (every matching window already in
`loopcut/` or `scripts/addons_core/loopcut/` of the base), exact-line allowlisted, or
unexplained. History classification combines matches from both old and new blobs
at the reported line. Upstream file hits require an exact-line allowlist entry;
commit/path metadata requires all windows to be public-in-base, and redacted locations
remain unexplained. The selected checker's importable Python companion supplies its
fingerprints and text decoding, including manifest-open suppression. Reports contain
both passes and only locations, owners, hashes and classification reasons.
Only owners whose hits are all explained enter a temporary second-pass `--allow` file, and a clean dry run requires every platform mode to exit 0 with no unexplained hits from either scanner.
`public_release.py leak-check BASE..TIP --out DIR` checks
an existing range without writing Git objects or refs. A single commit uses `--base`.

Both commands also scan **build inputs** in `--build-root` (default: the checkout
containing the script), including untracked and Git-ignored files. These are the recursive
working-tree install sources in `source/creator/CMakeLists.txt`: `scripts/`,
`release/extensions/`, `release/datafiles/{fonts,colormanagement,studiolights}/`,
`release/license/`, the macOS bundle template's `Contents/Resources/`, and `assets/`
when present. The separately installed `scripts/site/sitecustomize.py` is included even
though the directory copy excludes `site/`. External dependency libraries, generated
build products and explicitly named non-Python files (such as geometry `.dat` icons)
are outside this pass. Symlinks that would ship, missing required inputs and install-rule
drift make the scan incomplete instead of silently reducing coverage.

The shared `BUILD_INPUT_PATTERNS` list mirrors CMake's exclusions; tests and a runtime
comparison against the selected build checkout detect drift. It models the release
configuration with Freestyle enabled and the supported Python version from that checkout.
Reports identify the absolute build root and HEAD, list excluded files with SHA-256 hashes,
and include findings under `build-inputs`. A HEAD mismatch with `--source` (or the checked
range's tip) is a warning: the scan still checks the selected working-tree bytes, but does
not claim they represent the snapshot. Uncommitted edits are likewise build inputs only;
the snapshot always comes from the selected commit. A build-input leak blocks with exit 1.

Python caches are never useful release source: snapshot creation unconditionally removes
`__pycache__/`, `*.pyc` and `*.pyo`, including tracked files, and records them in
`excluded_paths`. The shipped add-on ZIP refuses a tree containing caches; existing trees
and supplied ZIPs report caches as non-allowlistable blockers. The installs of our Python
sources (`scripts/`, `release/extensions/`, the macOS resources helper) exclude all three
patterns; other scanned inputs have no exclusions, so any bytecode there blocks the scan. Explicit Python `FILES` installs name `.py` sources,
including Cycles' enumerated add-on files through `delayed_do_install`; they do not glob
caches. CPack's pre-build hook (`build_files/cmake/loopcut_release_hygiene.cmake`) removes
any bytecode from its final staging tree before packaging, including stale caches already in a
reused macOS app bundle, and logs how many entries it removed. Mac and Windows smoke tests run after
packaging: Blender's isolated Python ignores `PYTHONDONTWRITEBYTECODE` and may create
runtime caches in the local install tree. Python build helpers suppress bytecode, and
the separate `package_archive` copy also filters caches.

Private comparison files are read at runtime. Override their locations with
`--skills-dir`, `--prompts-dir`, `--research-dir`, `--economics-file`, or the matching
`LOOPCUT_RELEASE_SKILLS_DIR`, `LOOPCUT_RELEASE_PROMPTS_DIR`, `LOOPCUT_RELEASE_RESEARCH_DIR`,
`LOOPCUT_RELEASE_ECONOMICS_FILE` environment variables; `--cost-doc FILE` is repeatable.
The research folders (kept out of every snapshot) and the default `--research-dir` are named
only in the private `loopcut/track/research_folders.txt`; without it the research check reports
missing coverage.
Missing sources are reported as reduced coverage. Withheld add-on constants from
server-held-prompts plan section 1 are expected blockers until moved to the server.
Review other findings before adding their SHA-256 hashes and reasons to
`scripts/public_release_allowlist.txt`. Exit codes are 0 for clean/reviewed, 1 for
unexplained hits, and 2 for an incomplete check. This script contains no publishing action.

For a reviewed upstream API quote, append its exact blob-line hash without copying text:

    python3 loopcut/scripts/public_release.py allowlist-add --rev COMMIT --path source/file.cc --line 123 --reason "Reviewed upstream API documentation"

This command refuses Loopcut/add-on paths and empty reasons. Editing the line invalidates
the entry; allowlisting a fingerprint alone cannot explain an upstream platform hit.

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

V1 request parity runs in plain Python against `fake_model` and `prompt_proxy`:

    PYTHONDONTWRITEBYTECODE=1 python3 loopcut/harness/prompt_parity_check.py --out /private/tmp/loopcut-v1-review

The old add-on remains `a1b09694021`. `--prompts` and `--skills` default to the platform's
current directories; `--server-edits` defaults to its `prompts/fixtures/SOURCE.json`.
Only recorded `worker` replacement pairs alter the old comparison baseline, each matching
exactly once in the worker system string; the original captured bodies remain unchanged.
Single, orchestrator, router and control bodies, worker bytes outside that string, and
skill bodies still require exact parity. Missing or invalid edit metadata is INCOMPLETE
(exit 2); a replacement matching zero or multiple times fails. Reports show metadata
paths, hashes and edit counts, never replacement text. Other server/client changes remain
visible as failures; the 52-request run is not evidence of parity unless every pair and
source verification passes. On 2026-10-09 (C19) the server-held text matched after the three
recorded worker edits; 6 of 52 bodies were identical (router, controls) and the other 46 differed
only by client-side changes made after V1: the `inspect_api` (C17) and `search_assets` (C12b) tool
descriptions, and the worker brief's step count and budget wording (C16). Artifacts stay in a new directory under `/private/tmp`.


## What the agent can do

| Tool | For |
|---|---|
| `run_python` | Change the scene. Its result ends with "Scene changes": what was really added, removed and changed, measured from the scene, so the model reads ground truth instead of printing values to check itself. `capture` returns a viewport capture in the same result, one request instead of two. |
| `get_scene_info` | What is in the scene. Large scenes list names by collection and take `name_contains` / `type`. |
| `get_object_info` | How an object is set up: modifier settings, material and geometry node trees, constraints, animation, custom properties. Read before editing someone's material. `detail=transform`: only transform, dimensions, bounds, mesh counts, modifiers, parent/children and slot names. Past 8,000 characters whole sections go, largest first, named in `omitted` (`object_info.fit`), never a value cut in half. `"world"` (or a world's name) reads the world's node tree. A socket name a node has twice carries its index, here and in Scene changes' links: `Mix Shader.Shader (inputs[2])`, so a swapped connection shows. |
| `check_contact` | Placement measured on the evaluated world-space meshes, not judged from bounds or a capture: for pairs `{a, relation, b}`, `rests_on` (every foot or leg of a on b's top, gravity -Z; a leg through a rug fails), `touches`, `clear_of` (at least `gap` apart) or `inside` (b's convex hull), each gets pass, fail or unavailable with the reason, the gap or penetration, and for a failure the move that would fix it. Nothing is moved. An empty stands for its child meshes. `watch=true` measures the pairs again after every later `run_python` of the conversation and lists a change of status in its Scene changes. How: `contact.py`'s docstring. |
| `inspect_api` | The Python API of the running Blender: properties, enum values, operator arguments, node sockets, plus tested notes where recent versions differ from what models remember. |
| `capture_viewport` | A self-framed image (`three_quarter`, `front`, `side`, `top`, `camera`, `user`; style `material` or `distinct`) and which objects are nearest, because similar colors hide what is in front. Its caption ends with how it was drawn (viewport mode, view transform, size). Object names are drawn on only with `labels: true` (here and in `compare_with_reference`): on a close shot they covered the subject. A worker's own `capture_viewport` renders in its child instead: see "Worker looks". |
| `frame_camera` | Aim a camera and fit `objects` (a root or empty counts its children, instances included) into its render frame in one call: `direction` `keep` (its rotation), `front`, `side`, `top`, `three_quarter` or a vector, no roll; `margin` per side; `region` (x0,y0,x1,y1) fits into part of the frame, the lens shift kept; `lens`, or `ortho_scale` for an orthographic camera. Solved exactly from evaluated vertices, render aspect, sensor fit and shift (`framing.py`), not a bounding sphere; the clip range widens if the subject needs it; `create` makes a scene camera. Reports per object its bounds in frame, % inside, clipped edges, margins and offset, and which margin binds the fit. A scene change (checkpoint, approval, one undo step), except `report_only`, which only measures (`tools.only_reads`), with `frames` a line per frame. |
| `inspect_scene` | [Real inspection renders](VISUAL_INSPECTION.md) across named cameras and frames, in a labeled sheet; geometry, studio materials, or actual scene lighting, with stale-result rejection. |
| `compare_with_reference` | The attached reference on the left and a capture on the right in one image, for the compare-adjust loop of copying a picture. |
| `look_at_reference` | The attached reference at full resolution, or a region of it, to check a detail. |
| `list_files` / `read_file` / `write_file` / `move_file` | The disk: list and read, unasked inside the project (an image file comes back as an image); reads elsewhere, writes and moves wait for the user's approval. Nothing deletes. |
| `see_render` | The user's last render, or with `render=true` a render now, which waits for the user's OK unless they allowed renders. |
| `render_job` | `action`: `start` renders a saved scene copy to a verified PNG sequence or MP4 in the background (asks first, like any render), into a new subfolder of `output_dir` or straight into a new or empty `folder` by the .blend, `frames` or `start`/`end`, nothing but renders in the output; `status` shows progress, persisted in the Jobs view, and with `wait` waits up to 240 s for the job to end on the agent thread, not Blender's (see RENDER_JOBS.md); `cancel` stops a job; `resume` renders only missing or invalid frames under another approved attempt budget. |
| `inspect_uv` | UV maps measured, not judged from a picture (`uv_tools.py`): per object islands, faces outside 0-1, zero-area and flipped faces, overlaps (a raster of the tile), stretch (angle and area, per triangle), texel density in px/m at a resolution and its spread across objects, gutters in px, worst face ids. `checker` adds an image of the objects wearing a temporary checker (Workbench texture colour), everything put back; `plan` sizes an atlas for a density: coverage, memory, fragmentation. |
| `uv_unwrap` | smart, cube, cylinder, sphere, seams or angle_seams, then islands at one density, packed with padding into 0-1 (or one shared `atlas`), optionally at a `density`; returns the audit. Headless in a worker's child (a mesh the brief does not own is refused); an orchestrator may not unwrap what a worker built or a running worker owns. Rewriting a map that image textures read keeps the old layout as `<map> before unwrap`. |
| `transfer_textures` | Bakes each image the objects' materials read through `from_uv` onto `to_uv` (Cycles, same size and colour space; alpha baked separately; tangent-space normal maps re-baked for the new tangents), packs the new images, repoints the image nodes (and Normal Map nodes) at them through `to_uv`, makes `to_uv` active and rendering, keeps `from_uv`. Skips tiling textures, UDIM and images not read through a UV map; refuses a material shared with an object not in the call. Single loop only (the orchestrator's and workers' fixed-token caps). `harness/uv_transfer_check.py`. The op note on UV operators says when an unwrap would move painted artwork. |
| `inspect_texture` | A material's, image's or object's image nodes (`texture_tools.py`): the path to the shader socket each feeds, its UV map (through a Mapping node), colour space, size, packed or file, unsaved edits, a hash of its pixels that any paint changes; samples at uv points or faces, read as the node reads them; a thumbnail. |
| `search_assets` / `import_asset` | Asset libraries, by `source`: `blenderkit` (free assets with no account, a plan key for the rest), `polyhaven` (CC0), `library` (the user's Blender asset libraries, Online Essentials, remote ones) and `polypizza` (low-poly, CC0 or CC-BY; needs the user's free key). Search returns a numbered thumbnail sheet; import asks first and takes a checkpoint. |

The tool list is sent with every step, so related tools share one schema with a `source` or an
`action` rather than one each. Conversations saved before the merge still open: `conversations.load`
respells their calls to the old names (`tools.RENAMED`) for the model, and their cards keep the
names they showed.

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
Keep and Undo all (which restores that turn's checkpoint). The diff (`scene_diff.py`) counts each
mesh's active UV map and the pixels of the images scene materials read (capped; past the cap, an
image's unsaved-edits flag), so an unwrap or a repaint is a change, not "Scene changes: none".

### Blender 5.2 traps: UVs and images

Measured while building `uv_tools.py`, `texture_tools.py` and the painterly skills:

- A UV layer reference (`mesh.uv_layers[...]`, `.active`) held across an Edit Mode toggle reads freed
  memory and crashes Blender. Look layers up by name after every mode switch.
- In Edit Mode the mesh's UV data reads empty, even after `update_from_editmode()`; read UVs from the
  edit mesh (`bmesh.from_edit_mesh(mesh).loops.layers.uv`), as `uv_tools._gather` and
  `ownership.uv_maps` do.
- `mesh.materials.pop()` leaves the object's material slot behind; `mesh.materials.clear()` takes it.
- `pack_islands` with rotate `ANY` on `CONCAVE` shapes is slow (8 s for six squares); uv_unwrap packs
  bounding boxes turned by quarter turns (`CARDINAL`).
- A new 8-bit image takes sRGB values in `pixels`; only a float image (`float_buffer=True`) takes
  linear ones.
- Alpha below 1 darkens colour (it scales Color where a material reads the image): paint with alpha 1.
- An image with no user is dropped on save, and a painted image must be packed (`image.pack()`) again
  after each repaint, or the saved file (a worker's result included) keeps the old pixels. Ownership
  tokens see a repaint only through the packed size, so an unpacked repaint does not merge.

## Cost guard

`run_python` runs the model's code on Blender's main thread, and its timeout is a line trace, so it
cannot stop one long native call. Subdivision level 6 on a dense mesh, or a 1,000-count array,
freezes Blender the moment the scene is evaluated, which happens early: every `bpy.ops` call
updates the view layer before and after the operator (`bpy_op_view_layer_update` in
`bpy_operator_function.cc`), and the snapshot after the code updates it too. `cost_guard.py`
therefore runs before each of those:

- **Operators.** While the code runs, `bpy.ops`' own `_op_create_function` is patched, so every
  operator the code looks up is wrapped and checks before the real call. A headless spike showed
  this reaches every import style; swapping the `bpy.ops` attribute for a proxy missed
  `from bpy.ops import mesh`. Restored in `finally`.
- **The snapshot.** `scene_diff.snapshot(guard=...)` reads everything that does not evaluate
  (names, settings, modifier settings, original mesh counts), runs the guard, then calls
  `view_layer.update()` for placement and bounds.
- **Renders.** `see_render`, `render_job` `start` (a `resume` renders the saved copy that `start`
  checked), `inspect_scene`, and `object.bake` in the model's code check render levels and refuse
  with the reason; nothing is changed for them. Rendered viewport captures draw the viewport
  depsgraph, so viewport levels, which the operator and snapshot checks already covered. A refusal
  is a failed call like any other to the step budget below: the same refusal a third time gets
  "this approach is not working".
- **Final renders in code.** `render.render` and `render.play_rendered_anim` in the model's code are
  refused whatever the scene weighs: even a light Cycles render there holds the main thread until it
  ends. The refusal points to `see_render` (a still to look at) and `render_job` (files on disk).
  `object.bake` is left to the limits above, since there is no out-of-process bake. The approval
  gate (`tools.HEAVY_CODE`) decides before the step, from the code's text; the guard decides at the
  call, while it runs. So `HEAVY_CODE` leaves `render.render` out: asking to allow a render that
  the guard will refuse would contradict it.

A check estimates the final faces of each object whose modifiers changed since the last check (what
was there when the step started is the user's and is left alone) from settings alone: subdivision
and multires x4 per level (viewport or render levels), array x count (fit length from the object's
size; fit to curve unknown), mirror x2 per axis, solidify x2 plus a rim, bevel by segments and limit
method, screw edges x steps x iterations, triangulate x2, deform-only modifiers x1. Remesh, geometry
nodes, boolean with a collection and anything unlisted are unknown: counted x1 (the estimate becomes
"at least"), reported, never switched off. Above `MAX_OBJECT_FACES` (2M) per object or
`MAX_SCENE_FACES` (6M) over all objects with modifiers, the guard switches off `show_viewport` on the
modifier that lowers the estimate most, until it fits. Renders use `MAX_RENDER_OBJECT_FACES` (4M) and
`MAX_RENDER_SCENE_FACES` (12M). Modifier and render settings are never changed.

Nothing is silent: the tool result lists each switch ("Subsurf on Chair_Seat, est. 12M faces:
viewport display off; lower the viewport level") and each unknown, and the panel shows the same
line as a notice under the step, with "Show anyway" to turn it back on (which counts as a scene
change for the step budget's repeated reads).

Known and accepted as uncatchable: code that evaluates by itself (`evaluated_depsgraph_get`,
`evaluated_get`, `to_mesh`, `view_layer.update()`, `depsgraph.update()`, `frame_set`); operators
reached other than through a `bpy.ops` lookup made while the code runs (a callable fetched
earlier, UI buttons, timers); geometry built directly with bmesh or `from_pydata`. Non-mesh objects
(curves, text) with modifiers are reported as unknown.

## The loops

| Command | Time | Use |
|---|---|---|
| `loopcut/scripts/dev.sh` | save → redraw <1s | Blender stays open with the panel; saving any file under `scripts/addons_core/loopcut/` hot-reloads it and keeps the conversation. |
| `loopcut/scripts/shot.sh <fixture>` | ~10s | Renders `loopcut/harness/fixtures/<fixture>.json` to `../out/`: window PNG, panel-only PNG, and the frame's display list as JSON. No network. |
| `python3 -m unittest discover -s loopcut/tests` | <1s | Agent loop and layout. |
| `python3 loopcut/harness/anthropic_check.py [--compat]` | ~20s, a few cents | The Anthropic provider against the real API: a tool call, its result sent back with thinking and read from the cache, the same history without thinking; `--compat` adds Anthropic's OpenAI-compatible endpoint through the Custom path. Key from `ANTHROPIC_API_KEY` or `--env-file`/`--key-name`; never printed. |
| `Blender --factory-startup --python loopcut/harness/tools_check.py` | ~8s | The real tools against a real scene. |
| `Blender -b --factory-startup --python loopcut/harness/polyhaven_check.py` | ~30s | Poly Haven search and import against the live API (network; headless is fine). |
| `Blender -b --factory-startup --python loopcut/harness/asset_libraries_check.py` | ~20s | search_assets / import_asset (source `library`) on a kit made on the spot, then Online Essentials (network; downloads into Blender's own remote-assets cache). |
| `LOOPCUT_POLYPIZZA_KEY=… Blender -b --factory-startup --python loopcut/harness/polypizza_check.py` | ~20s | Poly Pizza search and GLB import; without the key it checks the no-key message and exits SKIPPED. |
| `Blender -b --factory-startup --python loopcut/harness/blenderkit_check.py` | ~40s | BlenderKit search and import of a free model, material and HDRI with no key; `LOOPCUT_BLENDERKIT_KEY` adds the plan-assets search. |
| `Blender --factory-startup --enable-event-simulate --python loopcut/harness/input_check.py` | ~8s | Click, type, select, undo, complete an @mention, Esc through simulated events. |
| `LOOPCUT_SHOTS=<dir> Blender --factory-startup --enable-event-simulate -p 0 0 1440 900 --python loopcut/harness/vote_check.py` | ~15s | The vote view end to end against a stand-in `/v1` service in the same process (the saved manifest in `fixtures/vote_manifest.json`, `/v1/votes` with other voters seeded): the strip, opening the ranked view, a vote, two quick votes in a row, a fourth refused without a request, a take-back, the tooltip, back to chat; panel screenshots `vote-*.png` in `LOOPCUT_SHOTS`. Run it with `BLENDER_USER_RESOURCES` set to a throwaway folder. |
| `Blender -b --factory-startup --python loopcut/harness/attachments_check.py` | ~3s | Attaching images: conversion to a bounded PNG, refusals, nothing left in `bpy.data`. `loopcut/scripts/shot.sh attachments` shows the chips. |
| `Blender -b --factory-startup --python loopcut/harness/image_budget_check.py` | ~3s | Stored images and request budgets with the real encoder: a PNG render stored as a 1568 px JPEG its card still loads, flat and see-through images kept as PNG, a request of eight renders fitted under the budget and halved after a dropped upload. |
| `Blender -b --factory-startup --python loopcut/harness/api_docs_check.py` | ~3s | `inspect_api` against the real API, and that the code in its notes still runs. |
| `Loopcut --enable-event-simulate --python loopcut/harness/onboarding_flow_check.py -- --stage <stage> --out <dir>` | ~15s a stage | The 0.2.6 setup in a `BLENDER_USER_RESOURCES` sandbox with `LOOPCUT_SERVICE_URL` pointing nowhere: modal (an outside click, Esc and Enter leave it open), an import from fake stock settings (`LOOPCUT_STOCK_BLENDER_DIR`) that re-enables every add-on, a missing operator and a step with no button (never modal), resumed after quitting, a failed sign-in and "Continue without an account", an install from before it skipped silently, every screen with fixture data, and every kind of button acting on one click after the pointer rests on it (`--stage clicks`). The docstring has the stages and the command. |
| `loopcut/scripts/onboarding_check.sh` | ~40s | First run of the build in a throwaway home folder: clicks through a fresh start, imports stock settings and checks they took effect and stock Blender's folder is byte-identical, screenshots every step to `../out/onboarding/`. |
| `Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/contact_check.py` | ~3s | `check_contact` on fixtures built on the spot (a floating table top, a lamp with a short foot, a chair through a rug, roots, a collection instance, penetrating cubes, a mirrored slab, a one-sided wall, a bowl): every status, depth and fix asserted, each fix applied and checked again; then watched pairs that `run_python` breaks and mends; then the shared_support eval's reading corner (`corner.py`: a side table wound inward, a lamp base's rim level with the chair's feet), whose gaps once read as metres of overlap; an open strap loop (a torus with a gap cut, wound either way) 39 mm clear of a lens barrel, a band round a lens mount, and the strap dipped 10 mm in, which once read as 375 mm. |
| `Blender -b --factory-startup --python loopcut/harness/camera_check.py` | ~1s | `frame_camera` on scenes made on the spot (a row with an Array, a tall box at 9:16 and with VERTICAL sensor fit, nested children, a collection instance, orthographic, a QUATERNION camera, lens, shift, clip, a region with a shift kept, report_only at several frames of a Track To shot): every evaluated vertex projected with `world_to_camera_view` is inside, the tight margin is the one asked, the subject centred, the report's numbers match; `report_only` changes nothing; one undo step. |
| `Blender -b --factory-startup --python loopcut/harness/snapshot_check.py` | ~2s | The build's in-place restore (skipped in stock Blender). |
| `python3 loopcut/harness/guard_check.py [--heavy]` | ~2s | The cost guard in headless Blender under a watchdog that kills it past a time or memory limit: heavy subdivision caught at an operator and at the snapshot, a 1,000-count array, renders refused, a final render in code refused however light. Light by default (tiny meshes, lowered limits); `--heavy` uses real sizes and limits, so run it with nothing else heavy open. |
| `Blender -b --factory-startup --python loopcut/harness/evals/selfcheck.py` | ~5s | Every eval check fails on the untouched scene and passes on its reference solution. No model. |
| `python3 loopcut/harness/evals/run.py [tasks] [--tag t] [--label "what changed"]` | ~6 min, costs tokens | 22 real agent turns, checked by reading the scene; `../out/evals/<time>/summary.md` has pass rate, time, steps, failed calls and what was won or lost against the previous run. |
| `Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/worker_host_check.py` | ~22s | Worker children (below): IPC round trip, a look per role, a rollback, a timeout killed, a mesh left with a corner-less face repaired before its look, a segfault's crash kit, Stop killing 3 children, a SIGKILLed parent leaving none, orphan reap, no key reaching a child. Prints spawn-to-ready, time to first look and peak RSS. |
| `Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/merge_check.py` | ~3s | The merge spike's 17 checks on `merge.py`, plus a root keeping the deliverable's name, quarantine and accept, scene pointers and their conflict, the shared-data rule, an exception mid-merge leaving the scene as it was, one undo step, no temporary files; then perfume_ad's lighting and camera workers and shared_support's lamp: nothing pointing at a result.blend afterwards (weak references included), render_job's missing-file refusal naming the datablock, "held back" notes, owned objects left as they were staying put, accept by name; created objects keeping the collections a worker put them in (the asset pack's ShopAssetPack), and an owner's recolour with no aspect. |
| `Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/glb_check.py` | ~1s | `export_glb` with Blender's own exporter: names, triangles after a modifier and sizes read back from the file against the scene (a rotated, scaled crate), a hidden child, the camera and the light left out, a Noise Texture warned about, the selection put back, no temporary file; overwrite needed, a failed check leaving the old file byte for byte, a good one replacing it. |
| `Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/motion_check.py [-- --bvh <SOMA77 .bvh> --render <dir> --ybot <glb>]` | ~1 min | `apply_motion`'s retargeting on rigs built on the spot (SOMA's own skeleton with its generated body, Mixamo with rolled bones and a previous action to stash, Mixamo lying along -Y as glTF imports it, a turned UE5 mannequin in an A-pose with an Euler bone, Auto-Rig Pro controls, a Character Creator-like rig matched by names, a generated Rigify rig solved through its constraints; SOMA's skeleton with no bone longer than a leg bone): arm, leg and torso directions against SOMA's skeleton frame by frame (Rigify's DEF bones), the pelvis path scaled by leg length, the feet on the ground, FK switches keyed; then a walk in place, in the world and resampled to 24 fps; controls that drive no deforming bone refused and undone, and a travel bone that does not carry the thighs kept from running away; a motion without fingers gives every rig a relaxed hand (each deforming joint toward the palm, none backwards, both hands, every frame), keeping fingers the current action keys. `--render` writes 8 frames per rig from the front of the body; `--ybot` adds a local Mixamo GLB (never committed). |
| `Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/acceptance_check.py` | ~1s | Every check kind, pass and fail, on fixtures built on the spot; `no_overlap` and `clear_of` agreeing on the reading corner (`corner.py`). |
| `Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/m4_e2e_check.py` | ~6s | The whole of M4 headless, against scripted models (`fake_model.py`), real children and a real merge: two workers build boxes on a rug, one tries to scale the rug; spawn order, rollback, merge, wave checkpoint, one undo step, a compact report, the finish gate's bounce and Verified block, JobLog and usage by role, no leftovers; then Stop mid-wave leaves no children, no merge and no files. |
| `Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/workspace_check.py [-- <dir> [bridge]]` | ~25s | The single agent's workspace (`LOOPCUT_WORKSPACE`) headless against a scripted model: shell (python3 is Blender's, numpy), write_file, a numpy paint script, run_blender on source.blend with a Workbench preview, view_image, apply_build with owns, edit_file (and its failure), a rebuild and a second apply that replaces the first (no .001), snapshot_scene, a crate made anew applied under its own name and a studio scene saved active that leaves the camera alone; the barrel, wood, lamp and camera unchanged, source.blend untouched. inspect_api activates bundled LoopTools (an unknown path in stock Blender), and craft notes require run_python. With `bridge`, the same turn through cc_bridge and `codex_answer.py --thread` with a stand-in codex: one Codex thread from the first step on. |

Claude Code as the model: `python3 loopcut/harness/cc_bridge.py serve` serves `/chat/completions` on
127.0.0.1:8765, and `loopcut/scripts/cc_session.sh` starts the dev Blender pointed at it. Every
request waits on disk under `../out/cc_bridge/<time>/` until a Claude Code session answers it. Each
request goes to a fresh subagent that follows `loopcut/harness/cc_bridge_model.md` and sees only the
request. Everything but the model runs for real. `cc_bridge.py report` writes `transcript.md` (every
request, reply, result and image, plus the model's private notes) and `summary.json`. Token counts
there are the add-on's own estimate.

Orchestrated arms: `run.py --orchestrate on|off` (off is the single loop, the baseline),
`--max-workers N`, the orchestrator's and workers' own endpoints (`--orch-base-url/--orch-model/--orch-key-env`,
`--worker-…` likewise), and `--cc-orchestrator`, which starts one cc_bridge per task (ports 8765 and up)
as the orchestrator with GLM workers; a subagent per task follows `loopcut/harness/cc_orchestrator_model.md`.
`--min-free-mb 1500` holds a launch back while memory is short; run orchestrated arms at `--jobs 2`.
Results add levels (against `evals/levels.json`), waves, workers, change requests, quarantines,
rollbacks, and tokens and $ per role (`pricing.py`).

Single-agent quality: `run.py --chatgpt-plan --single-model gpt-6.1-sol --single-effort xhigh` is the product
path, Loopcut's own loop on the ChatGPT plan, which is what a user signed in with ChatGPT gets (the workspace agent
in keep mode at 200k, no workers, router or craft notes; sign in once with `harness/chatgpt_signin.py sign-in`).
`run.py --codex-app` is the reference arm beside it: the same model in Codex's own agent loop (`codex app-server`,
the add-on's `codex_app.py`) with Loopcut's live tools as dynamic tools; each turn's JSON-RPC transcript is in
`<task>.workspace/.loopcut/codex/`. On the Arcane chest both matched Sol alone in Codex (evals 20261001-130553 and
-125708); `--cc-single` answered by `codex_answer.py` did not (see below).

GPT through Codex CLI: `evals/codex_answer.py` answers a task's bridges (`--cc-orchestrator --orch-model
gpt-6.1-sol --worker-bridge --worker-model gpt-6.1-sol`: `--orch-model` names who answers the orchestrator's
bridge in the record and prices it; its docstring has the steps). summary.md then marks both bridges' token
estimates "ref" and adds the driver's own counts as a second table. Each request folder keeps `codex_reasoning.md`,
the model's reasoning summaries (best-effort: about 3 calls in 5 get one). Afterwards
`evals/exit_interview.py <run>` asks the model for a retrospective and a ranked wish list
(`<run>/exit_interview/retrospective.md`).

`python3 loopcut/harness/fake_model.py --port 8766 --script s.json` serves scripted replies per model
(and per worker, routed by the name its brief carries) for a windowed run with no model at all; the
module docstring has the script format. Point `run.py --orchestrate on` at it with `--orch-base-url
http://127.0.0.1:8766/v1 --orch-model <model> --orch-key-env <any set variable>` (and `--worker-…`),
set `LOOPCUT_BASE_URL`/`LOOPCUT_API_KEY` to it too so nothing reaches a real model, and
`LOOPCUT_EVAL_PANEL_SHOT=<file.png>` saves the panel (wave and merge cards) as the run ends. Move such
a run out of `../out/evals/<time>/` afterwards, or the next real run compares itself with it.

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

`checkpoints.bundle_from_checkpoint(session, id, staging_dir)` collects an indexed snapshot
in a factory-startup child Blender and returns `{manifest, missing, unsupported}`. The staging
folder must be empty. Recovery snapshots keep `//` paths relative to their recorded source
file; stock copy-saves have already rebased those paths to the checkpoint folder. Collection
opens recovery files with `recover_auto_save`, copies external inputs, and saves `scene.blend`
with remapping disabled, leaving both the live scene and the snapshot unchanged. Packed inputs
stay packed. Image paths use `filepath_raw` so remapping does not reload and lose recovered
painted pixels; modified still/tiled images are saved only into staging. Nested libraries,
libraries with external dependencies, implicit disk point caches
and unknown input paths are reported as unsupported; missing files are reported without
aborting collection. Check those diagnostics before calling a bundle complete.

`bundles.manifest` hashes staged files in path order; `canonical_json` gives its UTF-8 contract
bytes and `bundles.upload` returns their SHA-256 after uploading only absent blobs. Creation
time comes from the checkpoint (or the entry file's mtime), so repeated serialization is stable.
`cloud_http.Client` carries service authentication and contract/version headers; its injectable
urlopen-style transport also lets the blob tests exercise presigned PUTs without sockets.

Every storage PUT goes through one retry loop (`blobs._send`: 5 tries with backoff, URL refresh
near expiry, cancel between tries and per body block, `cloud_http.describe` causes). When the
manifest offers `capabilities.blob_multipart` (contract 0.6) and a file is at least its `min_size`
(16 MiB), `blobs.put_multipart` sends it in the server's parts (5 MiB), each part through that loop,
with progress "Uploading scene.blend: part 4/16…", then completes; a failure or cancel aborts it.
Smaller files, servers without the capability, and a server answering the start with 404 get one PUT.
R2 connections from some networks break every 20-30 MB, which a single 80 MB PUT rarely survives.

Bundle check: `<blender> -b --factory-startup --python loopcut/harness/bundle_check.py -- <tmpdir>`.
It creates private test data, checks the saved relative texture path and every manifest hash,
and uploads twice to an in-process fake service. It prints PASS/FAIL and exits nonzero on failure.

The render service runs no Python, so it refuses a driver that needs it. Its rule, kept identical in
`bundles.driver_acceptable` and loopcut-web's `workers/render/render_job.py`: a driver is fine when its
F-curve is muted, it is not scripted, or its expression is simple (Blender evaluates those without
Python; a free BlenderKit rig's 100 "var"/"1-var" IK/FK switches all are). Linked data follows the same
rule. The packer's child lists every other driver as unsupported, by data-block and data path, so the
call is refused before any upload; it never edits drivers in the bundle. A driver's mute and type are
animatable, so the service also refuses any keyframes (in any action) or driver whose data path goes
through `animation_data` (`animation_data.drivers[0].mute`): that switched a muted Python driver on
mid-render. `bundles.animates_drivers` is the same rule; those are listed as unsupported too. Driver check:
`<blender> -b --factory-startup --python loopcut/harness/driver_bundle_check.py -- <tmpdir>
[<loopcut-web>/workers/render]`; with the worker folder (one with both driver rules) it also
runs the worker's scene checks.

## Conversations

Conversations survive Blender restarts. Opening a .blend resumes the latest conversation that
worked in it; "History" in the header lists the others for that file, and "New chat" starts a
fresh one without losing the old.

- Stored in Loopcut's data folder (`conversations/<id>/`), never inside the .blend, so sharing a
  file does not share the chats that built it, and the user's file is never written.
- A conversation belongs to every file it has worked in: Save As, the first save of an untitled
  scene, and checkpoint restores all add to that list. An unsaved scene always starts fresh.
- Images (captures, renders, attached pictures) are stored once beside the conversation and
  referenced from messages; they are expanded to data URIs only when a request is sent. Every
  request re-sends them, so `image_budget.py` keeps them small: stored as JPEG (quality 85, at most
  1568 px) unless they have transparency or PNG is smaller, and each request's images under 2 MB of
  base64 however many there are: the newest three tool images and the user's own as stored, older
  tool images as 512 px thumbnails, the oldest left out eight at a time with a note of what each
  showed; half the budget on a retry after the connection dropped as the request went up.
- A turn cut short (Stop, or Blender closing) gets its unanswered tool calls answered, so the
  history stays valid for the API. Files on disk are validated on load; a damaged one is skipped
  and left in place.
- Checkpoints keep working across restarts. A snapshot store untouched for 30 days is dropped
  to reclaim space; its conversation stays and shows those checkpoints as expired.

Check: `loopcut/harness/persistence_check.py` (two launches sharing a data folder; see its docstring).
Harness scripts default `LOOPCUT_DATA_DIR` to a temp folder so they never touch real data.

### Conversation logs and problem reports

`session_logs.py` (contract SPEC §3.12, proposal 015). After each turn, on every model route and signed in
or not, the conversation goes to `POST /v1/logs` as text: messages, replies, tool calls and results, the
chat's error, notice, limit and wave cards as events, the turn's worker conversations (referenced from the
parent by a `worker session` event), and the console lines since the conversation started in this Blender.
Each upload replaces the last for that conversation; the server keeps it 90 days and never trains on it.

- Never sent: images (an `image omitted` event with kind and size instead), .blend files, credentials. Every
  key in `credentials.json`, ChatGPT's tokens, key-like environment and `.env` values and key-shaped strings
  become `[redacted]`, URL queries are cut and the home folder becomes `~`, before gzip. Long fields keep
  their head and tail to fit 6 MiB decoded / 1 MiB gzipped; at most 2,000 entries, the newest kept.
- Off with the preference "Share conversations to improve Loopcut" (on by default; also on the first-run
  privacy screen), with `LOOPCUT_TELEMETRY=off` (the harness sets it), in background Blender, or without an
  install id. 404 and 426 stop uploads until the next launch, 429 pauses ten minutes, 409 drops that chat.
- The add-on has no log file: `register()` tees `sys.stdout`/`sys.stderr` into a 4,000-line buffer, and
  `unregister()` puts the streams back.
- "Report a Problem…" (`loopcut.report_problem`, `support.py`) lives in Preferences › Add-ons › Loopcut,
  never in the chat panel (`test_session_logs.PlacementTest`). It sends a note, an optional contact email,
  the recent console log and the three latest conversations to `POST /v1/reports`, even with sharing off,
  and puts the returned `LC-…` code on the clipboard.

Tests: `loopcut/tests/test_session_logs.py`; with `LOOPCUT_CONTRACT_DIR` set, the built bodies are
validated against `conversation-log.schema.json` and `problem-report.schema.json`.

## Context

Every request carries the conversation, and the provider caches the part of a request that matches
the previous one: appending is cheap, editing anything already sent re-bills everything after the
edit. `context.py` keeps each request within `LOOPCUT_CONTEXT_BUDGET` tokens (preference "Context
budget", default 40000, the system prompt and tool schemas included) by keeping the history
append-only while the model works and shrinking it in batches:

1. Step records. A step (an assistant message with tool calls, its results, the captures after
   it) can be sent as its record: what the model said at that step, word for word, then per call
   whether it worked (or its exception), what the code printed (400 characters) and the full
   "Scene changes" (15 lines). Code bodies and long outputs are left out. The model's own sentences
   are the part that says why: a run on 2026-09-23 lost them and the model undid its own fixes twice.
   The system prompt asks the model to open every tool-calling reply with what it sees and why.
   A folded step is only marked (`loopcut_folded`); its stored messages are never edited.
2. When the user sends a message, every step of the earlier turns is folded: one cache break per turn.
3. Mid-turn, only past 75% of the budget, the turn's oldest steps are folded at once, down to 50%,
   leaving the newest three whole.
4. Compaction, the last resort, when records alone exceed the budget: the model summarizes everything
   before a cut into one `<conversation_summary>` message, and the chat shows a "Summarized N
   earlier messages" notice. The summary is stored on the first message it does not cover, so
   checkpoint restores that cut the conversation cut or keep it correctly.
5. Images: the captures the model has not acted on yet, the newest capture of the conversation,
   and pinned references (768 px, until unpinned) are sent; a dropped capture is remembered in the
   record by what it showed. The automatic look is taken once per reply, after all of its calls,
   and only if the model did not look after its last change. Looks at an unchanged scene are
   refused after three in a turn and nudged after eight. Captures are 640 px (about 800 tokens).

Sizes are estimated from characters (3.4 per token, 800 per image) plus the measured size of the
system prompt and tool schemas (about 7k tokens, `context.fixed_tokens`), and the chars-per-token
ratio is corrected from the token count the API reports; the footer shows that count next to the
conversation's total. `harness/cc_bridge.py` records every request exactly as sent, which is the
way to check what the model actually saw.

## Turn progress and optional step budgets

User turns have no step limit by default: unset `LOOPCUT_MAX_STEPS` or `0` means unlimited for the
single, orchestrator and workspace loops, on the ChatGPT plan, BYOK and hosted models. Stop still
cancels a turn; hosted usage remains subject to credits and gateway limits. An optional positive
`LOOPCUT_MAX_STEPS` (1–10000) bounds requests for evals and power users. Preferences no longer export
this variable. The Codex app runs its own loop and does not read this override.

Workers have no step limits (`max_steps=0`): `job_max_usd=0.40` is shared across the turn's workers
and `worker_max_seconds=1800` bounds each run. Old brief `steps` values are silently dropped.
A `repeats.Ledger` per turn
remembers what was tried, and its notices go into the tool results so the history stays append-only:

- A failure that repeats (same tool, same error once numbers, addresses and quoted names are
  blanked) gets one line listing the earlier attempts the second time, and "this approach is not
  working" from the third. Four failing `run_python` calls in a row, whatever the errors, get a
  nudge. Code is never refused and the error is never cut.
- The same call twice in one reply runs once; the rest get "duplicate of call #k". A read
  (`tools.SAME_WHILE_UNCHANGED`: the scene and API reads, `check_contact` even with `watch`,
  `look_at_reference`, and `search_assets` for every source, the source being part of the call; also
  `frame_camera` with `report_only`) that returns what the identical call before it did, with no
  scene-changing step (`run_python`, `import_asset`, `frame_camera`) or user note in between, is
  nudged; a third is not run while that result is still in view. This also applies to failed reads.
  Viewport looks keep their own cap (above). Calls not run show as rejected, not failed.
  `render_job` with `action: status` is exempt
  from all of this (`tools.changes_over_time`): a job moves on by itself, so the same call can say
  something new. Its other actions are not reads.
- Polling a render job is nudged, never refused. An eval turn spent 38 of its 60 steps on 19 status
  calls, each followed by `run_python` sleeping or listing the output folder. A status without `wait`
  that finds a job queued or running gets a line pointing to `wait=` from the third in a turn
  (`repeats.POLLS`). A `run_python` whose code calls `sleep(` right after a `render_job` call gets a
  line saying the sleep holds Blender's main thread. The results of `start`, `resume` and a running
  status carry the same advice themselves (RENDER_JOBS.md).
- Any loop, including a worker, stops with `outcome="no_progress"` when the same tool and normalized arguments
  (`repeats.call_key`) accumulate eight failures or automatic refusals since the last scene change,
  even with other calls in between. Duplicate calls, cached reads, refused captures and checkpoint
  failures count without running the tool. Eight allows several retries after the existing repeat
  notes. A success resets only that key; successful reads of other keys leave counts unchanged.
  A scene change, user note or approval rejection resets every key. Equal before/after snapshots let
  failing mutations count; without snapshots, potentially scene-changing tools conservatively reset
  all counts. Workers use the child's measured `changed` flag instead of full snapshots. The chat
  shows a short notice, followed by a text-only wrap-up of what is done, blocked and unverified.
- When a positive step limit is set, three steps before it the model is told. At the limit,
  `outcome="step_limit"` is retained and one more request, with the same
  tools so the cached prefix holds, appends a `[Loopcut]` message saying tools are off and asking
  for what is done, what is left and what was not verified; the answer follows the "Stopped after
  N steps" marker. Not `tool_choice: "none"`: GLM and MiMo ignore it and Muse Spark refuses it
  (measured 2026-09-27). A tool call in the reply is dropped; with no text, or on any failure, the
  message is taken back and the bare stop stands.

## Workers

A request with several deliverables is split. The agent in the user's Blender is then an
orchestrator: it does small things itself (level 1), and briefs workers for the rest with
`delegate`, one wave per call (level 2 for one deliverable, 3 for several; levels are inferred from
what it does and kept in `session["turn_levels"]`). Design 07 in the private research findings has the
whole design; the frozen interfaces are there.

- `delegate` is refused unless every requirement and check quotes the user, kept objects are owned by
  no one, the palette and supports exist, prefixes are unique and the planned boxes agree (a layout
  dry-run), and a brief may not collide with a worker still running (its id, prefix, deliverable or
  owned names, or a `continues` of it). It takes the wave checkpoint and saves a spawn copy, then
  returns as soon as one of the turn's workers has finished (and any others within 20 s,
  `waves.BATCH_SECONDS`): those merge, and the compact report (at most 360 tokens per worker, status
  from checks, never the worker's word) lists who is still running. The orchestrator reviews each
  result then and can send a fix back with `continues` while the others build; `delegate` with
  `wait: true` and no wave returns the next results. A final reply while workers run waits for
  them, merges them and bounces once with their report, so nothing merges unseen.
- A call takes any number of briefs: at most `LOOPCUT_MAX_WORKERS` run at once across the turn's
  waves and the rest queue in brief order, so the orchestrator never schedules by hand. The start
  (and the dry run) says who starts now and who queues ("Schedule:"); each report lists who is done,
  still running, and queued with why it waits. Dependencies between briefs (a lighting pass after the
  props) are not modelled: a dependent brief needs a scene copy taken after its dependencies merge,
  which is a later delegate call.
- Aspect materials is global and exclusive (every material and shader group). Recolouring a worker's
  own objects (slots, face material indices, its own `<prefix>_` copies) needs only owns, so when
  several briefs claim the aspect, or one beside a running worker that holds it, repair_wave drops it
  from each brief that owns objects and says why; the dry-run conflict explains the difference.
- A brief that names a tool workers are not offered (their tools are the same for every role) is
  repaired to the offered one that does its job (`WORKER_INSTEAD`: inspect_scene becomes
  capture_viewport angle final); one with no stand-in (render_job, write_file...) is a warning.
- A worker's loop runs in this Blender, so keys stay here; its tools run in a child Blender (`-b
  --factory-startup --disable-autoexec`, at most `LOOPCUT_MAX_WORKERS`) over JSON-lines pipes, with
  an environment that holds no key. A call that changes something the brief does not own is undone
  in the child and the worker is told. Workers 2..N send their first request only after worker 1's
  first reply, so the wave pays for the shared prompt prefix once.
- Each merge is one main-thread call and one undo step; the wave checkpoint is taken when its workers
  start (restoring it undoes every merge of the wave and cuts the conversation back to the
  `delegate` call). A wave may merge in several calls, as its workers finish (`merge_wave(keep_spawn)`,
  `Spawn.moved`); what changed live in between, the orchestrator's own edits included, is a conflict.
  Datablocks outside a role's kinds wait hidden in `LC_Quarantine`; a conflict is reported, not
  forced. A live check naming a worker still running waits for its merge. Stop kills every child
  and merges nothing more. An `unchanged` check (a brief's, or `shared.keep`: names a later wave
  keeps first are their own `keep <n>`) compares with the spawn of the wave it came from, as the
  worker's copy did, and finds datablocks through the job's baseline, which follows what each merge
  replaced (`Job.follow`).
- A turn that delegated ends with the checks run on the live scene: failures send it back (twice at
  most), then the reply gets a "Verified by Loopcut on this scene" block, with how many of the user's
  requirements (the briefs' and the checks' quotes) the checks cover, naming those only partly covered
  (a placement word such as "hanging" nothing measures) or unverified; information, never a bounce.
  `checks` with no arguments
  runs them all now; its `snapshot` records named objects (origin, transform, size, bounds, topology
  and vertex hashes, materials) as `s1`, `s2`, ...; a `preserved` check compares with one, less what it
  excludes (a look pass: materials), in the worker's copy, after every merge and at the finish.
- Budgets: $0.40 per turn across all workers (`pricing.usd`); 1800 s per worker run, starting when
  its thread starts (queued time is free). Worker steps are counted in the panel, reports and JobLog,
  with no cap. Before each regular worker request, `waves.budget_steps_left` estimates remaining
  steps from seconds left / mean seconds per step, and, for priced models, dollars left /
  (mean job dollars per worker step × workers currently running). After at least one step, the first
  estimate at or below three appends one continuation note: "About 3 steps left in the budget.
  Stop improving now: call check_brief, then report_back." This wording also works with the older
  server-held worker prompt. Unpriced models use time only; a zero dollar budget disables that limit.
  Exhaustion raises `BudgetStop`, keeps `outcome="budget"`, and requests a text-only wrap-up. As before,
  requests already in flight and the wrap-up may take spending past the dollar budget; no new regular
  request starts once it is spent. The hard timeout grace still kills a worker stuck in a tool.
  Budget and no-progress stops without `report_back` get a measured partial report naming the stop
  reason. Reports and JobLog retain `cap_hit` for existing readers, now meaning either kind of stop
  without `report_back`, and include `unfinished`; JobLog also records `outcome`.
- Worker jobs live in `<data>/workers/<conversation>/<turn>-<n>/` (`job.json`, then `w<n>/<worker>/`
  with the transcript, captures and `worker.log`). A wave's `.blend` copies are deleted once its last
  worker has reported, merged or not, or on Stop; at start-up `waves.reap()` kills children an earlier Blender left, removes job
  folders older than 7 days or beyond the newest 20, and the copies of jobs whose Blender is gone.

| Module | What |
|---|---|
| `agent.py` | `run_loop(…, LoopSpec)`: one loop for the single agent, the orchestrator and the workers; usage by role. |
| `orchestrator.py` | `delegate`, `checks`, `accept_quarantine`; wave validation, the layout dry-run, the finish gate. |
| `waves.py`, `workers.py` | `run_wave` / `collect` / `drain` (spawn rule, the job's slots across waves, budgets, retries, cancel, reports as workers finish, JobLog); the worker's prompt and `check_brief` / `report_back`. |
| `worker_ipc.py`, `worker_host.py`, `worker_child.py`, `worker_capture.py` | The child: framing and environment, the process and tool routing (a crash leaves `<worker>/crash-<n>/`: Blender's crash report, the last .blend, the log's tail), its serve loop and rollback, headless looks (own meshes validated first; "Worker looks" below). |
| `ownership.py`, `merge.py`, `acceptance.py` | Stamps and tokens, the merge, the check DSL (the user's checks and a brief's advisory ones). |
| `glb.py` | The orchestrator's `export_glb`: a temporary .glb read back and checked against the scene (names, triangles, sizes, embedded images, nodes glTF drops) before one rename replaces the destination. |
| `facts.py` | What each deliverable measures at report time, and where a worker's report contradicts it. |

Settings: "Use workers for big jobs" in the preferences is `LOOPCUT_ORCHESTRATE` (on; off is today's
single loop). `LOOPCUT_ORCH_*` and `LOOPCUT_WORKER_*` (`BASE_URL`, `MODEL`, `API_KEY`,
`REASONING_EFFORT`) default to the `LOOPCUT_*` values, except that on the Loopcut service workers use
`fast`; then `LOOPCUT_MAX_WORKERS` (3),
`LOOPCUT_JOB_MAX_USD` (0.40, 0 = off), `LOOPCUT_WORKER_MAX_SECONDS` (1800 per worker run; the old
name `LOOPCUT_WAVE_MAX_SECONDS` still sets it) (`config.Roles`). `LOOPCUT_WORKER_MAX_STEPS` and
`LOOPCUT_JOB_MAX_STEPS` are obsolete and ignored, including invalid values.

In the panel a wave shows as a card with a row per worker (deliverable, status, steps taken,
checks, and Cancel while it runs, which stops that worker only), and its merge as a card listing
what came in, was replaced, held back or conflicted, with "Accept held back" and "Restore before
merge".

### Worker looks

A worker's `capture_viewport`, the look after a `run_python` with `capture`, check_brief's craft look
and its contact sheet cell are small real renders in its child (`worker_capture.py`), because a
background Blender has no viewport. What a look shows (the angle) and how it is lit (the shading)
are independent: `angle` or `angles` (one labelled sheet, at most 6 panels with the shadings),
`shading` one or two of:

| Shading | Render | Default for |
|---|---|---|
| `clay` | Workbench, one grey, cavity and outlines | modeling |
| `flat` | Workbench, each material's Base Color value, unlit | |
| `studio` | EEVEE, a white world at 0.55 plus a sun turned with each view to come from its upper left, the scene's lights and world off; a face toward the view shows about 0.9 of its albedo | materials; an unlit scene's cell |
| `albedo` | EEVEE, each shown material swapped for a temporary copy emitting its Base Color (value or texture): paint exactly, no light | |
| `scene` | the scene's own engine, world, lights and colour management, samples capped at 16 | lighting; angle `final` |

The camera and animation roles keep `solid` (Workbench, viewport colours, never textures). Every
shading but `scene` is rendered in Standard view on a transparent film over 18 % grey, and without
the compositor; `scene` (angle `crops` included) runs the scene's own when it has one that does
more than pass the render through (a Glare's bloom), and its caption says "its compositor". `studio`
replaced `studio.exr` fixed in world space, whose front half lit a painted cream label at 0.07-0.15
of its value and bluish: the Arcane run's workers saw slate and repainted for an hour (image data
was never stale; a repaint shows in the next render without `update()` or `pack()`).

- Every caption says how the image was made, read from the settings it rendered with: `Render:
  EEVEE 16 samples, studio (...), Standard view, 1024x768 px`. A cell carries a short stamp of it.
- Warnings, never gates: a blank panel, two panels showing the same picture, and other objects
  hiding the subject (`_hidden_by`: rays to up to 256 of its vertices; names them). `isolate` hides
  everything but the focus from the render.
- Angle `crops` is the camera-scale review: one render through the camera at the output size
  (at most 1920 px), the frame on top, each object cropped at one common scale below, and per object
  its size on screen, px per cm, cut edges, how much is hidden, and brightness, spread, saturation,
  clipping and near black of its own pixels (told apart by a flat Workbench render, a colour per object).
- Public, for other tools: `configured()`, `look(angle, focus, frames, step, angles, shading, isolate)`,
  `cell(tag, folder)`, `unlit(scene)`, `shadings_of`, `angles_of`, `panel_warnings`, `review_row`.
  Everything a look creates is tagged `lc_temp` and removed, and every setting it changes, a material
  slot or object colour included, is put back (`_Restore`).

## UI architecture

`ui/layout.py` is pure Python: session state in, display list out. `ui/draw.py` renders that list
(one SDF shader for rounded rects, `blf` for text). `ui/host.py` binds it to a Blender area and
handles input (`ui/textedit.py` is the input box's editing rules, pure and tested). `host.py`
picks the build's own editor when it exists and a borrowed Text Editor area otherwise.

`cloud_account.py` owns credit labels, per-call cost consent and account/project policy. Signed-in
No generation and Private mode preferences PATCH the account settings; signed-out preferences
stay local. The panel's settings menu stores `loopcut_no_generation` (`inherit`, `on`, `off`) on
the active scene. Account No generation remains binding even with project Off, per contract 0.2.
The same effective policy filters tool/skill discovery and travels in `project.no_generation`.

Managed estimates above the descriptor threshold, every quote, and BYOK calls ask on their own
cost card, including when ordinary edits are auto-approved. Quotes precede consent; their IDs
stay out of model input. One expired quote is replaced within the approved price. A higher
replacement price returns `quote_changed` and needs a fresh approved call. Account errors show
the refusing scope/data class; credit errors include an upgrade button and a short tool result.

Check persistence and real dispatch without external services:
`<blender> -b --factory-startup --python loopcut/harness/cloud_account_check.py`.
The script loads this checkout like `cloud_jobs_check.py`, uses temporary files and the canonical
mock's in-memory HTTP handler, and prints `PASS cloud account check` on success. Set
`LOOPCUT_CONTRACT_DIR` to the contract directory if it is not at the usual sibling path.
