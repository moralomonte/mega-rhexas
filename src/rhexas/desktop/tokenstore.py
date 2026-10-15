"""Invite token on disk. The value is never put in an error message."""
from __future__ import annotations

import os
import re
from pathlib import Path

_TOKEN = re.compile(r"^rhx_[A-Za-z0-9_-]{4,200}$")


class TokenError(ValueError):
    """The text was not saved. The message does not contain what was typed."""


def windows_acl_command(path: Path, user: str) -> list[str]:
    """icacls argv that leaves the token readable only by ``user``. No shell."""
    return ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"]


def _windows_user() -> str:
    import subprocess
    try:
        result = subprocess.run(["whoami"], capture_output=True, text=True, check=False)
    except OSError:
        result = None
    name = (result.stdout if result is not None else "").strip()
    return name or (os.environ.get("USERNAME") or "").strip()


def restrict_private(path: Path) -> None:
    """Owner-only permissions. On Windows, chmod cannot do that, so use icacls."""
    if os.name == "nt":
        user = _windows_user()
        if not user:
            return
        import subprocess
        subprocess.run(windows_acl_command(path, user), check=False, capture_output=True)
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_token(app_dir: Path, token: str) -> None:
    """Store one invite. Rejects anything that is not an ``rhx_`` token."""
    text = str(token or "").strip()
    if not text:
        raise TokenError("Paste the invite token from the person who invited you.")
    if not _TOKEN.fullmatch(text):
        raise TokenError("That doesn't look like an invite token. It should start with rhx_.")
    root = Path(app_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "token"
    tmp = path.with_name("token.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    restrict_private(tmp)
    os.replace(tmp, path)
    restrict_private(path)


def read_token(app_dir: Path) -> str:
    """Return the saved invite, or ``""`` when there is none. Does not log it."""
    path = Path(app_dir) / "token"
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return ""
    for line in lines:
        text = line.strip()
        if text and not text.startswith("#"):
            return text
    return ""
