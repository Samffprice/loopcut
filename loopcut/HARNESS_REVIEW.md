# Loopcut system and harness review — 2026-09-21

The biggest opportunity is to make Loopcut reliably finish a user's job across modeling, materials,
animation, rendering, and export. Its execution tools already do useful work. The weakest parts are
trustworthy visual feedback, current Blender API knowledge, recovery when the user changes workspace,
and knowing whether a deliverable actually exists.

This review combines the saved 25-turn anodized-material → ferrofluid → video conversation, the two
recent Codex product-shot tasks, saved evaluations from September 18–21, and execution of the current
checkout. These are a small development sample, not population-level product metrics.

## What the evidence says

| Observation | Implication |
|---|---|
| September 19 basic suite: 22/22 passed, 66 model steps, 509 seconds combined task time. | Single-operation tests are useful regressions but no longer discriminate whole-job quality. |
| September 21 showcase perfume runs: one timed out at 600 seconds; another took 872 seconds, 33 model steps, and failed the close-up-camera requirement after hitting the first turn's step limit. | Bigger budgets alone do not solve the workflow. Diagnose repeated effort and bad observations. |
| The live material/animation conversation hit the 25-step limit, needed “continue,” and repeatedly failed with “No 3D viewport is open” after workspace changes. | UI layout must not determine whether ordinary scene edits work. Recovery needs first-class support. |
| Most later turns in that conversation used no tools, despite repeated corrections about the Sequencer and a final “I just got images again.” | The assistant slipped from acting on live state into guessing menus. Inspect output/editor state and do the requested work. |
| The video request was redirected to PNG because `file_format` exposed only image formats. | Blender 5.2 selects IMAGE/VIDEO via `media_type`; contextual enum values are not build capabilities. |
| A bright red emission cube appeared in 18.7% of the EEVEE preview, but 0% of the Cycles viewport capture. | The black preview was a tool failure, not evidence that the artist should increase the lights. |
| Inspecting the real bottle cap crashed at `modifier.keys()`. | Blender 5.2 geometry-node inputs live under `modifier.properties.inputs`. |
| Basic and showcase tasks reused `perfume_ad` with different briefs/checks. | Historical pass/fail changes are not directly comparable merely because the task ID matches. |
| The Codex turntable render was interrupted by an explicit user request to undo the work. | Count it as cancelled; do not count it as a model failure. The later perfume task did finish and verify its 120 PNGs. |

## Changes implemented in this checkout

1. **Trustworthy preview semantics.** Rendered viewport captures now temporarily use synchronous EEVEE
   with scene lights/world, even for a Cycles scene, and explicitly label the result as an approximation.
   Material previews explicitly use studio lighting. Engine and viewport settings are restored on
   success and failure. This eliminates the reproduced black Cycles offscreen buffer; it does not make
   EEVEE equivalent to a final Cycles render.
2. **Current API inspection.** Geometry-node modifier inputs work with both the new socket properties
   and the older ID-property API. Blender IDs are serialized as references. RNA inspection traverses
   registered add-on properties such as `Scene.cycles` without requiring a `bpy.types` class attribute.
   Engine discovery uses the live enum membership, rather than RNA's incomplete static list.
3. **Work from the user's current editor.** Code execution finds the requested editor first, then
   temporarily borrows an ordinary editor when needed. It restores the editor/UI type afterward.
   Captures can also borrow a 3D editor. The agent no longer needs the user to switch back from the
   Sequencer just to change a light or material.
4. **Ground output advice in live state.** Scene context includes engines, output settings, video
   encoding capability, and open editor types. API notes explain IMAGE → VIDEO → FFMPEG/MPEG4/H264.
   Still preview rendering preserves an existing video-output configuration.
5. **Safer cancellation.** Stop checks occur between tools, after checkpointing, and at main-thread
   dispatch. Queued work is cancelled before it can mutate the scene. Already-running native Blender
   work is allowed to return before the turn becomes idle. Outcomes distinguish completion,
   cancellation, step exhaustion, and errors.
6. **Independent evidence for each process.** Viewport, reference, comparison, file-preview and render
   images have private temporary directories. Eval data is isolated and cleaned by the parent process.
   Parallel runs cannot overwrite each other's image filenames. GPU runs default to one at a time.
7. **More honest evaluations.** Each run records agent/harness hashes, task and fixture fingerprints,
   Blender build, model settings, time budget and heavy-operation policy. Changed/unknown task
   contracts are excluded from win/loss claims. Timeouts wait for cancellation before grading; a
   failed or unfinished first turn does not trigger the follow-up; prompt counts reflect actual sends.
   Heavy-operation approval in disposable evals is explicit with `--approve-heavy`.
   Manual-arm exports also carry task manifests, and comparisons refuse mismatched revisions;
   historical regrades without manifests require an explicit flag and are labelled unverified.
8. **Usable blind reports.** Fixed a variable-shadowing bug that destroyed the identity key and crashed
   multi-task reports. Blind image paths now use anonymous column names rather than revealing which
   arm produced the image; follow-up briefs are included.
9. **Action and completion guidance.** The agent is told to inspect rather than repeat disproven menu
   instructions, and to distinguish configured settings, previews, verified output files, and completed
   renders. This is guidance, not yet an enforced completion protocol.
10. **Tests that run together.** Updater UI classes are constructed at registration, so importing pure
    updater helpers no longer breaks full test discovery after the fake Blender module is loaded.

No provider, model, dependency, or installed application was changed. These are source changes,
not a rebuilt or published release.

## Next improvements, in order

### 1. Make rendering and export durable jobs

Add a job record containing input scene revision, engine, frames, dimensions, format, output directory,
time/sample budget, status, progress, and produced-file manifest. Render from a saved copy in a child
Blender process, keeping the editing UI responsive. Show queued/running/cancelled/failed/complete in
the product, with preview, cancel, resume and open-output actions. Resume verifies existing frames
before skipping them. PNG sequence → MP4 should be one supported workflow, with actual output
validation and an explicit policy for existing files.

Acceptance: a stopped 120-frame job resumes only missing/invalid frames; a successful job has all 120
decodable images at the requested dimensions, or a verified video with the requested duration/fps;
closing a chat does not lose the job; cancelled work is never labelled complete. Keep authorization
scoped to the requested job and budget rather than repeatedly asking for the same operation.

### 2. Give the agent a reliable visual inspection tool

Offer an inspection request with object focus, named camera(s), frame(s), and purpose: geometry,
materials, or final lighting. Return an annotated contact sheet plus structured facts about which
engine, camera, frame, scene revision and quality settings produced it. Use actual bounded renders
for lighting/material verification. The harness should grade every requested camera, not only the
active hero camera, and sample meaningful animation phases instead of redundant endpoints.

Acceptance: stale captures cannot be attached to a newer scene revision; unsupported/failed captures
are explicit errors; a lit test object remains visible; first/middle/last and named-camera checks
preserve scene state. Measure preview latency and token cost alongside quality.

### 3. Enforce preservation and completion with evidence

Create a compact job specification from the brief: requested artifacts, allowed edits, protected
objects/materials, output settings, and a small checklist. Before editing, record protected mesh
topology/coordinates, UVs, material graphs and relevant modifier inputs. Compare after editing using
semantics appropriate to the request: animating a parent may be allowed while modifying a mesh is
not. Store verification results and unresolved items with the turn.

The existing product-preservation checks mostly compare bounds and material names. A material can
keep its name while changing completely; equal bounds do not prove unchanged geometry. Add mutation
tests that change a node value, UV, vertex, parent transform, or protected camera and require the
appropriate check to fail. A reference solution passing proves much less than adversarial changes
being rejected. Avoid teaching the agent evaluator thresholds or reference solutions.

Acceptance: the material-name/bounds loopholes fail; requested but missing output prevents a
“complete” outcome; the UI tells the user what was verified and what remains.

### 4. Turn the real 25-turn workflow into a regression suite

Cover material creation → weathering → animation → user switches to another editor → lighting edit
→ render → export → interrupted job → resume. Add multi-turn tests for “keep this animation,” “undo
only your last change,” “continue,” and corrections received during execution. Include large scenes,
missing textures, low disk space, render failures, context compaction and reopened conversations.

Run deterministic tool/API tests on every change, and a small fixed set of real-model tasks on
candidate changes. Use at least three runs per task/model for comparisons, fixed budgets and fixture
revisions, and a held-out set of unfamiliar assets. Report completion rate, user interventions,
preservation violations, time-to-first-useful-result, total latency, token usage, repeated/no-op tools,
and independent visual ratings. Keep cancelled/infrastructure/approval-blocked runs separate.

### 5. Make long-turn recovery explicit

Persist the current job, completed steps, pending verification, artifact paths, and a small set of
validated API discoveries separately from conversational summaries. On continue, reconcile that
record with the current scene before acting. Provide a budget warning with a concrete unfinished
checklist before the hard step cap. Detect identical errors/no-progress cycles and change strategy
instead of only increasing the step budget or urging the agent to finish.
Treat truncated or prematurely closed model streams as incomplete: the current model transport's
finish reason also needs to be part of a future machine-enforced completion contract.

Acceptance: continuing after compaction or reopening does not recreate successful work; user edits
invalidate stale assumptions; an API error leads to a targeted lookup and a bounded retry.

### 6. Improve model selection only after the evidence is stable

Compare candidate models on the same fixed workflow suite and token/time budgets. Consider cheaper
models for straightforward inspection or explanation and stronger ones for complex scene planning
only when measured results justify routing. Avoid claiming that a model upgrade fixes incorrect
images, broken API tools, or incomplete export semantics.

## Verification and limitations

See [verification logs](../../out/harness-review/) and the per-run manifests. The new deterministic
checks exercise the real Blender build, not just mocked APIs. The historical black preview and
geometry-node crash were reproduced before correction. A fresh live run also exposed the existing
`Scene.cycles` traversal crash and an incomplete first implementation of engine discovery; that
failed run is retained at [20260921-130233](../../out/evals/20260921-130233/summary.md).

Validation results:

- **178 unit/integration tests passed** via `python3 -m unittest discover -s blender/loopcut/tests`.
- **PERCEPTION OK:** real fixture inspection, EEVEE/Cycles preview pixel assertions, workspace
  recovery, runtime capabilities and restoration after errors.
- **TOOLS OK:** the broader real-Blender tool suite, including a still preview while configured
  for H.264 video output, with the video settings preserved.
- **API DOCS OK:** Cycles property traversal, actual engine membership, IMAGE/VIDEO output settings,
  and installed add-on discovery.
- **SELFCHECK OK: 32/32** evaluator reference solutions, including follow-ups. This is checker
  validation, not 32 successful real-model jobs.
- Manual-arm export produced a task manifest that the comparison validator accepted; blind-report
  and mismatch-rejection regressions pass. Python compilation and scoped `git diff --check` pass.
- [Live rerun 20260921-131042](../../out/evals/20260921-131042/summary.md), same configured model:
  **448 seconds, 23 model steps, both turns completed, 12/13 scene requirements met. Overall FAIL.**
  There were two failed tool calls: an unavailable class-name lookup, and an invalid `ShaderNodeMix`
  data type. The latter left a partly built backdrop that the agent failed to repair before finishing
  stage 1; the amber-background check correctly remained failed. All five follow-up checks passed.
- Reopening both saved stages and grading/rendering them in Cycles independently reproduced
  **12/13**, confirming the result survives serialization. My visual assessment of the
  [final render](../../out/harness-review/live-grade/perfume_ad.f001.png) is mostly neutral gray with
  a weak pink cue, despite the follow-up's pink-setting check passing. That is a useful concrete
  example of the gap between node-value checks and the requested visual appearance, not a blind
  comparative quality score.

The historical 872-second run is context, not a controlled speed benchmark. The rerun was captured
under its recorded manifest before the final cancellation-edge guards and report-validator changes;
those later changes were tested separately. One rerun does not establish an uplift in visual quality,
a reliable completion rate, or a model ranking. The next agent-level priority is tracking and
resolving partial failed edits before declaring completion, rather than relaxing the checker.

Remaining architectural limits: native work can still block Blender's main thread; the changed
prompt is not a machine-enforced deliverable checklist; EEVEE previews cannot prove Cycles glass or
reflections; existing scene diffs do not capture every possible node/geometry edit; arbitrary Python
execution is a trusted-user capability, not a security sandbox; hard process kills can lose the last
unsaved conversation events. The roadmap above addresses those limits without pretending they are
already solved.
