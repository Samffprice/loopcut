# Auto-rig message inventory

Template expressions below are the exact source strings. Braces identify runtime values.
Preflight refusals append ` No credits used.` Bundle-check refusals append ` Your character has not been changed.`
Quote/approval wording uses the existing cloud UI. Typed anatomy refusals use the plain messages below.

## Experimental rigs (contract 0.9, tool version 3)

The rig card shows this line under the price (`rig_text.QUOTE`), or the clothing warning instead when the local check found a long coat:

> Gives your character a skeleton so it can move, with fingers when it can find the hands; made for human shapes, so others may bend in odd places.

The platform's quote carries its own `experimental_notice` (contract 0.9: "Best on humanoid characters; others get an experimental rig that may bend oddly."; later the card's earlier line, "Body bones with automatic weights, and finger bones when it can find them; best on humanoids, others may bend oddly."); the card does not show it. The owner approved the card's words on 2026-10-26 (R7-D3), and the web const takes them in a coordinated contract change.

The result card, with the real count:

> Added {n} bones so your character can bend. Loopcut can animate it with keyframes; ready-made motions need a person-shaped character.

Both motion tools refuse locally before quoting, as a neutral result (no red card; R12):

> Ready-made motions need a person-shaped character, so ask Loopcut to animate this one instead.

with the model's note `No credits used. This rig cannot use ready-made motions: animate it with keyframes instead, for free. Loopcut checks how it moves and bends when your reply ends.` (R13; it was `No credits used. Animate this experimental rig with bone keyframes instead (free), then run character_rig check_deformation: it checks the whole animation.`)

Other new messages:

- `Checking the rig and your original character…`
- `This experimental rig has no built-in test poses: pose or keyframe its bones, then run check_deformation.` (neutral)
- `The experimental rig choice could not be read.`
- `Refresh the tools to use the current auto-rig service.`

Bundle refusal prefixes (followed by ` Your character has not been changed.`):

- `The returned rig type is not supported.`
- `An experimental rig was returned without your agreement.`
- `The returned rig type disagrees with its body-part names.`
- `A standard rig includes unexpected experimental information.`
- `The experimental rig is missing its anatomy check.`
- `The experimental rig report could not be verified.`
- `The returned rig type disagrees with the job result.`

The model's separate continuation note includes tier, count, root, native chains,
motion-tool incompatibility, the XYZ rotation mode every native bone gets (so
ordinary rotation_euler keys work) and its next steps (R13): `Keyframe bones with ordinary Blender tools for a
simple cycle, such as a four-leg walk.`, `Loopcut checks how it moves and bends when your reply ends.`, `Then run
character_rig show: it hides {armature} (its bones draw over the mesh) and sets up the user's view to watch it.`,
`Rename bones, fix weights or delete stray bones only if asked.` A standard rig's note adds `next`: `Before a
ready-made walk or run, tell the user its price: {2} credits a second ({10} for {5} s). When it moves, character_rig
show (free) sets up the user's view to watch it.` (the price from the catalog's estimates; without a catalog, the
show sentence only). It tells the model to keep bone names in code and use plain words with the
user. It does not expand the user card with technical bone data.

## New local messages

### autorig.py

- `'Checking your character — free, on this computer.'`
- `'Uploading the character shape. Materials and textures stay here.'`
- `'Waiting for the rigger. The first run can take a few minutes.'`
- `'Rigging your character…'`
- `'Applying the rig to your original character…'`
- `'Checking how your character bends, including both knees and elbows…'`
- `'Choose between 1 and 64 character mesh objects. Join some parts first if there are more.'`
- `'A selected character part is no longer in this scene. Select the character again.'`
- `'Select the character meshes, including clothes, hair and eyes.'`
- `'Switch to Object Mode before checking or rigging this character.'`
- `'A selected character part has no faces. Select only the character meshes.'`
- `'Select one character with at most 64 mesh objects. Join some of its parts first.'`
- `'Make independent, editable copies of the selected character parts before rigging.'`
- `'This character already has a skeleton. Pose or animate the existing rig, or remove it explicitly before making a new one.'`
- `'This character already has controls with names reserved for this skeleton. Rename those controls, or use the existing rig.'`
- `'This character has zero scale. Restore its scale before rigging.'`
- `'Set a positive scene unit scale before rigging.'`
- `'Object rotation is removed from the upload; the rig returns in the original orientation.'`
- `'Size, position and rotation are included in the upload; the originals stay unchanged.'`
- `'Use the character rigger with its default settings. Facial expressions and hand or foot position controls are not included.'`
- `'Finish or cancel the pending rig jobs before preparing another character.'`
- `'Switch back to the scene that requested this rig.'`
- `'Your character changed while it was being rigged. Nothing was applied. Restore the original state before applying this saved result.'`
- `'The returned rig files describe different bones.'`
- `'A returned rig file is missing a bone.'`
- `'The returned rig files disagree about the skeleton.'`
- `'The returned rig files disagree about how the character should bend.'`
- `'The returned rig cannot be matched exactly to your original shape. Some parts have changed or cannot be told apart safely. Nothing was applied.'`
- `'The returned arms or fingers could not be identified safely. Try a neutral pose with the hands open and separated from the body. Nothing was applied.'`
- `'The returned feet do not clearly agree on which way the body faces. Use a neutral pose with both feet pointing forward. Nothing was applied.'`
- `'The returned skeleton could not be safely converted to a standard humanoid rig. Nothing was applied.'`
- `'Some existing controls use names this rig needs. Rename those controls first.'`
- `'The rig did not bend both knees and elbows correctly. Nothing was applied.'`
- `'Too much of the skeleton sits outside the character. Nothing was applied.'`
- `'The saved copy of this character is missing. Check the character again; nothing was applied.'`
- `'Blender could not create an undo step. Enable Global Undo and try applying the saved result again.'`
- `'The rig changed how your character looks before posing. Nothing was applied.'`
- `'The rig moved your character before posing. Nothing was applied.'`
- `'Some existing controls use names this rig needs. Rename those controls first.'`
- `'The rig changed parts of your original character that should be kept. Nothing was applied.'`
- `'Blender could not save the rig undo step. Nothing was applied.'`
- `Your character is ready: {bone_count} bones, {finger_summary} Your original shapes, materials, texture placement and saved shape changes are kept. Try a free test pose or ask for a walk. Undo once or use Remove rig to go back.` (finger summaries below)
- `'Select a character rig made by Loopcut.'`
- `'A rigged character part is missing. Use Undo to restore it first.'`
- `'A rigged character part is missing. Use Undo to restore it first.'`
- `'Rig removed. Your original character is back.'`
- `'Select a character rig made by Loopcut.'` (neutral, `not_loopcut_rig`)
- `'This rig already has animation or controls, so test poses are off; check_deformation checks its animation.'` (neutral)
- `'There is no test pose to reset.'`
- `'Original pose restored.'`
- `Free test pose: both knees and elbows bent 25°. Shoulders, hips and wrists were not tested; pose stress bends and checks every joint. To put it back, reset the test pose or undo once; tell the user it is the test pose.` (gentle; `the {side} knee and elbow bent 25°` and `were not tested, nor the {other} side` for one side)
- `Free test pose: {what}{, left side only}. Measured: {angles}.{ Left out: {note}.} To put it back, reset the test pose or undo once; tell the user it is the test pose.` then the deformation check (other poses)
- Deformation check (rig_deform.lines; model-facing, in this order):
  - `Worse since the last check: {joint} volume 97% → 54%, stretch 1.99x → 3.77x{, spikes 0 → 5}{; next joint …}; undo the last change.` or `Still worse than the first check of this pose: {joint} {changes}.` or `Better since the last check: {joint} {changes}.` or `No clear change since the last check of this pose.`
  - `Rest skeleton changed since rigging: the {left upper arm} joint moved {5.0} cm{, the {left hand} bone turned {10}°}{ and {n} more}; the skin was weighted to the old joints, so it can tear there.`
  - `Deformation check (free; the posed mesh against rest) at {elbows bent left 60°, right 61° from straight; shoulders turned left 80°, right 80° from rest}: problems at {joints}.` or `…: no spikes or torn edges at the turned joints.`
  - `- {left shoulder}, {turned 80° from rest | bent 60° from straight}: PROBLEM: {n} vertices pulled out into spikes, up to {cm} cm; skin torn: …; skin crushed: … (stretch {x}x, squash {x}x, volume {p}).` (`minor: {n} stray vertices, up to {cm} cm`, or `no spikes or torn edges`)
  - `Not tested, since not turned: {joints}.` and `Turned, but no skin there to test: {joints}.`
  - `Posed by hand, not with a built-in test pose: for test poses use preview with pose stress (free), even when other angles were asked; it bends every joint to known angles and checks them.`
  - `Spikes: vertices over 3 edge lengths off their neighbours with a stretched edge; torn or crushed: edges past 6x or under 0.1x. Volume is the joint's against rest; automatic weights lose some at strong bends. The check cannot see skin pushed through skin or layers poking out, so look at the joint too. Report these numbers and angles; do not call a flagged or untested joint clean.`
  - With no joint turned, instead: `Deformation check (free): nothing was tested, because no joint is turned from rest; this says nothing about how the rig bends, so do not call any joint clean. Pose it first: preview with pose stress (free) bends every joint and checks it.`
  - Regions: shoulder, elbow, wrist, hip, knee and (R12) ankle on each side. On an experimental rig (R12) the regions are body parts (rig_parts: `head` or `upper body`, `body`, `tail`, `{front|second|…|back} {left|right|middle} leg`, `{left|right} arm`, or `leg {k} of {n}` with no front to go by), `joint` reads `part` throughout, the rest-joint line says `a {front left leg} joint moved {cm} cm` / `a {tail} bone turned {n}°`, and the nothing-tested line ends `Pose or animate it first, then check again.`
  - On a rig with an animation (R12), first: `Worst at frame {59}: {left ankle}, {skin torn: edges stretched to 7.2x}.` (or `…, stretch {x}x, squash {x}x, no spikes or torn edges.`), then `Worse since the last check of this animation: …; undo the last change.` (only when no flagged joint was mended; else `Since the last check of this animation, better: …; worse: ….`; at most four items a list, then `and N more`) / `Still worse than the first check of this animation: ….` / `Better since the last check of this animation: ….` / `No clear change since the last check of this animation.`, then `Deformation check over the animation (free; {14} frames from {1} to {120}, each against rest): {problems at …. | no spikes or torn edges at the turned joints.}`, joint lines `- {joint}, worst at frame {f}, {angle}: …`, `Not tested, since never turned: ….`, and the footer ending ` It checked {14} frames, not every one, so look at the worst one too.` With nothing turned: `Deformation check (free): nothing was tested, because no joint is turned from rest in the {14} frames checked from {1} to {120}; this says nothing about how the rig bends, so do not call any joint clean.`
- `'Choose Check character, Try a free test pose, Check how it bends, Reset test pose, Watch it, Add a gesture or Remove character rig.'` (R13)
- `'Check character (free)'`
- `'Try a free test pose'`
- `'Check how it bends (free)'`
- `'This rig has no skinned mesh to check.'`, `'This rig has no joints to check.'`
- Inspect on a character that already has a skeleton (R12, neutral; JSON `rigged`, `armature`, `made_by_loopcut`, `tier`, `bone_count`, `meshes`, `animated`, `message`; R13 plain words): `Already rigged by Loopcut ({33} bones, not a person rig, so no ready-made walks): animate it with keyframes, for free; check_deformation checks {the current pose | the whole animation}. It needs no new rig.`, `Already rigged by Loopcut (standard, {34} bones): ready-made walks and runs work ({2 credits a second (10 for 5 s)}{; balance {4} credits}; tell the user first); free gestures (wave, raise_arm, point); preview pose stress tests poses; check_deformation checks …. It needs no new rig.`, `Already has its own skeleton ({n} bones): {ready-made motions can target it | animate it with bone keyframes}; Loopcut's test poses and bend check need a Loopcut rig. It needs no new rig.`
- Inspect's `price_note` (R12, from the catalog's estimates): `Before rigging, tell the user the total: the rig is {20} credits and a ready-made walk or run is {2} credits a second ({10} for {5} s).` R13: for a shape whose hints point to an experimental rig (any hint but `leg_gap`): `Before rigging, tell the user the total: the rig is {20} credits, and Loopcut then animates a shape like this with keyframes, for free (ready-made walks need a person shape).` Then, with the signed-in balance: ` Balance: {34} credits, enough for {the rig and a 5 s walk (30 in all) | the rig}.`, ` Balance: {24} credits, enough for the rig ({20}) but not a {5} s walk too ({30} in all).` or ` Balance: {4} credits, not enough for {the rig | a walk} ({20}).`
- run_python results (tools.py, R12; R13 wording, since the turn's end now runs the check): `Weights changed on {Mesh}: Loopcut checks how it bends when your reply ends.` (`Weights changed on {Mesh}: pose its bones and run character_rig check_deformation.` on a still experimental rig) and `Animation changed on {Character Rig}: Loopcut checks how it moves and bends when your reply ends.` (once per step, Loopcut-made rigs only). Animation checks set frames from the action's curves, or with frame_set (then put back) when the rig has constraints, drivers, playing NLA, action blending or skipped curves, or its meshes or shape keys are animated.
- Motion result (motion_retarget.py, R14 plain words; R7-B2: Fast repeats it to the user word for word). `cloud_jobs.result_summary` wraps it as `Applied motion: {…}. Job {job_id}; provenance recorded.` Its sentences, in order:
  - `Motion '{Motion: A man walks forward…}' now plays {on '{Character Rig}' | on a new skeleton '{Motion skeleton}' with a simple body '{Motion body}', because no rig was chosen} over frames {1}–{120} ({5.0} s at {24} fps)`
  - travel: `It moves {6.4} m {forward | backward | to its left | to its right} from where it starts` (where it ends up, past 1 m, against the way the character faced), `It moves up to {2.3} m from where it starts and ends near it`, `It stays within 1 m of where it starts`, or `It stays on the spot` (in_place)
  - `It replaces the earlier motion '{Motion: …}', which is kept, switched off, in the Nonlinear Animation editor`
  - `The scene now plays frames {1}–{120}` (the only animated object), or `The scene plays frames {1}–{250}, so part of this motion is outside it`
  - fingers: `Its fingers hold a relaxed pose; this motion does not move them{ (keys already on {n} finger bones were left as they are)}` or `This motion does not move the fingers{; keys already on …}`
  - motion.json warnings in words (`WARNINGS`): `fingers_static`, `resampled_from_*` and `downscaled_to_*` are dropped; `The prompt was made as {2} parts, one after another`, `The person is not in every frame of the video, so those frames are guessed`, `The video shows more than one person; it follows the largest`, `Part of the body is outside the video, so that part is guessed`; any other code as it came; then the retarget's own warnings
  - on a standard Loopcut rig: `Free on this rig: a wave, point or raised arm over this motion (character_rig gesture), and a side camera that follows it (character_rig show)`
  - Gone: `({Mixamo}, {22} bones mapped)`, `, resampled from {30}`, `root motion {in place | in world}`, `its previous action '…' is kept in a muted NLA track`, `scene frame range set to …`, `fingers set to a relaxed pose; the model doesn't animate fingers`, and R13's `It travels {5.5} m from where it starts; character_rig show (free) sets up the user's view to follow it`. That travel figure was always two leg lengths (a renamed variable), not the travel: ar-beginner-4's walk was told 1.6 m for about 6.4.
- A gesture-like motion_from_text on a standard Loopcut rig that already moves (R14, R7-B1; `autorig.free_gesture`, from `cloud_jobs.prepare_inputs`, before any quote, card or charge), a neutral result: `Not bought, no credits used: {a wave | pointing | a raised arm} on {Character Rig} is free with character_rig gesture (gesture {wave}{, side {right}}), which layers it over its current motion (frames {1}–{120}) and keeps that motion{; frames [{121}, {169}], after the motion ends, make it stop and then {wave | point | raise the arm}}. motion_from_text would replace that motion with a new one. Only if the user wants a new full-body motion in place of it, make the same call again; it is charged then.` (no stop hint on a looping animation). Its card's line: `Not bought, no credits used: {a wave} on {Character Rig} is free.` The same call again in the same user turn (that rig) is bought as usual.
- capture_viewport (not `angle: user`) and inspect_scene, while a visible Loopcut rig is drawn in front in a 3D view that draws bones (R14, R7-D1; the model's note, not the card): `Not in this picture: {Character Rig's bones | the bones of {A} and {B}}, which the user's viewport draws over the body as {grey sticks | grey bone shapes}; they never render.`
- `character_rig show` (R13, free): `Ready to watch (free): {Character Rig} is hidden so its bones do not draw over the character; Follow Camera is the scene camera and {follows it from the {left} side over frames {1}–{120} | watches it from the {left} side}; {the user's 3D view now looks through it | no 3D view was open to switch}. The user only has to play the animation. To pose it by hand later, show {Character Rig} again in the Outliner.` Card: `Set up the view to watch it (free)`. Refusal: `This rig has no skinned mesh to show.`
- `character_rig gesture` (R13, free; standard rigs): `Free, no credits used: {Character Rig} {waves | raises the arm | points forward} with the {right} arm over frames {72}–{120} ({2.0} s), easing in and out over {8} frames; its animation is unchanged elsewhere.{ The scene now ends at frame {170}.} character_rig show sets up the view to watch it.` Card: `Add a gesture (free)`. Neutral refusals: `Built-in gestures need a standard (person-shaped) rig; keyframe this rig's bones instead.` (experimental_rig), `This rig has controls or animation layers, so built-in gestures are off; keyframe its arm instead.` (already_animated). Others: `A test pose is on: reset it first, then add the gesture.`, `Choose a gesture: wave, raise_arm, point.`, `frames: [first, last], whole numbers.`, `A gesture needs at least 12 frames.`, `Choose the right or left arm.`, `This rig is missing the arm bones a gesture needs.`
- The turn-end rig check (rig_gate.py, R13), after a turn that changed a Loopcut rig's animation or weights. The user's stamp, in the reply's muted footer: `Verified by Loopcut on this scene: {1} of {2} checks.` then one line per check, failures first: `{The animation | {Rig}'s animation} {keeps moving from frame {1} to {120}{ (Loopcut refreshed it, so Blender now plays its loop)} | moves only in frames {1}–{25} of {1}–{120}, then stands still to frame {120} | stands still from frame {1} to {51}, then moves to frame {120} | stands still from frame {a} to {b}{ and from frame …}{ and {n} more times} of {1}–{120}}.`, `Nothing moves from frame {1} to {120}.`, and after Loopcut's own gesture or ready-made motion (a passing line: the hold is the scene's range) `The animation {moves in frames {1}–{49}, then holds still to frame {250} | holds still from frame …}.`, `It bends without tearing or spikes in the {16} frames checked.`, `In a test pose with arms up and elbows and knees bent, it bends without tearing or spikes.`, each failure as `Failed: {…}`, a bend failure as `Failed: the {front right leg} {pulls into spikes | tears | is crushed} at frame {87}{; the {…}}{; and {n} more places}.` The model's one message a turn: `[Loopcut] Loopcut checked the rig before your reply could end. Not working yet:` then `- {Rig}: {The animation …}.{ Its curves do move there, but Blender does not play them.}` or `- {Rig}: Worst at frame …` (with up to three PROBLEM joint lines), then `Fix it and check again, or tell the user plainly what still does not work; do not say it works. Loopcut sends this once per turn.` Its panel notice: `Loopcut checked the rig: {…}. Asked the assistant to fix it.` R14 (R7-D2/B2): a stamp with an animated rig ends `Press Space over the 3D view to play it.`, and its reply gets a `Watch it` button under the footer (`verification_action`, only `loopcut.rig_show` on a Loopcut armature in the file; tip `Free: hides the bones and follows the character with a side camera.`). The click runs character_rig show and shows for a moment `Watching {Character Rig} through Follow Camera, with its bones hidden. Press Space over the 3D view to play it.`, or show's refusal.
- Pose refusals (rig_pose.py): `Choose a test pose: …`, `This rig is missing the arm or leg bones a test pose needs.`, `This rig has no finger bones that can close, so there is no fist pose.`, `This rig has no shoulder bones to raise; the arms-up pose still works.`
- `'Remove character rig'`
- `'Reset test pose'`
- `'Character rig'` (fallback card title)

### rig_inspect.py

- `message + ' No credits used.'`
- `This character has {count} triangles; the limit is 100,000. Make a lower-detail copy and rig that copy. Moving its rig back to the detailed character is not available yet.`
- `This character has more than 300,000 points. Make a lower-detail copy and rig that copy.`
- `Select a character mesh with faces (at most 300,000 points).`
- `The mesh contains unusable point positions. Repair the mesh first.`
- `The mesh contains invalid faces. Repair the mesh first.`
- `Several complete characters are selected. Select just one character, including its clothes, hair and eyes.`
- `Ready for a rigging quote: {count} triangles. Checking was free.`
- `This shape may not get a person rig. Then there are no ready-made walks, and Loopcut animates it with keyframes instead, for free.` (R13, plain words, and no longer for a hidden leg gap alone; R12: `This unusual shape may get an experimental rig: no ready-made motions, so Loopcut animates it with bone keyframes, and the bend check works on it.`)

The last line is advisory: it follows the free-check message and appears with that
character's price on its quote card. Leg gaps, head shape, arm separation and body
proportions cannot block admission. The returned skeleton still has to pass the
worker's anatomy checks before settlement. Invalid mesh data, selection/size
limits and existing rigs remain blockers. Refund receipts still govern local
apply failures; their window, cap and unconfirmed outcomes are stated below.

### rig_bundle.py

- `message + ' Your character has not been changed.'`
- `'A returned rig file has missing or unreadable information.'`
- `'This rig result uses an unsupported format.'`
- `'The result belongs to another rig job.'`
- `'The result does not match the character that was submitted.'`
- `'The returned rig uses a size, direction or skeleton format this version cannot use.'`
- `'The rig is missing information needed to check where it came from.'`
- `'The rig includes a reliability rating this version cannot verify.'`
- `'The rig includes unreadable warnings.'`
- `'The cloud rig did not pass its checks.'`
- `'The cloud rig contains failed or missing checks.'`
- `'One of the cloud rig checks could not be read.'`
- `'The cloud rig report could not be read.'`
- `'The skeleton has too many or too few bones.'`
- `'The skeleton has invalid or duplicate bone names.'`
- `'A bone has an unreadable body-part name.'`
- `'The bones are not connected correctly.'`
- `'The skeleton contains unusable bone positions.'`
- `'A bone has an unusable starting position or direction.'`
- `'A bone has an unusable starting size.'`
- `'The bones must form one connected skeleton.'`
- `'The rig uses body-part names this version cannot use.'`
- `'The rig has unreadable or repeated body-part names.'`
- `'The returned body-part names disagree with the checks on this computer.'`
- `'The rig is missing a character part.'`
- `'The returned shape does not match your original character.'`
- `'The returned shape could not be matched exactly to your original character.'`
- `'Part of the character is not attached to the skeleton.'`
- `'A connection between the character and skeleton could not be read.'`
- `'Part of the character has unusable connections to the skeleton.'`
- `'The strength of the connections to the skeleton does not add up correctly.'`
- `'Some returned rig files are missing.'`
- `'A returned rig file has an unexpected format, size or contents.'`
- `'A downloaded rig file is missing.'`
- `'The downloaded rig is damaged or changed.'`

## Cloud result and agent surface additions

- Geometry export limit: `The geometry upload exceeds 50 MB. Use a lower-detail copy.`
- `'The returned rig files are too large or include unexpected files'`
- Geometry inspection also returns `facing_basis`: `'foot surface extent; confirmed against returned foot joints'`.

- The result card uses the success message directly. The existing card retains job metadata, artifacts and the actual credit receipt.
- Unexpected local apply failure: `The returned rig could not be verified or applied. Your character has not been changed.`
- Charged local failure suffix: ` The cloud job used {charged} credits; no refund has been confirmed.`
- `character_rig` description: `Free local inspect, standard-rig test poses (preview pose stress, even for other angles; not run_python), reset, bend check (any Loopcut rig; on an animated one, its whole animation) and rig removal. Inspect before loopcut_cloud.auto_rig (it quotes first). Experimental rigs: bone keyframes, no motion tools.`
- `targets` description: `Mesh names to inspect; defaults to selection.`
- `rig` description: `Rig name for preview, reset or remove; defaults to selection.`
- Cloud descriptor guidance appended by the client: ` First use character_rig(action='inspect') on the character. Loopcut checks again locally and generates geometry-only files; materials and textures stay here. For 'make him walk' on an unrigged character, rig it first, then use motion_from_text on the returned rig. Uncertain shapes receive a warning on the quote; the server checks the returned skeleton and refuses unsupported bodies without a charge. Never promise a refund for a local apply failure unless the service confirms it.`
- Low-level file and geometry details remain diagnostic logs. Unclear hands, facing direction and mismatched shapes have the specific plain messages above. All local failures include actual charge information when charged.

## Local-failure reporting and refund receipts

`{SUPPORT}` is always `https://loopcut.org/beta/report-a-bug`; `{job_id}` is the
original durable job ID. Server message text is not echoed. Existing successful
settlement card text becomes `No charge` after a confirmed full refund.

- `The returned rig files failed verification. Your character has not been changed.`
- `The rig could not be used. Reporting the failure; a refund is not confirmed yet.`
- `Reporting that the rig could not be added. A refund is not confirmed yet.`
- `The failed rig did not change your character.`
- `The service confirmed {amount} credits returned.`
- `The automatic refund window has passed.` followed by ` The failure was logged for review; no refund is approved yet. Support: {SUPPORT}`
- `The automatic refund limit has been reached.` followed by the same review/support suffix.
- ` The balance refresh is still pending.` (receipt outcome remains authoritative)
- `The report that the rig could not be added has not been confirmed. No refund is confirmed. Check job {job_id} again to retry, or contact {SUPPORT}`
- `The refund report could not be confirmed. Support: {SUPPORT}` (unexpected report interruption fallback)

The initial local failure suffix records the known charge, while the asynchronous
report is pending. The agent's completed result uses the confirmed receipt or
explicit uncertainty. Review is not refund approval. The original error remains
visible before the outcome. No new quote or spending approval is requested.

## Success template

`Your character is ready: {bone_count} bones, {finger_summary}`

Finger summary is exactly one of:

- `including fingers.` (five fingers on each hand)
- `without finger bones.`
- `with {count} finger on each hand.` (one)
- `with {count} fingers on each hand.` (two through four)
- `with fingers on each hand: {left} on the left and {right} on the right.` (different counts)

` Your original shapes, materials, texture placement and saved shape changes are kept. Try a free test pose or ask for a walk. Undo once or use Remove rig to go back.`


## Worker anatomy refusals

Prefix: `The rigger couldn't build a usable body rig for this character.`
One reason follows:

- `Its finger layout could not be identified safely.`
- `It was not clear which way the character faces.`
- `Its hips and legs did not form a supported body rig.`
- `It needs a complete body, including arms and legs.`
- `Its body layout is not supported by the human-body rigger.` (also the safe unknown-reason fallback)

With confirmed returned credits or a verified zero hold/charge, the ending is
`You weren't charged.` Otherwise it is
`The credit return has not been confirmed yet.` Internal worker text is not echoed.
