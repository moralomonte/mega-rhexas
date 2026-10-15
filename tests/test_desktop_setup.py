"""Desktop setup, token storage, and the update offer. No coordinator and no MAST."""
import json
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path

from rhexas import __version__
from rhexas.config import default_config
from rhexas.desktop.device import DevicePoll, DeviceStart
from rhexas.desktop.paths import AppPaths, app_data_dir
from rhexas.desktop.app import apply_desktop_defaults, ensure_stdio
from rhexas.desktop.setup import SetupGate
from rhexas.desktop.tokenstore import TokenError, read_token, windows_acl_command, write_token
from rhexas.desktop.tray import MENU_LABELS
from rhexas.desktop.updates import RELEASES_URL, check_for_update, is_newer, offer_from_release
from rhexas.worker.dashboard import DashboardHooks, bind_server, make_handler
from rhexas.worker.live import Store
from rhexas.worker.loop import resolve_worker, run_worker, wait_until_running

ROOT = Path(__file__).resolve().parents[1]
SECRET = "rhx_desktop_secret"


def test_app_data_paths_follow_the_platform(tmp_path):
    windows = app_data_dir(platform="win32", env={"LOCALAPPDATA": str(tmp_path / "local")}, home=tmp_path)
    assert windows == tmp_path / "local" / "Mega Rhexas"
    mac = app_data_dir(platform="darwin", env={}, home=tmp_path)
    assert mac == tmp_path / "Library" / "Application Support" / "Mega Rhexas"
    linux = app_data_dir(platform="linux", env={}, home=tmp_path)
    assert linux == tmp_path / ".local" / "share" / "mega-rhexas"


def test_desktop_defaults_are_the_friendly_ones(tmp_path):
    paths = AppPaths(tmp_path / "Mega Rhexas")
    cfg = apply_desktop_defaults(default_config(tmp_path), paths)
    assert cfg.worker.priority == "low"
    assert cfg.worker.max_cache_gb == 20
    assert cfg.worker.min_free_gb == 5
    assert Path(cfg.worker.cache_dir) == paths.cache
    assert Path(cfg.worker.token_file) == paths.token_file
    assert cfg.worker.token == ""
    assert cfg.search.n_periods == 20000
    assert cfg.filt.sde_min == 6
    paths.ensure()
    assert Store(paths.cache).requested_mode() == "live"


def test_token_file_is_private_and_errors_do_not_echo_it(tmp_path):
    write_token(tmp_path, SECRET)
    path = tmp_path / "token"
    assert read_token(tmp_path) == SECRET
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    try:
        write_token(tmp_path, "hello")
    except TokenError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected TokenError")
    assert "hello" not in message
    assert SECRET not in message
    assert read_token(tmp_path) == SECRET
    try:
        write_token(tmp_path, "")
    except TokenError as exc:
        assert SECRET not in str(exc)
    else:
        raise AssertionError("expected TokenError")


def test_missing_token_file_reads_as_empty(tmp_path):
    from rhexas.worker.loop import load_token
    cfg = resolve_worker(default_config(tmp_path), token_file=str(tmp_path / "missing"), environ={})
    assert load_token(cfg) == ""


class _Linker:
    def __init__(self):
        self.polled = []

    def start(self):
        return DeviceStart("device-secret", "https://megarhexas.com/device", 2, 30, "ABCD")

    def poll(self, code):
        self.polled.append(code)
        return DevicePoll("ready", SECRET)


def test_device_poll_stores_the_token_and_does_not_return_it(tmp_path):
    gate = SetupGate(tmp_path, "https://api.megarhexas.com", linker=_Linker())
    started = gate.begin_device()
    assert started["verification_uri"] == "https://megarhexas.com/device"
    assert "device_code" not in started
    assert "device-secret" not in json.dumps(started)
    payload = gate.poll_device()
    assert payload == {"status": "ready"}
    assert SECRET not in json.dumps(payload)
    assert read_token(tmp_path) == SECRET
    assert gate.status()["needs_token"] is False
    assert gate.status()["signin"]["label"] == "Sign in with megarhexas.com"


def _start(cache, services):
    httpd, port = bind_server("127.0.0.1", 0, make_handler(cache, {}, services))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    return httpd, port, thread


def _stop(httpd, thread):
    httpd.dashboard_stop.set()
    httpd.shutdown()
    thread.join(timeout=3)
    httpd.server_close()


def _post(port, path, payload):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_setup_page_saves_a_pasted_token_without_showing_it(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "rhexas.worker.dashboard.webbrowser.open",
        lambda url: opened.append(url) or True,
    )
    app = tmp_path / "Mega Rhexas"
    cache = app / "cache"
    cache.mkdir(parents=True)
    gate = SetupGate(app, "https://api.megarhexas.com", linker=_Linker())
    assert gate.status()["needs_token"] is True
    httpd, port, thread = _start(cache, DashboardHooks(setup=gate))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/setup", timeout=5) as response:
            status = json.loads(response.read())
        assert status["needs_token"] is True
        assert status["signin"]["start_path"] == "/api/v1/device/start"
        code, body = _post(port, "/api/setup/token", {"token": "not-a-token"})
        assert code == 400
        assert b"not-a-token" not in body
        code, body = _post(port, "/api/setup/token", {"token": SECRET})
        assert code == 200
        assert json.loads(body) == {"ok": True}
        assert SECRET.encode() not in body
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            page = response.read()
        assert b"Paste invite token" in page
        assert b"Sign in with megarhexas.com" in page
        assert SECRET.encode() not in page
        code, started = _post(port, "/api/setup/device/start", {})
        assert code == 200
        assert b"device-secret" not in started
        assert b"https://megarhexas.com/device" in started
        assert json.loads(started)["opened"] is True
        assert opened == ["https://megarhexas.com/device"]
        code, polled = _post(port, "/api/setup/device/poll", {})
        assert code == 200
        assert json.loads(polled) == {"status": "ready"}
        assert SECRET.encode() not in polled
    finally:
        _stop(httpd, thread)
    assert read_token(app) == SECRET
    for path in cache.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()


def test_update_check_offers_a_download_and_does_not_fetch_the_installer():
    payload = {
        "tag_name": "v9.9.0",
        "html_url": "https://github.com/moralomonte/mega-rhexas/releases/tag/v9.9.0",
        "assets": [
            {"name": "notes.txt", "browser_download_url": "https://example.invalid/notes"},
            {
                "name": "MegaRhexas-Setup-9.9.0.exe",
                "browser_download_url": "https://example.invalid/MegaRhexas-Setup-9.9.0.exe",
            },
        ],
    }
    seen = []

    class _Body:
        def read(self):
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(request, timeout=10):
        seen.append(request.full_url)
        return _Body()

    offer = check_for_update("1.0.0", opener=opener)
    assert seen == [RELEASES_URL]
    assert "example.invalid/MegaRhexas" not in seen[0]
    assert offer["version"] == "9.9.0"
    assert offer["asset_name"] == "MegaRhexas-Setup-9.9.0.exe"
    assert offer["asset_url"].endswith(".exe")
    assert check_for_update("9.9.0", opener=opener) is None
    assert offer_from_release({**payload, "prerelease": True}, "1.0.0") is None
    assert is_newer("1.10.0", "1.2.0") is True
    assert is_newer("1.2.0", "1.10.0") is False
    assert is_newer(__version__, __version__) is False


def test_tray_menu_and_pause_are_separate_from_eco(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    assert MENU_LABELS == ("Open dashboard", "Eco", "Live", "Turbo", "Pause", "Quit")
    cache = tmp_path / "cache"
    store = Store(cache)
    store.request_user_pause(True)
    store.request_mode("turbo")
    assert store.user_paused() is True
    assert store.requested_mode() == "turbo"
    sleeps = []

    def sleeper(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 1:
            store.request_user_pause(False)

    cfg = resolve_worker(default_config(tmp_path), token="rhx_test", environ={})
    settings = wait_until_running(cfg, store, {}, sleeper)
    assert sleeps
    assert store.user_paused() is False
    assert settings["mode"] == "turbo"
    assert settings["paused"] is False


def test_quit_event_stops_before_claiming_work(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)

    class _Client:
        def current(self):
            raise AssertionError("quit should happen before a claim")

    stop = threading.Event()
    stop.set()
    cfg = resolve_worker(default_config(tmp_path), token="rhx_test", environ={})
    assert run_worker(cfg, client=_Client(), quit_event=stop, heartbeat_s=0) == 0


def test_authorization_pending_keeps_polling_and_stores_nothing(tmp_path):
    class _Pending:
        def __init__(self):
            self.n = 0

        def start(self):
            return DeviceStart("code", "https://megarhexas.com/device", 5, 30)

        def poll(self, code):
            self.n += 1
            if self.n == 1:
                return DevicePoll("authorization_pending")
            if self.n == 2:
                return DevicePoll("slow_down")
            return DevicePoll("expired")

    gate = SetupGate(tmp_path, "https://api.megarhexas.com", linker=_Pending())
    gate.begin_device()
    assert gate.poll_device() == {"status": "pending", "interval": 5.0}
    assert gate.poll_device() == {"status": "pending", "interval": 10.0}
    expired = gate.poll_device()
    assert expired["status"] == "expired"
    assert "rhx_" not in json.dumps(expired)
    assert read_token(tmp_path) == ""


def test_desktop_command_ignores_a_broken_checkout_config(monkeypatch, tmp_path):
    seen = {}

    def run_from_args(cfg, args):
        seen["no_browser"] = args.no_browser
        seen["once"] = args.once
        return 7

    monkeypatch.setattr("rhexas.desktop.app.run_from_args", run_from_args)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rhexas.toml").write_text("this is not [toml", encoding="utf-8")
    from rhexas.cli import main
    assert main(["desktop", "--no-browser", "--once"]) == 7
    assert seen == {"no_browser": True, "once": True}


def test_pythonw_stdio_goes_to_a_log(tmp_path, monkeypatch):
    import sys
    log = tmp_path / "desktop.log"
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    ensure_stdio(log)
    print("started", flush=True)
    print("problem", file=sys.stderr, flush=True)
    text = log.read_text(encoding="utf-8")
    assert "started" in text
    assert "problem" in text
    assert "rhx_" not in text


def test_windows_token_acl_is_owner_only():
    command = windows_acl_command(
        Path("C:/Users/Ada/AppData/Local/Mega Rhexas/token"),
        r"DESKTOP\Ada",
    )
    assert command == [
        "icacls",
        "C:/Users/Ada/AppData/Local/Mega Rhexas/token",
        "/inheritance:r",
        "/grant:r",
        r"DESKTOP\Ada:(R,W)",
    ]


def test_installer_is_per_user_and_startup_defaults_off():
    iss = (ROOT / "packaging" / "windows" / "mega-rhexas.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in iss
    assert "Flags: unchecked" in iss
    assert "{autoprograms}\\Mega Rhexas" in iss
    assert "{userstartup}\\Mega Rhexas" in iss
    assert "Tasks: startup" in iss
    workflow = (ROOT / ".github" / "workflows" / "windows-installer.yml").read_text(encoding="utf-8")
    assert 'tags:' in workflow
    assert '"v*"' in workflow
    assert "windows-latest" in workflow
    assert "MegaRhexas-Setup-" in workflow
    assert "contents: write" in workflow
    script = (ROOT / "packaging" / "windows" / "build.ps1").read_text(encoding="utf-8")
    assert "[desktop]" in script
    assert "pythonw.exe" in script
    assert "lightkurve" in script
