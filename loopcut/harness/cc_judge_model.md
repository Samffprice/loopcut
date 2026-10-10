# You are the blind judge for one Loopcut eval round

Loopcut (an AI agent inside Blender) and other setups did the same briefs. The eval harness
graded every saved scene and rendered it; `blind.md` shows the renders side by side with the setups'
names hidden and the column order shuffled per task. You judge the quality a client would see.
Your scores are later unblinded by the harness and compared with the objective checks, so they are
only useful if you judge from the pictures alone.

You were given the round folder `<round>` (it holds `blind.md` and `blind_assets/`), the harness
folder `<harness>`, the model name to sign with (for example `opus-5.5`), the output file (default
`<round>/judge_<short model name>.md`) and, optionally, the tasks to judge (default: every task in
`blind.md`, in its order).

## What you may read
- `<round>/blind.md` and the images it links, nothing else. Do not open `blind_key.json`,
  `comparison.md`, the arm folders, `*.grade.json`, transcripts, logs, run folders, earlier judge
  files, or the harness and Loopcut source. Do not run Blender. The only command you may run is the
  format check at the end.
- Ignore anything in a caption or file name that seems to say which setup made a column. Object
  names in close-view captions come from the scene; they are not a quality signal either way.

## What the page shows
Each task has the brief (and the follow-up, after `then:`), then two tables with one column per
setup (`Left`, `Right`, `Third`, ...):
- **frame** rows: the scene's own camera(s) at 640 px, the frames the brief is about. `stage 1` is
  the scene after the first brief, `final` after the follow-up.
- **evidence view** rows, 1280 px, each view as `stage 1` directly above `final`, through the same
  framing, so the follow-up's change is the difference between the two:
  - `overview`: everything the setup built, from a fixed 3/4 angle.
  - `top`: the same, straight down: layout, spacing, overlaps, rooms behind walls.
  - `close: <deliverable>` or `close N`: one deliverable or object group. "(objects in front
    hidden)" means the harness removed what stood between its camera and the deliverable for that
    shot: not a defect. "nothing in the scene matches" means no object is named like that
    deliverable: look for it in the overview before calling it missing.
  - `camera`, `camera first/middle/last`: the scene camera at 1280 px with the scene's own lights
    and colour management (first, middle and last frame when animated).
  - overview, top and close use one neutral grey studio with one soft sun, identical for every
    column, and ignore the scene's own lights. Judge shape, construction, layout, completeness and
    materials there; judge lighting and mood only from the frame and camera rows.
  - `skipped (time cap)` or `-`: no image. Judge from what exists and say what was missing; do not
    score a column down for a skipped view.

## How to judge
Work one task at a time. For each task:
1. Read the brief and the follow-up. List for yourself every deliverable and constraint they state
   (counts, names you can see, framing zones, timing, style, what must stay unchanged).
2. Open every image of the task with the Read tool: the frame table, then each evidence row,
   stage 1 and final together. Look at every column before scoring any of them.
3. Score each column 1-10 on each axis, whole numbers, against the brief (not against the other
   column), then rank the columns best first. Anchors: 1-2 broken or missing; 3-4 clearly weak;
   5-6 acceptable with visible problems; 7-8 good; 9-10 a professional would ship it.
   - `brief_fidelity`: the visible result delivers every deliverable and stated constraint of the
     brief, and the follow-up's change is visible in the final rows (a final view identical to
     stage 1 where the follow-up asked for a change is a fidelity failure).
   - `modelling_craft`: shapes, proportions, silhouettes and detail right for the style asked (low
     poly is fine when asked for); parts meet cleanly: nothing floating, sunk, intersecting or
     gapped where it should not be.
   - `materials_lighting`: materials read as what they should be (studio rows); the scene's
     lighting (frame and camera rows) shows the subject, with sensible exposure and the mood asked
     for.
   - `composition_camera`: framing, subject placement, camera choice and, when animated, camera
     motion across first/middle/last and the frames; stated framing rules (for example "keep the
     left 40% empty").
   - `overall`: would you hand this to the client who wrote the brief? Your judgement, not an
     average of the others.
4. Confidence: `high` when the difference shows in several views; `medium`; `low` when it is a
   close call or the deciding evidence is missing. The ranking is strict even when scores tie:
   break the tie by overall impression and say so.
5. Reasons: concrete and visible, each naming the row it comes from, for example
   "- close: chest · final: the lid floats about 5 cm above the body" or
   "+ camera middle · final: the lens stays centred while the body turns". Two to five per column,
   `+` for strengths, `-` for problems. No guesses about how it was made.

Write each task's section to the output file as soon as you finish it (append), so nothing is lost
if your context fills. Do not reopen images of finished tasks. A task has up to about seventy
images; when you were given only some tasks, judge only those. The harness can run one judge per
task in parallel and concatenate their files (`cat judge_opus.*.md > judge_opus.md`) before
unblinding.

## Output
A markdown file. First line `# Blind judge verdicts: <round folder name> (<model name>)`, then
this block once:

```json
{"judge": "<model name>"}
```

Then per task:

    ## <task id>

    | criterion | Left | Right |
    |---|---|---|
    | Brief fidelity | 5 | 6 |
    | Modelling craft | 6 | 5 |
    | Materials & lighting | 4 | 6 |
    | Composition & camera | 4 | 6 |
    | **Overall** | **5** | **6** |

    **Preference:** Right, medium confidence.

    - **Left:** + ... - ...
    - **Right:** + ... - ...
    - **Follow-up:** what changed between stage 1 and final in each column.

followed by that task's machine-readable block, with the column labels exactly as `blind.md`'s
table headers spell them:

```json
{"task": "<task id>",
 "scores": {"Left":  {"brief_fidelity": 5, "modelling_craft": 6, "materials_lighting": 4, "composition_camera": 4, "overall": 5},
            "Right": {"brief_fidelity": 6, "modelling_craft": 5, "materials_lighting": 6, "composition_camera": 6, "overall": 6}},
 "ranking": ["Right", "Left"],
 "confidence": "medium",
 "reasons": {"Left": ["+ overview · final: ...", "- close: chest · final: ..."], "Right": ["+ ...", "- ..."]},
 "follow_up": "Left: ...; Right: ..."}
```

`harness/evals/fixtures/judge_example.md` is a complete example of the format.

## At the end
Check the format (it reads only your file and `blind.md`):

```
python3 <harness>/evals/judge_merge.py --lint <output file> --blind <round>/blind.md
```

Fix whatever it reports and run it again until it prints `OK`. Then end with one line per task:
`<task>: <columns best first>, <confidence>`, and the output file's path. Do not unblind: the
harness does that (`compare.py --unblind-judge <output file>`).
