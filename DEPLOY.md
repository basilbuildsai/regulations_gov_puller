# Deploying the puller on a server

The pull is a long-running, network-flaky job. Running it on a server that stays awake is dramatically more reliable than running it on a laptop.

## Prerequisites on the server

- Python 3.10+ (3.12 on Ubuntu 24.04 / 3.10 on Ubuntu 22.04 are both fine)
- ~5 GB free disk for a docket the size of FAA-2025-1908 (3,928 comments + ~1,300 PDFs); larger for bigger dockets
- An api.data.gov key in the server's `.env` file
- Outbound HTTPS to `api.regulations.gov` and `downloads.regulations.gov`

### Ubuntu Server one-time install

Ubuntu's base image doesn't ship `venv` or `tmux`. Install them once:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip tmux git
```

If you'll use the systemd recipe below, no further prereqs needed (systemd is in the base image).

## One-time server setup

```bash
git clone git@github.com:<your-org>/regulations_gov_puller.git
cd regulations_gov_puller

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
$EDITOR .env   # paste DATA_GOV_KEY
```

## Option A: tmux (simplest, recommended)

Detached, named session you can reattach to from any SSH login:

```bash
tmux new -d -s pull "cd $(pwd) && .venv/bin/python -m puller.pull_inline \
    --docket FAA-2025-1908 --data-dir data 2>&1 | tee data/pull_inline.log"

# inspect later
tmux ls
tmux attach -t pull             # ctrl-b d to detach
```

When pull finishes, run the next two stages the same way (or chain them with `&&`):

```bash
tmux new -d -s attach "cd $(pwd) && .venv/bin/python -m puller.fetch_attachments \
    --data-dir data 2>&1 | tee data/fetch_attachments.log"

tmux new -d -s extract "cd $(pwd) && .venv/bin/python -m puller.extract_pdfs \
    --data-dir data 2>&1 | tee data/extract_pdfs.log"
```

## Option B: systemd (survives reboots)

Useful if you want to kick off the pull and walk away for days. Drop this in `/etc/systemd/system/regulations-puller.service` (adjust paths and user):

```ini
[Unit]
Description=regulations.gov puller — FAA Part 108
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=youruser
WorkingDirectory=/home/youruser/regulations_gov_puller
EnvironmentFile=/home/youruser/regulations_gov_puller/.env
ExecStart=/home/youruser/regulations_gov_puller/.venv/bin/python -m puller.pull_inline --docket FAA-2025-1908 --data-dir data
StandardOutput=append:/home/youruser/regulations_gov_puller/data/pull_inline.log
StandardError=append:/home/youruser/regulations_gov_puller/data/pull_inline.log
TimeoutStartSec=86400

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl start regulations-puller     # kick off
sudo systemctl status regulations-puller    # check
tail -f data/pull_inline.log                # watch progress
```

## Option C: nohup (when you can't install anything)

```bash
nohup .venv/bin/python -m puller.pull_inline \
    --docket FAA-2025-1908 --data-dir data > data/pull_inline.log 2>&1 &
disown
```

## Pulling the data back to your laptop

After the server finishes, sync the corpus locally for analysis:

```bash
# from your laptop
rsync -az --info=progress2 \
    server:regulations_gov_puller/data/ \
    /local/path/Part_108_Analysis/raw/
```

Or if your local layout matches the server's, just sync the whole project dir.

## Resuming after interruption

All three stages are idempotent and resumable:

- `pull_inline.py` skips IDs already in `comments.jsonl`
- `fetch_attachments.py` skips PDFs already on disk
- `extract_pdfs.py` skips `.txt` files that already exist (use `--force` to redo)

You can safely re-run the same command after any kind of interruption.

## What to watch for

- **HTTP 429**: handled with exponential backoff, harmless to see in the log.
- **HTTP 5xx**: handled with retry; persistent 5xx means api.data.gov is down — pause and resume later.
- **Disk space**: `du -sh data/attachments` periodically. PDFs add up.
- **Forgotten `.env`**: the script exits immediately if `DATA_GOV_KEY` is missing.
