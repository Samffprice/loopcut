# Selective independent review

`LOOPCUT_CONTROLLER=on` enables a small controller around the existing executor. It reviews
meaningful paid actions, proposed final delivery, and repeated failures. It does not select
implementation steps, write Python, edit the scene, launch workers or add acceptance checks.
The default is **off**, preserving the baseline for a controlled comparison. Enabling it uses
additional model calls on the configured review provider.

## Configuration

Set these in the environment or Loopcut's `.env` (the same lookup as the other model settings):

| Setting | Default | Meaning |
|---|---:|---|
| `LOOPCUT_CONTROLLER` | `off` | Enable review for new turns |
| `LOOPCUT_CONTROLLER_PAID_MIN_CREDITS` | `20` | Review paid calls at/above this estimate; unknown prices and vendor-key calls also qualify |
| `LOOPCUT_CONTROLLER_MAX_CALLS` | `8` | Total review requests per user turn, including inspection follow-ups and failed requests |
| `LOOPCUT_CONTROLLER_MAX_SECONDS` | `30` | Deadline for each review event |
| `LOOPCUT_CONTROLLER_MAX_USD` | `0.10` | Stop starting review requests once recorded review dollars reach this value |
| `LOOPCUT_CONTROLLER_MAX_CORRECTIONS` | `2` | Additional attempts requested at completion |

The reviewer inherits the executor's HTTP provider, model and key, with low reasoning effort and
1,200 output tokens. Override with `LOOPCUT_CONTROLLER_MODEL`, `LOOPCUT_CONTROLLER_BASE_URL`,
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

* **Paid action:** approve its value, return an alternative to the executor, or ask the user to
  decide on the priced action. `spending.decide` remains authoritative, including “Only when I ask,”
  thresholds, vendor consent and burst/chat limits. Forbidden calls are refused before review.
  Review is outside the spending lock; policy is rechecked and credits reserved together under
  the lock after review and any user approval. Workers use the lead turn's controller and ledger.
  Direct user button purchases retain their existing consent path.
* **Finish:** present, request a grounded correction, or report an incomplete/unverified result.
  Existing acceptance, rig and contact gates run first. A mandatory correction must quote actual
  user text and name evidence the reviewer received. Inferred preferences are advisory only.
  Corrections cannot extend a step/time/budget limit. Exhausted/unavailable review reports the
  limitation; it cannot silently certify a result.
* **Repeated failure:** one early review when the existing tracker detects trouble. It can justify
  another repair, change approach or stop. It cannot reset the hard no-progress guard, buy more
  workers or extend execution budgets. Codex native command/file outcomes and dynamic tools feed
  the adapter's tracker too. A queued native command may already be running when a recovery review
  begins; the adapter's interrupt stops the turn if the controller chooses to stop.

Each decision carries a scene-version hash (from `inspection.live_token`) and an instruction hash
covering human messages and queued steering. Changes invalidate the decision. A stale paid action
must be proposed again. A stale completion goes back for inspection, within the correction limit.

The only inspection tools are scene rows, object details, a studio viewport capture, already
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
version hashes, explanations, correction count, wall time, request count, reported/estimated USD
and unknown-cost calls. Resume restores its limits. Malformed saved budget data cannot grant a
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

Record: case ID, arm, executor/provider versions, accepted result (yes/no), a blinded artist's
quality score, minutes of artist correction, total wall time, paid-tool credits, executor/worker
inference charges, review reported USD, review estimated USD, unknown-cost requests and review
wall time. Obtain actual provider charges for unknown-cost calls before claiming a total cost.
Do not count the controller's own verdict as artist acceptance or add subscription allowance
fractions to dollar costs. This public client does not include the private live evaluation suite.

Deterministic tests exercise protocol, consent, concurrency, stale evidence, correction limits,
delivery buffering and the Codex adapter without Blender or paid calls:

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
