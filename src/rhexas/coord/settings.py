"""Coordinator file: bind address, database path, lease length, agreement tolerances."""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

from rhexas.config import ConfigError, read_toml


@dataclass(frozen=True)
class Settings:
    """Agreement and leasing knobs. Scientific BLS thresholds are not here.

    Two honest runs of the same code on the same light curve match far more
    tightly than this. The window is wide enough for ordinary floating-point
    noise and tight enough that a wrong period, SDE, or depth is rejected.
    """

    unit_size: int = 500
    lease_s: int = 12 * 3600
    max_lease_s: int = 36 * 3600
    period_rtol: float = 0.002
    sde_atol: float = 0.25
    depth_rtol: float = 0.05
    depth_atol: float = 1e-4
    max_miss_fraction: float = 0.02
    online_window_s: int = 600
    # None: each project handler chooses (exoplanets checks twice, others once).
    replicas: int | None = None


@dataclass(frozen=True)
class CoordFile:
    path: Path
    host: str
    port: int
    db: Path
    settings: Settings


DEFAULT_TOML = """\
# Mega Rhexas friends-beta coordinator. Paths are relative to this file.
# The process only hands out work and stores compact results. It does not
# download light curves and it does not run BLS.

[coord]
host = "127.0.0.1"       # Caddy (or another proxy) should be the public side
port = 8787
db = "coord.sqlite"
lease_hours = 12         # a worker must heartbeat before this, or the batch is reissued
max_lease_hours = 36     # heartbeats cannot hold a batch longer than this
unit_size = 500          # TIC IDs per work unit

# A second worker's top BLS hit must land inside these windows.
period_rtol = 0.002      # 0.2% relative on the top period
sde_atol = 0.25          # absolute SDE
depth_rtol = 0.05        # 5% relative depth
depth_atol = 0.0001      # or 100 ppm, whichever is larger
max_miss_fraction = 0.02 # fraction of stars only one worker managed to measure

# Omit replicas to let the project handler decide. Exoplanets uses 2.
# replicas = 1             # accept a single upload and skip the comparison
"""


def _as_int(value) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"expected an integer, got {value!r}")
    return int(value)


def _as_float(value) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"expected a number, got {value!r}")
    return float(value)


def _as_str(value) -> str:
    return str(value)


def load_coord_file(path: Path | str | None = None, *, environ: dict | None = None) -> CoordFile:
    env = os.environ if environ is None else environ
    file_path = _resolve(path, env)
    data = read_toml(file_path)
    if not isinstance(data, dict):
        raise ConfigError(f"{file_path}: config must be a TOML table")
    unknown = set(data) - {"coord"}
    if unknown:
        raise ConfigError(f"{file_path}: unknown section(s): {', '.join(sorted(unknown))}")
    block = data.get("coord") or {}
    if not isinstance(block, dict):
        raise ConfigError(f"{file_path}: [coord] must be a table")
    allowed = {
        "host", "port", "db", "lease_hours", "max_lease_hours", "unit_size",
        "period_rtol", "sde_atol", "depth_rtol", "depth_atol", "max_miss_fraction",
        "online_window_s", "replicas",
    }
    extra = set(block) - allowed
    if extra:
        raise ConfigError(f"{file_path}: unknown key(s) in [coord]: {', '.join(sorted(extra))}")
    host = _as_str(block.get("host", "127.0.0.1"))
    port = _as_int(block.get("port", 8787))
    if not (1 <= port <= 65535):
        raise ConfigError("coord.port out of range")
    db = Path(str(block.get("db", "coord.sqlite"))).expanduser()
    if not db.is_absolute():
        db = (file_path.parent / db).resolve()
    settings = _settings_from(block)
    _validate(settings)
    return CoordFile(path=file_path, host=host, port=port, db=db, settings=settings)


def _settings_from(block: dict) -> Settings:
    current = Settings()
    updates = {}
    if "unit_size" in block:
        updates["unit_size"] = _as_int(block["unit_size"])
    if "lease_hours" in block:
        updates["lease_s"] = int(round(_as_float(block["lease_hours"]) * 3600))
    if "max_lease_hours" in block:
        updates["max_lease_s"] = int(round(_as_float(block["max_lease_hours"]) * 3600))
    for key in ("period_rtol", "sde_atol", "depth_rtol", "depth_atol", "max_miss_fraction"):
        if key in block:
            updates[key] = _as_float(block[key])
    if "online_window_s" in block:
        updates["online_window_s"] = _as_int(block["online_window_s"])
    if "replicas" in block:
        updates["replicas"] = _as_int(block["replicas"])
    if not updates:
        return current
    return Settings(**{f.name: updates.get(f.name, getattr(current, f.name)) for f in fields(Settings)})


def _validate(settings: Settings) -> None:
    if settings.unit_size < 1:
        raise ConfigError("unit_size must be >= 1")
    if settings.lease_s < 60:
        raise ConfigError("lease_hours must be at least 1 minute")
    if settings.max_lease_s < settings.lease_s:
        raise ConfigError("max_lease_hours must be >= lease_hours")
    if settings.replicas is not None and not 1 <= settings.replicas <= 8:
        raise ConfigError("replicas must be from 1 to 8")
    if settings.period_rtol < 0 or settings.sde_atol < 0 or settings.depth_rtol < 0 or settings.depth_atol < 0:
        raise ConfigError("agreement tolerances must be >= 0")
    if not (0 <= settings.max_miss_fraction <= 1):
        raise ConfigError("max_miss_fraction must be between 0 and 1")


def _resolve(path, env: dict) -> Path:
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p.resolve()
    env_path = (env.get("RHEXAS_COORD_CONFIG") or "").strip()
    if env_path:
        p = Path(env_path).expanduser()
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p.resolve()
    local = Path("coord.toml")
    if local.is_file():
        return local.resolve()
    system = Path("/var/lib/rhexas/coord.toml")
    if system.is_file():
        return system
    raise ConfigError("coord.toml not found. Run `rhexas coord init` in this directory, or pass --config.")


def write_default_config(path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_TOML, encoding="utf-8")
