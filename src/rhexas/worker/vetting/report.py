"""Markdown and JSON notes for one vetted candidate.

The files are meant to be attached to a research-note draft. They repeat the
advisory wording from the page: the checks do not confirm a signal.
"""
from __future__ import annotations

import json
from pathlib import Path

from .checks import DISCLAIMER


def report_dir(cache: Path) -> Path:
    path = Path(cache) / "vetting" / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stem(tic: int, sector: int) -> str:
    return f"TIC{int(tic)}_s{int(sector):04d}_vetting"


def _num(value, digits=4):
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return "—"
    return f"{number:.{digits}f}"


def render_markdown(detail: dict) -> str:
    tic = detail.get("tic")
    sector = detail.get("sector")
    lines = [
        f"# Advisory vetting notes — TIC {tic}, sector {sector}",
        "",
        DISCLAIMER,
        "",
        f"**Verdict:** {detail.get('verdict') or 'Not vetted yet'}",
        "",
        "These notes describe checks that already ran on this computer. They are not a discovery claim.",
        "",
        "## Signal",
        "",
        f"- TIC: {tic}",
        f"- Sector: {sector}",
        f"- Period (days): {_num(detail.get('period'), 5)}",
        f"- Epoch (days): {_num(detail.get('t0'), 5)}",
        f"- Duration (days): {_num(detail.get('duration'), 5)}",
        f"- Depth (fraction): {_num(detail.get('depth'), 6)}",
        f"- SNR: {_num(detail.get('snr'), 2)}",
        f"- SDE: {_num(detail.get('sde'), 2)}",
        f"- Radius (solar): {_num(detail.get('radius'), 3)}",
        f"- Mass (solar): {_num(detail.get('mass'), 3)}",
        "",
        "## Checks",
        "",
    ]
    for item in detail.get("checks") or []:
        lines.append(f"### {item.get('title') or item.get('name')}")
        lines.append("")
        lines.append(f"- Status: {item.get('status')}")
        lines.append(f"- {item.get('reason') or ''}")
        numbers = item.get("numbers") or {}
        if numbers:
            rendered = ", ".join(f"{key}={numbers[key]}" for key in numbers)
            lines.append(f"- Numbers: {rendered}")
        lines.append("")
    links = detail.get("links") or {}
    if links:
        lines.append("## Catalog pages")
        lines.append("")
        for name in ("simbad", "vsx", "exofop", "gaia"):
            if links.get(name):
                lines.append(f"- {name}: {links[name]}")
        lines.append("")
    lines.append("A person still needs to review this star and follow it up before writing a conclusion.")
    lines.append("")
    return "\n".join(lines)


def render_json(detail: dict) -> str:
    payload = {
        "tic": detail.get("tic"),
        "sector": detail.get("sector"),
        "verdict": detail.get("verdict"),
        "disclaimer": detail.get("disclaimer") or DISCLAIMER,
        "period": detail.get("period"),
        "t0": detail.get("t0"),
        "duration": detail.get("duration"),
        "depth": detail.get("depth"),
        "snr": detail.get("snr"),
        "sde": detail.get("sde"),
        "radius": detail.get("radius"),
        "mass": detail.get("mass"),
        "vetted_at": detail.get("vetted_at"),
        "checks": detail.get("checks") or [],
        "links": detail.get("links") or {},
        "plot": detail.get("plot"),
    }
    return json.dumps(payload, indent=2, allow_nan=False)


def write_reports(cache: Path, detail: dict) -> dict:
    folder = report_dir(cache)
    stem = _stem(int(detail["tic"]), int(detail["sector"]))
    markdown = render_markdown(detail)
    document = render_json(detail)
    md_path = folder / f"{stem}.md"
    json_path = folder / f"{stem}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(document, encoding="utf-8")
    return {
        "markdown": markdown,
        "json": document,
        "markdown_path": str(md_path),
        "json_path": str(json_path),
    }
