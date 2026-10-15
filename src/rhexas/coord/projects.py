"""Project handlers. The task tables and HTTP routes do not know about TESS.

Register a science by subclassing ``ProjectHandler`` and adding it to
``HANDLERS``. ``replicas`` is how many distinct workers must finish a unit
before it counts. ``1`` accepts the single upload. ``2`` (exoplanets) keeps
both copies and calls ``validate``. Scoring later should read the
contributions log, not these classes.
"""
from __future__ import annotations

import re

from .logic import compare_star_lists, new_tic_ids

DEFAULT_PROJECT = "exoplanets"
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


class ProjectHandler:
    """Generic science: one worker, no comparison, no candidate list."""

    name = "generic"
    replicas = 1

    def validate(self, payload: dict, stars_a: list, stars_b: list, settings) -> tuple[bool, str]:
        return True, "accepted"

    def verified_candidates(self, payload: dict, candidate_lists: list[list[dict]]) -> list[dict]:
        return []


class ExoplanetHandler(ProjectHandler):
    """TESS transit search. Two workers, BLS top-hit comparison, NEW intersection."""

    name = "exoplanets"
    replicas = 2

    def validate(self, payload: dict, stars_a: list, stars_b: list, settings) -> tuple[bool, str]:
        tics = [int(tic) for tic in payload.get("tics") or []]
        return compare_star_lists(tics, stars_a, stars_b, settings)

    def verified_candidates(self, payload: dict, candidate_lists: list[list[dict]]) -> list[dict]:
        tics = [int(tic) for tic in payload.get("tics") or []]
        sets: list[set[int]] = []
        rows: dict[int, dict] = {}
        for candidates in candidate_lists:
            sets.append(new_tic_ids(candidates))
            if not rows:
                rows = {
                    int(row["tic"]): row
                    for row in candidates
                    if row.get("status") == "NEW"
                }
        if not sets:
            return []
        agreed = set(sets[0])
        for extra in sets[1:]:
            agreed &= extra
        order = tics or list(rows)
        return [rows[tic] for tic in order if tic in agreed and tic in rows]


HANDLERS: dict[str, ProjectHandler] = {
    ExoplanetHandler.name: ExoplanetHandler(),
}


def parse_project(name: str) -> str:
    text = str(name or "").strip()
    if not _NAME.fullmatch(text) or text not in HANDLERS:
        raise ValueError(f"unknown project {name}")
    return text


def get_handler(name: str) -> ProjectHandler:
    return HANDLERS[parse_project(name)]
