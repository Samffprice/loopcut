# Project evals: Loopcut against other tools on whole jobs

The 23 unit tasks in tasks.py ask for one thing each. Above them sit two tiers of whole jobs, one
per audience the 2026 survey ranked highest, so a comparison between tools says something a user
of that audience would recognise.

**Showcase tier (showcase.py): two turns each, demo-grade.** The first brief is graded on the
scene saved after it, then a follow-up edit is sent and graded against a snapshot of that scene,
so "keep the cameras" and "faster than before" are measured. Every brief asks for a final-quality
Cycles setup and is graded on it. Both Loopcut and the Codex arm on the single-turn perfume brief
scored 11/11 first try, which is why this tier exists.

| task | audience | first brief | follow-up |
|---|---|---|---|
| perfume_ad | product visualization | perfume bottle and box (fixtures/perfume_bottle.blend) kept; polished stone, warm spot, amber gradient, three cameras (hero, close-up, overhead), Cycles | bright pastel pink, soft daylight, same cameras |
| product_reveal | product animation / ads | CC0 vintage camera (fixtures/camera_product.blend) kept; 8 s reveal: close-up of the lens pulling back as it rotates to a centred hero, dark studio, moving rim lights, smooth camera | energetic: faster middle, electric blue lights |
| asset_pack | game assets | 3 potions, chest, shelf, sign, barrel; flat palette, named, origins at base, under 2000 tris each, arranged, asset_pack.glb | ice variant, sizes and origins kept, re-exported |
| dungeon_modules | game environments | floor, wall, doorway, pillar modules on a 2 m grid as linked duplicates; enclosed 4 x 4 room, one doorway, chest, glowing crystals, walkthrough camera, dungeon.glb | a connected treasure room, original untouched |
| exploded_view | website / scroll animations | camera product: assemble, rotate, explode, reassemble; fixed camera; product in the right 60%; preview PNG sequence | mirrored composition, sequence re-rendered |

**Regression tier (projects.py): one turn each, cheap.**

| task | audience | what it asks for |
|---|---|---|
| brand_motion | advertising / brand motion | NOVA logo reveal: extruded text, animates in and settles, contrast, 72 f @ 24 fps, 1920x1080 |
| game_prop | game developers making assets | 1 m crate under 600 tris, UVs, one material, origin at bottom centre, transforms applied |
| archviz_room | architecture / interior | 4x5 m room, 2.7 m ceiling, window opening, sun through it, wood floor, camera inside |
| social_loop | social creators | ring of 8 candy spheres bobbing in a wave, pastel background, seamless 72 f loop |

Product tasks start from real assets rather than primitives: the Magie Noire bottle and box
(glass, liquid and label materials, stripped out of a full scene) and Poly Haven's CC0 Camera_01
split into lens, lens body, body and strap, both under `harness/fixtures/` and appended by the
task's setup. The tool is asked to build around the asset, not to model it, so the comparison is
about lighting, staging, camera and animation.

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
# 1. Loopcut arm: one or two real agent turns per task in a windowed Blender (costs tokens, minutes)
python3 blender/loopcut/harness/evals/run.py perfume_ad --label "baseline" [--record]
#    -> out/evals/<time>/perfume_ad.{json,md,work.blend,stage1.blend,final.blend,conversation/,mov}

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

## Replaying a run

Every Loopcut run keeps enough to re-enact it: the start scene (`<task>.work.blend`), the ordered
steps with the exact code the model ran (`steps` in `<task>.json`), the full transcript with
captures (`<task>.md`), and the raw conversation folder. `replay.py` opens the start scene in a
windowed Blender and applies the steps one by one with a pause, framing the viewport between
them, and `--stages` saves a .blend after each step so any moment can be rendered at full quality:

```
tools/Blender.app/Contents/MacOS/Blender --python blender/loopcut/harness/evals/replay.py -- out/evals/<time> perfume_ad --pause 2 --stages
```

That is a re-enactment of the scene's history, not the chat: for a demo video of the product
itself, record the real run with `run.py --record` (macOS screencapture; the terminal needs
Screen Recording permission in System Settings > Privacy & Security), or record the replay with
the transcript alongside. The manual arm's `<task>.steps/*.py` scripts replay with `--scripts`.

## Keeping it honest

- Both arms get the brief verbatim as the first message and nothing else. Extra rules for the
  other tool go in its system-level instructions (a ChatGPT Project), like Loopcut's system prompt.
- Every message typed after the brief is a prompt; every click in Blender is a manual fix. Both
  are reported, not hidden. Twenty minutes is the cap.
- Renders are of the saved scene at fixed frames, never of what the tool showed you. A tool that
  rendered a beautiful frame but saved a broken scene scores as broken.
- One run per arm is an anecdote. When a task matters, run each arm three times and look at the
  spread before believing a difference.
