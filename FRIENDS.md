# Run a Mega Rhexas worker

You were invited to the friends beta. Your computer downloads a few hundred TESS stars at a time, searches them for transits, uploads a small results file (usually well under a few megabytes), and deletes the big files. The server is **https://api.megarhexas.com**. That address is already the default. You need a personal token from the person who invited you. It starts with `rhx_`.

Two different people search each batch. Your token and someone else's have to both be running, or batches sit waiting for the second copy. Please don't share your token or run a second copy of it: the check is "two different invites agreed."

The search settings (period grid, SDE, SNR, and the rest) are the project's usual ones. The knobs below only change how hard your own computer works.

## Just download and run

On Windows you do not need git, Python, or a command prompt.

1. Download **MegaRhexas-Setup** from the [latest release](https://github.com/moralomonte/mega-rhexas/releases/latest).
2. Run it. If Windows says it protected your PC, that is SmartScreen reacting to an unsigned installer. Click **More info**, then **Run anyway**. The installer does not need an administrator password.
3. Leave **Start Mega Rhexas when I sign in** unchecked unless you want that. It is off by default.
4. Open **Mega Rhexas** from the Start menu. Your browser opens the dashboard. Eco, Live, and Turbo are on that page. Live is already selected.
5. The first time, paste the invite token (`rhx_…`) into the box and choose **Save and start**. It is stored on this computer and the page does not show it again. **Sign in with megarhexas.com** is there for when browser sign-in is switched on; until then, pasting the token is the way in.
6. Leave it running. The triskelion in the tray opens the dashboard, changes Eco / Live / Turbo, pauses, or quits. Closing the laptop pauses the work. Opening Mega Rhexas again continues.

The light curves stay in your AppData folder (`%LOCALAPPDATA%\Mega Rhexas\cache`), not in the install folder. The cap is 20 GB. The worker stays at low priority and stops if the drive has under 5 GB free. Uninstalling the app leaves that folder, including the token, so installing again does not ask you to paste it a second time. Delete `%LOCALAPPDATA%\Mega Rhexas` if you want the token gone.

A newer version shows up as a link on the dashboard. Quit Mega Rhexas from the tray, then download the installer and run it. The app will not install it for you. Quit first so Windows can replace the program files.

The installer is about 100 MB (about 400 MB once installed), because Python and the astronomy libraries are inside it. A Mac app is not built yet. Until it is, use the source checkout below.

## Windows (from source)

Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/) and tick **Add python.exe to PATH**. Then, in a folder you don't mind keeping, for example `C:\rhexas`:

```bat
git clone https://github.com/moralomonte/mega-rhexas.git
cd mega-rhexas
py -3 -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -e .
```

Put the token in `token.txt` (one line, nothing else) next to the checkout. In Notepad, save it and confirm the file is not named `token.txt.txt`.

```bat
.venv\Scripts\rhexas worker --token-file token.txt
```

Leave that window open. Closing the laptop pauses the work. When you open it again, run the same command. If the server still has your batch, the worker continues where it stopped. If the lease expired (12 hours without a heartbeat, 36 hours at most), the batch goes to someone else and you get a new one.

Stop with Ctrl+C. The batch stays checked out to you until the lease ends, so starting the command again is the way to resume.

## Mac or Linux

```bash
git clone https://github.com/moralomonte/mega-rhexas.git
cd mega-rhexas
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
printf '%s\n' 'rhx_paste_your_token_here' > token.txt
chmod 600 token.txt
rhexas worker --token-file token.txt
```

## What it is doing

1. Ask api.megarhexas.com for a batch (a sector and a list of TIC IDs).
2. Download only those stars from MAST (the STScI archive).
3. Run the same BLS search and the same filters as `rhexas search` and `rhexas filter`.
4. Upload the summary (period, SDE, depth, and any candidates). Plots and FITS files stay on your machine and are deleted after a successful upload.
5. Repeat.

The first time, it also fetches the public TOI and confirmed-planet catalogs (small files) so a known planet is not labeled new. Those stay in `worker-cache/catalogs`.

If MAST answers "slow down" (HTTP 429 or 503), the worker waits and retries. That backoff starts at 15 seconds and doubles, with a little random jitter so a room full of friends does not retry in lockstep. The same wait-and-retry covers the SSL EOF errors home connections see from the MAST download servers. A file that stopped halfway is continued, not started over. After that, any star that still has no file is tried once more, and any star that still has no measurement is tried once more before the results are sent. The worker uses 2 download threads by default.

## Make it gentler, or a bit faster

Defaults: **low** process priority, **2** search processes, **2** download threads, stop if the disk has under **5 GB** free, and keep the temporary light curves under **20 GB**. A batch is about a gigabyte and is deleted when the upload succeeds.

```bat
.venv\Scripts\rhexas worker --token-file token.txt --procs 4 --threads 2 --priority low --max-cache-gb 20
```

`--priority idle` yields even more to whatever else you are doing. `--priority normal` competes with your foreground apps. `--procs` is the one to raise on a desktop you are not sitting at. A laptop is happier at 2.

You can put the same values in `rhexas.toml` under `[worker]` (`procs`, `threads`, `priority`, `max_cache_gb`, `min_free_gb`, `cache_dir`). Leave `coordinator_url` as `https://api.megarhexas.com` unless the owner tells you otherwise. Keep the token in `token.txt`, not in the toml file.

Environment variables work too: `RHEXAS_WORKER_TOKEN` and `RHEXAS_COORDINATOR_URL`.

## Watch it live

Two commands show what **your** worker is doing. They stay on your machine. They do not call the coordinator, and the page does not show your token.

```bat
.venv\Scripts\rhexas worker --token-file token.txt --dashboard
```

```bash
rhexas worker --token-file token.txt --dashboard
rhexas worker-dashboard
```

`--dashboard` starts the worker and the page together, and opens a browser. `worker-dashboard` attaches to a worker you already started. The page binds to `127.0.0.1` port `8080`. If that port is busy it picks the next free one. Add `--dashboard-host 0.0.0.0` only when you want another computer on your LAN to open it. `--no-browser` skips opening a browser.

The page is the same mission-control screen as the sector dashboard: rings, sky map, the scanning light curve, the histogram that cycles SDE and SNR, and the console. Eco, Live, and Turbo change how hard your worker runs. Eco pauses between stars and keeps the batch you already have. The lit button is the mode the worker is using, and the click does not send your invite. The numbers are your batch (project, sector, unit, stars done, ETA, stars per second), your candidates with period, depth, and SNR, how many stars you have searched, how many batches you have uploaded, and how many contributions are pending or verified. Cache use is drawn against `--max-cache-gb`, with CPU, process count, thread count, and the coordinator connection.

Every hit is labeled **candidates, unverified**. Nothing on this page is a confirmed planet. Pending and verified describe contributions for a future account. There are no coins. The files it uses are `worker-cache/dashboard.sqlite` and `worker-cache/worker.log`. The speed buttons write the mode into that database.

## If something looks stuck

- `no work right now` means every batch is either finished or already checked out. The worker waits a minute and asks again. Leave it running.
- `coordinator unreachable` means your network, or the server is restarting. It waits and retries. You do not need to delete anything.
- A disk message means the drive is under 5 GB free even after the cache was emptied. Free some space and start the command again.
- The temporary files live in `worker-cache\` inside the checkout. Deleting that folder is safe. The worker will download a batch again if it still owns it.

Your name on the status page is the name on the invite, along with how many stars you have checked and how many new candidates both copies agreed on. Credit is recorded when a batch agrees, including the fact that two people did the work.
