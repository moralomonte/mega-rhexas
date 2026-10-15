"""Invite-only friends-beta coordinator.

Workers do the download and the search. This process hands out work units,
keeps compact results, and (for exoplanets) accepts a unit when two different
workers agree. Other sciences plug in as project handlers. It is meant to run
on a small VPS or a home server.
"""

__all__ = ["Settings"]

from .settings import Settings
