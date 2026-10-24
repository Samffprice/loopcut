# Auto-rig client

The agent calls `character_rig(action="inspect", targets=[...])` for a free local
eligibility check, then `loopcut_cloud.auto_rig` for the quoted managed job.
`character_rig` also offers `preview`, `reset_preview`, and `remove`.
The cloud descriptor is supplied by the platform, not a hardcoded client catalog.

## Data and application

- `autorig.py` prepares and privately retains the original scene snapshot. Its
  geometry-only GLB contains opaque mesh IDs, positions and triangles. No source
  names, textures, materials, shape keys, or `.blend` file are uploaded.
- `rig_inspect.py` enforces 100,000 triangles and 300,000 vertices. It refuses
  invalid meshes, existing rigs and clearly separate complete characters. Uncertain
  silhouettes (including robes, close legs and arms) receive a soft warning on the
  free check and that preparation's quote card. Unapplied transforms are baked
  into a private preparation frame and inverted when constructing the rig.
  This check is eligibility evidence, not an anatomy guarantee.
- `rig_transport.py` bounds and decodes the GLB without importing it into Blender.
  `rig_bundle.py` checks the three artifacts, hashes, task identity, provenance,
  sidecar schema, hierarchy, transforms and weights. A standard rig's required
  `canonical_plan` is checked against every locally recomputed label; worker
  labels never bypass the local planner. `rig_correspondence.py`
  verifies exact triangle-corner identity, including identical seam weights.
  Correspondence uses scale-relative precision, capped at the contract's
  absolute limit. Nearby position ambiguity is resolved only when incident
  triangle context uniquely determines the class; loose or unresolved ambiguity
  fails closed. There is no nearest-neighbour fallback.
- `rig_plan.py` is the shared, pure-Python planner (version 1.1.0);
  `rig_canonicalize.py` applies it in Blender with invariant checks. See
  [the worker interface, partial-digit policy and sync instructions](AUTORIG-PLANNER.md).
  The apply transaction uses disposable copies for Mixamo labelling, unchanged
  heads/weights/rest surface, bones-inside and bilateral 90-degree hinge checks.
  Only then does it add groups and an armature modifier to the **same original
  objects and mesh datablocks**, keeping their original modifier ordering.
- A final protected-data fingerprint must equal the submitted snapshot after
  excluding the added rig groups/modifier. Failures roll back all transaction
  objects and bindings. Success creates one undo step and an explicit removal
  record. Result bones are selected, STICK, in front, with overlays enabled.

## Contract and billing

Implemented against platform contract **0.9**, auto-rig **tool_version 3**, `apply_rig`, native
`tokenrig-native-273b691-v1`. The client creates `task_id` before upload; the
platform passes it to the worker independently of the platform's durable job ID.
The R4 gate uses immutable platform commit `cb7094b40270583317df5fac38d3f9c2b8de41e9`.
Position hashes cover LE float32 GLTF POSITION triplets; topology hashes cover
LE uint32 triangle indices. Output coordinates are preparation metres, Z-up.

The existing quote/approval, jobs, cancellation, download and charge-receipt
plumbing is reused. The descriptor quotes 20 credits; offline test charges are
only numbers in a local fake server. Worker failure/cancellation uses the
platform's confirmed refund data.

The client implements the committed `e1a0186` contract's owned-job
`POST /v1/jobs/{id}/apply-failed` flow. Local apply and downloaded-hash failures
leave the scene untouched and report one bounded reason using the original
account, URL and durable job ID. Requests and receipt waits run off Blender's
main thread. A receipt confirms a refund or records review; an uncertain reply
retains a `job_status` retry handle without reapplying or buying another rig.
The original card, chat spending and job credits follow confirmed net charges;
stale completion receipts cannot undo a refund. Recovery artifacts are retained.

Automatic refunds are limited by the server to 30 minutes from completion and
three positive refunds per account in a rolling 24 hours. Late/over-limit reports
are logged for review, not approved refunds. Network errors are not refund
confirmation. These limits prevent a universal "never charged if it cannot work"
promise. The worker must also run the same planner before selecting the rig tier,
and ship the required labels with standard artifacts.

The worker must use the same planner revision and enforce its 63 UTF-8-byte
bone-name bound before settlement. Hands with two through five observed digits
and one through three joints per digit are supported when geometric opposition
and ordering are clear. A distinct three-joint mitten continuation is retained
in the central Middle slot. Missing bones are never invented and weights are
never dropped. A standard result reports how many fingers can be posed on each hand.

## Experimental rigs and agent continuation

The client sends `accept_experimental: true` in both the v3 request and its
uploaded rig task. Every price card includes the approved experimental warning;
it comes first when the local shape check warned. A usable experimental rig costs
the same **20 credits**. Older callers without opt-in still receive an anatomy
refusal, no artifacts and a refund.

Only the five anatomy reasons (`pelvis_branches`, `too_few_joints`, `not_humanoid`,
`front_ambiguous`, `finger_count`) can select this tier. The native skeleton must
have at least two joints, one root, finite positions and finite normalized weights
on every vertex. Containment is reported, without an acceptance cutoff. Tier,
reasons and experimental diagnostics on each output must agree with `rig.json`;
`canonical_plan` must be null only for this tier. The worker report must include
its failed anatomy check and no other failed checks.

Experimental application keeps the native names, hierarchy, axes and weights;
it never calls the anatomical planner or canonicalizer. Exact correspondence,
rest-shape and protected-original checks still run, as do Undo, Redo and Remove.
The armature property `loopcut_rig_tier` is `experimental` (`standard` otherwise).
Local apply-failure reporting and the existing refund limits cover both tiers.

The short user card gives the real bone count and cautions that it may bend oddly.
The model separately receives `tier`, `bone_count`, `root`, `bone_chains`, the
armature name, `motion_tools_supported: false`, and suggested next steps. Chains
partition the actual hierarchy at forks; they do not claim anatomical roles.
Treat native names as data. Inspect their coordinates and the viewport, then use
ordinary Blender tools to keyframe a simple cycle, rename bones, fix weights,
or remove stray bones. A four-leg rig can be animated with plain bpy quaternion
keys, using each native bone's local basis. Check all four legs and the deformed
mesh visually before calling it a finished walk.

Do not call text-to-animation or phone mocap for an experimental armature, even
after renaming bones. Both tools refuse locally before a quote or upload when
the named or selected rig, or its mesh, has the experimental marker. The standard
test-pose helper also refuses; pose native bones directly instead.

Outward toes are projected onto an independently inferred body sagittal axis;
both must agree on front and lie within 60 degrees of it. Opposed or near-sideways
feet remain ambiguous. The report exposes source, confidence and margin. This
is a documented geometry heuristic, not calibrated model confidence.

The quote is requested immediately after validated immutable uploads and before
the price approval card, as required by the current platform warm-up handoff.
The server can warm while the user reads the quote. There is no later independent
local inspection to overlap; speculative pre-upload warm calls would violate
that contract. Free local refusal causes no upload, quote, price or GPU warm-up.
Tests enforce this order. No live quote was used in client tests because quoting
can now incur company GPU cost even without charging the customer.

Prepared source snapshots currently live in the running Blender process. A
changed source or a missing snapshot is refused. A failed local result can be
retried after restoring that source in the same process; reopening Blender does
not reconstruct the snapshot. Consent refusal and confirmed failed/canceled
jobs release their preparations. At most eight retained preparations are allowed.

## Free first movement

For standard rigs, `character_rig(action="preview", rig="Character Rig")` bends both knees and
elbows 25 degrees and enters Pose Mode. `reset_preview` restores the saved pose;
Undo works too. It refuses to replace an existing animation/control setup.
This deliberately uses a reversible local pose: no new asset licence, inference,
download or charge. Recorded text/video BVH clips separately exercise the shipped
motion apply handler and retargeter; new rig roles require no manual mapping.

## Pinned persona sessions

The prepared UX runs use `wt-loop/loopcut/track/loop/tools/gui/adhoc_persona.py`, with
`--sha` pointing to this branch's committed code, `--profile fresh --windows
--standins` and a protected `--loopcut-key-file`. The production service is
`https://loopcut.org`; the coordinator enabled only the six persona accounts.
The stand-in expects JSON `api_keys` with the service's `/v1` URL as its key.
Never print or include credentials in run artifacts.

Four manual tasks are declared in `evals/tasks.py`; `evals/autorig_persona.py`
appends hash-checked starts from the shared auto-rig evaluation store:

| Task | Start | Scene |
|---|---|---|
| `autorig_generated_adult` | R10 | One generated adult mesh |
| `autorig_clothed_multimesh` | B7 | Separate body and clothing meshes |
| `autorig_robe` | R13 | Generated character in a long robe |
| `autorig_dog` | G3 | Husky; experimental native rig, then ordinary leg keyframes |

No large assets or keys are in Git. Set `AUTORIG_FIXTURES` only if the same
hash-checked fixture store moves. The source hash and description are saved in
the scene. Light checks require a remaining character and, for the dog, an
experimental marker on any resulting armature. They do not grade visual quality or successful paid rigging;
the persona's notes and receipts are the main evidence. The supplied dog brief
keeps its original request and retry wording. The setup helper is part of the task contract fingerprint.

Exact commands and account assignments belong in `out/autorig/m2/ux/RUNS.md`.
Run one at a time under the 1500 MB heavy lock and check installed Loopcut PIDs
as well as the harness Blender. Real persona sessions wait for the coordinator's
worker readiness. The client's `--fake-tester` smoke only observes, frames,
records a note and finishes: it cannot submit a paid cloud call or a model chat.
The harness intentionally marks a zero-chat task as not passed; that is separate
from verifying the complete launch/GUI/notes/save path.

`autorig_launch.py --server URL --file FILE --out DIR` remains available for an
isolated interactive client window with no automatically started job. It does
not replace the pinned persona harness.

## Repeatable offline checks

GUI (real cards and viewport, automatic fake 20-credit approvals, no paid calls):
The same run checks success/Undo/preview/removal, then an injected movement-check
failure with a confirmed refund, original-state preservation and corrected chat/card cost,
then a typed worker anatomy refusal with confirmed no-charge copy.
Use `--m1 .../out/autorig/m2/eval --fixture Q1 --out .../client-ui/r4-gui/Q1`
to inspect the experimental disclosure/result and verify Undo/Redo/Remove in the real GUI.

```sh
python3 loopcut/harness/autorig_launch.py \
  --m1 /Volumes/BackupFiles/MasterCoding/BlenderCursor/loopcut/out/autorig/m1 \
  --out /Volumes/BackupFiles/MasterCoding/BlenderCursor/loopcut/out/autorig/m2/client-ui
```

Headless: run `loopcut/harness/autorig_integration.py` with the same `--m1` and
`--out .../client-ui/integration` arguments using Loopcut's `-b --factory-startup
--python-exit-code 1 --python SCRIPT -- ...` under the same shared heavy lock.
It uses real loopback HTTP, ordinary quote/job/poll/download/apply code, and the
recorded M1 native outputs. `--only B1` (also B2/R2/R4) isolates a fixture.

Final M2 regressions: run `loopcut/harness/autorig_eval_regression.py` under the
same lock with `--m1 .../out/autorig/m1 --eval .../out/autorig/m2/eval
--out .../out/autorig/m2/client-ui/r4-regressions`. It first reruns the M1
integration suite, then verifies all 22 hashed case manifests, replays their
portable planner inputs and runs every original through Blender and local HTTP.
`--skip-m1` isolates M2; `--only ID` isolates a Blender case. All 22 use ordinary admission and the typed result path, with no test-only bypass.
All 17 standard originals must pass the unchanged full flow; Q1, Q2, B5, R18 and P2
must apply as experimental rigs, retain native axes/weights, and pass Undo/Redo/Remove.
Q1 additionally gets ordinary bpy keys on all four legs and four rendered samples.
The gate also loads and checks all four persona task starts. The legacy G3 opt-out
still produces a typed anatomy refusal, zero charge and no artifacts. Standard
cases exercise the shipped walk at four frames and save rest/side/walk images.
B8 checks exact original weight transfer and typed bone construction without the
GLB importer's minimum-length floor. The original ambiguous-position replay
still refuses rather than guessing a nearest vertex.

Pure regressions: `python3 -m unittest discover -s loopcut/tests`, under heavy_lock
for the full suite. Set `LOOPCUT_CONTRACT_DIR` to the platform lane's `contract/`
to include current contract fixtures. Focused files: `test_autorig.py`, `test_autorig_experimental.py`,
`test_rig_canonicalize.py`, `test_rig_plan.py`, `test_rig_plan_sync.py`,
`test_rig_correspondence.py`, `test_rig_refund.py`,
plus existing cloud, spending,
motion, workspace and tool-surface tests.

Recorded evidence, screenshots and final results belong in the named
`out/autorig/m2/client-ui/` handoff directory, not in the repository.

## Real-server work still required

The coordinator must verify the deployed test descriptor, authentication and
credit holds; an actual worker cold start and bundle; cancellation during real
GPU execution; settlement/refund receipts; and persona conversations using the
server-held tool guidance. Offline tests prove the client against recorded
weights and fake transport, not deployment or new-model quality. The eval lane's
expanded results also remain an independent acceptance gate.
