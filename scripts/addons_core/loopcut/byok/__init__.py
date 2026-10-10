"""Direct service calls with user-owned keys, separate from the agent's tool registration.

Callers on worker threads must pass policy snapshots to run()/descriptors(); reading Blender
preferences belongs to the main thread. Files are local paths, keyed by descriptor file field.
"""

from .catalog import choose_backend, descriptors
from .runner import run

__all__ = ("choose_backend", "descriptors", "run")
