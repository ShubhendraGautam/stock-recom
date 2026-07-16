#!/usr/bin/env bash
# Nightly stockrec paper run (invoked by stockrec-nightly.timer).
# Resilient: single-instance lock, 3 attempts with backoff, per-day logs,
# post-run DB backups, bounded retention. Non-zero exit = failed night.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/logs"; BAK_DIR="$ROOT/backups"
mkdir -p "$LOG_DIR" "$BAK_DIR"
LOG="$LOG_DIR/$(date +%F).log"

exec 9>"$ROOT/.nightly.lock"
flock -n 9 || { echo "another run active; exiting" >>"$LOG"; exit 0; }

echo "=== nightly run $(date -Is) ===" >>"$LOG"

# never trade fresh picks on stale data: wait until TODAY's NSE EOD files
# (equity + F&O bhavcopy) are published, rechecking every 15 min. Give up
# waiting at 21:30 IST and run anyway (trading holiday, or NSE unusually
# late — the tool then falls back to the latest available day).
DEADLINE="$(date -d '21:30' +%s)"
STALE=""
until "$ROOT/.venv/bin/python" "$ROOT/deploy/probe.py" >>"$LOG" 2>&1; do
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        echo "--- EOD files missing at 21:30; defense-only run ---" >>"$LOG"
        STALE="--stale"
        break
    fi
    sleep 900
done

ok=1
for attempt in 1 2 3; do
    if "$ROOT/.venv/bin/python" -m stockrec --test $STALE >>"$LOG" 2>&1; then
        ok=0; break
    fi
    echo "--- attempt $attempt failed; retry in 10m ---" >>"$LOG"
    [ "$attempt" -lt 3 ] && sleep 600
done

if [ "$ok" -eq 0 ]; then
    d="$BAK_DIR/$(date +%F)"; mkdir -p "$d"
    for f in portfolio.db paper.db research.db order_plan.json; do
        [ -f "$ROOT/$f" ] && cp "$ROOT/$f" "$d/"
    done
    # keep the newest 30 daily backups
    ls -1d "$BAK_DIR"/20* 2>/dev/null | head -n -30 | xargs -r rm -rf
    "$ROOT/.venv/bin/python" "$ROOT/deploy/mail.py" >>"$LOG" 2>&1 || true
    echo "=== done $(date -Is) ===" >>"$LOG"
else
    echo "=== FAILED after 3 attempts $(date -Is) ===" >>"$LOG"
    "$ROOT/.venv/bin/python" "$ROOT/deploy/mail.py" failed >>"$LOG" 2>&1 || true
fi
find "$LOG_DIR" -name '*.log' -mtime +90 -delete
exit "$ok"
