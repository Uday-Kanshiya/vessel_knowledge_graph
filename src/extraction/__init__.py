"""
Extraction package. Deliberately light: importing it must not pull in the
runner, because src.attachments.digest imports src.extraction.store and the
runner imports src.attachments — a package-level runner import would be a
cycle. Import the runner explicitly: `from src.extraction.runner import ...`.
"""

from .record import DeltaError, WindowContext, apply_delta, new_record
from .store import RunStore
from .windows import WindowSpec, window_count, windows

__all__ = [
    "DeltaError",
    "RunStore",
    "WindowContext",
    "WindowSpec",
    "apply_delta",
    "new_record",
    "window_count",
    "windows",
]
