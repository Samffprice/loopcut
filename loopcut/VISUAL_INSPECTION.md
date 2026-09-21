# Reliable visual inspection

`inspect_scene` makes a contact sheet from a saved copy of the scene. It accepts named cameras,
frames, focus objects, a purpose, a pixel limit, samples and a worker wall-clock budget. The camera/frame
cross-product is capped at 12 views; each sheet stays within 1536 pixels on both axes. The default
is 384 pixels per view, 16 samples and a 90-second worker budget; the maximum budget is 180 seconds.
Scene-copy preparation happens before that budget starts.

| Purpose | What the image means |
|---|---|
| `geometry` | Workbench render of shape, using studio shading. |
| `materials` | An actual EEVEE render under a neutral studio-light rig created in the copy. It does not prove the final scene's lighting. |
| `final_lighting` | An actual render with the scene's engine, lights, world, compositor and color management, bounded in resolution and samples. It does not claim production quality. |

Named cameras retain their composition. With no camera requested or available, a temporary camera
frames the focus objects (or renderable scene geometry). Focus labels sit beside projected bounds;
bounds do not prove visibility through occluders. Each tile labels its camera, frame, purpose,
engine, dimensions, samples and saved scene revision. The accompanying metadata contains those
settings, focus bounds, image hashes, camera matrices, timing and pixel-signal measurements.
Nearly black output is reported as evidence, not automatically treated as a material failure.

The tool starts a [durable job](RENDER_JOBS.md), then the agent waits outside Blender's main thread.
The editor remains responsive. The job supervisor enforces the whole inspection budget and handles
cancellation even inside native rendering. Actual image decoding, contact-sheet composition and
text rendering happen in the child process using Blender's bundled `imbuf`, `blf` and NumPy.
There are no new dependencies, network calls or model calls in the inspection pipeline.

Freshness is checked before an image reaches the agent: a dependency-graph/frame/file/undo generation
and explicit scene/output/color settings are compared with the captured token. An edit or restart
invalidates the result; the tool returns an explicit stale result with no image. A second check at
the end of a model's tool batch prevents a later edit in that batch from attaching an earlier
inspection as current evidence. Archived inspection sheets remain available in Jobs, labeled with
their saved revision. Native render failures and unsupported engines return errors, not fallback
images silently presented as equivalent renders.

## Evaluation coverage

`Task.render_cameras` specifies camera names, or `["*"]` for all cameras in the scene. The perfume
task now renders every camera, including inactive ones. Animation tasks sample quarter phases as
well as first/middle/last, so a looping animation cannot be judged solely from matching endpoints.
The grader preserves engine and color management, caps quality, records per-image camera/frame/
revision/hash/latency metadata, and restores the camera, frame and output settings on success and
failure. The judge receives camera/frame captions. Grading code participates in the task-contract
hash, so old visual coverage cannot silently stand in for the new coverage.

## Verification on 2026-09-21

- A known red emission object remained visible in six real Cycles renders across two cameras and
  three frames. Geometry and material modes also produced visible images. User camera, frame,
  selection, editors and output settings were preserved.
- Camera, shader, exposure and resolution edits invalidated evidence. A later mutation in a tool
  batch suppresses the older inspection image. Agent-thread waiting left the main-thread pump
  available; cancelling that wait stopped the child job.
- The grader rendered both cameras at all three requested frames and restored VIDEO/FFmpeg output
  settings. Missing cameras failed explicitly and also restored state.
- The previous `20260921-131042` perfume result was inspected through `CAM_Closeup`, `CAM_Hero`,
  `CAM_Overhead` and the original `Camera`: four 332×332 Cycles renders, 8 samples. The 332×1536 sheet
  took **18.75 seconds** wall time (14.795 seconds rendering/composition) and **531,596 bytes**.
  The local context estimator counted **3,058 tokens including its fixed overhead**. No provider
  was called, so actual billed image tokens are unknown. This is a tool measurement on an existing
  scene, not a new model task pass or an end-to-end quality improvement claim.
- The 32-task reference-solution self-check passed. These checks validate the evaluator, not live
  model success rates.

Run from the repository root:

```sh
python3 -m unittest discover -s loopcut/tests
Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/inspection_check.py
Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/inspection_perfume_check.py -- <saved-perfume.blend> <output>
```

Inspection shares durable jobs' boundaries: Cycles uses CPU, external assets must resolve and stay
unchanged, compositor File Output nodes are disabled, and copy-saving large scenes still takes time
on the main thread. There is no promise that low-sample glass/noise matches a final production render.
