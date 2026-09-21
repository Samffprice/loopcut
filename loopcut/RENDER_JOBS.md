# Durable render and export jobs

`start_render_job` saves a scene copy, then launches a detached supervisor and a render process.
The editing process only waits for the copy-save, not for rendering. `render_job_status`,
`cancel_render_job`, and `resume_render_job` operate on a durable ID, independent of the conversation.
The Jobs button in the panel header shows progress, errors, Preview, Output, Cancel and Resume.
Jobs continue after the panel, conversation, or launching Blender process closes.

Each attempt requires approval for the saved settings and an explicit wall-clock budget. Resuming
uses the original budget again. It never includes newer edits to the live scene. A job's compressed
`.blend`, SHA-256 revision, engine, camera, frame list, dimensions, samples, FPS, source path, external
asset hashes and output location live in `<data>/jobs/<id>/spec.json`. They do not expire with chat
checkpoints. Each job creates a new `loopcut-<id>` output subdirectory; it cannot replace unrelated
files. Its own partial or invalid outputs can be replaced on resume.

The worker renders PNG RGBA frames to temporary names, checks complete PNG streams (CRCs, dimensions,
zlib completion), decodes them with Blender, and atomically installs them. The progress manifest
records file hashes and the saved scene revision. Resume verifies both hashes and decoding before
skipping a frame. A fresh attempt can therefore repair missing or corrupt frames without changing
the intact ones. MP4 export encodes the verified PNG sequence through Blender's FFmpeg support,
checks dimensions, frame count and FPS, then decodes first/middle/last frames. PNGs remain available.
`manifest.json` in the output directory is written only after verification.

The supervisor enforces cancellation and the attempt's time budget outside native rendering code.
A per-job lease rejects concurrent resume attempts. If the supervisor disappears, the job reports
interruption. A new attempt is refused while the previous supervisor or renderer is still alive;
the UI never sends a signal to a stale stored PID. Atomic records survive interruption. Completed,
cancelled, failed and interrupted are distinct states.

Current boundaries:

- EEVEE, Cycles (CPU), and Workbench; 1–10,000 explicit frames; up to 8192 pixels per side.
- PNG output is 8-bit RGBA. MP4 is silent H.264 and requires even dimensions and FFmpeg. Each requested
  source frame becomes one movie frame at the scene's FPS, including when the frame list has gaps.
- The requested camera is fixed, including across camera markers. File Output compositor nodes are
  disabled in the copy so they cannot write outside the job's directory. Other compositor processing
  remains enabled. This is a main-image export, not an export of compositor side outputs.
- External assets must exist as concrete files. Hashes are checked before and after rendering;
  changed assets invalidate verification/resume. Pack sequence/tiled textures first. Simulation
  caches must be baked and portable in the copied scene. Jobs do not bake simulations for you.
- Changing the Blender build or worker implementation requires a new job, avoiding mixed render
  versions in one sequence. Disk cleanup is manual; jobs and outputs are intentionally retained.
- Copy-saving/hashing large scenes and assets still takes time in the main process. Rendering,
  decoding and encoding happen in the worker. No model or network access is needed by workers.

Checks (from the repository root):

```sh
python3 -m unittest discover -s loopcut/tests
Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/jobs_check.py
```

The Blender check interrupts a 120-frame job, corrupts and removes frames, resumes and verifies all
120 without touching intact files, verifies MP4 metadata/decoding, enforces a one-second timeout,
and verifies a job completes after the Blender process that launched it exits. No live model calls.
