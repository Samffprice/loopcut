# Project evals: Loopcut against other tools on whole jobs

The 23 unit tasks in tasks.py ask for one thing each. The five project tasks in projects.py ask
for a whole small job, one per audience the 2026 survey ranked highest, so a comparison between
tools says something a user of that audience would recognise:

| task | audience | what it asks for |
|---|---|---|
| perfume_ad | product visualization + animation + lighting | a finished perfume bottle and box (fixtures/perfume_bottle.blend) kept as they are; dark studio, reflective floor, three lights with a rim, camera, 360 turn, 120 f @ 30 fps, 1080x1080 |
| brand_motion | advertising / brand motion | NOVA logo reveal: extruded text, animates in and settles, contrast, 72 f @ 24 fps, 1920x1080 |
| game_prop | game developers making assets | 1 m crate under 600 tris, UVs, one material, origin at bottom centre, transforms applied |
| archviz_room | architecture / interior | 4x5 m room, 2.7 m ceiling, window opening, sun through it, wood floor, camera inside |
| social_loop | social creators | ring of 8 candy spheres bobbing in a wave, pastel background, seamless 72 f loop |

The product task starts from a real asset rather than a primitive: a Magie Noire bottle and box
with glass, liquid and label materials, stripped out of a full scene into `harness/fixtures/
perfume_bottle.blend` and appended by the task's setup. The tool is asked to build the ad around
it, not to model it, so the comparison is about lighting, staging, camera and animation.

## What is measured

Three layers, because "did it work" and "is it nice" are different questions:

1. **Requirements** (objective, free): each task is a checklist read from the saved scene, such as
   "turns 340-380 degrees over the frame range" or "the opening is 1.2-2 m wide". A partial job
   scores partially. selfcheck.py proves every requirement fails on the start scene and passes on
   the task's reference solution, so a failed check is the tool's fault, not the check's.
2. **Judge** (blind, cheap): grade.py renders the same frames of every arm the same way (EEVEE,
   640 px, the scene's own camera). judge.py shows a vision model the brief and both arms'
   frames, scores five axes 1-5, picks one, then asks again with the arms swapped; a preference
   only counts when both orders agree. Set `LOOPCUT_JUDGE_BASE_URL/_API_KEY/_MODEL` to a model
   that is not one of the contestants; without them it uses Loopcut's own model and says so.
3. **People** (blind, the real answer for taste): blind.md shows the renders with names hidden and
   order shuffled per task; blind_key.json says which was which. Get a few people to fill in
   "Prefer:" before anyone reads the key.

Effort sits next to the scores: seconds, prompts typed, edits made by hand, model steps and
tokens (Loopcut only; the other tool's usage is whatever it reports).

## Running a comparison

```
# 1. Loopcut arm: one real agent turn per task in a windowed Blender (costs tokens, a few minutes)
python3 blender/loopcut/harness/evals/run.py perfume_ad --label "baseline"
#    -> out/evals/<time>/perfume_ad.{json,md,final.blend}

# 2. Other tool: export the start scene, do the task there, save <task>.final.blend next to it
tools/Blender.app/Contents/MacOS/Blender -b --factory-startup --python blender/loopcut/harness/evals/start_scene.py -- out/projects/chatgpt perfume_ad
#    see chatgpt_arm.md for the ChatGPT protocol and the instructions that make it save its own logs

# 3. Grade both the same way, judge, write the report
python3 blender/loopcut/harness/evals/compare.py --arm loopcut=out/evals/<time> --arm chatgpt=out/projects/chatgpt --judge
#    -> out/projects/<time>/comparison.md, blind.md, blind_key.json, judge.json, <arm>/<task>.f*.png
```

Any number of arms works (a second Loopcut run with a different prompt is an arm too); `--judge`
wants exactly two. Every task in projects.py can be exported and compared; start with one, since
the other tool's usage is the expensive part.

## Keeping it honest

- Both arms get the brief verbatim as the first message and nothing else. Extra rules for the
  other tool go in its system-level instructions (a ChatGPT Project), like Loopcut's system prompt.
- Every message typed after the brief is a prompt; every click in Blender is a manual fix. Both
  are reported, not hidden. Twenty minutes is the cap.
- Renders are of the saved scene at fixed frames, never of what the tool showed you. A tool that
  rendered a beautiful frame but saved a broken scene scores as broken.
- One run per arm is an anecdote. When a task matters, run each arm three times and look at the
  spread before believing a difference.
