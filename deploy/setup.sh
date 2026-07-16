#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu VM (Oracle Always Free A1 recommended).
# Run from the repo root:  bash deploy/setup.sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Indian market days/dates everywhere (equity_log, gap checks, timeouts)
sudo timedatectl set-timezone Asia/Kolkata

if ! [ -x "$ROOT/.venv/bin/python" ]; then
    sudo apt-get update -y
    sudo apt-get install -y python3-venv
    python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/pip" install -U pip
"$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"

chmod +x "$ROOT/deploy/nightly.sh"
for unit in stockrec-nightly.service stockrec-nightly.timer; do
    sed "s|__ROOT__|$ROOT|g" "$ROOT/deploy/$unit" |
        sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now stockrec-nightly.timer

echo
echo "Done. Timer status:"
systemctl list-timers stockrec-nightly.timer --no-pager
echo
echo "Handy alias (add to ~/.bashrc):"
echo "  alias stockrec='$ROOT/.venv/bin/python -m stockrec'"
