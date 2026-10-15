"""TIC ID lists for batch runs (one sector subset, or a worker batch)."""
from __future__ import annotations

import re
from pathlib import Path


def split_tic_blob(text: str) -> list[int]:
    """Parse TIC IDs from text: commas, whitespace, and ``#`` comments."""
    ids: list[int] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for part in line.replace(",", " ").split():
            ids.append(int(part))
    return ids


def parse_tics(*, text: str = "", path: str = "", positional: tuple | list = ()) -> set[int] | None:
    """Return TIC IDs, or None when the caller did not specify a list.

    None means "the whole sector". An explicit empty file is an empty set,
    so a blank batch does not fall back to downloading every star.
    """
    specified = bool(text) or bool(path) or bool(positional)
    if not specified:
        return None
    ids: list[int] = []
    ids.extend(int(x) for x in positional)
    if text:
        ids.extend(split_tic_blob(text))
    if path:
        ids.extend(split_tic_blob(Path(path).read_text(encoding="utf-8-sig")))
    return set(ids)


def parse_sector_list(text: str) -> list[int]:
    """Comma/whitespace-separated sector numbers, order preserved."""
    if not str(text).strip():
        return []
    out: list[int] = []
    for part in str(text).replace(",", " ").split():
        out.append(int(part))
    return out


_TIC_IN_NAME = re.compile(r"-(\d{16})-")


def tic_from_filename(name: str) -> int | None:
    """TIC from a SPOC light-curve name (``...-0000000170634116-..._lc.fits``)."""
    m = _TIC_IN_NAME.search(Path(name).name)
    return int(m.group(1)) if m else None


def filename_from_url(url: str) -> str:
    """Same filename split the historical downloader uses."""
    return url.split("uri=")[-1].split("/")[-1]
