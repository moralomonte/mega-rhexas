"""One config object for the pipeline.

The ``rhexas`` CLI resolves settings in this order (later wins):

1. built-in defaults (100 GB free-space guard, 400 GB project cap)
2. ``rhexas.toml``
3. environment variables (``MR_ROOT``, ``MR_SECTOR``, ``MR_DATA_DIR``,
   ``MR_RESULTS_DIR``, ``MR_LOGS_DIR``, ``MR_CATALOGS_DIR``, ``MR_MIN_FREE_GB``,
   ``MR_MAX_DATA_GB``, ``MR_CONFIG``)
4. CLI flags

Every sector writes ``results/sNNNN/``. Thresholds, disk limits, and dashboard
ports live here; the dashboard reads this file rather than keeping its own copies.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path


class ConfigError(ValueError):
    """Invalid or unknown configuration."""


@dataclass(frozen=True)
class DownloadConfig:
    threads: int = 8
    retries: int = 5
    timeout_s: int = 120
    curl_timeout_s: int = 60
    # 15 * 2**t seconds on HTTP 429/503; 5 * 2**t seconds on other errors.
    # jitter is the equal-jitter fraction (0.2 -> 80% to 120% of that delay).
    throttle_base_s: int = 15
    throttle_factor: int = 2
    retry_base_s: int = 5
    jitter: float = 0.2


@dataclass(frozen=True)
class SearchConfig:
    procs: int = 28
    period_min_days: float = 0.5
    period_max_days: float = 13.0
    n_periods: int = 20000
    durations_days: tuple[float, ...] = (0.04, 0.06, 0.08, 0.12, 0.16, 0.24)
    flatten_window: int = 721
    bin_minutes: float = 10.0
    min_points: int = 300
    outlier_sigma_upper: float = 4.0
    outlier_sigma_lower: float = 20.0


@dataclass(frozen=True)
class FilterConfig:
    sde_min: float = 6.0
    snr_min: float = 7.0
    min_transits: int = 3
    max_depth: float = 0.05
    odd_even_max_sigma: float = 3.0
    secondary_max_sigma: float = 3.0
    max_duration_fraction: float = 0.12
    eb_depth: float = 0.03
    crowdsap_min: float = 0.8
    n_plots: int = 50
    # Plot periodogram grid (filter.plot historical literals, not the search grid).
    plot_period_min_days: float = 0.5
    plot_period_max_days: float = 13.0
    plot_n_periods: int = 20000
    plot_durations_days: tuple[float, ...] = (0.04, 0.08, 0.12, 0.24)


@dataclass(frozen=True)
class DashboardSector:
    sector: int
    port: int
    label: str = ""


# One row per sector for the switcher and `rhexas dashboard --all`.
# Demo mode is `rhexas dashboard --demo` on demo_port. A second sector-107 row
# duplicated 107 in the switcher, so it is not part of this table.
DEFAULT_DASHBOARD_SECTORS: tuple[DashboardSector, ...] = (
    DashboardSector(107, 8080),
    DashboardSector(106, 8082),
    DashboardSector(105, 8083),
    DashboardSector(104, 8084),
    DashboardSector(103, 8085),
    DashboardSector(102, 8086),
    DashboardSector(101, 8087),
    DashboardSector(100, 8088),
    DashboardSector(99, 8089),
    DashboardSector(98, 8090),
    DashboardSector(97, 8091),
    DashboardSector(96, 8092),
)


@dataclass(frozen=True)
class DashboardConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    mode: str = "live"
    demo_port: int = 8081
    sectors: tuple[DashboardSector, ...] = DEFAULT_DASHBOARD_SECTORS


@dataclass(frozen=True)
class WorkerConfig:
    """Friends-beta worker. Local search commands ignore this block.

    Disk numbers here are the worker cache only. They do not change the
    pipeline's 100 GB free-space guard or 400 GB project cap.
    ``coordinator_url`` is the invite-only API host. The public site is the
    apex, megarhexas.com.
    """

    coordinator_url: str = "https://api.megarhexas.com"
    token: str = ""
    token_file: str = ""
    procs: int = 2
    threads: int = 2
    priority: str = "low"  # idle | low | normal
    max_cache_gb: float = 20.0
    min_free_gb: float = 5.0
    poll_s: float = 60.0
    cache_dir: str = "worker-cache"
    prefetch: int = 1  # 0 waits for the upload; 1 downloads the next batch during search


@dataclass(frozen=True)
class MultisectorConfig:
    period_min_days: float = 0.3
    period_max_days: float = 13.0
    n_periods: int = 30000
    durations_days: tuple[float, ...] = (0.03, 0.04, 0.06, 0.08)
    snr_min: float = 5.0
    fixed_period_steps: int = 400
    fixed_period_frac: float = 0.001


@dataclass(frozen=True)
class Config:
    root: Path
    sector: int = 107
    data_dir: Path = Path("data")
    results_root: Path = Path("results")
    sector_results: Path = Path("results")
    logs_dir: Path = Path("logs")
    catalogs_dir: Path = Path("results")
    archive_dir: Path = Path("archive")
    # None means <data_dir>/multisector (follows data_dir).
    multisector_dir: Path | None = None
    min_free_gb: float = 100.0
    max_project_gb: float = 400.0
    env_overhead_gb: float = 4.5
    download: DownloadConfig = field(default_factory=DownloadConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    filt: FilterConfig = field(default_factory=FilterConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    multisector: MultisectorConfig = field(default_factory=MultisectorConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)

    @property
    def sector_data(self) -> Path:
        return sector_data_dir(self.data_dir, self.sector)

    @property
    def tpf_dir(self) -> Path:
        return self.data_dir / "tpf"

    @property
    def multisector_download_dir(self) -> Path:
        if self.multisector_dir is None:
            return self.data_dir / "multisector"
        return self.multisector_dir

    def for_sector(self, sector: int) -> Config:
        return replace(
            self,
            sector=int(sector),
            sector_results=sector_results_dir(self.results_root, int(sector)),
        )


def sector_data_dir(data_dir: Path, sector: int) -> Path:
    return Path(data_dir) / f"s{int(sector):04d}"


def sector_results_dir(results_root: Path, sector: int) -> Path:
    """Every sector, including 107, writes ``results/sNNNN/``."""
    return Path(results_root) / f"s{int(sector):04d}"


def default_config(root: Path | None = None) -> Config:
    root = Path(root or Path.cwd()).resolve()
    return _with_root(Config(root=root), root)


def _with_root(cfg: Config, root: Path) -> Config:
    """Resolve any relative path fields against ``root``."""
    root = Path(root).resolve()

    def abs_path(p: Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (root / p).resolve()

    data = abs_path(cfg.data_dir)
    results = abs_path(cfg.results_root)
    multi = None if cfg.multisector_dir is None else abs_path(cfg.multisector_dir)
    return replace(
        cfg,
        root=root,
        data_dir=data,
        results_root=results,
        logs_dir=abs_path(cfg.logs_dir),
        catalogs_dir=abs_path(cfg.catalogs_dir),
        archive_dir=abs_path(cfg.archive_dir),
        multisector_dir=multi,
        sector_results=sector_results_dir(results, cfg.sector),
    )


_TOP_KEYS = {
    "project": {"root", "sector"},
    "paths": {"data_dir", "results_dir", "logs_dir", "catalogs_dir", "multisector_dir", "archive_dir"},
    "disk": {"min_free_gb", "max_project_gb", "env_overhead_gb"},
    "download": {f.name for f in fields(DownloadConfig)},
    "search": {f.name for f in fields(SearchConfig)},
    "filter": {f.name for f in fields(FilterConfig)},
    "dashboard": {f.name for f in fields(DashboardConfig)},
    "multisector": {f.name for f in fields(MultisectorConfig)},
    "worker": {f.name for f in fields(WorkerConfig)},
}

_ENV_KEYS = {
    "MR_ROOT": "root",
    "MR_SECTOR": "sector",
    "MR_DATA_DIR": "data_dir",
    "MR_RESULTS_DIR": "results_root",
    "MR_LOGS_DIR": "logs_dir",
    "MR_CATALOGS_DIR": "catalogs_dir",
    "MR_MIN_FREE_GB": "min_free_gb",
    "MR_MAX_DATA_GB": "max_project_gb",
}


def load_config(
    path: Path | str | None = None,
    *,
    environ: dict | None = None,
    overrides: dict | None = None,
    search_files: bool = True,
) -> Config:
    """Load config from defaults, an optional TOML file, env, then overrides."""
    env = os.environ if environ is None else environ
    file_path = _resolve_config_path(path, env, search_files=search_files)
    # Keep paths relative until every layer has been applied, so a later root
    # override still resolves "data" / "results" against the final root.
    cfg = Config(root=_discovery_root(file_path, env))
    if file_path is not None:
        cfg = _apply_toml(cfg, file_path)
    cfg = _apply_env(cfg, env)
    cfg = _apply_overrides(cfg, overrides or {})
    cfg = _with_root(cfg, cfg.root)
    _validate(cfg)
    return cfg


def _discovery_root(file_path: Path | None, env: dict) -> Path:
    if (env.get("MR_ROOT") or "").strip():
        return Path(env["MR_ROOT"]).expanduser().resolve()
    if file_path is not None:
        return file_path.parent.resolve()
    return Path.cwd().resolve()


def _resolve_config_path(path, env: dict, *, search_files: bool) -> Path | None:
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p.resolve()
    env_path = (env.get("MR_CONFIG") or "").strip()
    if env_path:
        p = Path(env_path).expanduser()
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p.resolve()
    if not search_files:
        return None
    return _find_toml(Path.cwd())


def _find_toml(start: Path) -> Path | None:
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        cand = p / "rhexas.toml"
        if cand.is_file():
            return cand
    return None


def _apply_toml(cfg: Config, path: Path) -> Config:
    data = _read_toml(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: config must be a TOML table")
    unknown_top = set(data) - set(_TOP_KEYS)
    if unknown_top:
        raise ConfigError(f"{path}: unknown section(s): {', '.join(sorted(unknown_top))}")
    for section, allowed in _TOP_KEYS.items():
        block = data.get(section)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ConfigError(f"{path}: [{section}] must be a table")
        extra = set(block) - allowed
        if extra:
            raise ConfigError(f"{path}: unknown key(s) in [{section}]: {', '.join(sorted(extra))}")
    project = data.get("project") or {}
    if str(project.get("root") or "").strip():
        cfg = replace(cfg, root=Path(str(project["root"])).expanduser())
    if "sector" in project:
        cfg = replace(cfg, sector=int(project["sector"]))
    paths = data.get("paths") or {}
    path_map = {
        "data_dir": "data_dir",
        "results_dir": "results_root",
        "logs_dir": "logs_dir",
        "catalogs_dir": "catalogs_dir",
        "multisector_dir": "multisector_dir",
        "archive_dir": "archive_dir",
    }
    updates = {}
    for key, attr in path_map.items():
        if key in paths and str(paths[key]).strip():
            updates[attr] = Path(str(paths[key])).expanduser()
    disk = data.get("disk") or {}
    for key in ("min_free_gb", "max_project_gb", "env_overhead_gb"):
        if key in disk:
            updates[key] = float(disk[key])
    if updates:
        cfg = replace(cfg, **updates)
    dash_block = dict(data.get("dashboard") or {})
    sector_rows = dash_block.pop("sectors", None)
    dashboard = _fill_dc(DashboardConfig, dash_block, _DASH_CAST)
    if sector_rows is not None:
        dashboard = replace(dashboard, sectors=_parse_dashboard_sectors(sector_rows, path))
    return replace(
        cfg,
        download=_fill_dc(DownloadConfig, data.get("download") or {}, _DOWNLOAD_CAST),
        search=_fill_dc(SearchConfig, data.get("search") or {}, _SEARCH_CAST),
        filt=_fill_dc(FilterConfig, data.get("filter") or {}, _FILTER_CAST),
        dashboard=dashboard,
        multisector=_fill_dc(MultisectorConfig, data.get("multisector") or {}, _MULTI_CAST),
        worker=_fill_dc(WorkerConfig, data.get("worker") or {}, _WORKER_CAST),
    )


def _apply_env(cfg: Config, env: dict) -> Config:
    updates = {}
    if (env.get("MR_ROOT") or "").strip():
        updates["root"] = Path(env["MR_ROOT"]).expanduser()
    if (env.get("MR_SECTOR") or "").strip():
        updates["sector"] = int(env["MR_SECTOR"])
    for env_key, attr in _ENV_KEYS.items():
        if env_key in ("MR_ROOT", "MR_SECTOR"):
            continue
        raw = (env.get(env_key) or "").strip()
        if not raw:
            continue
        if attr in ("min_free_gb", "max_project_gb"):
            updates[attr] = float(raw)
        else:
            updates[attr] = Path(raw).expanduser()
    if not updates:
        return cfg
    return replace(cfg, **updates)


def _apply_overrides(cfg: Config, overrides: dict) -> Config:
    clean = {k: v for k, v in overrides.items() if v is not None}
    if not clean:
        return cfg
    if "root" in clean:
        cfg = replace(cfg, root=Path(str(clean.pop("root"))).expanduser())
    path_attrs = {"data_dir", "results_root", "logs_dir", "catalogs_dir", "multisector_dir", "archive_dir"}
    updates = {}
    for key, value in clean.items():
        if key in path_attrs:
            updates[key] = Path(str(value)).expanduser()
        elif key in ("min_free_gb", "max_project_gb", "env_overhead_gb"):
            updates[key] = float(value)
        elif key == "sector":
            updates[key] = int(value)
        elif key == "download_threads":
            cfg = replace(cfg, download=replace(cfg.download, threads=int(value)))
        elif key == "search_procs":
            cfg = replace(cfg, search=replace(cfg.search, procs=int(value)))
        elif key == "dashboard_host":
            cfg = replace(cfg, dashboard=replace(cfg.dashboard, host=str(value)))
        elif key == "dashboard_port":
            cfg = replace(cfg, dashboard=replace(cfg.dashboard, port=int(value)))
        elif key == "dashboard_mode":
            cfg = replace(cfg, dashboard=replace(cfg.dashboard, mode=str(value)))
        else:
            raise ConfigError(f"unknown override: {key}")
    if updates:
        cfg = replace(cfg, **updates)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.sector < 1:
        raise ConfigError(f"sector must be >= 1, got {cfg.sector}")
    if cfg.min_free_gb < 0:
        raise ConfigError("min_free_gb must be >= 0")
    if cfg.max_project_gb <= 0:
        raise ConfigError("max_project_gb must be > 0")
    if cfg.download.threads < 1:
        raise ConfigError("download.threads must be >= 1")
    if cfg.download.retries < 1:
        raise ConfigError("download.retries must be >= 1")
    if not (0 <= cfg.download.jitter <= 1):
        raise ConfigError("download.jitter must be between 0 and 1")
    if cfg.search.procs < 1:
        raise ConfigError("search.procs must be >= 1")
    if cfg.search.n_periods < 2:
        raise ConfigError("search.n_periods must be >= 2")
    if cfg.filt.sde_min < 0 or cfg.filt.snr_min < 0:
        raise ConfigError("filter thresholds must be >= 0")
    if cfg.dashboard.mode not in ("eco", "live", "turbo"):
        raise ConfigError("dashboard.mode must be eco, live, or turbo")
    if not (1 <= cfg.dashboard.port <= 65535):
        raise ConfigError("dashboard.port out of range")
    seen_ports = set()
    for row in cfg.dashboard.sectors:
        if row.sector < 1:
            raise ConfigError(f"dashboard sector must be >= 1, got {row.sector}")
        if not (1 <= row.port <= 65535):
            raise ConfigError(f"dashboard port out of range: {row.port}")
        if row.port in seen_ports:
            raise ConfigError(f"dashboard port {row.port} is listed more than once")
        seen_ports.add(row.port)
    if cfg.worker.procs < 1 or cfg.worker.threads < 1:
        raise ConfigError("worker.procs and worker.threads must be >= 1")
    if cfg.worker.priority not in ("idle", "low", "normal"):
        raise ConfigError("worker.priority must be idle, low, or normal")
    if cfg.worker.max_cache_gb <= 0:
        raise ConfigError("worker.max_cache_gb must be > 0")
    if cfg.worker.min_free_gb < 0:
        raise ConfigError("worker.min_free_gb must be >= 0")
    if cfg.worker.poll_s < 0:
        raise ConfigError("worker.poll_s must be >= 0")
    if cfg.worker.prefetch not in (0, 1):
        raise ConfigError("worker.prefetch must be 0 or 1")


def read_toml(path: Path) -> dict:
    """Load a TOML file. A leading UTF-8 BOM is ignored.

    Windows PowerShell's ``Set-Content -Encoding utf8`` writes a BOM, and
    ``tomllib`` rejects the file if that byte is still there.
    """
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ConfigError("tomllib requires Python 3.11+") from exc
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _read_toml(path: Path) -> dict:
    return read_toml(path)


def _fill_dc(cls, block: dict, casts: dict):
    current = cls()
    updates = {}
    for key, value in block.items():
        cast = casts[key]
        updates[key] = cast(value)
    return replace(current, **updates) if updates else current


def _as_float_tuple(value) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise ConfigError(f"expected a list of numbers, got {value!r}")
    return tuple(float(v) for v in value)


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


_DOWNLOAD_CAST = {name: _as_int for name in (
    "threads", "retries", "timeout_s", "curl_timeout_s", "throttle_base_s", "throttle_factor", "retry_base_s",
)}
_DOWNLOAD_CAST["jitter"] = _as_float
_SEARCH_CAST = {
    "procs": _as_int,
    "period_min_days": _as_float,
    "period_max_days": _as_float,
    "n_periods": _as_int,
    "durations_days": _as_float_tuple,
    "flatten_window": _as_int,
    "bin_minutes": _as_float,
    "min_points": _as_int,
    "outlier_sigma_upper": _as_float,
    "outlier_sigma_lower": _as_float,
}
_FILTER_CAST = {
    "sde_min": _as_float,
    "snr_min": _as_float,
    "min_transits": _as_int,
    "max_depth": _as_float,
    "odd_even_max_sigma": _as_float,
    "secondary_max_sigma": _as_float,
    "max_duration_fraction": _as_float,
    "eb_depth": _as_float,
    "crowdsap_min": _as_float,
    "n_plots": _as_int,
    "plot_period_min_days": _as_float,
    "plot_period_max_days": _as_float,
    "plot_n_periods": _as_int,
    "plot_durations_days": _as_float_tuple,
}
_DASH_CAST = {
    "host": _as_str,
    "port": _as_int,
    "mode": _as_str,
    "demo_port": _as_int,
}


def _parse_dashboard_sectors(rows, path: Path) -> tuple[DashboardSector, ...]:
    if not isinstance(rows, list) or not rows:
        raise ConfigError(f"{path}: dashboard.sectors must be a non-empty array of tables")
    parsed = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ConfigError(f"{path}: dashboard.sectors[{i}] must be a table")
        extra = set(row) - {"sector", "port", "label"}
        if extra:
            raise ConfigError(f"{path}: unknown key(s) in dashboard.sectors[{i}]: {', '.join(sorted(extra))}")
        if "sector" not in row or "port" not in row:
            raise ConfigError(f"{path}: dashboard.sectors[{i}] needs sector and port")
        parsed.append(DashboardSector(int(row["sector"]), int(row["port"]), str(row.get("label") or "")))
    return tuple(parsed)
_WORKER_CAST = {
    "coordinator_url": _as_str,
    "token": _as_str,
    "token_file": _as_str,
    "procs": _as_int,
    "threads": _as_int,
    "priority": _as_str,
    "max_cache_gb": _as_float,
    "min_free_gb": _as_float,
    "poll_s": _as_float,
    "cache_dir": _as_str,
    "prefetch": _as_int,
}
_MULTI_CAST = {
    "period_min_days": _as_float,
    "period_max_days": _as_float,
    "n_periods": _as_int,
    "durations_days": _as_float_tuple,
    "snr_min": _as_float,
    "fixed_period_steps": _as_int,
    "fixed_period_frac": _as_float,
}
