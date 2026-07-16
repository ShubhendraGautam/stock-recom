# Hosting stockrec

Two options. **GitHub Actions is the current one** (no card details
needed); the Oracle VM instructions below still work if that ever changes.

## Option A — GitHub Actions (current)

The repo itself is the storage: every run commits `portfolio.db`,
`paper.db`, `research.db`, `order_plan.json` and `plans/` back, so git
history doubles as the backup archive.

### One-time setup

1. Create a **PRIVATE** GitHub repo (the DBs hold your trading history —
   never public) and push this project to it. `.gitignore` already
   excludes `.venv/`, `.cache/`, `logs/`, `backups/`.
2. Repo → Settings → Actions → General → Workflow permissions →
   **Read and write permissions**.
3. Optional email — Settings → Secrets and variables → Actions →
   New repository secret: `MAIL_FROM`, `MAIL_TO`, `MAIL_APP_PASSWORD`
   (a Gmail app password: Google Account → Security → 2-Step
   Verification → App passwords — never your real password).
4. Test once: Actions tab → `nightly` → Run workflow.

### How it runs

- Schedule: **Mon–Fri 18:00 IST** (12:30 UTC), plus a **22:30 IST
  fallback** that exits instantly if the day already ran — GitHub cron
  is best-effort and can skip or delay, the second trigger covers that.
- Data first: the job polls `deploy/probe.py` every 15 min until TODAY's
  NSE EOD files (equity + F&O bhavcopy) are published. If they never
  appear by 21:30 IST (trading holiday / NSE late), it runs **defense
  only** (`--stale`): news is refreshed and held positions can exit, but
  nothing new is opened and no duplicate history rows are saved.
- `.cache/` (analog dataset, price caches) is carried between runs via
  actions/cache; losing it only costs one full rebuild, never data.
- After a good run: results are committed (`run YYYY-MM-DD`) and the
  picks are emailed if secrets are set. A failed run emails an alert.

### Daily life

- Picks land in your inbox, and in `plans/YYYY-MM-DD.json` in the repo
  (symbol/price/conviction/stop/target, newest 30 kept). When you start
  real trading, act on the latest file — from any device, it's just a
  file on GitHub. `order_plan.json` is always a copy of the latest.
- Full readable report: the workflow log in the Actions tab (kept 90
  days). Check paper progress anytime by cloning/pulling and running
  `stockrec test`.
- Known limits, honestly: GitHub-hosted runners share datacenter IPs, so
  Yahoo/NSE can occasionally rate-limit or block a night — the fallback
  trigger, retries inside data fetchers, and the gap warning make a
  silent multi-day hole very unlikely, but watch the first week. Jobs
  are capped at 6 h (the workflow budgets 350 min: wait + analysis).

## Option B — Oracle/any Linux VM (alternative)

1. Ubuntu VM, SSH in, copy the project (DBs included — they carry the
   test-phase history). Do NOT copy `.venv` or `.cache`:

       scp -r -i key.pem `
           stockrec deploy requirements.txt PLAN.md README.md `
           portfolio.db paper.db research.db order_plan.json `
           ubuntu@VM_IP:~/stock-recom/

2. `cd ~/stock-recom && bash deploy/setup.sh` — sets IST, builds the
   venv, installs a systemd timer: Mon–Fri 18:00 IST, `Persistent=true`
   (missed runs fire on next boot), 3 attempts 10 min apart. The same
   probe/deadline/defense-only logic as Actions lives in `nightly.sh`.
3. Email: create `~/.stockrec-mail` (see header of `deploy/mail.py`;
   also supports OCI Email Delivery SMTP instead of Gmail).
4. Verify: `sudo systemctl start stockrec-nightly.service` then
   `tail -f logs/$(date +%F).log`. Logs kept 90 days, DB backups in
   `backups/` (newest 30). Timer health:
   `systemctl list-timers stockrec-nightly.timer`
