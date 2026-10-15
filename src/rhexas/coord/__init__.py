"""Friends-beta coordinator.

Workers do the download and the search. This process hands out work units,
keeps compact results, and (for exoplanets) accepts a unit when two different
accounts agree. People sign up on the API (invite-only unless coord.toml says
open) and link a computer with a device code. Old invite tokens still work as
legacy accounts. It is meant to run on a small VPS or a home server.
"""

__all__ = ["Settings"]

from .settings import Settings
