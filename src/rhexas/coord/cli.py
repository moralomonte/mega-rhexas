"""``rhexas coord`` / ``python -m rhexas.coord``: init, enroll, tokens, serve."""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

from rhexas.config import ConfigError
from rhexas.tics import split_tic_blob

from .logic import tics_from_curl_script
from .server import serve
from .settings import load_coord_file, write_default_config
from .store import CoordError, Store

_CURL = "https://archive.stsci.edu/missions/tess/download_scripts/sector/tesscurl_sector_{sector}_lc.sh"


def config_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    # SUPPRESS so a parent ``--config`` is not wiped by a subcommand that omits it.
    parent.add_argument("--config", dest="coord_config", default=argparse.SUPPRESS, help="coord.toml (default: ./coord.toml or /var/lib/rhexas/coord.toml)")
    return parent


def add_subcommands(sub, parent: argparse.ArgumentParser | None = None) -> None:
    parent = config_parent() if parent is None else parent
    sub.add_parser("init", parents=[parent], help="Create coord.toml, the database, and the admin token")
    p = sub.add_parser("serve", parents=[parent], help="Listen for workers and serve the status page")
    p.add_argument("--host", default=None, help="Override coord.toml host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="Override coord.toml port (default 8787)")
    p = sub.add_parser("enroll", parents=[parent], help="Split one sector into ~500-star work units")
    p.add_argument("--sector", type=int, required=True)
    p.add_argument("--tics-file", default="", help="Text file of TIC IDs")
    p.add_argument("--curl-script", default="", help="Local MAST tesscurl_sector_N_lc.sh")
    p.add_argument("--fetch", action="store_true", help="Download that sector's MAST file list (not the light curves)")
    p.add_argument("--unit-size", type=int, default=None, help="TIC IDs per unit (default 500)")
    p.add_argument("--project", default="exoplanets", help="Project name (default exoplanets)")
    p = sub.add_parser("token", parents=[parent], help="Issue or revoke a worker invite")
    tokens = p.add_subparsers(dest="token_cmd", required=True)
    created = tokens.add_parser("create", help="Print a new worker token. It is shown once.")
    created.add_argument("name", help="Friend's name, used on the status page")
    tokens.add_parser("list", help="List workers, without tokens")
    revoked = tokens.add_parser("revoke", help="Disable a worker token and drop their open lease")
    revoked.add_argument("name")
    p = sub.add_parser("invite", parents=[parent], help="Create or revoke a signup invite code")
    invites = p.add_subparsers(dest="invite_cmd", required=True)
    created_invite = invites.add_parser("create", help="Print a signup code. It is shown once.")
    created_invite.add_argument("--uses", type=int, default=1, help="How many accounts can use this code (default 1)")
    created_invite.add_argument("--label", default="", help="Note shown in invite list, not the code")
    invites.add_parser("list", help="List invite codes by id, without the secret")
    revoked_invite = invites.add_parser("revoke", help="Disable an invite by id or by the code itself")
    revoked_invite.add_argument("code")
    p = sub.add_parser("admin-token", parents=[parent], help="Rotate the status-page token")
    p.add_argument("--reset", action="store_true", help="Invalidate the current token and print a new one")
    sub.add_parser("status", parents=[parent], help="Print progress from the local database")


def build_parser() -> argparse.ArgumentParser:
    parent = config_parent()
    parser = argparse.ArgumentParser(
        prog="rhexas coord",
        description="Friends-beta coordinator. Workers search the stars; this process only assigns batches and checks agreement.",
        parents=[parent],
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_subcommands(sub, parent)
    return parser


def _config_arg(args):
    return getattr(args, "coord_config", None) or getattr(args, "config", None)


def _command(args) -> str:
    return getattr(args, "coord_cmd", None) or args.cmd


def execute(args) -> int:
    command = _command(args)
    try:
        if command == "init":
            return _init(_config_arg(args))
        coord = load_coord_file(_config_arg(args))
        store = Store(coord.db, coord.settings)
        store.init_schema()
        try:
            if command == "serve":
                if not store.admin_configured():
                    print("rhexas coord: no admin token yet. Run `rhexas coord init` first.", file=sys.stderr)
                    return 2
                serve(
                    store, args.host or coord.host, args.port or coord.port,
                    accounts=coord.accounts, smtp=coord.smtp,
                )
                return 0
            if command == "enroll":
                return _enroll(store, args)
            if command == "token":
                return _token(store, args)
            if command == "invite":
                return _invite(store, args)
            if command == "admin-token":
                return _admin_token(store, coord.db.parent / "admin.token", reset=args.reset)
            if command == "status":
                return _status(store)
        finally:
            if command != "serve":
                store.close()
    except (ConfigError, CoordError, ValueError) as exc:
        print(f"rhexas coord: {exc}", file=sys.stderr)
        return 2
    print(f"rhexas coord: unknown command {command}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    return execute(build_parser().parse_args(argv))


def _init(path) -> int:
    target = Path(path).expanduser() if path else Path("coord.toml")
    if not target.exists():
        write_default_config(target)
        print(f"wrote {target.resolve()}")
    coord = load_coord_file(target)
    store = Store(coord.db, coord.settings)
    try:
        store.init_schema()
        token = store.ensure_admin()
        token_path = coord.db.parent / "admin.token"
        if token:
            _write_secret(token_path, token)
            print("Admin token for the status page (shown once):")
            print(token)
            print(f"saved {token_path}")
        else:
            print(f"database ready: {coord.db}")
            print(f"admin token already exists ({token_path} if init wrote it; otherwise admin-token --reset)")
            print(f"listening config: http://{coord.host}:{coord.port}")
            if coord.accounts.invite_only:
                print("Signup is invite-only. Create a code with `rhexas coord invite create`.")
            else:
                print("Signup is open.")
            if coord.smtp.enabled():
                print(f"Verification email is on ({coord.smtp.host}).")
            else:
                print("Verification email is off. Accounts work unverified.")
        return 0
    finally:
        store.close()


def _enroll(store: Store, args) -> int:
    sources = [bool(args.tics_file), bool(args.curl_script), bool(args.fetch)]
    if sum(sources) != 1:
        print("rhexas coord: enroll needs exactly one of --tics-file, --curl-script, or --fetch", file=sys.stderr)
        return 2
    if args.tics_file:
        tics = split_tic_blob(Path(args.tics_file).read_text(encoding="utf-8-sig"))
    elif args.curl_script:
        tics = tics_from_curl_script(Path(args.curl_script).read_text(encoding="utf-8-sig"))
    else:
        tics = tics_from_curl_script(_fetch_curl(args.sector))
    summary = store.enroll_tics(args.sector, tics, unit_size=args.unit_size, project=args.project)
    print(
        f"sector {summary['sector']}: {summary['stars']} stars, "
        f"{summary['units']} units of up to {summary['unit_size']}"
    )
    return 0


def _fetch_curl(sector: int) -> str:
    url = _CURL.format(sector=int(sector))
    request = urllib.request.Request(url, headers={"User-Agent": "mega-rhexas-coord"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8", errors="replace")


def _token(store: Store, args) -> int:
    if args.token_cmd == "create":
        token = store.create_worker(args.name)
        print(f"Worker token for {args.name} (shown once; send it in a private message):")
        print(token)
        return 0
    if args.token_cmd == "list":
        rows = store.list_workers()
        if not rows:
            print("no workers yet")
            return 0
        for row in rows:
            state = "revoked" if row["revoked_at"] else "active"
            print(
                f"{row['name']}\t{state}\t"
                f"stars={row['stars_submitted']} submitted/{row['stars_verified']} verified\t"
                f"batches={row['batches_submitted']} submitted/{row['batches_verified']} verified\t"
                f"candidates={row['candidates_submitted']} submitted/{row['candidates_agreed']} agreed\t"
                f"activity={row['activity']}\t"
                f"seen={row['last_seen']}\t"
                f"{row['presence']}"
            )
        return 0
    if args.token_cmd == "revoke":
        store.revoke_worker(args.name)
        print(f"revoked {args.name}")
        return 0
    print("rhexas coord: token needs create, list, or revoke", file=sys.stderr)
    return 2


def _invite(store: Store, args) -> int:
    from . import identity
    if args.invite_cmd == "create":
        code = identity.create_invite(store, uses=args.uses, label=args.label)
        print("Signup invite (shown once):")
        print(code)
        return 0
    if args.invite_cmd == "list":
        rows = identity.list_invites(store)
        if not rows:
            print("no invites yet")
            return 0
        for row in rows:
            state = "revoked" if row["revoked_at"] else f"uses_left={row['uses_left']}"
            label = row["label"] or "-"
            print(f"{row['id']}\t{label}\t{state}\t{row['created_at']}")
        return 0
    if args.invite_cmd == "revoke":
        identity.revoke_invite(store, args.code)
        print("invite revoked")
        return 0
    print("rhexas coord: invite needs create, list, or revoke", file=sys.stderr)
    return 2


def _admin_token(store: Store, path: Path, *, reset: bool) -> int:
    if reset:
        token = store.reset_admin()
        _write_secret(path, token)
        print("New admin token (shown once). The previous one no longer works:")
        print(token)
        print(f"saved {path}")
        return 0
    if store.admin_configured():
        print(f"admin token is set. File, if init saved it: {path}")
        print("Run with --reset to rotate it.")
        return 0
    print("no admin token yet. Run `rhexas coord init`.", file=sys.stderr)
    return 2


def _status(store: Store) -> int:
    snap = store.status()
    print(
        f"units {snap['units_agreed']} verified, "
        f"{snap['units_submitted']} submitted, "
        f"{snap['units_in_progress']} in progress, "
        f"{snap['units_total']} total"
    )
    print(
        f"stars {snap['tics_agreed']} verified, "
        f"{snap['tics_submitted']} submitted, "
        f"{snap['tics_in_progress']} in progress, "
        f"{snap['tics_total']} total"
    )
    print(
        f"new candidates {snap['candidates_new']}, "
        f"workers online {snap['workers_online']}, "
        f"idle {snap['workers_idle']}, "
        f"offline {snap['workers_offline']} "
        f"({snap['workers_known']} known)"
    )
    for sector in snap["sectors"]:
        print(
            f"  sector {sector['sector']}: "
            f"units {sector['agreed']} verified, {sector['units_submitted']} submitted, "
            f"{sector['units_in_progress']} in progress, {sector['units']} total; "
            f"stars {sector['tics_agreed']} verified, {sector['tics_submitted']} submitted, "
            f"{sector['tics_in_progress']} in progress, {sector['tics']} total"
        )
    return 0


def _write_secret(path: Path, token: str) -> None:
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
