# macOS build (not shipped yet)

The Windows installer is the only packaged build. The app entry is the same on
both platforms: `rhexas desktop`.

A Mac build should:

1. Ship a real CPython (not a PyInstaller freeze) and `pip install ".[desktop]"`,
   for the same reason Windows does: numpy, astropy, and lightkurve.
2. Launch `python -m rhexas desktop` with no console window.
3. Use the app-data directory the code already picks on macOS:
   `~/Library/Application Support/Mega Rhexas`
   (`token` next to `cache/`).
4. Put the same tray actions on the status item: Open dashboard, Eco, Live,
   Turbo, Pause, Quit. `rhexas.desktop.tray.MENU_LABELS` is that list.
5. Offer the `.dmg` or `.pkg` from the GitHub release. Do not install it silently.
   The update check already prefers an `.exe` named like `Setup`; teach
   `offer_from_release` to prefer a `.dmg` when adding this build.

No Mac installer is produced by the tag workflow yet.
