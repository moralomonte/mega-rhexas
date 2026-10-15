"""Password hashing, cookie flags, and the optional SMTP hook.

Google sign-in is not implemented. Accounts carry ``provider`` and
``provider_sub`` so a later login can insert ``provider='google'`` without
a password hash. Email verification is the same idea: when ``[smtp]`` has no
host, accounts work unverified and nothing is sent.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import smtplib
import sys
import threading
from email.message import EmailMessage
from urllib.parse import urlsplit

from .settings import SmtpConfig
from .store import CoordError

SESSION_COOKIE = "rhexas_session"
SESSION_PREFIX = "rhs_"

# Memory is about 16 MiB at these settings, under hashlib's default cap.
# A 1 GB droplet can hash a login without swapping. Rate limits keep it rare.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

SIGNUP_PER_IP = 8
SIGNUP_PER_HOUR_GLOBAL = 40
SIGNUP_WINDOW_S = 3600
LOGIN_FAIL_PER_IP = 20
LOGIN_FAIL_PER_EMAIL = 10
LOGIN_WINDOW_S = 900
DEVICE_START_PER_IP = 30
DEVICE_START_WINDOW_S = 3600
DEVICE_POLL_PER_IP = 200
DEVICE_POLL_WINDOW_S = 600
RESEND_PER_ACCOUNT = 5
RESEND_WINDOW_S = 3600

DEVICE_TTL_S = 600
DEVICE_INTERVAL_S = 5

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DISPLAY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{1,39}$")

_DUMMY: str | None = None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        kind, n_text, r_text, p_text, salt_hex, derived_hex = stored.split("$")
        if kind != "scrypt":
            return False
        expected = bytes.fromhex(derived_hex)
        got = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def dummy_hash() -> str:
    """A real scrypt string so a missing account takes the same time as a real one."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not-a-real-password")
    return _DUMMY


def check_password(password: str) -> str:
    if not isinstance(password, str) or not password:
        raise CoordError("password is required", 400)
    if len(password) < 10:
        raise CoordError("password must be at least 10 characters", 400)
    if len(password) > 200:
        raise CoordError("password is too long", 400)
    return password


def normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        raise CoordError("enter a valid email address", 400)
    return email


def check_display_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not _DISPLAY.fullmatch(cleaned):
        raise CoordError(
            "display name must be 2–40 characters: letters, digits, space, dot, underscore, hyphen",
            400,
        )
    return cleaned


def clean_hostname(value: str) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable())
    text = " ".join(text.split())[:80]
    return text or "this computer"


def new_user_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def canonical_user_code(value: str) -> str:
    raw = "".join(ch for ch in (value or "").upper() if ch.isalnum())
    if len(raw) != 8 or any(ch not in _CODE_ALPHABET for ch in raw):
        raise CoordError("code is not valid", 400)
    return raw


def format_user_code(canonical: str) -> str:
    return canonical[:4] + "-" + canonical[4:]


def header(headers: dict, name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def client_ip(headers: dict) -> str:
    """Client address for rate limits.

    Caddy appends the real peer to ``X-Forwarded-For``. The last hop is the one
    this process can trust, because the coordinator only listens on localhost.
    """
    forwarded = header(headers, "x-forwarded-for")
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if parts:
        candidate = parts[-1]
        if len(candidate) <= 64 and all(ch.isalnum() or ch in ".:-" for ch in candidate):
            return candidate
    return "local"


def origin_of(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text if "://" in text else "")
    if parts.scheme not in ("https", "http") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def session_cookie(token: str, max_age: int, *, secure: bool) -> tuple[str, str]:
    """HttpOnly session cookie. Cross-site pages need SameSite=None and Secure."""
    value = token if max_age else ""
    parts = [
        f"{SESSION_COOKIE}={value}",
        "HttpOnly",
        "Path=/",
        f"Max-Age={int(max_age)}",
    ]
    if secure:
        parts.extend(("Secure", "SameSite=None"))
    else:
        parts.append("SameSite=Lax")
    return ("Set-Cookie", "; ".join(parts))


def safe_next(value: str, default: str = "/account") -> str:
    text = (value or "").strip()
    if not text.startswith("/") or text.startswith("//") or text.startswith("/\\"):
        return default
    if any(ch in text for ch in ("\r", "\n", "\\")):
        return default
    return text


class SmtpMailer:
    """Sends mail when SMTP is configured. ``send`` does not wait on the network."""

    def __init__(self, smtp: SmtpConfig | None = None):
        self.smtp = smtp or SmtpConfig()

    def enabled(self) -> bool:
        return bool(self.smtp.host.strip())

    def send(self, to: str, subject: str, body: str) -> None:
        if not self.enabled():
            return
        threading.Thread(
            target=self._deliver,
            args=(to, subject, body),
            daemon=True,
        ).start()

    def _deliver(self, to: str, subject: str, body: str) -> None:
        try:
            message = EmailMessage()
            message["From"] = self.smtp.from_addr or "Mega Rhexas <noreply@megarhexas.com>"
            message["To"] = to
            message["Subject"] = subject
            message.set_content(body)
            if self.smtp.ssl:
                client = smtplib.SMTP_SSL(self.smtp.host, self.smtp.port, timeout=15)
            else:
                client = smtplib.SMTP(self.smtp.host, self.smtp.port, timeout=15)
            try:
                if self.smtp.starttls and not self.smtp.ssl:
                    client.starttls()
                if self.smtp.username:
                    client.login(self.smtp.username, self.smtp.password)
                client.send_message(message)
            finally:
                try:
                    client.quit()
                except Exception:
                    pass
        except Exception as exc:
            print(f"rhexas coord: email to {to} failed: {exc}", file=sys.stderr, flush=True)
