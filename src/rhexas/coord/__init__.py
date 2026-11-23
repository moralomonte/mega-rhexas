"""Coordinator for one worker.

The worker does the download and the search. This process hands out batches,
stores the compact results, and checks each upload before it trusts the batch.
Signup and device linking are closed. The owner's worker uses an ``rhx_`` token.
Old account tables stay in the database. It is meant to run on a small VPS.
"""

__all__ = ["Settings"]

from .settings import Settings
