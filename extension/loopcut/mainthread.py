"""bpy is main-thread only. Worker threads hand work to a timer-driven pump here."""

import queue
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


def _pump() -> float:
    global _redraw_requested
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
    if _redraw_requested:
        _redraw_requested = False
        from .ui import host
        host.tag_redraw_all()
    return _INTERVAL


def register() -> None:
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, persistent=True)


def unregister() -> None:
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
