"""Loopcut's own check of a rig before the turn that changed it ends (R6-D1, R6-B1, R6-D2).

Round 6: run_python's "Animation changed on Character Rig: run character_rig check_deformation before telling the
user it works." fired on 9+ steps over two runs and the free plan's model called check_deformation 0 times; it
"verified" a gallop that froze after frame 25 by comparing frames 1 and 25. Facts the model must act on are not
acted on, so Loopcut acts: the code paths that change a Loopcut rig's animation or weights mark it on the turn's
user message (mark: autorig.rig_changes after run_python as 'animation' or 'weights', motion_retarget.apply as
'motion', character_rig gesture as 'gesture'; a long hold fails only after the model's own keys), and the
turn's last reply runs, on the main thread, the motion coverage (autorig.coverage) and the bend check over the
animation (autorig.measure), or, for a still standard rig whose weights changed, the bend check in pose stress. An
animation whose curves move where the copy Blender plays does not (ar-dog-4's Cycles modifiers, added a step after
the keys) is refreshed by Loopcut first and read again, so that freeze never reaches the model.

agent._final_reply calls finish_gate once per final reply, after the spec's own gate and before contact's:
- a problem sends the model back once a turn, from completion_guard's nudge budget (RIG_CHECKED), with the
  findings in plain words, unless the turn cannot go on, the reply stops honestly or a card waits for the user;
- otherwise the reply gets a stamp in the runtime's "Verified by Loopcut on this scene" block, stating any problem
  plainly.
It never blocks: any error, a helper's loop, a Stop, or a turn that changed no Loopcut rig gives None and nothing; nor
does a final reply with nothing marked since the turn's stamp (contact sent the turn on after it).
Rigs a helper animated in its own process and the orchestrator merged are not marked (not seen in round 6).

R7-D2/B2: character_rig show got 0 calls over both round 7 runs and all of round 6, with result facts pointing at it,
and both users hunted for a character that walked out of the front view. So the stamp of an animated rig says how to
play it, and its reply carries a "Watch it" button (verification_action) that runs show itself. A saved conversation
is untrusted when it loads: the footer runs only an op in ACTIONS, on a rig that is a Loopcut armature in the file.
"""
import json

from . import rig_deform, rig_motion

# On the turn's user message, as JSON text ({rig name: ['animation', 'weights']}): conversations.load refuses a marker
# that is not a string, number or bool.
CHANGED = 'loopcut_rig_changed'
# 'animation': the model's own keys (run_python); 'gesture' and 'motion': Loopcut's built-in gestures and ready-made
# motions, whose hold after their own length is deliberate (a wave on a still rig, a clip in a longer scene).
KINDS = ('animation', 'weights', 'gesture', 'motion')
MARKS = 'loopcut_rig_marks'      # How many marks the turn has had,
STAMPED = 'loopcut_rig_stamped'  # and that count when its last stamp went on a reply: nothing new, no second stamp.
HEAD = '{head}: {passed} of {total} checks.'  # completion_guard._UI_RECORD reads it.
PLAY = 'Press Space over the 3D view to play it.'
ACTIONS = {'loopcut.rig_show': 'Watch it'}  # The only ops a reply's footer button may run, and their labels.
ACTION = 'verification_action'  # On the reply item: {'label', 'op', 'rig'}.


def _request(messages):
    from . import context
    return next((m for m in reversed(messages or ()) if context.is_turn_start(m)), None)


def changed(request):
    """{rig name: kinds} marked on the turn's message; {} for none or a damaged mark."""
    try:
        found = json.loads((request or {}).get(CHANGED) or '{}')
    except (TypeError, ValueError):
        return {}
    return {n: [k for k in v if k in KINDS] for n, v in found.items() if isinstance(n, str) and isinstance(v, list)} \
        if isinstance(found, dict) else {}


def mark(names, kind):
    """Main thread, from the code that changed them: these rigs' animation or weights changed in the running turn.
    Never raises; nothing outside a turn."""
    try:
        from . import state
        session = state.session()
        request = _request(session.get('messages')) if session.get('busy') else None
        if request is None or kind not in KINDS:
            return
        found = changed(request)
        for name in names:
            if isinstance(name, str) and name:
                kinds = found.setdefault(name, [])
                if kind not in kinds:
                    kinds.append(kind)
        if found:
            request[CHANGED] = json.dumps(found, sort_keys=True)
            request[MARKS] = request.get(MARKS, 0) + 1
    except Exception as ex:  # A mark is a hint for the turn's end, never a reason for the change to fail.
        print(f'Loopcut: rig change not marked: {type(ex).__name__}')


def _bend(rows, flagged, limit):
    """'the front right leg pulls into spikes at frame 87': plain words for the worst flagged regions."""
    def what(row):
        found = rig_deform.problems(row, limit)[0]
        if any('spikes' in f for f in found):
            return 'pulls into spikes'
        return 'tears' if any('torn' in f for f in found) else 'is crushed'
    worst = sorted(flagged, key=rig_deform.severity, reverse=True)
    named = [f"the {row['name']} {what(row)}" + (f" at frame {row['frame']}" if 'frame' in row else '')
             for row in worst[:2]]
    more = len(worst) - 2
    return '; '.join(named) + (f'; and {more} more {"place" if more == 1 else "places"}' if more > 0 else '')


def verdict(arm, kinds):
    """Main thread. {'rig', 'checks': [{'ok', 'line' (the user's), 'detail' (the model's)}], 'unplayed'} for one rig,
    or None when there is nothing to check."""
    from . import autorig
    if arm is None or arm.type != 'ARMATURE' or autorig._KEY not in arm:
        return None
    action, channels = autorig._channels(arm)
    span = autorig._span(action, channels) if action is not None else None
    checks, unplayed = [], False
    if span:
        found, refreshed = autorig.coverage(arm), False
        if found is not None and found.get('unplayed'):  # Blender plays an older copy: Loopcut updates it itself.
            import bpy
            autorig.refresh_animation(arm)
            bpy.context.view_layer.update()
            found, refreshed = autorig.coverage(arm) or found, True
        if found is not None:
            # A long hold counts against the model's own keys only (ar-dog-4's frozen loop); after Loopcut's own gesture or
            # ready-made motion it is the scene's range. Never moving, or Blender not playing the curves, always counts.
            deliberate = 'animation' not in kinds
            unplayed = bool(found.get('unplayed'))
            ok = not rig_motion.problem(found, deliberate) and not unplayed
            line = rig_motion.line(found, deliberate and ok) + (REFRESHED if refreshed and ok else '')
            checks.append({'ok': ok, 'kind': 'motion', 'line': line, 'detail': line,
                           'coverage': {k: found.get(k) for k in ('first', 'last', 'still', 'longest')}})
        measured = autorig.measure(arm)
        pose = None
    elif 'weights' in kinds:
        measured, pose = autorig.stress_measure(arm), 'in a test pose with arms up and elbows and knees bent'
    else:
        measured = pose = None
    if measured is not None and measured['tested']:
        animation = measured['animation']
        limit = rig_deform.ANIMATION_SPIKES if animation else rig_deform.SPIKES
        flagged = measured['flagged']
        where = (f"in the {animation['frames']} frames checked" if animation else pose)
        line = (_bend(measured['rows'], flagged, limit) if flagged
                else f'it bends without tearing or spikes {where}')
        lines = measured['text'].split('\n')
        detail = [lines[0]] + [l for l in lines if l.startswith('- ') and 'PROBLEM' in l][:3] if flagged else lines[:1]
        checks.append({'ok': not flagged, 'kind': 'bend', 'line': line if not flagged or animation else f'{pose}, {line}',
                       'detail': '\n'.join(detail)})
    return {'rig': arm.name, 'checks': checks, 'unplayed': unplayed, 'animated': bool(span),
            'watch': bool(span) and _shows(arm)} if checks else None


def _shows(arm):
    """Whether character_rig show can frame this rig: it has a skinned mesh."""
    from . import autorig
    try:
        return bool(autorig._rig_meshes(arm))
    except Exception:  # A damaged rig record: no button, the stamp as before.
        return False


def check(changed):
    """Main thread: verdict() for every marked rig still in the file."""
    import bpy
    out = []
    for name, kinds in changed.items():
        try:
            found = verdict(bpy.data.objects.get(name), kinds)
        except Exception as ex:  # Fail open per rig: the turn still ends, unchecked.
            print(f'Loopcut: rig check of {name} skipped: {type(ex).__name__}: {ex}')
            continue
        if found:
            out.append(found)
    return out


def _sentence(subject, phrase):
    """'The animation moves only…' from 'it moves only…'; 'Failed: …' stays with its own capital."""
    if phrase.startswith('it '):
        phrase = subject + phrase[2:]
    return phrase[0].upper() + phrase[1:] + '.'


def stamp(verdicts, head='Verified by Loopcut on this scene'):
    """The user's block: the runtime's head (orchestrator.block_head), then one plain line per check, failures first."""
    rows = [(v, c) for v in verdicts for c in v['checks']]
    lines = [HEAD.format(head=head, passed=sum(c['ok'] for _, c in rows), total=len(rows))]
    for v, c in sorted(rows, key=lambda row: row[1]['ok']):
        subject = ('the animation' if len(verdicts) == 1 else f"{v['rig']}'s animation") if c['kind'] == 'motion' \
            else ('it' if len(verdicts) == 1 else v['rig'])
        text = _sentence(subject, c['line'])
        lines.append(text if c['ok'] else 'Failed: ' + text[0].lower() + text[1:])
    if any(v.get('animated') for v in verdicts):
        lines.append(PLAY)
    return '\n'.join(lines)


# ar-dog-4: Cycles modifiers added a step after the keys never reached the copy Blender plays (rig_motion).
REFRESHED = ' (Loopcut refreshed it, so Blender now plays its loop)'
UNPLAYED = ' Its curves do move there, but Blender does not play them.'


def bounce(verdicts):
    """The model's one message: what failed, in words it can pass on, and what to do."""
    lines = ['[Loopcut] Loopcut checked the rig before your reply could end. Not working yet:']
    for v in verdicts:
        for c in v['checks']:
            if not c['ok']:
                extra = UNPLAYED if c['kind'] == 'motion' and v['unplayed'] else ''
                lines.append(f"- {v['rig']}: " + _sentence('the animation', c['detail'].split('\n')[0]) + extra
                             if c['kind'] == 'motion' else f"- {v['rig']}: " + c['detail'].replace('\n', '\n  '))
    lines.append('Fix it and check again, or tell the user plainly what still does not work; do not say it works. '
                 'Loopcut sends this once per turn.')
    return '\n'.join(lines)


def notice(verdicts):
    """The panel's line while the model goes back: what Loopcut found, plainly."""
    failed = [_sentence('the animation' if c['kind'] == 'motion' else 'it', c['line'])[:-1]
              for v in verdicts for c in v['checks'] if not c['ok']]
    return 'Loopcut checked the rig: ' + '; '.join(f[0].lower() + f[1:] for f in failed) + \
        '. Asked the assistant to fix it.'


def finish_gate(session, turn, completion, role, run_on_main):
    """agent._final_reply's rig check (module docstring). run_on_main(fn) runs fn on the main thread and returns
    its result. Returns the text that sends the model back, or None."""
    if role == 'worker' or turn.cancel.is_set():
        return None
    try:
        request = _request(session.get('messages'))
        marked = changed(request)
        if not marked or request.get(STAMPED) is not None and request.get(STAMPED) == request.get(MARKS):
            return None  # Nothing marked, or nothing new since this turn's stamp (contact sent the turn on after it).
        verdicts = run_on_main(lambda: check(marked))
        if turn.cancel.is_set() or not verdicts:
            return None
        from . import completion_guard, orchestrator, state
        failed = any(not c['ok'] for v in verdicts for c in v['checks'])
        if failed and _may_bounce(session, turn, request, completion):
            request[completion_guard.RIG_CHECKED] = True
            session['items'].append(state.item_notice(notice(verdicts)))
            print('Loopcut: the rig check found a problem; sent back once.')
            return bounce(verdicts)
        orchestrator._append_to_reply(session, completion,
                                      stamp(verdicts, orchestrator.block_head(session, 'Rig checked this turn')))
        request[STAMPED] = request.get(MARKS, 0)
        watched = next((v['rig'] for v in verdicts if v.get('watch')), None)
        reply = getattr(turn, 'pending_reply', None)
        if reply is None:
            reply = session['items'][-1] if session['items'] else {}
        if watched and reply.get('kind') == 'assistant' and reply.get('verification'):
            reply[ACTION] = {'label': ACTIONS['loopcut.rig_show'], 'op': 'loopcut.rig_show', 'rig': watched}
    except Exception as ex:  # Never in the way of the reply.
        print(f'Loopcut: rig check not run: {type(ex).__name__}: {ex}')
    return None


def _may_bounce(session, turn, request, completion):
    from . import agent, completion_guard
    if (turn.outcome != 'running' or request.get(completion_guard.RIG_CHECKED)
            or completion_guard.nudges_left(request) <= 0
            or completion_guard.stops_honestly(getattr(completion, 'text', '') or '')):
        return False
    return not any(item.get('status') == 'awaiting' or (item.get('kind') == 'offer' and not item.get('resolved'))
                   for item in agent._turn_items(session, request))


def footer_action(item, rigs):
    """(label, rig) of a reply's footer button, or None. No bpy (ui.layout draws it): only an op in ACTIONS, with
    the label from ACTIONS, never the item's, for a rig named in rigs (watchable's, from ui.host)."""
    action = item.get(ACTION) if isinstance(item, dict) else None
    if not isinstance(action, dict) or not isinstance(action.get('op'), str) or action['op'] not in ACTIONS:
        return None
    rig = action.get('rig')
    return (ACTIONS[action['op']], rig) if isinstance(rig, str) and rig in rigs else None


def watchable(session):
    """Main thread: the rigs the session's footer buttons name that are Loopcut armatures in the file now. The panel
    draws with it, so it never raises."""
    out = set()
    try:
        import bpy
        from . import autorig
        for item in session.get('items') or ():
            action = item.get(ACTION) if isinstance(item, dict) else None
            rig = action.get('rig') if isinstance(action, dict) else None
            ob = bpy.data.objects.get(rig) if isinstance(rig, str) else None
            if ob is not None and ob.type == 'ARMATURE' and autorig._KEY in ob:
                out.add(rig)
    except Exception:  # No button, never a broken panel.
        return frozenset()
    return frozenset(out)


def run_footer(session, index):
    """Main thread, the footer button's click (ui.host): checked again, then the op, free. Returns the line the panel
    shows for a moment, or '' for a button that is not (or no longer) valid, which runs nothing."""
    items = session.get('items') or []
    item = items[index] if isinstance(index, int) and 0 <= index < len(items) else None
    found = footer_action(item, watchable(session))
    if found is None:
        return ''
    from . import autorig
    result = autorig.tool('show', rig=found[1])  # The only op in ACTIONS.
    if session.get('turn') is not None:  # A running turn's reads may say something new now.
        session['turn'].ledger.scene_changed()
    if not result.ok:
        return result.text
    return f'Watching {found[1]} through Follow Camera, with its bones hidden. ' + PLAY
