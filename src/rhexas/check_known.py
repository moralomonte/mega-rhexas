"""Show BLS rows for stars that are known TOIs. Usage: rhexas check-known [bls_raw.csv]"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import Config
from .filter import load_catalogs


def run(cfg: Config, raw: str = "bls_raw.csv") -> int:
    path = Path(raw) if Path(raw).is_absolute() else cfg.sector_results / raw
    d = pd.read_csv(path)
    toi, _cp = load_catalogs(cfg)
    t = toi[["TIC ID", "TOI", "Period (days)", "Depth (ppm)", "TFOPWG Disposition"]]
    m = d.merge(t, left_on="tic", right_on="TIC ID")
    pd.set_option("display.width", 250)
    cols = ["tic", "TOI", "TFOPWG Disposition", "Period (days)", "Depth (ppm)", "period", "depth", "snr", "sde",
            "n_transits", "odd_even_sigma", "sec_sigma", "error"]
    print(m[cols].to_string())
    print(d.error.notna().sum(), "errors")
    print(d.error.dropna().head())
    return 0
