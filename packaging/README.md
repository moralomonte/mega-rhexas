# Packaging

`rhexas desktop` is the app. It starts the worker with the local dashboard,
opens that page, and shows the tray icon.

Windows packages an embeddable CPython and `pip install ".[desktop]"` into a
per-user Inno Setup installer (`packaging/windows`). That is a normal interpreter
plus wheels, which is what numpy, astropy, and lightkurve install cleanly into.
PyInstaller is not used: those libraries ship data files and extension modules
that a freeze often misses.

The tag workflow (`.github/workflows/windows-installer.yml`) builds
`MegaRhexas-Setup-<version>.exe` on `windows-latest` and attaches it to the
GitHub release. The file is not code-signed.

macOS is the same command and the same app-data layout. See `macos/README.md`.
