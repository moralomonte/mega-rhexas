"""Coordinator file: bind address, database path, lease length, agreement tolerances."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

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
class AccountsConfig:
    """Signup policy and the public origins that may call this API with cookies.

    ``provider`` on each account is ``password`` today. ``google`` is reserved:
    a later sign-in stores a subject and leaves the password hash empty.
    """

    signup: str = "invite"  # invite | open
    public_origin: str = "https://megarhexas.com"
    api_public_url: str = "https://api.megarhexas.com"
    cookie_secure: bool | None = None
    session_days: int = 30
    extra_origins: tuple[str, ...] = ()

    @property
    def invite_only(self) -> bool:
        return self.signup != "open"

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return bool(self.cookie_secure)
        return self.api_public_url.lower().startswith("https://")

    def verification_url(self) -> str:
        return self.api_public_url.rstrip("/") + "/link"

    def allowed_origins(self) -> set[str]:
        found = set()
        for value in (self.public_origin, self.api_public_url, *self.extra_origins):
            origin = _origin_of(value)
            if origin:
                found.add(origin)
        return found

    def origin_allowed(self, origin: str) -> bool:
        if origin in self.allowed_origins():
            return True
        if self.secure_cookies:
            return False
        parts = urlsplit(origin or "")
        return parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}


@dataclass(frozen=True)
class SmtpConfig:
    """Empty host means verification mail is off and accounts work unverified."""

    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    from_addr: str = ""
    starttls: bool = True
    ssl: bool = False

    def enabled(self) -> bool:
        return bool(self.host.strip())


@dataclass(frozen=True)
class CoordFile:
    path: Path
    host: str
    port: int
    db: Path
    settings: Settings
    accounts: AccountsConfig = field(default_factory=AccountsConfig)
    smtp: SmtpConfig = field(default_factory=SmtpConfig)


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

[accounts]
# invite: signup needs a code from `rhexas coord invite create` (the default).
# open: anyone can create an account on megarhexas.com.
signup = "invite"
public_origin = "https://megarhexas.com"          # browser origin allowed to call this API
api_public_url = "https://api.megarhexas.com"    # device-link and email links
cookie_secure = true     # Secure + SameSite=None so the public site can send the session cookie
session_days = 30
# extra_origins = ["http://localhost:5173"]  # more browser origins, if you need them

# Email verification. Leave [smtp] out, or leave host empty, and accounts work
# unverified. Fill it in to send a verification link. Google sign-in is not
# wired yet; accounts are stored so it can be added without a new table.
# [smtp]
# host = "smtp.example.com"
# port = 587
# username = ""
# password = ""
# from = "Mega Rhexas <noreply@megarhexas.com>"
# starttls = true
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
    unknown = set(data) - {"coord", "accounts", "smtp"}
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
    accounts = _accounts_from(data.get("accounts") or {}, file_path)
    smtp = _smtp_from(data.get("smtp") or {}, file_path)
    return CoordFile(
        path=file_path, host=host, port=port, db=db, settings=settings, accounts=accounts, smtp=smtp,
    )


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


def _accounts_from(block, file_path: Path) -> AccountsConfig:
    if not isinstance(block, dict):
        raise ConfigError(f"{file_path}: [accounts] must be a table")
    allowed = {
        "signup", "public_origin", "api_public_url", "cookie_secure",
        "session_days", "extra_origins",
    }
    extra = set(block) - allowed
    if extra:
        raise ConfigError(f"{file_path}: unknown key(s) in [accounts]: {', '.join(sorted(extra))}")
    current = AccountsConfig()
    signup = _as_str(block.get("signup", current.signup)).strip().lower()
    if signup not in ("invite", "open"):
        raise ConfigError("accounts.signup must be \"invite\" or \"open\"")
    public_origin = _as_str(block.get("public_origin", current.public_origin)).strip()
    api_public_url = _as_str(block.get("api_public_url", current.api_public_url)).strip().rstrip("/")
    if _origin_of(public_origin) == "" or _origin_of(api_public_url) == "":
        raise ConfigError("accounts.public_origin and accounts.api_public_url must be http(s) origins")
    cookie_secure = current.cookie_secure
    if "cookie_secure" in block:
        cookie_secure = _as_bool(block["cookie_secure"])
    session_days = current.session_days
    if "session_days" in block:
        session_days = _as_int(block["session_days"])
    if not 1 <= session_days <= 365:
        raise ConfigError("accounts.session_days must be from 1 to 365")
    extra_origins = current.extra_origins
    if "extra_origins" in block:
        raw = block["extra_origins"]
        if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
            raise ConfigError("accounts.extra_origins must be a list of origins")
        cleaned = []
        for item in raw:
            origin = _origin_of(_as_str(item))
            if not origin:
                raise ConfigError(f"accounts.extra_origins entry is not an origin: {item!r}")
            cleaned.append(origin)
        extra_origins = tuple(cleaned)
    return AccountsConfig(
        signup=signup,
        public_origin=public_origin,
        api_public_url=api_public_url,
        cookie_secure=cookie_secure,
        session_days=session_days,
        extra_origins=extra_origins,
    )


def _smtp_from(block, file_path: Path) -> SmtpConfig:
    if not isinstance(block, dict):
        raise ConfigError(f"{file_path}: [smtp] must be a table")
    allowed = {"host", "port", "username", "password", "from", "starttls", "ssl"}
    extra = set(block) - allowed
    if extra:
        raise ConfigError(f"{file_path}: unknown key(s) in [smtp]: {', '.join(sorted(extra))}")
    if not block:
        return SmtpConfig()
    current = SmtpConfig()
    host = _as_str(block.get("host", current.host)).strip()
    port = _as_int(block.get("port", current.port)) if "port" in block else current.port
    if not (1 <= port <= 65535):
        raise ConfigError("smtp.port out of range")
    starttls = _as_bool(block["starttls"]) if "starttls" in block else current.starttls
    ssl = _as_bool(block["ssl"]) if "ssl" in block else current.ssl
    return SmtpConfig(
        host=host,
        port=port,
        username=_as_str(block.get("username", "")),
        password=_as_str(block.get("password", "")),
        from_addr=_as_str(block.get("from", "")),
        starttls=starttls,
        ssl=ssl,
    )


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"expected true or false, got {value!r}")


def _origin_of(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text if "://" in text else "")
    if parts.scheme not in ("https", "http") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


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
