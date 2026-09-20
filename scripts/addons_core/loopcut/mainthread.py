"""bpy is main-thread only. Worker threads hand work to a timer-driven pump here."""

import queue
import time
from concurrent.futures import Future
from typing import Callable

import bpy

_jobs: "queue.Queue[tuple[Callable, Future]]" = queue.Queue()
_redraw_requested = False
_INTERVAL = 0.03


def run_on_main(fn: Callable) -> Future:
    future: Future = Future()
    _jobs.put((fn, future))
    return future


def request_redraw() -> None:
    """Safe from any thread."""
    global _redraw_requested
    _redraw_requested = True


ANIMATE_EVERY = 1 / 15  # Redraws a second while a turn runs, for the thinking animation.
_animated_at = 0.0


def _animating() -> bool:
    from . import state
    return bool(state.session().get("busy"))


def _pump() -> float:
    global _redraw_requested, _animated_at
    while True:
        try:
            fn, future = _jobs.get_nowait()
        except queue.Empty:
            break
        if not future.set_running_or_notify_cancel():
            continue
        try:
            future.set_result(fn())
        except BaseException as ex:  # Delivered to the waiting worker, which reports it.
            future.set_exception(ex)
    now = time.monotonic()
    if _redraw_requested or (now - _animated_at >= ANIMATE_EVERY and _animating()):
        _redraw_requested, _animated_at = False, now
        from .ui import host
        host.tag_redraw_all()
    return _INTERVAL


def register() -> None:
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, persistent=True)


def unregister() -> None:
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
