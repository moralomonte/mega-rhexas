"""Local, advisory vetting for candidates this worker already found.

The coordinator is not involved. Catalog lookups run only when
``[vetting] enabled`` is true, and only on a background thread.
"""

from .runner import ensure_runner, notify, stop_all

__all__ = ["ensure_runner", "notify", "stop_all"]
