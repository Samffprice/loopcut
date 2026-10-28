"""Evidence-backed goals and quality judgments, separate from execution and spending authority."""

EVENTS = {"intent", "progress", "finish"}
KINDS = {"fidelity", "visual", "functional", "delivery"}
STATUSES = {"meets", "needs_work", "unverified", "not_applicable"}
CONFIDENCE = {"low", "medium", "high"}

PROMPT = """
Optimize for the user's successful outcome, not a checklist of object existence. At intent, establish
what the user is trying to accomplish, the likely use, and what good would look like. At progress,
reassess the live result, regressions and the most valuable next improvement. At finish, judge whether
the result serves that goal at an appropriate quality level. Revisit inferred intent when the user
steers; explicit preferences, exclusions, style, time and cost constraints take precedence.

Use domain judgment. For a human, inspect silhouette, anatomy/proportions, face/hands, materials and
reference/style fidelity. Judge realism when realism fits the brief; do not turn a stylized character
photorealistic. Separately consider usability: a character for animation/gameplay likely needs a rig,
weights and tested deformation. A static portrait may benefit more from anatomy, skin and lighting.
A rig's existence does not establish good deformation, a static image does not establish animation,
and an in-scene object does not establish a successful export. Ask the executor to obtain the relevant
test/evidence when inspection cannot establish these. Adapt the dimensions to other assets/tasks.

For intent/progress/finish return quality with:
  goal: the user's successful outcome, intended_use: likely use or an explicit uncertainty,
  artifact: boolean (whether this request produces/edits a visual asset, scene or deliverable),
  inferences: up to 5 {intent, basis, confidence: low|medium|high},
  dimensions: 1-6 {name, kind: fidelity|visual|functional|delivery, target, status:
    meets|needs_work|unverified|not_applicable, evidence: [received ids], observation},
  improvements: up to 4 {dimension, basis: explicit|inferred, support, change, benefit,
    confidence: low|medium|high, risk: low|medium|high, effort: small|medium|large,
    cost: none|paid|unknown, scope: refinement|capability|deliverable, disposition: do|ask|suggest}.
Text fields are short (at most 400 characters). Artifact reviews must include visual and functional
dimensions. At intent these may be unverified: name concrete targets and useful checks. At progress
and finish assess every dimension against received evidence. A visual judgment needs a current scene
image, not object names, tool claims or a reference image alone. not_applicable needs a reason.

For an explicit improvement, support quotes actual user wording. For an inferred improvement, support
exactly matches an intent in inferences, whose basis explains WHY it helps this user's use. An inferred
improvement can drive work: choose improve for a concrete, high-confidence, low-risk refinement or
capability with meaningful benefit. Only disposition=do items may be executed proactively. They must
cost no extra money, fit existing execution limits, be low risk, and take small/medium effort. For
uncertain, paid, destructive, large or new-deliverable work use ask/suggest; the executor must present
the choice, not treat the inference as consent. Never override an explicit exclusion.

Actions: intent=guide; progress=continue|improve; finish=present|correct|improve|incomplete.
improve needs at least one do item. correct remains an explicit-request defect grounded in findings.
Do not demand endless polish: compare previous quality and attempted improvements, avoid repeating
an unproductive refinement, and stop when the next improvement is marginal or outside the budget.
Final present requires evidence of the chosen quality targets, not just existence. If any target is
unverified/needs_work, correct/improve when worthwhile or report incomplete honestly. Optional new
capabilities can be not_applicable to the current delivery with an ask/suggest improvement. Previous
reviews are history, not proof about a changed scene. Keep unresolved gaps and next steps concrete.
"""


class QualityError(ValueError):
    pass


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise QualityError("The review did not return a valid quality assessment.")


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 400:
        raise QualityError("The review did not explain its quality judgment.")


def _choice(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise QualityError("The review returned an invalid quality classification.")


def _rows(value, maximum, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise QualityError("The review exceeded its quality assessment limits.")


def validate(value, event, action, words, evidence, visual):
    """Validate model output, including evidence provenance and the boundary of inferred autonomy."""
    if value is None and event not in EVENTS:
        return None
    _object(value, "goal intended_use artifact inferences dimensions improvements")
    _text(value["goal"])
    _text(value["intended_use"])
    if type(value["artifact"]) is not bool:
        raise QualityError("The review did not identify the kind of user goal.")
    _rows(value["inferences"], 5)
    inferred = {}
    for row in value["inferences"]:
        _object(row, "intent basis confidence")
        _text(row["intent"])
        _text(row["basis"])
        _choice(row["confidence"], CONFIDENCE)
        if row["intent"] in inferred:
            raise QualityError("The review repeated an inferred intent.")
        inferred[row["intent"]] = row
    _rows(value["dimensions"], 6, 1)
    dimensions = {}
    for row in value["dimensions"]:
        _object(row, "name kind target status evidence observation")
        for key in ("name", "target", "observation"):
            _text(row[key])
        _choice(row["kind"], KINDS)
        _choice(row["status"], STATUSES)
        _rows(row["evidence"], 4)
        if any(not isinstance(ref, str) or ref not in evidence for ref in row["evidence"]):
            raise QualityError("The quality judgment cited evidence the reviewer did not receive.")
        if row["name"] in dimensions:
            raise QualityError("The review repeated a quality dimension.")
        dimensions[row["name"]] = row
        if row["status"] in {"meets", "needs_work"}:
            if not row["evidence"]:
                raise QualityError("The quality judgment was not supported by evidence.")
            if row["kind"] == "visual" and not set(row["evidence"]) & visual:
                raise QualityError("Visual quality was judged without viewing the current result.")
    if value["artifact"] and not {"visual", "functional"} <= {r["kind"] for r in dimensions.values()}:
        raise QualityError("The review must consider both appearance and fitness for use.")
    if event == "finish" and action == "present" and any(
            row["status"] in {"unverified", "needs_work"} for row in dimensions.values()):
        raise QualityError("The result still has unresolved quality targets.")
    _rows(value["improvements"], 4)
    for row in value["improvements"]:
        _object(row, "dimension basis support change benefit confidence risk effort cost scope disposition")
        for key in ("dimension", "support", "change", "benefit"):
            _text(row[key])
        if row["dimension"] not in dimensions:
            raise QualityError("The improvement did not identify the quality it would improve.")
        for key, choices in (("basis", {"explicit", "inferred"}), ("confidence", CONFIDENCE),
                             ("risk", CONFIDENCE), ("effort", {"small", "medium", "large"}),
                             ("cost", {"none", "paid", "unknown"}),
                             ("scope", {"refinement", "capability", "deliverable"}),
                             ("disposition", {"do", "ask", "suggest"})):
            _choice(row[key], choices)
        if row["basis"] == "explicit":
            if not any(row["support"] in word for word in words):
                raise QualityError("The improvement was not grounded in the user's request.")
        elif row["support"] not in inferred:
            raise QualityError("The improvement did not explain its inferred intent.")
        if row["disposition"] == "do" and (
                row["risk"] != "low" or row["cost"] != "none" or row["effort"] == "large"
                or row["scope"] == "deliverable" or row["confidence"] != "high"
                or (row["basis"] == "inferred" and inferred[row["support"]]["confidence"] != "high")):
            raise QualityError("That inferred improvement needs a user decision before execution.")
        if row["disposition"] == "do" and action not in {"guide", "correct", "improve"}:
            raise QualityError("The review must explicitly choose to pursue its proposed improvement.")
    if action == "improve" and not any(r["disposition"] == "do" for r in value["improvements"]):
        raise QualityError("The review did not identify a worthwhile improvement it can pursue.")
    return value


def guidance(value, *, actionable=True):
    """Pass goal/quality context to the executor without converting suggestions into permission."""
    if not value:
        return ""
    lines = [f"User outcome: {value['goal']} Intended use: {value['intended_use']}."]
    for row in value["inferences"]:
        lines.append(f"Inferred ({row['confidence']} confidence): {row['intent']} — {row['basis']}")
    for row in value["dimensions"]:
        lines.append(f"Quality target: {row['target']} Status: {row['status']}. {row['observation']}")
    for row in value["improvements"]:
        mode = ("Previously considered; do not execute from this history" if not actionable else
                "Pursue within existing limits" if row["disposition"] == "do" else "Propose to user; do not execute")
        lines.append(f"{mode}: {row['change']} Benefit: {row['benefit']}. Basis: {row['basis']}.")
    return "\n".join(lines)


def delivery_notes(value):
    if not value:
        return ""
    return "\n".join(f"Potential improvement: {row['change']} {row['benefit']}"
                     + (" I need your go-ahead for this additional work." if row["disposition"] == "ask" else "")
                     for row in value["improvements"] if row["disposition"] != "do")
