"""Start and stop the read-only dashboard (``dashboard/server.py``).

Ports and thresholds come from ``rhexas.toml``. Pid files are
``dashboard_sN.pid`` (and ``dashboard_demo.pid``). ``rhexas dashboard --stop``
also clears a ``dashboard.pid`` left by the previous sector-107 script.
"""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from .config import Config


def sectors_table(cfg: Config) -> list[dict]:
    jobs = []
    for entry in cfg.dashboard.sectors:
        label = entry.label or ""
        jobs.append({
            "sector": int(entry.sector),
            "port": int(entry.port),
            "demo": label.lower() == "demo",
            "label": label,
        })
    return jobs


def server_script(cfg: Config) -> Path:
    return cfg.root / "dashboard" / "server.py"


def pid_path(cfg: Config, *, sector: int, demo: bool) -> Path:
    """``dashboard_sN.pid`` for every sector, ``dashboard_demo.pid`` for the demo."""
    here = cfg.root / "dashboard"
    if demo:
        name = "dashboard_demo.pid"
    else:
        name = f"dashboard_s{sector}.pid"
    return here / name


def _stop_files(cfg: Config, *, sector: int, demo: bool) -> list[Path]:
    files = [pid_path(cfg, sector=sector, demo=demo)]
    if not demo and sector == 107:
        leftover = cfg.root / "dashboard" / "dashboard.pid"
        if leftover not in files:
            files.append(leftover)
    return files


def _python(detach: bool) -> str:
    exe = Path(sys.executable)
    if detach and os.name == "nt":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return sys.executable


def _cmdline(cfg: Config, *, sector: int, port: int, host: str, mode: str, demo: bool, detach: bool) -> list[str]:
    script = server_script(cfg)
    if not script.is_file():
        raise SystemExit(f"dashboard server not found: {script}")
    cmd = [_python(detach), str(script), "--port", str(port), "--host", host, "--mode", mode, "--root", str(cfg.root)]
    toml = cfg.root / "rhexas.toml"
    if toml.is_file():
        cmd += ["--config", str(toml)]
    if sector:
        cmd += ["--sector", str(sector)]
    if demo:
        cmd += ["--demo"]
    return cmd


def _alive(pid: int) -> bool:
    import psutil
    return psutil.pid_exists(pid)


def _pid_is_server(pid: int) -> bool:
    import psutil
    try:
        cmd = " ".join(psutil.Process(pid).cmdline())
    except Exception:
        return False
    return "server.py" in cmd


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:
        print(f"could not open a browser for {url}: {exc}")


def launch(cfg: Config, *, sector: int, port: int, host: str, mode: str, demo: bool = False,
           detach: bool = False, open_browser: bool = True) -> int:
    pidfile = pid_path(cfg, sector=sector, demo=demo)
    if pidfile.exists():
        try:
            old = int(pidfile.read_text(encoding="utf-8").strip().split()[0])
        except Exception:
            old = 0
        if old and _alive(old):
            url = f"http://127.0.0.1:{port}"
            print(f"Already running (pid {old}) at {url}")
            if open_browser:
                _open_browser(url)
            return 0
    cmd = _cmdline(cfg, sector=sector, port=port, host=host, mode=mode, demo=demo, detach=detach)
    url = f"http://127.0.0.1:{port}"
    log_out = cfg.root / "dashboard" / f"dashboard_out_{port}.log"
    log_err = cfg.root / "dashboard" / f"dashboard_err_{port}.log"
    popen_kwargs = {}
    if detach and os.name == "nt":
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    elif detach:
        popen_kwargs["start_new_session"] = True
    out_fh = open(log_out, "a", encoding="utf-8")
    err_fh = open(log_err, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cfg.root / "dashboard"), stdout=out_fh, stderr=err_fh, **popen_kwargs,
        )
    finally:
        out_fh.close()
        err_fh.close()
    try:
        import psutil
        psutil.Process(proc.pid).nice(psutil.IDLE_PRIORITY_CLASS if os.name == "nt" else 19)
    except Exception:
        pass
    pidfile.write_text(str(proc.pid), encoding="utf-8")
    if detach:
        try:
            code = proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            code = None
        if code is not None:
            print(f"dashboard exited immediately ({code}); see {log_err}", file=sys.stderr)
            pidfile.unlink(missing_ok=True)
            return code or 1
        print(f"Dashboard started (pid {proc.pid}) -> {url}")
        if open_browser:
            _open_browser(url)
        return 0
    print(f"Dashboard started (pid {proc.pid}) -> {url}")
    if open_browser:
        _open_browser(url)
    try:
        return proc.wait()
    finally:
        try:
            if pidfile.exists() and pidfile.read_text(encoding="utf-8").strip() == str(proc.pid):
                pidfile.unlink()
        except Exception:
            pass


def _stop_one(pidfile: Path) -> int:
    if not pidfile.exists():
        return 0
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip().split()[0])
    except Exception:
        print(f"pid file is unreadable ({pidfile.name}); nothing stopped.")
        return 1
    if _alive(pid) and not _pid_is_server(pid):
        print(f"pid {pid} is not the dashboard (or already gone); nothing stopped.")
        pidfile.unlink(missing_ok=True)
        return 0
    if _alive(pid):
        import psutil
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        print(f"Stopped dashboard (pid {pid})")
    else:
        print(f"pid {pid} is not the dashboard (or already gone); nothing stopped.")
    pidfile.unlink(missing_ok=True)
    return 0


def stop(cfg: Config, *, sector: int, demo: bool = False) -> int:
    files = _stop_files(cfg, sector=sector, demo=demo)
    if not any(path.exists() for path in files):
        print("No pid file; dashboard not running.")
        return 0
    code = 0
    for pidfile in files:
        rc = _stop_one(pidfile)
        if rc:
            code = rc
    return code


def launch_all(cfg: Config, *, host: str, mode: str, detach: bool = True) -> int:
    """One dashboard per ``dashboard.sectors`` row. Detached, no browser spam."""
    code = 0
    for job in sectors_table(cfg):
        rc = launch(
            cfg, sector=job["sector"], port=job["port"], host=host, mode=mode,
            demo=job["demo"], detach=True if detach else False, open_browser=False,
        )
        label = f" ({job['label']})" if job["label"] else ""
        print(f"sector {job['sector']}{label} on port {job['port']} (exit {rc})")
        if rc:
            code = rc
    return code
