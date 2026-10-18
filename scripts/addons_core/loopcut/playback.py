"""Play and Stop for the chat's audio cards, through Blender's own audio library (aud): one sound at a time.

aud mixes on its own thread, so play() hands the sound over and returns (about 6 ms in Blender 5.2) and the
panel never waits on it. The device is opened on the first Play, not at load, so a Blender without an audio
output still starts; aud.error says why a file or the device failed. Main thread only, like the panel.
"""

from pathlib import Path

PEAKS = 48  # Bars in a card's waveform.

_device = None
_now: dict | None = None  # {"path", "handle"}: the sound playing.


def play(path: str) -> str:
    """Start the file at `path`, stopping whatever plays. Returns "" or why it could not play."""
    global _device, _now
    stop()
    name = Path(path).name or path
    if not Path(path).is_file():
        return f"Could not play {name}: the file is no longer at {path}."
    try:
        import aud
    except ImportError:
        return f"Could not play {name}: Blender's audio library (aud) is not available."
    try:
        if _device is None:
            _device = aud.Device()
        _now = {"path": path, "handle": _device.play(aud.Sound(path))}
    except aud.error as ex:  # An unreadable file or no audio output; the file is read on play, not by Sound().
        return f"Could not play {name}: {ex}"
    return ""


def stop() -> None:
    global _now
    if _now is not None:
        handle, _now = _now["handle"], None
        handle.stop()  # False once the sound ended by itself: nothing is left to stop.


def playing() -> dict | None:
    """{"path", "position" (seconds)} while a sound plays; None once it ended or was stopped."""
    global _now
    if _now is None:
        return None
    import aud
    handle = _now["handle"]
    if handle.status != aud.STATUS_PLAYING:  # Blender 5.2 reports a bool here: True while playing.
        _now = None
        return None
    return {"path": _now["path"], "position": handle.position}


def peaks(path: str, count: int = PEAKS) -> list[float]:
    """The loudest sample in each of `count` equal slices, 0-1 with the loudest at 1, for a card's waveform.
    [] when aud cannot decode the file (the card then draws a plain progress bar; Play says why)."""
    try:
        import aud
    except ImportError:
        return []
    try:
        data = aud.Sound(path).data()  # samples x channels, floats.
    except aud.error:
        return []
    import numpy
    level = numpy.abs(data).max(axis=1) if data.ndim == 2 else numpy.abs(data)
    if level.size == 0:
        return []
    top = float(level.max()) or 1.0
    return [round(float(part.max()) / top, 2) if part.size else 0.0 for part in numpy.array_split(level, count)]
