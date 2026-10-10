"""Facts at operator calls, without changing their arguments or results.

No package imports: workspace Blenders also run this file with --python. Live
Blender installs only during agent code; startup files install for the process.
"""

import contextlib
import sys

NOTICE_PREFIX = "Loopcut note:"
NOTES = {
    "uv.pack_islands": ((
        lambda kwargs: kwargs.get("margin_method") == "FRACTION",
        "pack_islands with margin_method='FRACTION' adds margin around each island. "
        "Neighbouring islands are about 2 x margin x texture size apart; the minimum "
        "tile-border distance is about margin x texture size. At 2048 px, margin=8/2048 "
        "leaves about 16 px between islands and 8 px at the border. A gap of g px "
        "at size R corresponds to margin=g/(2R).",
    ),),
}
# Python values only. A module reload and take() both preserve once-per-process delivery.
_seen = globals().get("_seen", set())
_notices = globals().get("_notices", [])


def _record(notes, kwargs, echo, active):
    try:
        if not active[0]:
            return
        for predicate, text in notes:
            if text not in _seen and predicate(kwargs):
                notice = f"{NOTICE_PREFIX} {text}"
                _seen.add(text)
                _notices.append(notice)
                if echo:
                    print(notice, flush=True)
    except Exception:
        # Intentional diagnostic boundary, like api_docs.error_note: a broken
        # predicate or output stream must never replace the operator's outcome.
        return


class _NotedOp:
    __slots__ = ("_op", "_notes", "_echo", "_active")
    __doc__ = property(lambda self: self._op.__doc__)

    def __init__(self, op, notes, echo, active):
        self._op, self._notes, self._echo, self._active = op, notes, echo, active

    def __call__(self, *args, **kwargs):
        _record(self._notes, kwargs, self._echo, self._active)
        return self._op(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._op, name)

    def __repr__(self):
        return repr(self._op)

    def __str__(self):
        return str(self._op)

    def __dir__(self):
        return dir(self._op)


def install(echo=False):
    """Install for the process; return a cleanup for scoped, stack-ordered use."""
    module = sys.modules.get("bpy.ops")
    real = getattr(module, "_op_create_function", None)
    if real is None:
        return lambda: None
    active = [True]

    def create(submodule, name):
        op = real(submodule, name)
        try:
            notes = NOTES.get(f"{submodule}.{name}", ())
            return _NotedOp(op, notes, echo, active) if notes else op
        except Exception:
            # Intentional diagnostic boundary: keep the original lookup intact.
            return op

    def restore():
        active[0] = False  # A proxy saved by agent code also becomes inert.
        module._op_create_function = real

    module._op_create_function = create
    return restore


@contextlib.contextmanager
def watching_ops():
    """Observe calls only while agent code runs; compose with cost_guard's hook."""
    restore = install()
    try:
        yield
    finally:
        restore()


def take():
    """Return and clear pending notices, without repeating previously seen facts."""
    notices = list(_notices)
    _notices.clear()
    return notices


if __name__ == "__main__":
    install(echo=True)
