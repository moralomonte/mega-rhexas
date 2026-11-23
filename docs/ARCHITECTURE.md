# Mega Rhexas architecture

One person runs the search. A Windows worker on that computer talks to the
coordinator at `api.megarhexas.com`. The coordinator lives on the droplet at
`/opt/mega-rhexas`, stores its database at `/var/lib/rhexas/coord.sqlite`, and
runs as the `rhexas-coord` service. No volunteer fleet is expected.

This note is the map for the phase-1 shell. Later phases add the batch grid,
the sectors view, the gallery, and candidate vetting on top of the same worker
and the same coordinator API.

## Components

**Coordinator** (`src/rhexas/coord/`). A `ThreadingHTTPServer` with one global
lock around `App.handle`. SQLite is in WAL mode. Schema changes are
`CREATE TABLE IF NOT EXISTS` and `ALTER TABLE` only (`Store._migrate`,
`_migrate_accounts`). Nothing in this tree drops a table or a row to change
shape.

**Worker** (`src/rhexas/worker/`). The installed app claims a batch, downloads
SPOC light curves, runs the search, and uploads one result. It also serves the
local dashboard on `127.0.0.1:8080`. Pause, download-ahead, and the processed
size limit are preferences in the worker cache, not coordinator settings.

**Public site** (`site/`). Static pages for megarhexas.com. Signup and device
linking render a closed notice. Login of an account that already exists still
works. The worker does not need the site.

**Vetting** (`src/rhexas/worker/vetting/`, page `vetting.html`). Opt-in checks
on candidates already stored on this computer. The new shell links to it. The
page itself was not rewritten in this phase.

## Data flow

1. The owner pastes a worker token (`rhx_…`). The worker sends
   `Authorization: Bearer` on every coordinator call. Session cookies
   (`rhs_`, `rhexas_session`) and the admin token are for the site and the
   operator, not for the search loop.
2. `claim` leases one open unit. An optional `sectors` list limits the lease
   to those TESS sectors. An empty list or a missing field means any sector,
   which is what the already-installed app sends.
3. The worker downloads the unit, searches it, and `submit`s one star row per
   TIC plus any candidates. `heartbeat` renews the lease. `release` gives it
   back. `receipt` reports whether the stored JSON covers every TIC. `peek`
   looks at the next unit without leasing it.
4. The coordinator writes the upload, then checks that one copy
   (`verify_single_copy`). Every TIC in the unit must have a stored star row.
   A row may be an honest failure (`error` set, or period/SDE/depth missing).
   A measured row must sit in a wide physical window: period in `(0, 1000]`,
   depth in `[-0.5, 1.5]`, SDE in `[-1e6, 1e6]`. A `NEW` candidate must name a
   TIC in the unit whose star was measured. Extra stars outside the unit are
   ignored.
5. A passing check marks the unit agreed and inserts candidates. A failing
   check reissues the unit (generation bumps, verdict `disagreed`). Candidate
   rows are not inserted. The result row stays, so a later `reverify` can
   check it again without deleting anything.
6. The local dashboard polls `GET /api/status` about every 1.5 seconds and
   draws CPU, network, worker state, and disk from that one payload.

`Store.copies_required` is always 1. A `coord.toml` that still says
`copies_required = 2` loads, and the process still checks one upload. The
value `3` is still a config error. `replica_count()` returns 1, so
`settings.replicas` is not used to decide how many leases a unit gets.

## Coordinator endpoints

Worker task API, unchanged for the installed app:

| Method | Path | Role |
| --- | --- | --- |
| GET | `/api/v1/projects/{project}/tasks/current` | Current lease. Legacy: `/api/v1/work/current`. |
| POST | `/api/v1/projects/{project}/tasks/claim` | Lease a unit. Body `{}` or `{prefetch:true}`. Optional `sectors`. |
| POST | `…/peek` | Next unit, no lease. Optional `sectors`. |
| POST | `…/heartbeat` | Renew a lease. |
| POST | `…/submit` | Upload stars and candidates. |
| POST | `…/release` | Drop a lease. |
| POST | `…/receipt` | Completeness of a stored upload. |

Account routes still exist so an old client gets a JSON 403 rather than a
404. `signup`, `device/start`, `device/poll`, `device/preview`,
`device/approve`, and `device/deny` refuse immediately. HTML `GET`/`POST
/signup` and `GET /link` return the same closed message. Login, logout, and
token create/list/revoke stay. Invite codes can still be created; signup will
not consume them.

`GET /api/v1/public/config` is always
`{"signup":"closed","copies_required":1,"device_linking":"closed"}`.

Operator reads (`/api/v1/status`, `/api/v1/workers`, `/api/v1/candidates`,
`/api/v1/events`) still require the admin token. Phase 3 adds a private
``GET /sectors`` HTML page (admin token or account session) and
``GET /api/v1/mission/overview`` (same auth). ``GET /api/v1/mission/progress``
accepts a worker ``rhx_`` token or operator auth; the worker dashboard proxies
it at ``GET /api/mission/progress``.

## Worker dashboard endpoints

The dashboard sends `Cache-Control: no-store` on every response. HTML
substitutes `__APP_VERSION__` with the package version, and asset URLs carry
`?v=`.

| Method | Path | Role |
| --- | --- | --- |
| GET | `/` | Status shell. |
| GET | `/sectors` | Holdings, download-ahead sectors, processed limit, per-sector delete. |
| GET | `/vetting` | Existing vetting page. |
| GET | `/batches`, `/discoveries`, `/candidates` | Placeholders for later phases. |
| GET | `/api/status` | One snapshot: batch, pause, CPU, network, processed bytes. |
| GET | `/events` | One-shot SSE kept so older clients still parse. The new page does not use it. |
| POST | `/api/pause` | `{paused: bool}`. Same flag as the tray. |
| POST | `/api/prefetch/sectors` | `{sectors:[…]}`. Empty means any sector. |
| GET | `/api/retention` | Processed bytes, limit, per-sector rows. |
| POST | `/api/retention/limit` | `{processed_limit_gb}`. Enforces immediately. |
| POST | `/api/retention/delete` | `{sector}`. Refuses a sector that is still being scanned. |

## Storage

**Coordinator** (`coord.sqlite`). Units, assignments, results, candidates,
workers, accounts, sessions, device grants, invite codes, contributions,
events. Old account and device tables stay even though signup and linking are
closed. Candidates are inserted only after verification. Result rows are kept
on a failed check.

**Worker cache** (under the worker data directory):

- `data/sNNNN/` — downloaded and kept light curves.
- `results/**/sNNNN/` — per-assignment search output for replay.
- `dashboard.sqlite` — local star rows and candidates. `candidate=1` and the
  `candidates` table mark files the retention pass must not delete.
- `prefs.json` — `max_cache_gb` (download admission), `download_sectors`,
  `processed_limit_gb` (default 20).
- `state.json`, `next.json`, `queue.json` — the batch in hand. A sector that
  is not `done` or `uploaded` is active and is not deleted.

Two caps are different. `max_cache_gb` limits how much the downloader will
hold before a search. `processed_limit_gb` (default 20 GB) limits light curves
and result folders kept afterwards. Enforcement drops the oldest sector that
has no candidates until the tree fits, and it never deletes a candidate light
curve or the result folder of a sector that has candidates. A manual delete
does the same for one sector and refuses a sector that is still in progress.

## What was fragile, and why

The previous dashboard was one page patched in place.

- Canvases were sized from their own parent height inside a shared helper.
  A canvas that grew its parent made the next frame grow again, so panels
  blanked or shrank.
- The same histogram drawer was defined twice. Whichever copy won depended on
  script order.
- Sky, starfield, and periodogram loops used `requestAnimationFrame` all the
  time, including when the tab was in the background. That is the ~1 fps
  chop when the machine was also searching.
- “Loading sky survey…” had no abort. A slow or failed image left the label
  up, and the next poll did not clear it.
- History arrays and the log grew without a cap, so a long session redrew
  more points every frame.
- HTML was easy to keep from an older worker build. A new script against an
  old page, or the reverse, failed quietly. API responses were already
  `no-store`; the page itself was not version-stamped.

The coordinator has its own sharp edges, unchanged by this phase except for
the single-copy check:

- One process-wide lock serializes every request, and SQLite can still return
  `busy` if a backup or another process holds the file.
- The events table is trimmed. It is a log, not a queue.
- `X-Forwarded-For` is trusted because the server binds on localhost behind
  Caddy. Do not expose port 8787 directly.
- `reverify` promotes a pending complete upload. It does not delete result or
  assignment rows. A deploy of this code does not need a migration.

The new shell gives each panel a fixed CSS height, sizes a canvas from the
`.plot` box (never from the canvas height), caps history at 90 points and the
feed at 12 lines, polls one status URL, and stamps assets with the version.
Charts redraw when a sample arrives, not on every animation frame.
