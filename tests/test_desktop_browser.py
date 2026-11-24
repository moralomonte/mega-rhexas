"""Desktop dashboard window launcher. No network."""
from pathlib import Path
import threading

from rhexas.config import default_config, load_config
from rhexas.desktop import app as desktop_app
from rhexas.desktop.browser import open_dashboard
from rhexas.desktop.paths import AppPaths


def test_desktop_config_defaults_app_window_on(tmp_path):
    cfg = default_config(tmp_path)
    assert cfg.desktop.app_window is True


def test_desktop_config_app_window_from_toml(tmp_path):
    path = tmp_path / "rhexas.toml"
    path.write_text("[desktop]\napp_window = false\n", encoding="utf-8")
    cfg = load_config(path, environ={}, overrides={"root": tmp_path})
    assert cfg.desktop.app_window is False


def test_open_dashboard_uses_app_mode_when_browser_found(tmp_path, monkeypatch):
    calls = []
    exe = tmp_path / "msedge.exe"
    exe.write_text("", encoding="utf-8")

    monkeypatch.setattr("rhexas.desktop.browser.find_chromium_app", lambda: exe)

    class _Proc:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr("rhexas.desktop.browser.subprocess.Popen", _Proc)
    monkeypatch.setattr(
        "rhexas.desktop.browser.webbrowser.open",
        lambda url: (_ for _ in ()).throw(AssertionError("should not open a tab")),
    )
    url = "http://127.0.0.1:8080/?v=1"
    assert open_dashboard(url, app_window=True) is True
    assert calls[0][0] == [str(exe), f"--app={url}"]


def test_open_dashboard_falls_back_to_webbrowser(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("rhexas.desktop.browser.find_chromium_app", lambda: None)
    monkeypatch.setattr("rhexas.desktop.browser.webbrowser.open", lambda url: opened.append(url) or True)
    assert open_dashboard("http://127.0.0.1:9/", app_window=True) is True
    assert opened == ["http://127.0.0.1:9/"]


def test_open_dashboard_tab_mode_skips_chromium(monkeypatch):
    opened = []

    def _boom():
        raise AssertionError("should not look for chromium")

    monkeypatch.setattr("rhexas.desktop.browser.find_chromium_app", _boom)
    monkeypatch.setattr("rhexas.desktop.browser.webbrowser.open", lambda url: opened.append(url) or True)
    assert open_dashboard("http://127.0.0.1:9/", app_window=False) is True
    assert opened == ["http://127.0.0.1:9/"]


def test_windows_candidates_include_common_paths(monkeypatch):
    from rhexas.desktop import browser
    monkeypatch.setattr(browser.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", r"C:\Pf")
    monkeypatch.setenv("ProgramFiles(x86)", r"C:\Pf86")
    monkeypatch.setattr(browser, "_edge_from_registry", lambda: None)
    paths = browser._windows_candidates()
    assert paths[0].as_posix().endswith("Microsoft/Edge/Application/msedge.exe")
    assert any(p.name == "chrome.exe" for p in paths)


def test_run_desktop_opens_via_app_launcher(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "rhexas.desktop.app.launch_in_thread",
        lambda *a, **k: "http://127.0.0.1:4242/",
    )
    monkeypatch.setattr(
        "rhexas.desktop.app.open_dashboard",
        lambda url, *, app_window: opened.append((url, app_window)) or True,
    )
    monkeypatch.setattr(desktop_app, "run_worker", lambda *a, **k: 0)
    monkeypatch.setattr("rhexas.desktop.app._start_tray", lambda *a, **k: None)
    monkeypatch.setattr("rhexas.desktop.app.check_for_update", lambda v: None)

    class _Gate:
        def __init__(self, *a, **k):
            self.ready = threading.Event()
            self.ready.set()

    monkeypatch.setattr("rhexas.desktop.app.SetupGate", _Gate)
    paths = AppPaths(tmp_path / "Mega Rhexas")
    paths.ensure()
    cfg = desktop_app.apply_desktop_defaults(default_config(tmp_path), paths)
    desktop_app.run_desktop(cfg, open_browser=True, tray=False, paths=paths)
    assert opened == [("http://127.0.0.1:4242/", True)]