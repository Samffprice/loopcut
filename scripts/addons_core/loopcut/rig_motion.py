"""Which part of the playback range a rig's animation moves in, and how a finished rig check reads to the user;
pure Python, so tests need no Blender.

R6-D2 (ar-dog-4): a hand-keyed 24-frame gallop played to frame 120 froze after frame 25, and the model "verified"
the loop by comparing frames 1 and 25. Nothing deforms in a frozen stretch, so the bend check cannot see it:
autorig.coverage reads, per frame of the playback range, whether any of the rig's curves Blender plays changed
since the frame before, and coverage() turns that into the stretches it moves in and the long ones it holds still.
"""

HOLD_SECONDS = 1.0  # A still stretch counts once it is at least this long
HOLD_SHARE = .25    # and this share of the playback range: a beat between a walk and a wave is not one.
SHOWN = 2           # Still stretches named in a line; the rest are counted.


def held(moves, frames):
    """[(from, to)] frames over which the pose holds: moves[k] says whether frames[k + 1] differs from frames[k],
    so a run of False from k to j holds frames[k] through frames[j + 1]."""
    out, start = [], None
    for k, moved in enumerate(list(moves) + [True]):
        if not moved and start is None:
            start = k
        elif moved and start is not None:
            out.append((frames[start], frames[k]))
            start = None
    return out


def coverage(moves, frames, fps):
    """{'first', 'last', 'moving': reads that differ from the one before, 'still': [(from, to)] holds long enough to
    count, 'never': nothing moves at all}. frames: the frames read, in order (every frame, or every few of a long
    range); fps: the scene's, for HOLD_SECONDS."""
    first, last = frames[0], frames[-1]
    least = max(fps * HOLD_SECONDS, HOLD_SHARE * (last - first))
    holds = held(moves, frames)
    return {'first': first, 'last': last, 'moving': sum(map(bool, moves)), 'never': not any(moves),
            'still': [(a, b) for a, b in holds if b - a >= least],
            'longest': max((b - a for a, b in holds), default=0)}  # Frames, for calibration and the result's detail.


def problem(found, deliberate=False):
    """True when the animation never moves, or stands still for a stretch that counts. deliberate: the motion is
    Loopcut's own (a built-in gesture, a ready-made motion with its own length), whose hold after it is the user's
    scene range, not a frozen loop, so only never moving counts."""
    return bool(found and (found['never'] or found['still'] and not deliberate))


def line(found, deliberate=False):
    """The plain words for a coverage: 'it moves only in frames 1–25 of 1–120, then stands still to frame 120', or for a
    deliberate motion 'it moves in frames 1–49, then holds still to frame 250'."""
    first, last, still = found['first'], found['last'], found['still']
    if found['never']:
        return f'nothing moves from frame {first} to {last}'
    if not still:
        return f'it keeps moving from frame {first} to {last}'
    stands = 'holds still' if deliberate else 'stands still'
    if len(still) == 1:
        a, b = still[0]
        if b == last and a > first:
            if deliberate:
                return f'it moves in frames {first}–{a}, then holds still to frame {last}'
            return f'it moves only in frames {first}–{a} of {first}–{last}, then stands still to frame {last}'
        if a == first and b < last:
            return f'it {stands} from frame {first} to {b}, then moves to frame {last}'
    named = ' and '.join(f'from frame {a} to {b}' for a, b in still[:SHOWN])
    more = len(still) - SHOWN
    return f'it {stands} {named}' + (f' and {more} more times' if more > 0 else '') + f' of {first}–{last}'
