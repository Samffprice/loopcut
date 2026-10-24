# Auto-rig message inventory

Template expressions below are the exact source strings. Braces identify runtime values.
Preflight refusals append ` No credits used.` Bundle-check refusals append ` Your character has not been changed.`
Quote/approval wording uses the existing cloud UI. Typed anatomy refusals use the plain messages below.

## Experimental rigs (contract 0.9, tool version 3)

The rig card shows this line under the price (`rig_text.QUOTE`), or the clothing warning instead when the local check found a long coat:

> Body bones with automatic weights, and finger bones when it can find them; best on humanoids, others may bend oddly.

The platform's quote carries its own `experimental_notice` ("Best on humanoid characters; others get an experimental rig that may bend oddly."); the card does not show it.

The result card, with the real count:

> Added {n} bones so your character can bend. Loopcut can animate it with keyframes; ready-made motions need a person-shaped character.

Both motion tools refuse locally before quoting:

> Ready-made motions need a person-shaped character, so ask Loopcut to animate this one instead.

Other new messages:

- `Checking the rig and your original character…`
- `This experimental rig has no standard test pose. Ask the agent to pose or animate it.`
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
ordinary rotation_euler keys work) and keyframing/renaming/weight-fixing/deletion
options. It tells the model to keep bone names in code and use plain words with the
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
- `'Select a character rig made by Loopcut.'`
- `'This rig already has animation or controls. Pose it directly or play its animation.'`
- `'There is no test pose to reset.'`
- `'Original pose restored.'`
- `Free test pose: both knees and elbows are bent. To put it back, reset the test pose or undo once; tell the user it is the test pose.` (gentle; `the {side} knee and elbow are` for one side)
- `Free test pose: {what}{, left side only}. Measured: {angles}.{ Left out: {note}.} To put it back, reset the test pose or undo once; tell the user it is the test pose.` then the deformation check (other poses)
- `'Choose Check character, Try a free test pose, Check how it bends, Reset test pose or Remove character rig.'`
- `'Check character (free)'`
- `'Try a free test pose'`
- `'Check how it bends (free)'`
- `'The deformation check needs a standard rig; look at this one in the viewport instead.'`
- `'This rig has no skinned mesh or joints to check.'`
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
- `This character's shape is unusual. It may get an experimental rig.`

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
- `character_rig` description: `Free local character rig checks, test pose and removal. Inspect before auto_rig or before making an unrigged character walk. Inspection never uploads or uses credits. After inspection use loopcut_cloud.auto_rig for an eligible humanoid; it quotes before spending.`
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
