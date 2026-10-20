# Runtime cloud tools

`cloud.py` discovers the service catalog through `cloud_http`, using contract 0.2. Discovery
and skill downloads run in daemon threads. The panel timer checks every ten seconds and
refreshes the manifest after ten minutes while a Loopcut area is open. Sign-in, sign-out and
`tool_disabled` also refresh it. An unsupported contract hides tools and shows the update message.

Caches live in `checkpoints.data_root()/cloud/<identity>/`: `manifest.json`, `skills/<sha256>.json`
and job working folders. Identity is a digest of the service URL and credential; no key is stored
in the cache. Signing out cannot restore a signed-in catalog's access. Network failures preserve
the last verified catalog and skills. Successful interest events are retained across restarts.

Each vendor key in Preferences → Your own keys has a "Use this key instead of credits" box, shown
once the key is set and on by default (`LOOPCUT_OWN_KEY_FIRST_<KEY NAME>=false` turns it off). On,
every tool whose own-key adapter uses that vendor takes the key, even on a funded account; a failing
own-key call shows its error and never moves to credits. Off, funded accounts use credits first, then
the key. Without credits, supported vendor keys enable the local BYOK catalog. Managed schemas follow server descriptors; BYOK schemas follow
the adapter's local input contract. Account no-generation applies until `LOOPCUT_NO_GENERATION`
or the local preference explicitly overrides it. Managed availability is still subject to server
enforcement. Services off disables managed routes; premium skills off removes all served skills.

Chat completions and Codex dynamic tools use `loopcut_cloud__<name>`. The Responses adapter
uses a separate `loopcut_cloud` namespace and converts calls and replays back to that dispatch
name. Resolve and apply run in the main-thread pump; the runner waits on the agent thread.
Long calls return an in-process handle for `loopcut_cloud.job_status`, with optional cancellation.
Job status applies successful outputs once. These handles do not survive restarting Blender.

C3 supplies `cloud_jobs.resolve_files`, `run`, and `apply_result`. Calls fail with a clear build
availability result until all three exist. Errors carry credits, refund and retry information.
Calls use the agent's approval/checkpoint cards. BYOK, quoted calls, credit estimates over the
descriptor threshold and references outside allowed file roots request approval.

Motion (contract 0.7: `motion_from_video`, `motion_from_text`) returns `motion.bvh`, a rig-neutral
SOMA77 skeleton, and `motion.json`. `apply_motion` (`cloud_jobs._apply_motion`, `motion_retarget.py`)
retargets it onto the call's `target_armature` here, for free; without one, onto a new SOMA skeleton
with a generated capsule body. A descriptor's `apply_args` (here `target_armature`) are offered to the
model with its inputs, stripped before quotes and jobs, and kept with the job for the handler.
`prepare_inputs` resolves the target when the tool is called (the named object or a mesh's armature,
else the active or the one selected humanoid armature) and refuses a named non-humanoid before any
upload. A video reference may be a movie clip, a video strip or a file: mp4, mov or webm, within the
tool's size, at most 60 s captured; the client sends its measured `video_seconds`, and a trimmed strip's
trim as `start_s`/`end_s`. Retargeting is checked in Blender by `harness/motion_check.py`.

Skill digests cover original Markdown bytes, but the endpoint returns parsed metadata and body.
The client verifies the canonical LF format and unquoted/single-quoted/double-quoted regex fields.
Other original whitespace/quoting/order cannot be recovered; those skills are rejected. A raw-file
field in the response would remove this limitation. No served skill body ships in the add-on.

Run `python3 -m unittest discover -s loopcut/tests`. `test_cloud.py` uses offline transports and
the loopback subprocess helper `mock_service.py`. Set `LOOPCUT_CONTRACT_DIR` to override the
platform checkout; mock tests skip if it is missing.

For Blender's real pump:

```sh
blender --background --factory-startup --python loopcut/tests/cloud_blender_smoke.py
```

The smoke uses an offline transport and fake C3 handlers and leaves the scene unchanged.
It checks thread ownership, not C3's actual exporters/importers or GPU drawing.
