from .cli import main

if __name__ == "__main__":
    import sys
    # pythonw.exe (the Start-menu shortcut) leaves stdout and stderr as None.
    if sys.stdout is None or sys.stderr is None:
        try:
            from .desktop.app import ensure_stdio
            from .desktop.paths import AppPaths
            ensure_stdio(AppPaths.discover().root / "desktop.log")
        except Exception:
            pass
    raise SystemExit(main())
