# Shared auto-rig planner

Source: `scripts/addons_core/loopcut/rig_plan.py`. `PLANNER_VERSION = "1.1.0"`;
JSON interface version 1; contract policy `mixamo-tokenrig-v1` (contract 0.8).
The module imports only Python's standard `math` module. No Blender, NumPy,
filesystem, network, package import or trained model is needed. The Blender
application/invariant code remains in `rig_canonicalize.py`, which imports this
same planner. The worker must vendor **exact bytes**, not independently port it.

## Worker interface

```python
import rig_plan

result = rig_plan.plan_json({
    "schema_version": 1,
    "joints": [{"name": "bone_0", "parent": None, "head": [0.0, 0.0, 1.0]}, ...],
    "vertices": [[0.1, 0.0, 0.0], ...],
})
if result["status"] == "planned":
    rig_sidecar["canonical_plan"] = result["canonical_plan"]
else:
    # Refuse BEFORE publishing artifacts or settling the customer hold.
    refusal = {"code": "unsupported_anatomy", "anatomy_reason": result["anatomy_reason"]}
```

Input objects have exactly the illustrated fields. Each head and vertex is a
finite three-number vector in the **same metric coordinate system**. For the
rig sidecar use its preparation-metre, Z-up frame; transform GLTF positions and
rest heads consistently before calling. Strip other sidecar joint metadata.
Both sides must use **the positions decoded from the returned native GLB**, in
its primitive traversal order, retaining seam duplicate multiplicity, together
with `rig.json.joints` heads/order. Do not substitute original mesh vertices:
the no-toe quantiles can change when seam multiplicity changes. The client
verifies native correspondence first, recomputes the plan from these exact
inputs before any scene staging, and transforms only drawing tails/axes when
applying. Blender world transforms never enter label agreement.

There are 15–256 unique joints and 1–300,000 vertices. Names are safe strings of
at most 63 UTF-8 bytes; indices are integers, not booleans. Parents may be in any
order but must form one connected acyclic tree. Names/order never infer anatomy.

Success contains `schema_version`, `planner_version`, `status:"planned"`,
`canonical_plan`, and `report`. The closed contract projection is exactly
`{policy, labels:[{index, source, role, name}]}` in input joint order. **Do not put
the planner version or diagnostic axes inside that closed object.** Record the
version, source commit and SHA-256 in worker provenance/vendor notes separately.
`report` contains every joint's role, tail, roll axis, confidence and reasoning in
the planning frame. The Blender wrapper separately marks its returned diagnostic
frame as `blender_world` for movement checks and camera framing.
JSON serialization must reject non-finite values (`allow_nan=False` in Python).

Refusal contains `schema_version`, `planner_version`, `status:"refused"`,
`code:"unsupported_anatomy"`, `anatomy_reason` and a diagnostic `message`.
The five reasons are `finger_count`, `front_ambiguous`, `pelvis_branches`,
`too_few_joints`, `not_humanoid`. Malformed JSON-object input returns a bounded
`not_humanoid` refusal; transport/schema validation remains the worker's job.
Use the platform's friendly refusal copy, not the diagnostic message.
The raising Python API `plan(joints, vertices)` remains available to the client.

## Partial-digit policy

All observed joints, parents and weights are retained. No source name, fixture
identity, gold rig, assumed facing direction or missing bone is used.

- Hands with two through five forks accept **one to three joints per digit**.
  The thumb must show clear separation/opposition to forearm continuation. Its
  score is the sum of `1 - cosine` for wrist-to-base and wrist-to-tip against
  the forearm axis. The top score must be at least 0.20 and exceed the next by
  0.12. This avoids the incorrect nearest-base thumb on R11's right hand.
- Remaining bases are ordered along their widest knuckle line, away from the
  thumb. Ambiguous thumb-side or coincident order refuses. Tied widest axes must all
  produce the same ordering; otherwise the hand refuses instead of using joint
  order as a tie breaker. Ordered non-thumb
  digits use the first available `Index`, `Middle`, `Ring`, `Pinky` slots.
  These are **compact retarget slots**, not inferred identities of absent digits:
  four observed digits become Thumb/Index/Middle/Ring; two become Thumb/Index.
  Chain depth becomes suffix 1/2/3, with no missing phalanges invented.
- One unbranched six/seven-joint arm is split into a three/four-joint arm and
  three-joint continuing digit only when the wrist transition is clear: each
  digit step ≤60% of forearm length, total reach ≤125%, alignment cosine ≥0.5.
  The central continuation receives `Middle1/2/3` with heuristic confidence
  0.74. No thumb can be established without opposition evidence. Shorter
  ambiguous continuations refuse. An unbranched four-node arm also needs a
  proximal segment ≤75% of the middle segment and a distal segment ≥60% of the
  middle segment; this prevents a short digit on a no-clavicle arm from being
  silently treated as its wrist. Seven-joint R12/R13/R17 retain all 28 joints.
- No-finger hands stay fingerless. Mixed left/right counts remain explicit.
  The success message reports the actual number of finger chains per hand.

The policy is deliberately conservative at ambiguous thresholds. It does not
claim anatomical reconstruction of missing digits, finger-contact accuracy,
facial controls, or a measured probability of animation quality.

## Front inference

The head/pelvis establishes up and the bilateral forearms establish body lateral.
Each horizontal toe is projected onto the resulting body sagittal axis. All
available toes must agree on sign and have absolute projection >0.5 (within
60 degrees of body forward). Thus ordinary outward feet can disagree with one
another by more than the old pairwise limit while agreeing with the body frame.
Opposed/near-sideways/degenerate toes still refuse. This combines independent
body and foot geometry; it does not relax the old pairwise 0.7 cutoff.

The report exposes `front_source`, `front_confidence`, and `front_margin`.
Confidence is `0.78 + 0.19 * minimum_alignment` for bilateral toes (0.10 for
one toe); margin is minimum alignment minus 0.5. Without toes, both shoe-surface
extents must independently agree and their mean asymmetry must exceed 1.2% of
height; confidence is 0.78. These are disclosed heuristics, not calibrated odds.

Knee/elbow roll uses the measured three-joint bend plane only when its tangent
lies within 60 degrees of the expected body flexion direction. Near-straight
chains and primarily sideways curvature use the independent body-front plane.
This matters for B8's bowed legs: their measured plane moved the ankle sideways
and slightly forward at 90 degrees. The correction changes drawing/rotation axes,
never heads or weights; the same strict 90-degree and backward-knee checks remain.
The regression also covers the geometry at full and tiny scale.

## Sync and verification

The worker lane runs, from this client checkout:

```sh
python3 loopcut/scripts/sync_rig_plan.py /absolute/path/to/worker-checkout
python3 loopcut/scripts/sync_rig_plan.py /absolute/path/to/worker-checkout --check
```

Only `workers/autorig/rig_plan.py` is copied; `--check` does not write. Output
records the client commit and exact SHA-256. Commit that file and its provenance
in the worker lane, retaining repository licence notices. The client lane tests
the script using temporary directories and does not edit the worker checkout.
Set `LOOPCUT_AUTORIG_WORKER_DIR` when running `test_rig_plan_sync` to check an
actual vendored file too. The default suite tests copying, drift, read-only
checks, missing/symlink destinations and isolated `python -S` import without bpy.

`test_rig_plan.py` covers 2/3/4/5 digits, 1/2/3 joint depths, single continuations,
farther thumbs, ambiguity, hostile input, rigid transforms, tiny scale and
reordered joints. Three-digit coverage is synthetic: none of the retained 22
native fixtures has that topology. `autorig_eval_regression.py` replays all 22
hashed case manifests and originals, then every case through Blender/HTTP.
It separately records preflight admission, planner refusal, typed apply, sampled
walk and protected-state restoration. Diagnostic preflight bypasses are never
counted as product successes. The client compares all worker labels with its own
result before committing any rig. Different revisions must fail closed.

B5's two-child pelvis, Q1/P2/Q2 and R18 remain refused. B8's original ambiguous
position replay still fails closed: it has no triangle context. Where incident
triangles uniquely disambiguate an overlapping tolerance class, correspondence
intersects those constraints; traversal order and nearest distance do not decide
identity. Each face is bounded to 64 candidate assignments. A dedicated test
proves that topology can select a farther, correct vertex over the nearest one;
remaining ambiguity still refuses. The client's geometry export supplies
unambiguous exact correspondence, and typed armature construction
avoids the GLB importer's 4 mm minimum length on the 21 mm transformed character.
