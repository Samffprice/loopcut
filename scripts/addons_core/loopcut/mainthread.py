"""bpy is main-thread only. Worker threads hand work to a timer-driven pump here."""

import collections
import queue
import time
from concurrent.futures import Future
from typing import Callable

import bpy

_jobs: "queue.Queue[tuple[Callable, Future]]" = queue.Queue()
_redraw_requested = False
_INTERVAL = 0.03
SLOW_SECONDS = 0.15  # Main-thread work this long is a freeze the user notices.
# The latest freezes, newest last: (time.time(), what held the interface, milliseconds). Each one is
# also printed to the system console, so a tester's report names it (loopcut/track/FREEZE-2026-10-10.md).
slow: "collections.deque[tuple[float, str, int]]" = collections.deque(maxlen=20)
_pumped_at = 0.0  # time.monotonic() when the pump last returned; 0 before the first run.
# Set by an agent thread while it waits for work Blender does elsewhere (shaders.wait: a preview job
# compiling a look's shaders), so a late pump meanwhile says when it happened. Waiting itself is a
# few short tasks and never a slow line.
background_work = ""


def run_on_main(fn: Callable) -> Future:
    future: Future = Future()
    _jobs.put((fn, future))
    return future


def named(name: str, fn: Callable) -> Callable:
    """fn, called `name` in a slow-task line (run_on_main(named("checkpoint", ...)))."""
    fn.loopcut_task = name
    return fn


def task_name(fn: Callable) -> str:
    return getattr(fn, "loopcut_task", None) or getattr(fn, "__qualname__", None) or repr(fn)


def note_slow(what: str, seconds: float) -> None:
    """Cheap enough to call after every task: only work past SLOW_SECONDS is kept and printed."""
    if seconds < SLOW_SECONDS:
        return
    ms = round(seconds * 1000)
    slow.append((time.time(), what, ms))
    print(f"Loopcut: the interface was blocked for {ms} ms by {what}", flush=True)


def request_redraw() -> None:
    """Safe from any thread."""
    global _redraw_requested
    _redraw_requested = True


ANIMATE_EVERY = 1 / 15  # Redraws a second while a turn runs, for the thinking animation.
EASE_EVERY = 1 / 40     # While a panel transition runs (state.ui["animate_until"]): bars easing to a new length.
_animated_at = 0.0


def _animating() -> float | None:
    """The redraw period wanted now, or None."""
    from . import playback, state
    if time.time() < state.ui.get("animate_until", 0.0):
        return EASE_EVERY
    # A chat audio card's waveform fills in as its sound plays.
    return ANIMATE_EVERY if state.session().get("busy") or playback.playing() else None


def _turn_running() -> bool:
    from . import state
    return bool(state.session().get("busy"))


def _pump() -> float:
    global _redraw_requested, _animated_at, _pumped_at
    # Late by more than a freeze while a turn runs: the main thread was busy outside these tasks
    # (Blender drawing or compiling shaders, another add-on, or a Loopcut thread holding Python).
    late = time.monotonic() - _pumped_at - _INTERVAL
    if _pumped_at and late >= SLOW_SECONDS and _turn_running():
        during = f", while {background_work}" if background_work else ""
        note_slow(f"Blender's main thread outside Loopcut's tasks{during} (the pump ran late)", late)
    while True:
        try:
            fn, future = _jobs.get_nowait()
        except queue.Empty:
            break
        if not future.set_running_or_notify_cancel():
            continue
        started = time.monotonic()
        try:
            future.set_result(fn())
        except BaseException as ex:  # Delivered to the waiting worker, which reports it.
            future.set_exception(ex)
        note_slow(task_name(fn), time.monotonic() - started)
    now = time.monotonic()
    period = _animating()
    if _redraw_requested or (period is not None and now - _animated_at >= period):
        _redraw_requested, _animated_at = False, now
        from .ui import host
        host.tag_redraw_all()
    _pumped_at = time.monotonic()
    return _INTERVAL


def register() -> None:
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, persistent=True)


def unregister() -> None:
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
