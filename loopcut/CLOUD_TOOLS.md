# Runtime cloud tools

`cloud.py` discovers the service catalog through `cloud_http`, using contract 0.2. Discovery
and skill downloads run in daemon threads. The panel timer checks every ten seconds and
refreshes the manifest after ten minutes while a Loopcut area is open. Sign-in, sign-out and
`tool_disabled` also refresh it. An unsupported contract hides tools and shows the update message.

Caches live in `checkpoints.data_root()/cloud/<identity>/`: `manifest.json`, `skills/<sha256>.json`
and job working folders. Identity is a digest of the service URL and credential; no key is stored
in the cache. Signing out cannot restore a signed-in catalog's access. Network failures preserve
the last verified catalog and skills. Successful interest events are retained across restarts.

Funded accounts use managed tools unless selected in Preferences → Your own keys → Use own keys
for tools, or `LOOPCUT_BYOK_TOOLS=image_generate,voice_line`. Otherwise supported vendor keys
enable the local BYOK catalog. Managed schemas follow server descriptors; BYOK schemas follow
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
