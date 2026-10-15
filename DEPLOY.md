# Deploy the Mega Rhexas coordinator

This is the friends-beta server. It does not download light curves and it does not run the transit search. Friends' computers do that. The server only hands out batches of about 500 TIC IDs, stores the small result files, and keeps a batch when two different people agree.

The public address is **https://api.megarhexas.com**. The domain `megarhexas.com` is registered at Namecheap, and DNS stays at Namecheap for now. The apex (`megarhexas.com` with no subdomain) is reserved for the future public site. This droplet serves only `api`.

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
5. The apex stays free for the public site. The host on this record is `api`, so the name that resolves is `api.megarhexas.com`. Leave any parking or redirect Namecheap already has on `@` (the apex) as it is, and do not add an A record whose host is `@` pointing at this droplet.
6. Save. Check from your computer:

```bash
dig +short api.megarhexas.com
```

It should print `DROPLET_IP`. Namecheap usually updates in a few minutes. Sometimes it takes an hour.

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

That writes `/var/lib/rhexas/coord.toml` and `/var/lib/rhexas/coord.sqlite`, and prints an **admin token** once. It is also stored in `/var/lib/rhexas/admin.token` (mode 600). You paste this token into the status page. It is not a worker invite.

`coord.toml` binds `127.0.0.1:8787` on purpose. The hostname `api.megarhexas.com` is Caddy's job, not a setting in this file.

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

One token per person. Two people must be running or a batch can never finish: every batch is given to two different tokens, and the server keeps it only when both top hits agree (period within 0.2%, SDE within 0.25, depth within 5% or 100 ppm).

```bash
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord --config /var/lib/rhexas/coord.toml token create ada
```

The token starts with `rhx_` and is shown once. Send it in a private message. The friend follows [FRIENDS.md](FRIENDS.md). Their coordinator URL is already `https://api.megarhexas.com`; they only need the token.

```bash
sudo -u rhexas env PYTHONPATH=/opt/mega-rhexas/src \
  python3 -m rhexas.coord --config /var/lib/rhexas/coord.toml token list
```

Revoke a lost token with `token revoke ada`. That also drops any batch they still have checked out.

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

Caddy obtains a Let's Encrypt certificate for `api.megarhexas.com` by itself, once the A record points here and ports 80 and 443 are open.

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

Visit **https://api.megarhexas.com**. The page asks for the admin token from step 4. Worker tokens do not open it: the page lists unpublished candidate stars. After login you should see sector progress, who is online, candidates both copies agreed on, and a console of claims, agreements, and expired leases. It refreshes every two seconds.

If the browser says the certificate is missing, `dig +short api.megarhexas.com` is the first check, then `journalctl -u caddy -n 50`. Let's Encrypt will not issue a certificate until the name resolves to this machine.

## 9. What you will see once friends are running

- A batch is checked out to two different names. While they search, it counts as leased.
- Each worker heartbeats. The lease is 12 hours and can be extended up to 36 hours. After that the batch goes back to the pool. A late upload is refused.
- If the two period / SDE / depth values disagree, neither person gets credit and the batch is issued again (a new generation).
- When they agree, both get credit for the stars they actually measured, and for new candidates that **both** lists marked NEW. Those candidates show on the page. The numbers are what a later public leaderboard will use.
- Raw light curves never land on this server. An upload is a few hundred kilobytes to a couple of megabytes.

## 10. Backups

The whole beta is `/var/lib/rhexas` (the sqlite file, the admin token, `coord.toml`).

```bash
mkdir -p /var/lib/rhexas/backups
sqlite3 /var/lib/rhexas/coord.sqlite ".backup /var/lib/rhexas/backups/coord-$(date +%F).sqlite"
chown rhexas:rhexas /var/lib/rhexas/backups/coord-$(date +%F).sqlite
```

Copy that file off the droplet sometimes. Losing the sqlite file loses invites, progress, and credit.

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
```

The database stays in `/var/lib/rhexas`. A pull does not touch it.

## 12. Move to a home server or another host

Stop the service (`systemctl stop rhexas-coord`). Copy `/var/lib/rhexas` to the new machine. Install Ubuntu 24.04, Python 3, and Caddy the same way, with the same `/opt/mega-rhexas` checkout and the same systemd unit. At Namecheap, change the `api` A record to the new public IP. Workers keep `https://api.megarhexas.com` and the same invite tokens, because the tokens live in the sqlite file. Caddy will ask Let's Encrypt for a new certificate after DNS points at the new machine.

Give the home connection a stable address (a DHCP reservation, or a small VPS in front) so the A record does not go stale. The coordinator itself has no DigitalOcean-specific code.

## Docker, if you would rather use containers

On a 1 GB droplet, Docker plus the science stack is the wrong trade. Docker with only this coordinator and Caddy is fine if you already like Compose. The image does not install the astronomy libraries.

```bash
cd /opt/mega-rhexas
docker compose build
docker compose run --rm coord init --config /var/lib/rhexas/coord.toml
docker compose run --rm coord --config /var/lib/rhexas/coord.toml enroll --sector 107 --fetch
docker compose run --rm coord --config /var/lib/rhexas/coord.toml token create ada
docker compose up -d
```

`deploy/Caddyfile` defaults to `api.megarhexas.com`. Ports 80 and 443 are published. The coordinator is only reachable through Caddy.

## What stays open for later

This beta is the exoplanet search only. The tables are not TESS-specific: each work unit, assignment, and result has a `project` (default `exoplanets`), and the task body is a JSON payload. Workers call `/api/v1/projects/exoplanets/tasks/claim` (and `current`, `heartbeat`, `submit`, `release`). The older `/api/v1/work/...` paths do the same thing for exoplanets.

Period, SDE, and depth comparison is an exoplanet project handler. Another science would register its own handler. The generic handler accepts one upload and does not compare.

`workers.id` is the stable user id for now. The `contributions` table is append-only: user id, project, unit, outcome (`submitted`, `agreed`, or `rejected`), a verified flag, and a timestamp. Nothing in this server updates or deletes those rows. Later scores should count rows with verified = 1, not CPU time. The status page still shows the running star and candidate totals for this beta.

Leave `replicas` unset and exoplanets keeps two copies. Set `replicas = 1` to accept a single upload with no comparison. User accounts, teams, and leaderboards are not built here.

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

These are check windows, not the search. The BLS grid, SDE cut, and SNR cut stay in `rhexas.toml` on each worker and are the same as a local `rhexas search`.

Restart `rhexas-coord` after editing the file. Tolerances are read at startup.
