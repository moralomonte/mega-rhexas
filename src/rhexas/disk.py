"""Free-space guard and project-size cap."""
from __future__ import annotations

import shutil
from pathlib import Path


def free_gb(data_dir: Path) -> float:
    data_dir.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(str(data_dir)).free / 1e9


def project_gb(data_dir: Path, overhead_gb: float = 4.5) -> float:
    # Sector folders only, one level of files, plus a flat env/venv allowance.
    # Does not walk venv.
    total = 0
    if data_dir.exists():
        total = sum(
            f.stat().st_size
            for d in data_dir.glob("s0*")
            for f in d.glob("*")
            if f.is_file()
        )
    return total / 1e9 + overhead_gb


def disk_limit_hit(free: float, project: float, min_free_gb: float, max_project_gb: float) -> bool:
    return free < min_free_gb or project > max_project_gb
