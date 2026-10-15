<p align="center"><img src="assets/triskelion.svg" width="120" alt="Mega Rhexas triskelion"></p>

# Mega Rhexas

Local TESS exoplanet transit search. It downloads SPOC 2-minute light curves from MAST,
runs a Box Least Squares search, applies the v2 filters (SDE, SNR, secondary eclipse,
duration, EB-depth and crowding flags, ExoFOP TOI and NASA confirmed-planet cross-match),
and can vet survivors with centroid and neighbor checks. No model API is involved.

`rhexas` is the only entry point. Every sector, including 107, writes `results/sNNNN/`.
Catalogs (`toi_catalog.csv`, `confirmed_planets.csv`) stay in `results/`. Disk limits
default to 100 GB free and a 400 GB project cap, and every command uses those unless
you override them.

## Install

### Just download and run

Friends who were invited do not need git, Python, or a command line. On 64-bit Windows, download
**MegaRhexas-Setup** from the [latest GitHub release](https://github.com/moralomonte/mega-rhexas/releases/latest)
and run it. The installer is per-user: it does not ask for an administrator password. It adds
**Mega Rhexas** to the Start menu. "Start when I sign in" is a checkbox and is off unless you
turn it on.

Opening Mega Rhexas starts the worker and the local dashboard (the same screen as
`rhexas worker --dashboard`, with Eco, Live, and Turbo) and opens that page in the browser.
A triskelion sits in the tray: Open dashboard, Eco, Live, Turbo, Pause, and Quit.

The first time, the page asks you to paste the invite token. It is saved in your AppData
folder and is not shown again. Cache, low priority, Live pace, a 20 GB cap, and the disk-space
guard are already set.

The installer is about 100 MB (about 400 MB once installed) because it contains Python plus numpy, astropy, and lightkurve.
It is not code-signed. Windows SmartScreen will say it doesn't recognize the publisher.
Choose **More info**, then **Run anyway**. That warning stays until the file is signed
with an Authenticode certificate. The app checks for a newer GitHub release when it
starts and links to the download. It does not install an update by itself.

The same `rhexas desktop` command is what a future macOS app should launch. That build
is not in this release. Mac and Linux instructions for a source checkout are in `FRIENDS.md`.

Python 3.11 or newer. Windows is the machine this is run on today; the same install works
on macOS and Linux.

From a checkout, using the virtualenv you already have or a new one:

```bash
python -m venv .venv
```

Windows (existing environment):

```bat
venv\Scripts\pip install -e .
venv\Scripts\rhexas.exe --help
```

macOS / Linux:

```bash
source .venv/bin/activate
pip install -e .
rhexas --help
```

`pip install -e ".[dev]"` also installs pytest. `requirements.txt` is the full freeze of the
original Windows environment. `pyproject.toml` pins the libraries the pipeline imports.

## Switching the existing C:\Mega Rhexas install

Already-downloaded light curves stay (`data\sNNNN\*lc.fits` and `.download.done`).
Processed tables, plots, vetting output, and logs are archived so the new layout can
start clean. From `C:\Mega Rhexas`:

1. Stop dashboards that are still running (`dashboard\stop_dashboard.ps1`, or
   `venv\Scripts\rhexas dashboard --stop --sector N` for each sector).
2. If this shell still has the old caps exported, clear `MR_MIN_FREE_GB` and
   `MR_MAX_DATA_GB`. Those used to default to 45 and 50 and would override the file.
3. Install, archive the old products, and re-search the sectors that are already on disk:

```bat
venv\Scripts\pip install -e .
venv\Scripts\rhexas reset-results
venv\Scripts\rhexas rerun --sectors 107,106,105,104,103,102,101,100,99,98,97,96
```

`reset-results` moves `bls_raw.csv`, `candidates.csv`, `plots/`, `vetting/`, smoke
tables, and pipeline logs into `archive\YYYYMMDD-HHMMSS\`. It does not touch
`data\`. Shared catalogs in `results\` stay. Add `--delete` to remove those products
instead of archiving them, or `--sectors 107,106` to limit which sectors move.
A full reset (no `--sectors`) also archives the old sector-107 files that used to
sit directly in `results\` (`bls_raw.csv`, `plots\`, `vetting\`, `logs\full_run.log`).

`rerun` searches, filters, and vets from the files already downloaded. A sector
whose `data\sNNNN\.download.done` marker exists is not downloaded again. A sector
without that marker resumes the download, then continues. To overlap one incomplete
download with one search:

```bat
venv\Scripts\rhexas queue --rerun --sectors 107,106,105,104,103,102,101,100,99,98,97,96
```

The same commands are `rhexas rerun --queue --sectors ...`. After this, sector 107
results live in `results\s0107\` like every other sector.

`.download.done` means that download command finished, including a run that used
`--limit` or a TIC batch. A marker left by a partial batch will make `rerun` skip
the rest of the sector's download.

## Configure

`rhexas.toml` in the project root is the config file. Every key is optional; the copy in
the repo is the documented defaults. The dashboard reads thresholds and its sector/port
table from this file. Later sources win:

1. Built-in defaults
2. `rhexas.toml` (or `--config` / `MR_CONFIG`)
3. Environment variables
4. CLI flags

Relative paths are resolved from the directory that contains the TOML file, unless `root`
or `MR_ROOT` points somewhere else.

| Key | Default | Meaning |
| --- | --- | --- |
| `project.sector` | 107 | TESS sector for a single-sector command |
| `paths.data_dir` | `data` | Parent of `sNNNN/` light-curve folders |
| `paths.results_dir` | `results` | Results root. Every sector writes `results/sNNNN/` |
| `paths.logs_dir` | `logs` | Log files |
| `paths.catalogs_dir` | `results` | `toi_catalog.csv` and `confirmed_planets.csv` |
| `paths.archive_dir` | `archive` | Where `reset-results` moves old products |
| `paths.multisector_dir` | `<data_dir>/multisector` | Light curves downloaded by `multisector` |
| `disk.min_free_gb` | 100 | Stop downloading when the data drive has less free space |
| `disk.max_project_gb` | 400 | Stop when sector files plus `env_overhead_gb` exceed this |
| `disk.env_overhead_gb` | 4.5 | Flat size allowance (the check does not walk `venv`) |
| `download.threads` | 8 | MAST concurrency |
| `download.retries` | 5 | Attempts per file, then one more pass over stars that still have no file |
| `download.timeout_s` | 120 | Per-file timeout |
| `download.jitter` | 0.2 | Equal jitter on those waits (0.2 is 80–120% of the backoff). A partial file is resumed |
| `download.curl_timeout_s` | 60 | Sector curl-script timeout |
| `download.throttle_base_s` | 15 | HTTP 429/503 backoff, `base * factor**attempt` |
| `download.throttle_factor` | 2 | |
| `download.retry_base_s` | 5 | Backoff for other errors |
| `search.procs` | 28 | BLS worker processes |
| `search.period_min_days` / `period_max_days` | 0.5 / 13 | Period grid |
| `search.n_periods` | 20000 | |
| `search.durations_days` | 0.04, 0.06, 0.08, 0.12, 0.16, 0.24 | Trial transit durations |
| `search.flatten_window` | 721 | ~1 day at 2-minute cadence |
| `search.bin_minutes` | 10 | |
| `search.min_points` | 300 | |
| `search.outlier_sigma_upper` / `lower` | 4 / 20 | |
| `filter.sde_min` | 6 | Also the dashboard's preliminary cut |
| `filter.snr_min` | 7 | |
| `filter.min_transits` | 3 | |
| `filter.max_depth` | 0.05 | 5% |
| `filter.odd_even_max_sigma` | 3 | |
| `filter.secondary_max_sigma` | 3 | Absolute secondary sigma |
| `filter.max_duration_fraction` | 0.12 | duration/period |
| `filter.eb_depth` | 0.03 | Deeper than this is kept and flagged "likely EB" |
| `filter.crowdsap_min` | 0.8 | Below this is flagged "contaminated aperture" |
| `filter.n_plots` | 50 | Top NEW candidates plotted |
| `dashboard.host` | 0.0.0.0 | |
| `dashboard.port` | 8080 | |
| `dashboard.mode` | live | `eco`, `live`, or `turbo` |
| `dashboard.demo_port` | 8081 | |
| `[[dashboard.sectors]]` | 107:8080, 106:8082 … 96:8092 | One row per sector (switcher and `--all`). Optional `label`. Demo mode uses `demo_port` and is not a second row. |
| `multisector.period_min_days` | 0.3 | |
| `multisector.n_periods` | 30000 | |
| `multisector.snr_min` | 5 | Detections used in the ephemeris |
| `multisector.fixed_period_frac` | 0.001 | Fixed grid is 0.999P–1.001P |
| `multisector.fixed_period_steps` | 400 | |

Environment variables, when set, override the file: `MR_ROOT`, `MR_CONFIG`, `MR_SECTOR`,
`MR_DATA_DIR`, `MR_RESULTS_DIR`, `MR_LOGS_DIR`, `MR_CATALOGS_DIR`, `MR_MIN_FREE_GB`,
`MR_MAX_DATA_GB`.

## Commands

Whole sector:

```bash
rhexas download --sector 107
rhexas search --sector 107 --procs 28
rhexas filter --sector 107
rhexas run --sector 107
```

`run` downloads, searches, and filters. It does not vet, and it does not skip a sector
that already has `.download.done`.

Several sectors, one after another:

```bash
rhexas run --sectors 106,105 --data-dir D:\MegaRhexasData --max-project-gb 150 --min-free-gb 100
```

Re-search sectors that are already downloaded (search, filter, and vet; skip download
when `.download.done` exists):

```bash
rhexas rerun --sectors 107,106,105,104,103,102,101,100,99,98,97,96
rhexas rerun --queue --sectors 107,106
```

Overlap one download with one search. `--rerun` is the same skip-and-vet behavior:

```bash
rhexas queue --sectors 104,103,102,101 --predownload 105
rhexas queue --rerun --sectors 107,106,105
```

Archive or delete processed results. Downloads stay either way:

```bash
rhexas reset-results
rhexas reset-results --sectors 107,106
rhexas reset-results --delete
```

A batch of TIC IDs inside one sector. Omit the list and the command processes the whole
sector. A text file can use commas, whitespace, and `#` comments. An empty file means
no stars, not the whole sector:

```bash
rhexas run --sector 107 --tics-file batch.txt --procs 8 --threads 4
rhexas download --sector 107 --tics 170634116,178284730
rhexas search --sector 107 --tics-file batch.txt
rhexas filter --sector 107 --tics-file batch.txt
```

`--limit N` means "the first N files after the TIC filter". Download exits 2 when the
disk guard stops it, and writes `data/sNNNN/.download.done` when that run finishes.
Search appends `results/sNNNN/bls_raw.csv` and skips stars already in that file. Filter
writes `candidates.csv` plus `plots/TIC*.png` for the top NEW rows.

Vetting:

```bash
rhexas vet 88481081 27986137
rhexas vet --new
```

`rhexas vet --new` vets every NEW row in `candidates.csv`. One star failing does not
stop the rest. Output is `results/sNNNN/vetting/vetting.csv`, `vetting.json`, and
`TIC*_vet.png`. If `candidates.csv` is missing, or it has no NEW rows, the command
exits 0 without writing a vetting table.

Multi-sector check (any number of stars; a period and epoch apply to a single TIC):

```bash
rhexas multisector 13345688
rhexas multisector 13345688 170634116
rhexas multisector 13345688 --period 0.6961 --t0 2460000.5
```

The seed period is `--period`, else the star's row in this sector's `bls_raw.csv`, else
the strongest single-light-curve BLS. The dashed line on the fold plot is that star's
BLS depth.

Known-TOI diagnostic:

```bash
rhexas check-known
rhexas check-known bls_raw.csv
```

Dashboard (the stdlib SSE server and the HTML in `dashboard/`):

```bash
rhexas dashboard --detach
rhexas dashboard --sector 106 --port 8082 --detach --no-browser
rhexas dashboard --demo --port 8081
rhexas dashboard --local-only
rhexas dashboard --all
rhexas dashboard --stop
rhexas dashboard --stop --sector 106
```

`--all` starts one process per `[[dashboard.sectors]]` row (one per sector, ports 8080 and 8082–8092). Demo mode is separate: `rhexas dashboard --demo` binds `demo_port` (8081) and is not a second sector-107 entry in the switcher.
The pass/fail numbers on the page are the `[filter]` thresholds. While `rhexas run`,
`rerun`, or `queue` is going, the page treats the pipeline as alive by reading
`logs/pipeline.pid`, `logs/pipeline_queue.pid`, and `logs/run_sNNNN.pid`.

Thin Windows wrappers call the same CLI and do not set their own disk caps or a
wait-pid. `src\run_full.ps1` is `rhexas run --sector 107`. `src\run_multi.ps1` and
`src\pipeline_queue.ps1` forward the sectors you pass (`-Rerun` adds `--rerun`).
`dashboard\start_dashboard.ps1` and `stop_dashboard.ps1` call `rhexas dashboard`.
`allow_dashboard_firewall.ps1` still opens TCP 8080–8092 (Administrator, Private
profile, local subnet only); that range has to match `[[dashboard.sectors]]`.
There is no Linux/macOS firewall helper; bind `--host` and use the OS firewall if
the page should not be reachable on the LAN.

`rhexas run` and `rhexas rerun` append to `logs/run_sNNNN.log`. `rhexas queue` also
writes `logs/download_sNNNN.log` and `logs/pipeline_queue.log`. Sky positions are
cached at `dashboard/sky_cache_sNNNN.json`.

## Friends beta

The coordinator hands batches of about 500 TIC IDs to friends' computers,
which run the same download, search, and filter code. Each exoplanet batch is
searched twice, by two different accounts. The coordinator keeps a result only
when the two top hits agree on period, SDE, and depth. It is a small Python
process with a SQLite file, meant for one Ubuntu VPS (or, later, a home
server). The public name is `https://api.megarhexas.com`. The apex
`megarhexas.com` is the public site in `site/`; its pages call that API with a session cookie.

Every unit and result carries a `project` (`exoplanets` today). Routes are
`/api/v1/projects/{project}/tasks/...`; `/api/v1/work/...` is the same API for
exoplanets. Exoplanet comparison lives in a project handler, so a later science
can register its own. Credit is an append-only contributions log: account,
project, unit, outcome, a verified flag, and the time. Verified stays 0 until
another account agrees. A public leaderboard reads those rows by display name.
Redundant assignment is that handler's `replicas` (exoplanets uses 2; the
generic handler uses 1). `replicas` in `coord.toml` overrides it. Signup is
invite-only until `accounts.signup` is `open`. Teams and other sciences are
not in this beta. There are no coins.

```bash
rhexas worker --login
rhexas worker --token-file token.txt
rhexas worker --token-file token.txt --dashboard
rhexas worker-dashboard
rhexas coord init
rhexas coord enroll --sector 107 --fetch
rhexas coord invite create
rhexas coord token create ada
rhexas coord serve
```

`rhexas worker --login` is the device flow: the browser opens, you approve this computer, and the token is stored for the next run. An installer can call `rhexas.worker.login.link_this_computer`. `--token-file` still overrides that saved token.

`rhexas worker --dashboard` runs the worker and a live page for **this** machine, then opens a browser. `rhexas worker-dashboard` attaches to a worker that is already running. Both bind to `127.0.0.1` and port `8080`. If `8080` is taken, the next free port is used. The page does not call the coordinator, and it never shows the invite token.

```bash
rhexas worker --token-file token.txt --dashboard --dashboard-port 8093
rhexas worker-dashboard --no-browser
rhexas worker-dashboard --dashboard-host 0.0.0.0
```

`--dashboard-host` is opt-in. Leave it unset and the page is only reachable on this computer. Set it (for example `0.0.0.0`, or a LAN address) when another machine on your network should open the page.

The worker writes `worker-cache/dashboard.sqlite` and `worker-cache/worker.log` while it searches. The page is the same mission-control screen as `rhexas dashboard`. The speed buttons write one small row back: `eco`, `live`, or `turbo`. The worker reads that row between stars. Eco pauses there and drops to one idle process, and the leased batch stays. Live is the procs, threads, and priority the worker was started with. Turbo raises that load and uses normal priority. The highlighted button is the mode the worker is actually running. The request carries only the mode name, so opening the page on the LAN with `--dashboard-host` does not reveal the invite. Pixel movies and survey cutouts stay on the sector page, because the worker cache does not keep those files. Updates arrive over server-sent events.

Hits are labeled **candidates, unverified**. They are not confirmed planets. The three chips are verified contributions, pending contributions, and batches submitted. Those states are there for a future account. This beta has no coins.

`rhexas dashboard` is still the sector pipeline server. It reads `results/sNNNN/`. The worker page reads only the worker cache.

Setup for the server is [DEPLOY.md](DEPLOY.md). Setup for a friend's PC is [FRIENDS.md](FRIENDS.md).
The worker's disk cap and process count are separate from the pipeline defaults above.
Search thresholds are not.

## Layout

```
pyproject.toml          package metadata and pinned dependencies
rhexas.toml             config, including dashboard sector ports and the coordinator URL
src/rhexas/             importable package and the rhexas CLI
src/rhexas/coord/       friends-beta coordinator (SQLite, status page)
src/rhexas/worker/      friends-beta worker loop and the local worker dashboard
deploy/                 Dockerfile, Caddy, systemd unit, example coord.toml
site/                   public website for megarhexas.com
src/*.ps1               thin wrappers around the CLI
dashboard/              SSE server and static UI
tests/                  offline tests, including coordinator and worker with no MAST
DEPLOY.md               droplet setup for api.megarhexas.com and the public site
FRIENDS.md              how a friend installs the worker
```

Per sector the dashboard reads `results/sNNNN/bls_raw.csv`, `candidates.csv`,
`plots/TIC*.png`, and `vetting/vetting.csv`. Demo mode reads `smoke_*.csv` in that
same sector directory.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The tests never call MAST. The smoke test builds a synthetic light curve, runs the
BLS metrics, and checks that the injected transit passes the v2 thresholds. Catalog
cross-match is tested with local CSV fixtures. Reset and rerun tests use a temporary
directory and stand-in search functions.

## What a "NEW" candidate is

NEW means the signal passed the simple cuts and is not in the TOI list or the confirmed
planet table. Most of those are still systematics, blends, or stellar variability. They
need a look at the plots and the centroid test before anyone reports them.

## License

Mega Rhexas is free software under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE). If you run a modified version as a network service, you must make your changes available to its users.
