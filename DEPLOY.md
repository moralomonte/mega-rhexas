# Deploy the Mega Rhexas coordinator

This is the friends-beta server. It does not download light curves and it does not run the transit search. Friends' computers do that. The server only hands out batches of about 500 TIC IDs, stores the small result files, and keeps a batch when two different people agree.

The coordinator's public address is **https://api.megarhexas.com**. The same droplet also serves the public website at **https://megarhexas.com** and **https://www.megarhexas.com** from the `site/` folder. The domain `megarhexas.com` is registered at Namecheap, and DNS stays at Namecheap for now. Pages on `https://megarhexas.com` call the API with cookies; Caddy serves the site as static files and does not proxy the apex.

One small machine is enough: a DigitalOcean Basic droplet, about $6/month, **1 GB RAM / 1 vCPU / 25 GB SSD**, Ubuntu. The same files run later on a home server or another host. Copy the database and the workers keep using the same URL.

## 1. Create the droplet in San Francisco (SFO3)

1. Sign in at [digitalocean.com](https://www.digitalocean.com/) and create a project if you do not have one.
2. **Create → Droplets**.
3. **Region: San Francisco, datacenter SFO3** (US west coast). Some workers are in the Americas, so this is the closest cheap region to them. The owner in Japan is fine with about 100 ms of delay, sometimes a bit more: a worker only sends a short "give me a batch" message and later a few-megabyte result. That traffic is not sensitive to latency.
4. To use a different region, pick another city on that same Region step before you click create (for example New York, Amsterdam, or Singapore, whichever is nearer the computers that will run the worker). The software does not care which datacenter it is. You cannot move a droplet to another region later; you would create a new one and copy `/var/lib/rhexas` across (see the last section).
5. **Image: Ubuntu 24.04 LTS**. The coordinator needs Python 3.11 or newer. Ubuntu 24.04 already has it. Ubuntu 22.04's default Python is too old.
6. **Size: Basic, shared CPU, the plan with 1 GB RAM / 1 vCPU / 25 GB SSD** (about $6/month). If the price in the UI has moved, pick the size with those specs.
7. **Authentication: SSH key.** On your own computer, `ssh-keygen -t ed25519` (press Enter through the prompts) and paste the contents of `~/.ssh/id_ed25519.pub` into DigitalOcean. A password login is easier to guess; use a key.
8. Hostname: `rhexas`. Create the droplet and copy its public IPv4 address. This guide calls it `DROPLET_IP`.

In DigitalOcean's firewall (optional, under Networking → Firewalls), allow inbound TCP **22, 80, and 443** only. Leave 8787 closed. The coordinator listens on localhost; Caddy is the only public program.

## 2. Point api.megarhexas.com at the droplet

DNS is at Namecheap (Namecheap BasicDNS), not at DigitalOcean.

1. Sign in at [namecheap.com](https://www.namecheap.com/) → **Domain List** → **Manage** next to `megarhexas.com`.
2. **Nameservers** should be **Namecheap BasicDNS**. That is the default. Leave it there while DNS stays at Namecheap. If you later move DNS to another company, you will recreate this same A record there; Caddy does not care who hosts the DNS.
3. Open the **Advanced DNS** tab.
4. **Add New Record**:
   - Type: **A Record**
   - Host: **`api`**
   - Value: `DROPLET_IP`
   - TTL: Automatic
5. Add the public website on the same address. These two records serve `site/` (the static pages). Remove any Namecheap parking or URL redirect already sitting on `@`, or the apex will not reach this droplet.
   - Type: **A Record**, Host: **`@`**, Value: `DROPLET_IP`, TTL: Automatic
   - Type: **A Record**, Host: **`www`**, Value: `DROPLET_IP`, TTL: Automatic
   The `api` record from the previous step stays. All three names point at this droplet. Caddy chooses the site from the hostname.
6. Save. Check from your computer:

```bash
dig +short api.megarhexas.com
dig +short megarhexas.com
dig +short www.megarhexas.com
```

Each one should print `DROPLET_IP`. Namecheap usually updates in a few minutes. Sometimes it takes an hour.

## 3. Log in and install the program

From your computer:

```bash
ssh root@DROPLET_IP
```

On the droplet:

```bash
apt update
apt install -y python3 ca-certificates curl git debian-keyring debian-archive-keyring apt-transport-https
git clone https://github.com/moralomonte/mega-rhexas.git /opt/mega-rhexas
```

Do not `pip install` this repository on the droplet. That would download the astronomy libraries (gigabytes of RAM and disk) that only the workers need. The coordinator is plain Python 3.12 from Ubuntu, using `PYTHONPATH`.

```bash
useradd --system --create-home --home-dir /var/lib/rhexas --shell /usr/sbin/nologin rhexas
mkdir -p /var/lib/rhexas
chown rhexas:rhexas /var/lib/rhexas
```

## 4. Create the database and the admin token

```bash
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord init --config /var/lib/rhexas/coord.toml
```

That writes `/var/lib/rhexas/coord.toml` and `/var/lib/rhexas/coord.sqlite`, and prints an **admin token** once. It is also stored in `/var/lib/rhexas/admin.token` (mode 600). You paste this token into the status page. It is not a worker invite and it is not a signup code.

Signup starts **invite-only**. The example `[accounts]` block in `deploy/coord.example.toml` is already the default when those keys are absent, so an existing `coord.toml` does not need a rewrite. `coord.toml` binds `127.0.0.1:8787` on purpose. The hostname `api.megarhexas.com` is Caddy's job, not a setting in this file.

## 5. Enroll sectors

This downloads the MAST *list* of star IDs for a sector (a text file), not the light curves. The light curves are downloaded later by each friend's PC.

```bash
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord --config /var/lib/rhexas/coord.toml enroll --sector 107 --fetch
```

Repeat for each sector you want in the beta, for example 106 down through 96. Each sector becomes batches of 500 TIC IDs (the last batch in a sector can be smaller). Running enroll twice for the same sector is refused, so a sector is not queued twice.

If the droplet cannot reach MAST, download `tesscurl_sector_107_lc.sh` on your own computer from `https://archive.stsci.edu/missions/tess/download_scripts/sector/` and:

```bash
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord --config /var/lib/rhexas/coord.toml enroll --sector 107 --curl-script /path/to/tesscurl_sector_107_lc.sh
```

## 6. Invite friends

Signup is invite-only until you set `signup = "open"` under `[accounts]`. Create a signup code and send that, not an `rhx_` token:

```bash
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord --config /var/lib/rhexas/coord.toml invite create --label ada
```

The code starts with `inv_` and is shown once. The friend opens **https://api.megarhexas.com/signup** (or the public site, when it is up), creates an email and password, then on their computer runs `rhexas worker --login`. That opens a browser to a link-this-computer page. They approve, and the worker stores its own `rhx_` token. No copy-paste. `--token-file` still works for a token you already have.

```bash
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord --config /var/lib/rhexas/coord.toml invite list
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord --config /var/lib/rhexas/coord.toml invite revoke 1
```

`invite list` prints id, label, and remaining uses. It does not print the code. `invite revoke` takes that id or the code itself. `--uses N` on create lets one code open N accounts (default 1).

Two people must be running or a batch can never finish: every batch is given to two **different accounts**, and the server keeps it only when both top hits agree (period within 0.2%, SDE within 0.25, depth within 5% or 100 ppm). Two computers on the same account cannot be the two copies of one batch. One account can still link several computers; those computers just will not be paired with each other.

Old `token create` invites keep working. On the next restart each existing `rhx_` token becomes a legacy account named after that invite, with no email and no password. New ones do the same:

```bash
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord --config /var/lib/rhexas/coord.toml token create ada
```

The token starts with `rhx_` and is shown once. Send it in a private message if you are not using signup. `token list` and `token revoke ada` are unchanged. Revoke drops any batch that token still has checked out. A friend who already has a token does not need to sign up.

## 7. Run the coordinator under systemd

```bash
cp /opt/mega-rhexas/deploy/rhexas-coord.service /etc/systemd/system/rhexas-coord.service
systemctl daemon-reload
systemctl enable --now rhexas-coord
systemctl status rhexas-coord
```

It listens on `127.0.0.1:8787` only. Check the text summary over SSH:

```bash
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord --config /var/lib/rhexas/coord.toml status
```

## 8. HTTPS with Caddy

Caddy obtains Let's Encrypt certificates for `api.megarhexas.com`, `megarhexas.com`, and `www.megarhexas.com` by itself, once those A records point here and ports 80 and 443 are open.

```bash
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update
apt install -y caddy
cp /opt/mega-rhexas/deploy/Caddyfile.host /etc/caddy/Caddyfile
systemctl reload caddy
```

Optional, so Let's Encrypt can email you before a certificate expires: add this block at the top of `/etc/caddy/Caddyfile`, with your own address, then reload Caddy again.

```
{
    email you@example.com
}
```

Open the firewall on the droplet if `ufw` is enabled:

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

Visit **https://api.megarhexas.com**. The page asks for the admin token from step 4. Worker tokens and account passwords do not open it: the page lists unpublished candidate stars. After login you should see sector progress, who is online, candidates both copies agreed on, and a console of claims, agreements, and expired leases. It refreshes every two seconds. That admin page is unchanged.

The same host also serves the fallback account pages, in the same dark style, so people can sign up before the public site is deployed: `/signup`, `/login`, `/account`, and `/link`. `/login` on a GET is the account form. The admin token is still a POST to `/login`.

Visit **https://megarhexas.com**. That is the public site: home, how it works, the leaderboard, and the account pages. **https://www.megarhexas.com** serves the same files. The pages call `https://api.megarhexas.com` for `GET /api/v1/public/stats` and for the account routes. Caddy only serves files for the apex; it does not proxy them to the coordinator.

`deploy/Caddyfile.host` imports `deploy/Caddyfile.site`. The import path is `/opt/mega-rhexas/deploy/Caddyfile.site`, so the snippet stays in the git checkout. Do not copy the site files somewhere else unless you also change `root` in that snippet. The root is `/opt/mega-rhexas/site`.

If the browser says the certificate is missing, `dig +short` for that hostname is the first check, then `journalctl -u caddy -n 50`. Let's Encrypt will not issue a certificate until the name resolves to this machine. A certificate error on the apex alone usually means the `@` or `www` record is still parking, or Caddy was reloaded before DNS updated. Caddy retries.

## 9. What you will see once friends are running

- A batch is checked out to two different accounts. While they search, it counts as leased.
- Each worker heartbeats. The lease is 12 hours and can be extended up to 36 hours. After that the batch goes back to the pool. A late upload is refused.
- If the two period / SDE / depth values disagree, neither account gets credit and the batch is issued again (a new generation).
- When they agree, both accounts get credit for the stars they actually measured, and for new candidates that **both** lists marked NEW. Those candidates show on the admin page only. Until the other account agrees, the upload counts as pending on the public leaderboard (display names, no emails).
- Raw light curves never land on this server. An upload is a few hundred kilobytes to a couple of megabytes.

## 10. Backups

The whole beta is `/var/lib/rhexas` (the sqlite file, the admin token, `coord.toml`).

```bash
mkdir -p /var/lib/rhexas/backups
sqlite3 /var/lib/rhexas/coord.sqlite ".backup /var/lib/rhexas/backups/coord-$(date +%F).sqlite"
chown rhexas:rhexas /var/lib/rhexas/backups/coord-$(date +%F).sqlite
```

Copy that file off the droplet sometimes. Losing the sqlite file loses accounts, invites, progress, and credit.

If you put an SMTP password in `coord.toml`, keep that file mode 600 and owned by `rhexas` (`chmod 600 /var/lib/rhexas/coord.toml`). The coordinator reads it as that user. Leave `[smtp]` out, or leave `host` empty, and accounts work without a verification email.

To rotate the status-page token:

```bash
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord --config /var/lib/rhexas/coord.toml admin-token --reset
```

## 11. Update the code later

```bash
cd /opt/mega-rhexas
git pull
systemctl restart rhexas-coord
systemctl reload caddy
```

The database stays in `/var/lib/rhexas`. A pull does not copy it, replace it, or rebuild its tables. Restart applies additive migrations in place (`ALTER TABLE` and `CREATE TABLE IF NOT EXISTS` only). Existing `rhx_` invites become legacy accounts named after the invite. Existing contribution rows stay; they gain an account id and a `revoked` flag that stays 0. Take the sqlite backup in section 10 before the first restart that includes accounts, the same way you would before any other upgrade. Caddy reads the public site from disk, so the reload is what picks up a changed `deploy/Caddyfile.site`.

## 12. Move to a home server or another host

Stop the service (`systemctl stop rhexas-coord`). Copy `/var/lib/rhexas` to the new machine. Install Ubuntu 24.04, Python 3, and Caddy the same way, with the same `/opt/mega-rhexas` checkout and the same systemd unit. At Namecheap, change the `api`, `@`, and `www` A records to the new public IP. Workers keep `https://api.megarhexas.com` and the same invite tokens, because the tokens and the accounts live in the sqlite file. The public site keeps `https://megarhexas.com`. Caddy will ask Let's Encrypt for new certificates after DNS points at the new machine.

Give the home connection a stable address (a DHCP reservation, or a small VPS in front) so the A record does not go stale. The coordinator itself has no DigitalOcean-specific code.

## Docker, if you would rather use containers

On a 1 GB droplet, Docker plus the science stack is the wrong trade. Docker with only this coordinator and Caddy is fine if you already like Compose. The image does not install the astronomy libraries.

```bash
cd /opt/mega-rhexas
docker compose build
docker compose run --rm coord init --config /var/lib/rhexas/coord.toml
docker compose run --rm coord --config /var/lib/rhexas/coord.toml enroll --sector 107 --fetch
docker compose run --rm coord --config /var/lib/rhexas/coord.toml invite create --label ada
docker compose up -d
```

`deploy/Caddyfile` defaults to `api.megarhexas.com`. Ports 80 and 443 are published. The coordinator is only reachable through Caddy. Compose does not serve the public site. The apex and `www` are the systemd path in `deploy/Caddyfile.site`.

## What stays open for later

This beta is the exoplanet search only. The tables are not TESS-specific: each work unit, assignment, and result has a `project` (default `exoplanets`), and the task body is a JSON payload. Workers call `/api/v1/projects/exoplanets/tasks/claim` (and `current`, `heartbeat`, `submit`, `release`). The older `/api/v1/work/...` paths do the same thing for exoplanets.

Period, SDE, and depth comparison is an exoplanet project handler. Another science would register its own handler. The generic handler accepts one upload and does not compare.

`workers.id` is still the device id stored as `contributions.user_id`, so an assignment's `user_id` does not change meaning. Credit belongs to `contributions.account_id`. The table stays append-only: this process does not update or delete those rows. A row is pending after `submitted`, and verified when the other account agrees (`outcome = agreed`, `verified = 1`). Phase 4 scoring can set `revoked = 1` to drop pending or verified points without rewriting history. `accounts.tier` and `accounts.trust_score` are stored empty for that scoring. There is no point total in the database. The public leaderboard is computed from these rows.

Leave `replicas` unset and exoplanets keeps two copies, which must come from two different accounts. Set `replicas = 1` to accept a single upload with no comparison. Teams are not built here. Google sign-in is not wired; `accounts.provider` and `accounts.provider_sub` are reserved for it.

## Knobs worth knowing

In `/var/lib/rhexas/coord.toml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `unit_size` | 500 | TIC IDs per batch |
| `lease_hours` | 12 | Silent worker loses the batch |
| `max_lease_hours` | 36 | Heartbeats cannot hold it longer than this |
| `period_rtol` | 0.002 | Top periods must be within 0.2% |
| `sde_atol` | 0.25 | Top SDE absolute window |
| `depth_rtol` / `depth_atol` | 0.05 / 0.0001 | Depth within 5% or 100 ppm, whichever is larger |
| `max_miss_fraction` | 0.02 | Stars only one worker measured, as a fraction of the batch |
| `replicas` | omitted | Copy count. Omit it and the project decides (exoplanets: 2). `1` skips the comparison |
| `[accounts] signup` | `invite` | `invite` requires `invite create`. `open` lets anyone sign up |
| `[accounts] public_origin` | `https://megarhexas.com` | Browser origin allowed to call the API with cookies |
| `[accounts] api_public_url` | `https://api.megarhexas.com` | Device-link and email links |
| `[accounts] cookie_secure` | `true` when the API URL is https | `Secure` and `SameSite=None` on the session cookie |
| `[accounts] session_days` | 30 | Session lifetime, from 1 to 365 |
| `[accounts] extra_origins` | none | More browser origins, as a list |
| `[smtp] host` | empty | Empty means no verification email. Accounts still work |

These are check windows, not the search. The BLS grid, SDE cut, and SNR cut stay in `rhexas.toml` on each worker and are the same as a local `rhexas search`.

Restart `rhexas-coord` after editing the file. Tolerances, signup mode, and SMTP are read at startup.

## Accounts and the public site

The public site at `https://megarhexas.com` is not served by this droplet. It calls `https://api.megarhexas.com` from the browser. Responses echo that origin, set `Access-Control-Allow-Credentials: true`, and answer `OPTIONS` preflights. The site must send `credentials: 'include'` and `Content-Type: application/json` on POSTs. A present `Origin` that is not `public_origin`, `api_public_url`, or `extra_origins` is rejected on cookie-changing requests. Missing `Origin` (curl, workers) is allowed.

The session cookie is `rhexas_session`. It is `HttpOnly`, `Path=/`, `Secure`, and `SameSite=None` when `cookie_secure` is true, so the apex can send it to the API host. It is not readable from JavaScript. Logout clears it. The admin cookie (`rhexas_admin`) is separate and still only for the status page.

JSON the public site uses (no worker tokens in these responses except device poll):

| Call | Body | Result |
| --- | --- | --- |
| `POST /api/v1/signup` | `email`, `password`, `display_name`, `invite_code` | 201, `Set-Cookie`, account fields plus `email_verification` (`sent`, `failed`, or `skipped`) |
| `POST /api/v1/login` | `email`, `password` | 200 and `Set-Cookie` |
| `POST /api/v1/logout` | empty | 204, cookie cleared |
| `GET /api/v1/me` | session cookie | `id`, `email`, `display_name`, `email_verified`, `provider`, `legacy`, `created_at` |
| `GET /api/v1/me/devices` | session cookie | `{devices: [{id, name, hostname, kind, created_at, last_seen_at, revoked_at}]}` |
| `POST /api/v1/me/devices/{id}/revoke` | session cookie | `{status: revoked}` |
| `GET /api/v1/me/stats` | session cookie | display name, email, verified and pending stars and batches, device count |
| `GET /api/v1/public/stats` | none | sector progress and `leaderboard` of `{display_name, stars_verified, stars_pending, batches_verified, batches_pending}` |
| `POST /api/v1/device/start` | `{hostname}` | `{device_code, user_code, verification_url, interval, expires_in}` |
| `POST /api/v1/device/poll` | `{device_code}` | `authorization_pending`, `slow_down`, or `approved` with the `rhx_` token once |
| `GET /api/v1/device?code=` | session cookie | `{hostname, status, code}` for the approve screen |
| `POST /api/v1/device/approve` or `/deny` | `{code}` or `{user_code}` | hostname and device id. The token is not returned to the browser |

`verification_url` is `https://api.megarhexas.com/link` with no query. The worker opens `verification_url?code=USER_CODE`. Codes last 10 minutes and work once. A signed-in user sees “Link this computer:” followed by the hostname, and approves. Public stats omit emails, worker names, and candidate lists. Candidates stay on the admin page.

`GET /api/v1/me/stats` is the only stats route that includes an email, and only for the signed-in account. Signup and login are rate limited per IP. Failed logins are also limited per email. Passwords are hashed with stdlib scrypt.

`rhexas worker --login` stores the token at `~/.local/share/rhexas/worker.token` on Linux (`XDG_DATA_HOME` if set), `~/Library/Application Support/rhexas/worker.token` on macOS, and `%APPDATA%\rhexas\worker.token` on Windows, mode 600 where the filesystem allows it. An installer or the dashboard setup screen can call `rhexas.worker.login.link_this_computer()`.

To turn verification email on, add `[smtp]` with `host`, `port`, `username`, `password`, `from`, and `starttls`. A signup then returns `email_verification: sent` and the account can confirm with the link. Login and device linking work before that click. If `host` is empty, `email_verification` is `skipped` and the account stays unverified on purpose.
