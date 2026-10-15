# Run a Mega Rhexas worker

You were invited to the friends beta. Your computer downloads a few hundred TESS stars at a time, searches them for transits, uploads a small results file (usually well under a few megabytes), and deletes the big files. The server is **https://api.megarhexas.com**. That address is already the default. You need a personal token from the person who invited you. It starts with `rhx_`.

Two different people search each batch. Your token and someone else's have to both be running, or batches sit waiting for the second copy. Please don't share your token or run a second copy of it: the check is "two different invites agreed."

The search settings (period grid, SDE, SNR, and the rest) are the project's usual ones. The knobs below only change how hard your own computer works.

## Windows

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

If MAST answers "slow down" (HTTP 429 or 503), the worker waits and retries. That backoff starts at 15 seconds and doubles, the same as a local download. The worker uses 2 download threads by default so a room full of friends does not stampede the archive.

## Make it gentler, or a bit faster

Defaults: **low** process priority, **2** search processes, **2** download threads, stop if the disk has under **5 GB** free, and keep the temporary light curves under **20 GB**. A batch is about a gigabyte and is deleted when the upload succeeds.

```bat
.venv\Scripts\rhexas worker --token-file token.txt --procs 4 --threads 2 --priority low --max-cache-gb 20
```

`--priority idle` yields even more to whatever else you are doing. `--priority normal` competes with your foreground apps. `--procs` is the one to raise on a desktop you are not sitting at. A laptop is happier at 2.

You can put the same values in `rhexas.toml` under `[worker]` (`procs`, `threads`, `priority`, `max_cache_gb`, `min_free_gb`, `cache_dir`). Leave `coordinator_url` as `https://api.megarhexas.com` unless the owner tells you otherwise. Keep the token in `token.txt`, not in the toml file.

Environment variables work too: `RHEXAS_WORKER_TOKEN` and `RHEXAS_COORDINATOR_URL`.

## If something looks stuck

- `no work right now` means every batch is either finished or already checked out. The worker waits a minute and asks again. Leave it running.
- `coordinator unreachable` means your network, or the server is restarting. It waits and retries. You do not need to delete anything.
- A disk message means the drive is under 5 GB free even after the cache was emptied. Free some space and start the command again.
- The temporary files live in `worker-cache\` inside the checkout. Deleting that folder is safe. The worker will download a batch again if it still owns it.

Your name on the status page is the name on the invite, along with how many stars you have checked and how many new candidates both copies agreed on. Credit is recorded when a batch agrees, including the fact that two people did the work.
