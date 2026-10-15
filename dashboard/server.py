r"""Mega Rhexas live dashboard: read-only tail of the pipeline log and results, served via SSE.
Usage: rhexas dashboard [--port 8080] [--demo]
Thresholds, ports, and paths come from rhexas.toml. Every sector reads results/sNNNN/.
Demo mode reads smoke_*.csv inside that sector directory."""
import argparse, csv, json, os, re, sys, threading, time, glob, io
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import numpy as np, psutil

HERE = Path(__file__).resolve().parent; ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8080)
ap.add_argument("--demo", action="store_true")
ap.add_argument("--host", default="0.0.0.0")
ap.add_argument("--mode", default="live", choices=["eco", "live", "turbo"])
ap.add_argument("--sector", type=int, default=0)
ap.add_argument("--root", default="")
ap.add_argument("--config", default="")
A = ap.parse_args()
from rhexas.config import load_config
from rhexas.pipeline import pipeline_pid_files
_overrides = {"root": A.root} if A.root else None
_path = A.config or None
if not _path and A.root:
    _candidate = Path(A.root) / "rhexas.toml"
    if _candidate.is_file():
        _path = str(_candidate)
CFG0 = load_config(path=_path, overrides=_overrides, search_files=_path is None)
SECTOR = A.sector or CFG0.sector
CFG = CFG0.for_sector(SECTOR)
_F = CFG.filt
SDE_MIN, SNR_MIN, MIN_TR = _F.sde_min, _F.snr_min, _F.min_transits
MAX_DEPTH, OE_MAX, SEC_MAX = _F.max_depth, _F.odd_even_max_sigma, _F.secondary_max_sigma
DUR_MAX = _F.max_duration_fraction
RES_ROOT, RES, CATS, LOGS, DATA = CFG.results_root, CFG.sector_results, CFG.catalogs_dir, CFG.logs_dir, CFG.sector_data
RAW = RES / ("smoke_raw.csv" if A.demo else "bls_raw.csv")
CAND = RES / ("smoke_candidates.csv" if A.demo else "candidates.csv")
LOG = LOGS / f"run_s{SECTOR:04d}.log"
LOGLIST = [LOGS / f"download_s{SECTOR:04d}.log", LOG]
PID_FILES = pipeline_pid_files(LOGS, SECTOR)
SKY_CACHE = HERE / f"sky_cache_s{SECTOR:04d}.json"
DASH_SECTORS = [{"sector": s.sector, "port": s.port, "label": s.label} for s in CFG.dashboard.sectors]
MODES = {"eco": (2.0, 6.0), "live": (0.5, 2.0), "turbo": (0.25, 1.0)}   # (state push s, plot refresh s)
active = {}  # client id -> mode

try:  # stay out of the search's way
    p = psutil.Process(); p.nice(psutil.IDLE_PRIORITY_CLASS if os.name == "nt" else 19)
except Exception: pass

lock = threading.Lock()
S = dict(stage="Starting", sector=SECTOR, total=0, downloaded=0, searched=0, errors=0, rate=0.0, eta_min=None,
         free_gb=None, project_gb=None, cpu=0.0, mem=0.0, pipeline_alive=False, pid=None, passed=0, n_new=0, n_known=0,
         filtered=False, demo=A.demo, updated=0, started=None)
logs = deque(maxlen=3000); log_seq = [0]
rows = []                    # light rows from raw csv
cands = {"v": 0, "list": []}
lc = {"v": 0, "data": None}
sky = {"pts": []}; sky_by_tic = {}
hist = {"v": 0}
known_toi, known_cp = {}, {}

def level(s):
    l = s.lower()
    if re.search(r"error|traceback|fail|exception", l): return "error"
    if re.search(r"warn|stop|low space|limit", l): return "warn"
    if re.search(r"all done|finished|candidate|passed vetting|recovered", l): return "success"
    return "info"
def push_log(text, src="pipeline", lvl=None):
    with lock:
        log_seq[0] += 1
        logs.append({"id": log_seq[0], "t": time.strftime("%H:%M:%S"), "src": src, "lvl": lvl or level(text), "msg": text})

def f(x):
    try: v = float(x); return v if np.isfinite(v) else None
    except Exception: return None

def load_catalogs():
    try:
        with open(CATS / "toi_catalog.csv", encoding="utf-8", errors="ignore") as fh:
            for r in csv.DictReader(l for l in fh if not l.startswith("#")):
                try: known_toi.setdefault(int(float(r["TIC ID"])), []).append(r.get("TOI", ""))
                except Exception: pass
        with open(CATS / "confirmed_planets.csv", encoding="utf-8", errors="ignore") as fh:
            for r in csv.DictReader(fh):
                try: known_cp.setdefault(int(str(r["tic_id"]).replace("TIC", "").strip()), []).append(r.get("pl_name", ""))
                except Exception: pass
        push_log(f"catalogs loaded: {len(known_toi)} TOI hosts, {len(known_cp)} confirmed-planet hosts", "dashboard")
    except Exception as e: push_log(f"catalog load failed: {e}", "dashboard", "warn")

def classify(tic):
    if tic in known_cp: return "known_confirmed", ";".join(known_cp[tic])
    if tic in known_toi: return "known_TOI", ";".join(known_toi[tic])
    return "NEW", ""

def vet(r):
    try:
        return (r["sde"] >= SDE_MIN and r["snr"] >= SNR_MIN and r["n_transits"] >= MIN_TR and 0 < r["depth"] <= MAX_DEPTH
                and r["odd_even_sigma"] <= OE_MAX and abs(r["sec_sigma"]) <= SEC_MAX and r["duration"] / r["period"] <= DUR_MAX)
    except Exception: return False

# ---------- tailers ----------
log_pos = {}
def tail_log():
    for lf in LOGLIST: tail_one(lf)
def tail_one(LOG):
    if not LOG.exists(): return
    sz = LOG.stat().st_size; pos = log_pos.get(LOG, 0)
    if sz < pos: pos = 0
    if sz == pos: return
    with open(LOG, "rb") as fh:
        if pos == 0 and sz > 400_000: fh.seek(sz - 400_000); fh.readline()
        else: fh.seek(pos)
        data = fh.read(); log_pos[LOG] = fh.tell()
    if data.count(b"\x00") > len(data) // 4: data = data.replace(b"\xff\xfe", b"").replace(b"\x00", b"")   # PowerShell 5 writes UTF-16 logs
    for line in data.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line: continue
        push_log(line, "download" if "download_s" in LOG.name else "pipeline"); parse_line(line)

def parse_line(l):
    m = re.search(r"sector \d+: (\d+) files", l)
    if m: S["total"] = int(m.group(1)); S["stage"] = "Downloading"
    m = re.search(r"^(\d\d):(\d\d):(\d\d) downloaded (\d+)", l)
    if m:
        S["stage"] = "Downloading"; ts = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)); n = int(m.group(4))
        dl = S.setdefault("_dl", []); dl.append((ts, n)); del dl[:-10]
        if len(dl) > 1 and dl[-1][0] > dl[0][0] and S["total"]:
            r = (dl[-1][1] - dl[0][1]) / ((dl[-1][0] - dl[0][0]) % 86400); S["rate"] = 0; S["dl_rate"] = round(r, 2)
            S["eta_min"] = int((S["total"] - n) / r / 60) if r > 0 else None
    m = re.search(r"(\d+) files, (\d+) done, (\d+) to do", l)
    if m: S["stage"] = "Searching"; S["total"] = S["total"] or int(m.group(1)); S["_done0"] = int(m.group(2)); S["_todo"] = int(m.group(3))
    m = re.search(r"(\d+)/(\d+) stars\s+([\d.]+)/s\s+ETA (\d+) min", l)
    if m: S["stage"] = "Searching"; S["rate"] = float(m.group(3)); S["eta_min"] = int(m.group(4))
    if "search finished" in l: S["stage"] = "Filtering"; S["eta_min"] = 0
    if "stars searched=" in l: S["stage"] = "Filtering"
    if "ALL DONE" in l: S["stage"] = "Done"; S["eta_min"] = 0

raw_pos = [0]; raw_hdr = [None]; buf = [""]
NUM = ["period","t0","duration","depth","depth_err","snr","sde","n_transits","odd_even_sigma","sec_sigma","npts"]
def tail_raw():
    if not RAW.exists(): return
    sz = RAW.stat().st_size
    if sz < raw_pos[0]: raw_pos[0] = 0; rows.clear(); buf[0] = ""; raw_hdr[0] = None
    if sz == raw_pos[0]: return
    with open(RAW, "rb") as fh: fh.seek(raw_pos[0]); data = fh.read(); raw_pos[0] = fh.tell()
    text = buf[0] + data.decode("utf-8", "ignore"); lines = text.split("\n"); buf[0] = lines.pop()
    new = []
    for line in lines:
        line = line.strip("\r")
        if not line: continue
        vals = next(csv.reader([line]))
        if raw_hdr[0] is None: raw_hdr[0] = vals; continue
        d = dict(zip(raw_hdr[0], vals))
        r = {"tic": int(f(d.get("tic")) or 0), "file": d.get("file", ""), "error": d.get("error", "")}
        for k in NUM: r[k] = f(d.get(k))
        r["ok"] = not r["error"] and r["sde"] is not None
        r["pass"] = r["ok"] and vet(r)
        if r["pass"]: r["status"], r["known"] = classify(r["tic"]); r["score"] = r["snr"] * min(r["sde"], 30) / 10
        rows.append(r); new.append(r)
    if new:
        S["searched"] = len(rows); S["errors"] = sum(1 for r in rows if not r["ok"])
        for r in new:
            if r["pass"] and not S["filtered"]:
                push_log(f"{'â˜… NEW' if r['status']=='NEW' else 'known'} signal TIC {r['tic']}: P={r['period']:.3f} d depth={r['depth']*1e6:.0f} ppm SNR={r['snr']:.1f} SDE={r['sde']:.1f}" + (f" ({r['known']})" if r['known'] else ""),
                         "search", "success" if r["status"] == "NEW" else "info")
        for r in new[-3:]:
            if r["ok"]: push_log(f"TIC {r['tic']} searched: best P={r['period']:.3f} d SDE={r['sde']:.1f} SNR={r['snr']:.1f}", "search", "debug")
        hist["v"] += 1
        if not S["filtered"]: rebuild_cands_from_raw()

def rebuild_cands_from_raw():
    L = [dict(tic=r["tic"], period=r["period"], depth=r["depth"], duration=r["duration"], snr=r["snr"], sde=r["sde"], n_transits=r["n_transits"],
              status=r["status"], known=r["known"], score=r["score"], prelim=True) for r in rows if r["pass"]]
    L.sort(key=lambda x: -x["score"]); set_cands(L)

vet_cache = {"mt": 0, "map": {}}
def vet_map():
    f = RES / "vetting" / "vetting.csv"
    try:
        mt = f.stat().st_mtime
        if mt != vet_cache["mt"]:
            m = {}
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for d in csv.DictReader(fh): m[int(float(d["tic"]))] = d
            vet_cache.update(mt=mt, map=m)
    except Exception: pass
    return vet_cache["map"]
def verdict_counts(L, vm):
    c = {"promising": 0, "unchecked": 0, "fp": 0, "known_ctoi": 0}
    for x in L:
        if x.get("status") != "NEW": continue
        v = (vm.get(int(x["tic"])) or {}).get("verdict")
        if v == "promising": c["promising"] += 1
        elif v == "known": c["known_ctoi"] += 1
        elif v: c["fp"] += 1
        else: c["unchecked"] += 1
    c["total"] = sum(c.values()); return c
vet_caches = {}
def vet_map_at(d):
    f = d / "vetting" / "vetting.csv"; vc = vet_caches.setdefault(str(f), {"mt": 0, "map": {}})
    try:
        mt = f.stat().st_mtime
        if mt != vc["mt"]:
            m = {}
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for d2 in csv.DictReader(fh): m[int(float(d2["tic"]))] = d2
            vc.update(mt=mt, map=m)
    except Exception: pass
    return vc["map"]
def sector_vm(sec):
    return vet_map_at(RES_ROOT / f"s{int(sec):04d}")
all_t = [0]
def all_counts():
    if time.time() - all_t[0] < 30: return
    all_t[0] = time.time(); vm = vet_map(); tot = {"promising": 0, "unchecked": 0, "fp": 0, "known_ctoi": 0, "total": 0}; per = {}
    try: secs = sorted({int(d["sector"]) for d in DASH_SECTORS})
    except Exception: secs = [SECTOR]
    for sec in secs:
        f = RES_ROOT / f"s{int(sec):04d}" / "candidates.csv"
        if not f.exists(): continue
        with open(f, encoding="utf-8", errors="ignore") as fh: c = verdict_counts(list(csv.DictReader(fh)), sector_vm(sec))
        if not c: continue
        per[sec] = c
        for k in tot: tot[k] += c.get(k, 0)
    S["all_counts"] = tot; S["per_sector_counts"] = per
def set_cands(L):
    vm = vet_map()
    for c in L:
        v = vm.get(c["tic"])
        if v: c["verdict"] = v.get("verdict"); c["vet_flags"] = v.get("flags") or ""; c["rp_earth"] = f(v.get("rp_earth"))
    for c in L: c["plot"] = (f"TIC{c['tic']}.png" if (RES / "plots" / f"TIC{c['tic']}.png").exists() else None)
    sig = json.dumps(L, sort_keys=True, default=str)
    if sig != cands.get("sig"):
        cands.update(sig=sig, list=L); cands["v"] += 1
    S["new_counts"] = verdict_counts(L, vm); all_t[0] = 0
    S["passed"] = len(L); S["n_new"] = sum(c["status"] == "NEW" for c in L); S["n_known"] = len(L) - S["n_new"]

cand_mt = [0]
def load_cands():
    if not CAND.exists(): return
    mt = CAND.stat().st_mtime + vet_cache["mt"]; vet_map(); mt = CAND.stat().st_mtime + vet_cache["mt"]
    if mt == cand_mt[0]: return
    cand_mt[0] = mt; L = []
    with open(CAND, encoding="utf-8", errors="ignore") as fh:
        for d in csv.DictReader(fh):
            L.append(dict(tic=int(f(d["tic"]) or 0), period=f(d.get("period")), depth=f(d.get("depth")), duration=f(d.get("duration")), snr=f(d.get("snr")),
                          sde=f(d.get("sde")), n_transits=f(d.get("n_transits")), status=d.get("status", "NEW"), score=f(d.get("score")) or 0,
                          flags=d.get("flags", ""), known=";".join(x for x in [d.get("confirmed_planet", ""), ("TOI " + d["toi"]) if d.get("toi") else ""] if x), prelim=False))
    S["filtered"] = True; set_cands(L)
    push_log(f"candidates.csv loaded: {len(L)} vetted signals ({S['n_new']} NEW)", "filter", "success")

# ---------- system ----------
last_sys = [0]
def sysinfo():
    S["cpu"] = psutil.cpu_percent(None); S["mem"] = psutil.virtual_memory().percent
    try: S["free_gb"] = round(psutil.disk_usage(str(ROOT)).free / 1e9, 1)
    except Exception: pass
    pid = None; alive = False
    for pf in PID_FILES:
        try:
            q = int(pf.read_text().strip().split()[0])
            if psutil.pid_exists(q): pid, alive = q, True; break
            pid = pid or q
        except Exception: pass
    S["pid"] = pid; S["pipeline_alive"] = alive
    if time.time() - last_sys[0] > 10:
        last_sys[0] = time.time(); n = 0; tot = 0
        try:
            with os.scandir(DATA) as it:
                for e in it:
                    if e.name.endswith("lc.fits"): n += 1; tot += e.stat().st_size
        except Exception: pass
        S["downloaded"] = n; S["data_gb"] = round(tot / 1e9, 2)
        S["project_gb"] = round(tot / 1e9 + 4.5, 1)  # data + ~4.5 GB env/venv (approx; avoids walking the tree)
    if S["stage"] == "Searching" and S["total"]:
        S["eta_min"] = S["eta_min"]

# ---------- FITS work (background, throttled) ----------
def header_radec(path):
    from astropy.io import fits
    h = fits.getheader(path, 0)
    return f(h.get("RA_OBJ")), f(h.get("DEC_OBJ")), f(h.get("TESSMAG"))

def sky_worker():
    try:
        if SKY_CACHE.exists():
            for p in json.loads(SKY_CACHE.read_text()): sky_by_tic[p[0]] = p
    except Exception: pass
    last_save = time.time()
    while True:
        try:
            files = []
            try:
                with os.scandir(DATA) as it: files = [e.name for e in it if e.name.endswith("lc.fits")]
            except Exception: pass
            if A.demo: files = [r["file"] for r in rows]
            todo = []
            for fn in files:
                m = re.search(r"-(\d{16})-", fn)
                if m and int(m.group(1)) not in sky_by_tic: todo.append((int(m.group(1)), fn))
            for i, (tic, fn) in enumerate(todo[:400]):
                try:
                    ra, dec, tm = header_radec(DATA / fn)
                    if ra is not None: sky_by_tic[tic] = [tic, round(ra, 3), round(dec, 3), round(tm or 12, 1)]
                except Exception: sky_by_tic[tic] = [tic, None, None, None]
                if i % 20 == 19: time.sleep(0.05)
            if todo and time.time() - last_save > 30:
                SKY_CACHE.write_text(json.dumps(list(sky_by_tic.values()))); last_save = time.time()
        except Exception as e: push_log(f"sky worker: {e}", "dashboard", "warn")
        time.sleep(3 if todo else 10)

def bin_arr(x, y, n):
    if len(x) <= n: return x, y
    edges = np.linspace(x.min(), x.max(), n + 1); idx = np.clip(np.digitize(x, edges) - 1, 0, n - 1)
    cnt = np.bincount(idx, minlength=n); ok = cnt > 0
    return (np.bincount(idx, x, n)[ok] / cnt[ok]), (np.bincount(idx, y, n)[ok] / cnt[ok])

def make_lc(row):
    from astropy.io import fits
    from astropy.timeseries import BoxLeastSquares
    with fits.open(DATA / row["file"], memmap=True) as h:
        d = h[1].data; t = np.array(d["TIME"], float); y = np.array(d["PDCSAP_FLUX"], float); q = np.array(d["QUALITY"])
        ra, dec = f(h[0].header.get("RA_OBJ")), f(h[0].header.get("DEC_OBJ")); tm = f(h[0].header.get("TESSMAG"))
    ok = np.isfinite(t) & np.isfinite(y) & (q == 0); t, y = t[ok], y[ok]
    y = y / np.nanmedian(y)
    # cheap detrend: subtract 1-day running median on 30-min bins
    tb, yb = bin_arr(t, y, int((t.max() - t.min()) * 48))
    k = 49; pad = np.pad(yb, k // 2, mode="edge"); trend = np.median(np.lib.stride_tricks.sliding_window_view(pad, k), axis=1)
    flat = yb - np.interp(tb, tb, trend) + 1
    P = row.get("period"); D = row.get("duration") or 0.1; T0 = row.get("t0")
    periods = np.exp(np.linspace(np.log(0.5), np.log(min(13, (t.max() - t.min()) / 1.5)), 1500))
    pw = BoxLeastSquares(tb, flat).power(periods, [0.06, 0.12, 0.24], objective="snr").power
    rx, ry = bin_arr(t, y, 900)
    fold = None
    if P and T0 is not None:
        ph = ((tb - T0 + 0.5 * P) % P) - 0.5 * P; m = np.abs(ph) < 4 * D
        if m.sum() > 5: o = np.argsort(ph[m]); fold = [np.round(ph[m][o] * 24, 3).tolist(), np.round(flat[m][o], 6).tolist(), np.round(tb[m][o], 4).tolist()]
    return dict(t0=T0, tic=row["tic"], ra=ra, dec=dec, tmag=tm, period=P, duration=D, depth=row.get("depth"), sde=row.get("sde"), snr=row.get("snr"),
                status=row.get("status") if row.get("pass") else "below vetting threshold", passed=row.get("pass"),
                raw=[np.round(rx, 4).tolist(), np.round(ry, 6).tolist()], flat=[np.round(tb, 4).tolist(), np.round(flat, 6).tolist()],
                pgram=[np.round(periods, 4).tolist(), np.round(pw, 3).tolist()], fold=fold, t=time.strftime("%H:%M:%S"))

def lc_worker():
    last_tic = None; last_dl = None
    while True:
        time.sleep(plot_interval())
        try:
            row = next((r for r in reversed(rows) if r["ok"]), None)
            if row is None and not A.demo:   # download stage: preview the most recently downloaded file
                try:
                    fs = sorted(glob.glob(str(DATA / "*lc.fits")), key=os.path.getmtime)[-2:-1]
                except Exception: fs = []
                if fs and fs[0] != last_dl:
                    last_dl = fs[0]; m = re.search(r"-(\d{16})-", fs[0])
                    row = {"tic": int(m.group(1)), "file": Path(fs[0]).name, "ok": False, "pass": False}
                else: continue
            if A.demo and row is not None:   # cycle through smoke rows so the panel moves
                ok = [r for r in rows if r["ok"]]; row = ok[int(time.time() / max(plot_interval(), 2)) % len(ok)]
            if S["stage"] == "Done" and not A.demo and cands["list"]:
                L = [c for c in cands["list"]][:12]; pick = L[int(time.time() / 8) % len(L)]
                row = next((r for r in rows if r["tic"] == pick["tic"]), row)
            if row is None or row["tic"] == last_tic: continue
            last_tic = row["tic"]; d = make_lc(row)
            if not row.get("ok"): d["status"] = "downloaded (awaiting search)"
            lc["data"] = d; lc["v"] += 1
        except Exception as e: push_log(f"light-curve preview: {e}", "dashboard", "warn")

T = {"raw": 0, "sys": 0}
def plot_interval():
    ms = list(active.values())
    if not ms: return 5.0   # nobody watching: idle
    return min(MODES[m][1] for m in ms)
def main_loop():
    load_catalogs(); psutil.cpu_percent(None)
    if A.demo: S["stage"] = "Done"; S["total"] = 200
    while True:
        try:
            if not A.demo: tail_log()
            now = time.time()
            if now - T["raw"] >= 0.25: T["raw"] = now; tail_raw(); load_cands()
            if now - T["sys"] >= 0.5: T["sys"] = now; sysinfo(); all_counts()
            if A.demo: S["downloaded"] = S["total"] = max(S["total"], len(rows))
            if S["stage"] == "Starting" and rows: S["stage"] = "Searching"
            S["updated"] = time.time()
        except Exception as e: push_log(f"dashboard loop: {e}", "dashboard", "warn")
        time.sleep(0.2)

def hist_payload():
    sde = np.array([r["sde"] for r in rows if r["ok"]], float); snr = np.array([r["snr"] for r in rows if r["ok"] and r["snr"] is not None], float)
    e1 = np.linspace(0, 30, 61); e2 = np.linspace(0, 60, 61)
    return dict(v=hist["v"], sde=[np.histogram(np.clip(sde, 0, 29.99), e1)[0].tolist(), e1.tolist(), SDE_MIN],
                snr=[np.histogram(np.clip(snr, 0, 59.99), e2)[0].tolist(), e2.tolist(), SNR_MIN])

def sky_payload(start):
    res_by = {r["tic"]: (2 if (r["pass"] and r["status"] == "NEW") else 3 if r["pass"] else 1 if r["ok"] else 0) for r in rows}
    pts = [p for p in list(sky_by_tic.values()) if p[1] is not None]
    return [[p[0], p[1], p[2], p[3], res_by.get(p[0], -1)] for p in pts]

# ---------- candidate detail (computed on demand, cached) ----------
DCACHE = HERE / f"cache_s{SECTOR:04d}"; DCACHE.mkdir(exist_ok=True); dmem = {}; dlock = threading.Lock()
def r6(a, n=6): return np.round(np.asarray(a, float), n).tolist()
def detail(tic):
    if tic in dmem: return dmem[tic]
    cf = DCACHE / f"TIC{tic}.json"
    row = next((r for r in rows if r["tic"] == tic), None)
    if row is None: return {"error": "unknown TIC"}
    if cf.exists() and cf.stat().st_mtime > RAW.stat().st_mtime - 1e9:
        d = json.loads(cf.read_text()); d["vet"] = vet_map().get(tic); dmem[tic] = d; return d
    from astropy.io import fits
    from astropy.timeseries import BoxLeastSquares
    with dlock:
        with fits.open(DATA / row["file"], memmap=True) as h:
            H0, H1 = h[0].header, h[1].header; dd = h[1].data
            t = np.array(dd["TIME"], float); y = np.array(dd["PDCSAP_FLUX"], float); q = np.array(dd["QUALITY"])
            c1 = np.array(dd["MOM_CENTR1"], float); c2 = np.array(dd["MOM_CENTR2"], float)
        ok = np.isfinite(t) & np.isfinite(y) & (q == 0); t, y, c1, c2 = t[ok], y[ok], c1[ok], c2[ok]; y = y / np.nanmedian(y)
        tb, yb = bin_arr(t, y, int((t.max() - t.min()) * 144)); k = 145
        trend = np.median(np.lib.stride_tricks.sliding_window_view(np.pad(yb, k // 2, mode="edge"), k), axis=1); fl = yb / trend
        P, T0, D, dep = row["period"], row["t0"], row["duration"], row["depth"]
        per = np.exp(np.linspace(np.log(0.5), np.log(min(13, (t.max() - t.min()) / 1.5)), 4000))
        pw = BoxLeastSquares(tb, fl).power(per, [0.04, 0.08, 0.12, 0.24], objective="snr").power
        ph = ((tb - T0 + 0.5 * P) % P) - 0.5 * P; n = np.round((tb - T0) / P).astype(int)
        w = np.abs(ph) < 4 * D
        def fold_bin(mask, phase):
            if mask.sum() < 3: return None
            x = phase[mask] * 24; o = np.argsort(x); xb, ybb = bin_arr(x[o], fl[mask][o], 60); return [r6(x[o], 3), r6(fl[mask][o]), r6(xb, 3), r6(ybb)]
        ph2 = ((tb - T0) % P) - 0.5 * P   # centred on phase 0.5
        cph = ((t - T0 + 0.5 * P) % P) - 0.5 * P; cm = np.abs(cph) < 4 * D
        oot = cm & (np.abs(cph) > 0.75 * D)
        cen = None
        if cm.sum() > 5:
            o = np.argsort(cph[cm]); xs = cph[cm][o] * 24
            a = (c1[cm][o] - np.nanmedian(c1[oot])) * 21; b = (c2[cm][o] - np.nanmedian(c2[oot])) * 21
            xb, ab = bin_arr(xs, np.nan_to_num(a), 50); _, bb = bin_arr(xs, np.nan_to_num(b), 50)
            cen = [r6(xb, 3), r6(ab, 3), r6(bb, 3)]
        rx, ry = bin_arr(t, y, 2500)
        img = None
        tp = glob.glob(str(ROOT / "data" / "tpf" / "**" / f"*{tic:016d}*tp.fits"), recursive=True)
        if tp:
            try:
                with fits.open(tp[0], memmap=True) as h:
                    tt = np.array(h[1].data["TIME"], float); fx = np.array(h[1].data["FLUX"], float); qq = np.array(h[1].data["QUALITY"])
                    from astropy.wcs import WCS
                    wcs = WCS(h[2].header); px, py = wcs.all_world2pix([[H0["RA_OBJ"], H0["DEC_OBJ"]]], 0)[0]
                g = np.isfinite(tt) & (qq == 0); tt, fx = tt[g], fx[g]
                p2 = ((tt - T0 + 0.5 * P) % P) - 0.5 * P
                oi = np.nanmedian(fx[(np.abs(p2) > 0.75 * D) & (np.abs(p2) < 2.5 * D)], 0); ii = np.nanmedian(fx[np.abs(p2) < 0.4 * D], 0)
                img = dict(oot=r6(oi, 1), diff=r6(oi - ii, 2), target=[float(px), float(py)])
            except Exception as e: img = {"error": str(e)[:80]}
        d = dict(tic=tic, period=P, t0=T0, duration=D, depth=dep, snr=row["snr"], sde=row["sde"], n_transits=row["n_transits"],
                 odd_even_sigma=row["odd_even_sigma"], sec_sigma=row["sec_sigma"],
                 star=dict(ra=f(H0.get("RA_OBJ")), dec=f(H0.get("DEC_OBJ")), tmag=f(H0.get("TESSMAG")), teff=f(H0.get("TEFF")), radius=f(H0.get("RADIUS")),
                           logg=f(H0.get("LOGG")), crowdsap=f(H1.get("CROWDSAP")), camera=H0.get("CAMERA"), ccd=H0.get("CCD")),
                 raw=[r6(rx, 4), r6(ry)], flat=[r6(tb, 4), r6(fl)], transits=r6(T0 + P * np.arange(np.ceil((t.min() - T0) / P), np.floor((t.max() - T0) / P) + 1), 4),
                 pgram=[r6(per, 5), r6(pw, 3)], fold=fold_bin(w, ph), odd=fold_bin(w & (n % 2 == 1), ph), even=fold_bin(w & (n % 2 == 0), ph),
                 secondary=fold_bin(np.abs(ph2) < 4 * D, ph2), centroid=cen, img=img)
        cf.write_text(json.dumps(d)); d["vet"] = vet_map().get(tic); dmem[tic] = d
        return d

# ---------- star images: TESS pixels (local TPF flipbook or aperture mask) + survey cutout (hips2fits, cached, rate-limited) ----------
CUT = HERE / "cutouts"; CUT.mkdir(exist_ok=True); cut_lock = threading.Lock(); cut_last = [0.0]; cut_fail = {}
def cutout(tic, ra, dec):
    f = CUT / f"TIC{tic}.jpg"
    if f.exists(): return f.read_bytes()
    if time.time() - cut_fail.get(tic, 0) < 600: return None
    with cut_lock:
        wait = 3.0 - (time.time() - cut_last[0])
        if wait > 0: time.sleep(wait)
        cut_last[0] = time.time()
        try:
            import requests
            r = requests.get("https://alasky.cds.unistra.fr/hips-image-services/hips2fits", timeout=20, params=dict(
                hips="CDS/P/DSS2/color", width=300, height=300, fov=6/60, projection="TAN", ra=ra, dec=dec, format="jpg"))
            if r.ok and r.content[:2] == b"\xff\xd8": f.write_bytes(r.content); return r.content
        except Exception: pass
        cut_fail[tic] = time.time(); return None
pix_mem = {}
def pixels(tic):
    if tic in pix_mem: return pix_mem[tic]
    from astropy.io import fits
    row = next((r for r in rows if r["tic"] == tic), None)
    out = {"tic": tic}
    tp = glob.glob(str(ROOT / "data" / "tpf" / "**" / f"*{tic:016d}*tp.fits"), recursive=True)
    try:
        if tp and row and row.get("period"):
            with fits.open(tp[0], memmap=True) as h:
                t = np.array(h[1].data["TIME"], float); fx = h[1].data["FLUX"]; q = np.array(h[1].data["QUALITY"]); ap = np.array(h[2].data)
            P, T0, D = row["period"], row["t0"], row["duration"]
            g = np.where(np.isfinite(t) & (q == 0))[0]
            tc = T0 + P * np.ceil((t[g[0]] - T0) / P) + P          # second transit in the file
            sel = g[np.abs(t[g] - tc) < 3 * D]
            if len(sel) > 90: sel = sel[np.linspace(0, len(sel) - 1, 90).astype(int)]
            fr = np.nan_to_num(np.array(fx[sel], float)); med = np.median(fr, 0)
            out.update(kind="tpf", frames=np.round(fr, 1).tolist(), median=np.round(med, 1).tolist(),
                       intransit=[bool(abs(x - tc) < D / 2) for x in t[sel]], dt_h=np.round((t[sel] - tc) * 24, 2).tolist(),
                       aperture=((ap & 2) > 0).astype(int).tolist())
        elif row:
            with fits.open(DATA / row["file"], memmap=True) as h: ap = np.array(h[2].data)
            out.update(kind="aperture", aperture=((ap & 2) > 0).astype(int).tolist(), mask=((ap & 1) > 0).astype(int).tolist())
    except Exception as e: out["error"] = str(e)[:100]
    if len(pix_mem) > 50: pix_mem.clear()
    pix_mem[tic] = out; return out

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/favicon.ico": p = "/img/favicon-32.png"
        if p.startswith("/img/"):
            fn = p[5:]; fp = HERE / "static" / "img" / fn
            if re.fullmatch(r"[\w.-]+\.(svg|png)", fn) and fp.exists():
                return self.send(200, fp.read_bytes(), "image/svg+xml" if fn.endswith(".svg") else "image/png")
        if p in ("/", "/index.html"): return self.send(200, (HERE / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if p.startswith("/plots/"):
            fn = Path(p[7:]).name; fp = RES / "plots" / fn
            if re.fullmatch(r"TIC\d+\.png", fn) and fp.exists(): return self.send(200, fp.read_bytes(), "image/png")
            return self.send(404, b"no", "text/plain")
        if p == "/api/detail":
            try: tic = int(re.search(r"tic=(\d+)", self.path).group(1)); d = detail(tic)
            except Exception as e: d = {"error": str(e)[:200]}
            return self.send(200, json.dumps(d, default=str).replace("NaN", "null").replace("Infinity", "null").encode(), "application/json")
        if p == "/api/pixels":
            try: d = pixels(int(re.search(r"tic=(\d+)", self.path).group(1)))
            except Exception as e: d = {"error": str(e)[:100]}
            return self.send(200, json.dumps(d).replace("NaN", "null").encode(), "application/json")
        if p == "/api/cutout":
            try:
                q = dict(x.split("=") for x in self.path.split("?", 1)[1].split("&")); b = cutout(int(q["tic"]), float(q["ra"]), float(q["dec"]))
            except Exception: b = None
            return self.send(200, b, "image/jpeg") if b else self.send(404, b"no cutout", "text/plain")
        if p == "/api/sectors":
            L = DASH_SECTORS or [{"sector": SECTOR, "port": A.port}]
            return self.send(200, json.dumps({"current": SECTOR, "list": L}).encode(), "application/json")
        if p == "/api/state": return self.send(200, json.dumps(S, default=str).encode(), "application/json")
        if p == "/events": return self.sse()
        self.send(404, b"not found", "text/plain")
    def sse(self):
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        m = re.search(r"mode=(\w+)", q); mode = m.group(1) if m and m.group(1) in MODES else A.mode
        fast = MODES[mode][0]; cid = id(self); active[cid] = mode
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive"); self.end_headers()
        sent_log = 0; v_c = v_l = v_h = -1; sky_n = -1; sky_t = 0; st_t = 0
        def ev(name, obj): self.wfile.write(f"event: {name}\ndata: {json.dumps(obj, default=str)}\n\n".encode())
        try:
            ev("mode", {"mode": mode, "fast": fast, "plot": MODES[mode][1]})
            while True:
                out = False; now = time.time()
                with lock: new = [l for l in logs if l["id"] > sent_log] if logs and logs[-1]["id"] > sent_log else []
                if new: ev("log", new[-500:]); sent_log = new[-1]["id"]; out = True
                if now - st_t >= fast:
                    st_t = now; ev("state", {k: v for k, v in S.items() if not k.startswith("_")}); out = True
                    if cands["v"] != v_c: v_c = cands["v"]; ev("cands", cands["list"])
                    if hist["v"] != v_h: v_h = hist["v"]; ev("hist", hist_payload())
                    if len(sky_by_tic) != sky_n and now - sky_t > 5 or now - sky_t > 20:
                        sky_n = len(sky_by_tic); sky_t = now; ev("sky", sky_payload(0))
                if lc["v"] != v_l and lc["data"]: v_l = lc["v"]; ev("lc", lc["data"]); out = True
                if out: self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError): return
        finally: active.pop(cid, None)

if __name__ == "__main__":
    push_log(f"dashboard starting ({'DEMO: smoke-test data' if A.demo else 'live'}) on http://{A.host}:{A.port}", "dashboard", "success")
    for fn in (main_loop, sky_worker, lc_worker): threading.Thread(target=fn, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer((A.host, A.port), H).serve_forever()
