# Durable render and export jobs

The agent's `render_job` tool with action `start` saves a scene copy, then launches a detached
supervisor and a render process. The editing process only waits for the copy-save, not for rendering.
Actions `status`, `cancel` and `resume` operate on a durable ID, independent of the conversation.
The Jobs button in the panel header shows progress, errors, Preview, Output, Cancel and Resume.
Jobs continue after the panel, conversation, or launching Blender process closes.

Waiting for a job. `status` with `wait` (seconds, capped at `job_tools.WAIT_CAP`, 240) returns when
the job is no longer queued or running (complete, failed, cancelled or cancelling, interrupted), when
the wait is up, or when the user stops the turn. A stop ends the wait, never the job. The main-thread
call only reads the job and hands back the rest (`ToolResult.then`). `agent._run_tool_on_main` runs
that on the agent thread, between main-thread calls: it reads `status.json` (`jobs.current`, one
small file) every second and checks for a stop every 0.1 s. Then it reads the status on the main
thread as usual, with a preview if asked. Blender and its UI stay free the whole time. The result
ends with a line on where the job stands: frames done of total, time into the attempt, a rough time
left from the frames rendered so far, and the final state.

The guidance is in the results, so the system prompt and tool list stay fixed. After `start` and
`resume`: the job goes on after the turn, so don't poll it. Call status with wait if its output is
needed this turn, and otherwise finish. A status that finds the job running suggests `wait=`.
The step budget adds a backstop (see DEVELOPING.md, The step budget). An eval turn had spent 38 of
its 60 steps on status calls, each paired with `run_python` sleeping or listing the output folder.
The sleep also held Blender's main thread.

Each attempt requires approval (unless the user chose "Always allow renders" in that conversation)
for the saved settings and an explicit wall-clock budget. Resuming
uses the original budget again. It never includes newer edits to the live scene. A job's compressed
`.blend`, SHA-256 revision, engine, camera, frame list, dimensions, samples, FPS, source path, external
asset hashes and output location live in `<data>/jobs/<id>/spec.json`. They do not expire with chat
checkpoints. Each job renders into a new `loopcut-<id>` subdirectory of `output_dir`, or straight into
`folder` (relative to the .blend), which must be new or empty (hidden files aside) and not another
running job's; it cannot replace unrelated files. Its own partial or invalid outputs can be replaced
on resume. The output holds only renders: the job knows its folder by device and inode (a folder
made again under the name is not its), a job started later into the same folder takes it over, and
`manifest.json` sits with the job's records. `frames`, or `start`/`end` (each defaulting to the
scene's range).

The worker renders PNG RGBA frames to temporary names, checks complete PNG streams (CRCs, dimensions,
zlib completion), decodes them with Blender, and atomically installs them. The progress manifest
records file hashes and the saved scene revision. Resume verifies both hashes and decoding before
skipping a frame. A fresh attempt can therefore repair missing or corrupt frames without changing
the intact ones. MP4 export encodes the verified PNG sequence through Blender's FFmpeg support,
checks dimensions, frame count and FPS, then decodes first/middle/last frames. PNGs remain available.
`manifest.json` in the job's folder is written only after verification.

The supervisor enforces cancellation and the attempt's time budget outside native rendering code.
A per-job lease rejects concurrent resume attempts. If the supervisor disappears, the job reports
interruption. A new attempt is refused while the previous supervisor or renderer is still alive;
the UI never sends a signal to a stale stored PID. Atomic records survive interruption. Completed,
cancelled, failed and interrupted are distinct states.

Current boundaries:

- EEVEE, Cycles (CPU), and Workbench; 1–10,000 explicit frames; up to 8192 pixels per side.
- PNG output is 8-bit RGBA. MP4 is opaque, silent H.264 and requires even dimensions and FFmpeg. Each requested
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
120 without touching intact files, verifies MP4 metadata/decoding, waits on a job with status `wait` until it completes, enforces a
one-second timeout, and verifies a job completes after the Blender process that launched it exits.
No live model calls. `test_jobs.StatusWaitTests` covers the wait without Blender: it returns on
the job's end, at the end of the wait, and at once on a stop, and it polls off the main thread.
