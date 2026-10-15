"""Drop root inside the container, then run the coordinator. Stdlib only."""
import os
import sys
from pathlib import Path

UID = 10001
data = Path(os.environ.get("RHEXAS_DATA", "/var/lib/rhexas"))
data.mkdir(parents=True, exist_ok=True)
if hasattr(os, "geteuid") and os.geteuid() == 0:
    for path in [data, *data.rglob("*")]:
        try:
            os.chown(path, UID, UID)
        except OSError:
            pass
    os.setgid(UID)
    os.setuid(UID)
os.execvp(sys.executable, [sys.executable, "-m", "rhexas.coord", *sys.argv[1:]])
