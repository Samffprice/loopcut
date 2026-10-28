# Goal and quality review

`LOOPCUT_CONTROLLER=on` enables an independent controller around the existing executor. It establishes
the user's desired outcome, intended use, explicit requirements and plausible inferred needs. It
reassesses quality during work, at meaningful paid actions, on repeated failures and before delivery.
It can direct worthwhile improvements; the executor chooses the implementation. It does not write
Python, edit the scene, launch workers or rewrite measured acceptance checks.
The default is **off**, preserving the baseline for a controlled comparison. Enabling it uses
additional model calls on the configured review provider.

## Configuration

Set these in the environment or Loopcut's `.env` (the same lookup as the other model settings):

| Setting | Default | Meaning |
|---|---:|---|
| `LOOPCUT_CONTROLLER` | `off` | Enable review for new turns |
| `LOOPCUT_CONTROLLER_PAID_MIN_CREDITS` | `20` | Review paid calls at/above this estimate; unknown prices and vendor-key calls also qualify |
| `LOOPCUT_CONTROLLER_MAX_CALLS` | `12` | Total review requests per user turn, including inspection follow-ups and failed requests |
| `LOOPCUT_CONTROLLER_MAX_SECONDS` | `30` | Deadline for each review event |
| `LOOPCUT_CONTROLLER_MAX_USD` | `0.10` | Stop starting review requests once recorded review dollars reach this value |
| `LOOPCUT_CONTROLLER_MAX_CORRECTIONS` | `2` | Additional attempts requested at completion |
| `LOOPCUT_CONTROLLER_CHECK_EVERY` | `3` | Reassess after this many HTTP tool batches or completed Codex tool/command events |
| `LOOPCUT_CONTROLLER_MAX_CHECKPOINTS` | `3` | Maximum progress reviews per user turn |
| `LOOPCUT_CONTROLLER_MAX_IMPROVEMENTS` | `3` | Maximum proactive improvement passes, shared by progress and final review |

The reviewer inherits the executor's HTTP provider, model and key, with medium reasoning effort and
2,400 output tokens. Override with `LOOPCUT_CONTROLLER_MODEL`, `LOOPCUT_CONTROLLER_BASE_URL`,
`LOOPCUT_CONTROLLER_API_KEY`, `LOOPCUT_CONTROLLER_REASONING_EFFORT` and
`LOOPCUT_CONTROLLER_MAX_OUTPUT_TOKENS`. The review prefix is independent and stable; the ordinary
executor's system prompt and tools stay unchanged.

Hosted, own-key and ChatGPT-plan turns all use the shared controller. The Codex-app adapter
calls it from its own tool/completion events, buffers its final-answer phase and can restart the
same Codex thread with a targeted correction. Codex-app sign-in alone is **not** an HTTP review
credential: configure the review provider explicitly if there is no executor HTTP configuration.
Using `chatgpt-plan://` requires Loopcut's existing ChatGPT-plan sign-in. No credentials are copied
from Codex. The normal provider's allowance and billing rules still apply.

The dollar setting is a stop-before-next-request threshold, **not a guaranteed invoice ceiling**.
A request already in flight can cross it. Reported charges take precedence over estimates from
the existing price table. Unknown prices, subscription usage and interrupted requests are recorded
as unknown, never free. Call count and deadlines also bound review when dollar prices are unknown.
The ChatGPT-plan adapter does not support an output-token cap; the deadline still applies.
Native Blender operations already running cannot be safely preempted, as with existing tools.

## Decisions and authority

* **Intent:** establish the useful outcome and quality targets before execution, and revisit them when
  the user steers. Explicit preferences, exclusions, style and budget take precedence over inference.
* **Progress:** assess what has been built, regressions, missing capabilities and the best next
  improvement. A high-confidence, low-risk improvement may proceed without literal wording in the
  brief when it adds no monetary cost, takes small/medium effort and stays within the existing limits.
  The controller records its rationale and expected benefit. Paid, uncertain, large or new-deliverable
  additions become user choices. Repeat attempts at the same optional improvement are refused.
  If an inferred improvement turns out to require an unrequested paid tool, the actual quoted call
  asks for approval even below the usual review threshold. An inference does not authorize a charge.
* **Paid action:** approve its value, return an alternative to the executor, or ask the user to
  decide on the priced action. `spending.decide` remains authoritative, including “Only when I ask,”
  thresholds, vendor consent and burst/chat limits. Forbidden calls are refused before review.
  Review is outside the spending lock; policy is rechecked and credits reserved together under
  the lock after review and any user approval. Workers use the lead turn's controller and ledger.
  Direct user button purchases retain their existing consent path.
* **Finish:** present, request an explicit correction or useful inferred improvement, or report an
  incomplete/unverified result.
  Existing acceptance, rig and contact gates run first. A mandatory correction must quote actual
  user text and name evidence the reviewer received. Inferred improvements instead name their
  contextual basis, confidence, benefit, effort, risk and cost. Appearance and fitness for use must
  both be considered for an asset. Unresolved quality targets cannot be silently marked ready.
  Corrections and improvements cannot extend a step/time/budget limit. Exhausted/unavailable review reports the
  limitation; it cannot silently certify a result.
* **Repeated failure:** one early review when the existing tracker detects trouble. It can justify
  another repair, change approach or stop. It cannot reset the hard no-progress guard, buy more
  workers or extend execution budgets. Codex native command/file outcomes and dynamic tools feed
  the adapter's tracker too. A queued native command may already be running when a recovery review
  begins; the adapter's interrupt stops the turn if the controller chooses to stop.

The controller checks intent throughout the active turn without starting a separate polling agent.
Model-based progress reviews happen at bounded tool boundaries. Intent/progress cannot consume the
last four review requests and stop starting once known review costs reach 75% of the dollar threshold.
This reserves capacity for final judgment; paid-action/failure reviews still use the shared allowance.
There is no claim of continuous background evaluation after the user turn ends.

## What quality means

The controller maintains a durable assessment with a goal, intended use, explained inferences,
task-specific dimensions, evidence, unresolved gaps and improvement proposals. Each dimension states
a concrete target and is `meets`, `needs_work`, `unverified` or `not_applicable`. Targets adapt to the
user's task rather than imposing the same aesthetic on every asset.

| User context | Useful quality judgment | Likely next step |
|---|---|---|
| Realistic human for a game | Anatomy, silhouette, face/hands, skin/materials, poseability and deformation | Refine a visible defect; add a suitable rig if feasible within scope; obtain pose-test evidence |
| Stylized character for a still image | Shape language, proportions, shading and composition in the requested style | Improve readability or materials; rigging remains optional if it does not help the image |
| Human with an armature | A skeleton exists, but weights, joint bends and animation may still be untested | Obtain deformation tests; do not certify animation readiness from bones alone |
| Product animation/export | Motion clarity, intersections, final appearance and an actually usable deliverable | Inspect multiple views/poses and verify the requested export, not just object existence |

A visual pass needs a current-result image actually received by the reviewer. Scene rows, executor
claims and user reference images cannot satisfy it. The reviewer can request a focused view or a
four-view sheet, inspect structure, and read measured results. Static images cannot prove motion or
deformation quality. The executor obtains those tests through its existing tools; the controller
does not create a rig or change poses itself. Optional recommendations requiring a user decision
are included in the final delivery instead of being lost as internal advice.

Each decision carries a scene-version hash (from `inspection.live_token`) and an instruction hash
covering human messages and queued steering. Changes invalidate the decision. A stale paid action
must be proposed again. A stale completion goes back for inspection, within the correction limit.

The only inspection tools are scene rows, object details, focused/multiple studio viewport views, already
attached images, bounded reads of listed result-store IDs, and omitted human instructions by
message index. Dispatch validates arguments and allowlists independently of the model's schema.
There is no arbitrary file path,
regex, shell, Python, network tool, paid tool or scene mutation available to the reviewer.

## Delivery and records

With review enabled, ambiguous HTTP text is buffered until the response establishes whether it
calls tools. Tool-bearing progress is then shown; text-only completion waits for the finish gates
and review. Codex's explicit commentary phase can stream; final or unknown phases are buffered.
Rejected/cancelled drafts never become visible assistant cards, and retained internal messages
are marked as withheld so a resumed executor cannot mistake them for an accepted delivery.
Runtime verification footers attach to the pending final card.

The reviewer sees a bounded packet: original intent, recent human corrections, recent attempts,
tool evidence, stored-result pointers, available reference images, previous review decisions and
distinct resource categories. It can retrieve additional evidence without copying the executor's
conversation or private reasoning. Inference history is isolated from the executor.

`usage_by_role.controller` accounts for its tokens and requests in the running session. The
originating user message's `loopcut_controller` JSON-string marker persists review events,
version hashes, explanations, quality assessments, inferred intent, attempted improvements,
checkpoint/correction counts, wall time, request count, reported/estimated USD and unknown-cost calls.
Resume restores its limits and goal context. Malformed saved budget data cannot grant a
new review budget. These are conversation records, not new anonymous telemetry fields.

## Controlled comparison

Run paired trials from the same saved scene and prompt, with identical executor model, reasoning,
worker settings, paid-tool policy and execution limits. Randomize trial order. The only experiment
toggle is `LOOPCUT_CONTROLLER=off` versus `on`. Use multiple repetitions and save the conversation,
scene and payment receipts for each run. Keep any hosted prompt/version changes out of a pair.

| Case | What to arrange | What to inspect |
|---|---|---|
| Paid asset selection | A suitable existing/library asset and an optional paid replacement | Whether a purchase improved the requested result; total actual charges |
| Premature completion | A multi-part brief with one missing deliverable | Whether “done” was held until correction or an honest incomplete report |
| Visual defect | An explicit user constraint violated in the scene, with an inspectable reference | Whether the review finds and fixes it without inventing new requirements |
| Repeated failures | A reproducible failing command/API strategy | Whether the next strategy works; attempts, elapsed time and total cost |
| Implicit usability | A game character request that does not literally mention a rig | Whether useful rigging and real deformation checks follow, within scope and consent |
| Style and restraint | A stylized static character, followed by an explicit request to avoid rigging | Whether appearance improves while style and the user's exclusion remain authoritative |
| Quality over existence | A recognisable human with poor proportions/materials and an untested armature | Whether the review detects and improves substantive defects rather than merely counting assets |

Record: case ID, arm, executor/provider versions, accepted result (yes/no), a blinded artist's
quality score, minutes of artist correction, total wall time, paid-tool credits, executor/worker
inference charges, review reported USD, review estimated USD, unknown-cost requests and review
wall time. Obtain actual provider charges for unknown-cost calls before claiming a total cost.
Do not count the controller's own verdict as artist acceptance or add subscription allowance
fractions to dollar costs. This public client does not include the private live evaluation suite.

Deterministic tests exercise protocol, consent, concurrency, stale evidence, correction limits,
inferred improvements, visual evidence requirements, progress steering, delivery buffering and the
Codex adapter without Blender or paid calls:

```sh
python3 -m unittest discover -s loopcut/tests
```

A factory-scene smoke check exercises object inspection and revision invalidation in Blender,
using scripted responses and no paid model calls:

```sh
/path/to/Blender --background --factory-startup --disable-autoexec --python loopcut/harness/controller_check.py
```

Those tests establish control-flow behavior, not a visual-quality improvement. Real Blender/model
trials remain necessary before changing the default or claiming that review earns its overhead.
