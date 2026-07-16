#!/usr/bin/env python3
"""Email the day's picks (or a failure alert) after the nightly run.

Setup once on the VM (needs a Gmail app password, not your real password:
Google Account -> Security -> 2-Step Verification -> App passwords):

    cat > ~/.stockrec-mail <<EOF
    FROM=you@gmail.com
    TO=you@gmail.com
    APP_PASSWORD=abcd efgh ijkl mnop
    EOF
    chmod 600 ~/.stockrec-mail

Alternative (Oracle-native, no Gmail password): OCI Email Delivery —
create an Approved Sender + SMTP credentials in the OCI console, then add:

    SMTP_HOST=smtp.email.<region>.oci.oraclecloud.com
    SMTP_PORT=587
    SMTP_USER=<generated ocid1...@...smtp credential username>

and put the generated SMTP password in APP_PASSWORD; FROM must be the
approved sender. Defaults (no SMTP_* keys) are Gmail on port 465.

On GitHub Actions the same keys come from env instead (repo secrets):
MAIL_FROM, MAIL_TO, MAIL_APP_PASSWORD (+ optional MAIL_SMTP_HOST/PORT/USER).

No config anywhere -> silently does nothing (email is optional).
Usage: mail.py [failed]
"""
import json
import os
import smtplib
import ssl
import sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONF = Path.home() / ".stockrec-mail"


def _config() -> dict | None:
    if CONF.exists():
        conf = dict(line.split("=", 1) for line in
                    CONF.read_text().splitlines() if "=" in line)
        return {k.strip(): v.strip() for k, v in conf.items()}
    env = {k: v for k in ("FROM", "TO", "APP_PASSWORD",
                          "SMTP_HOST", "SMTP_PORT", "SMTP_USER")
           if (v := os.environ.get(f"MAIL_{k}"))}
    return env if env.get("APP_PASSWORD") else None


def main() -> int:
    conf = _config()
    if conf is None:
        return 0

    today = date.today().isoformat()
    if len(sys.argv) > 1 and sys.argv[1] == "failed":
        subject = f"stockrec FAILED {today}"
        body = ("Nightly run failed.\nCheck the run log "
                f"(GitHub Actions, or logs/{today}.log on a VM).")
    else:
        plan = ROOT / "plans" / f"{today}.json"
        picks = (json.loads(plan.read_text(encoding="utf-8"))["picks"]
                 if plan.exists() else [])
        subject = f"stockrec picks {today} ({len(picks)})"
        rows = [f"{p['symbol']:<14} @ {p['price'] or 0:>9.2f}  "
                f"conv {p['conviction']:>5.1f}  "
                f"stop {p['stop']}  target {p['target']}"
                for p in picks] or ["(no picks today)"]
        body = ("\n".join(rows)
                + "\n\nFull report: the run log "
                  f"(GitHub Actions, or logs/{today}.log on a VM).")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, conf["FROM"], conf["TO"]
    msg.set_content(body)
    host = conf.get("SMTP_HOST", "smtp.gmail.com")
    port = int(conf.get("SMTP_PORT", "465"))
    user = conf.get("SMTP_USER", conf["FROM"])
    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls(context=ctx)
    with server:
        server.login(user, conf["APP_PASSWORD"])
        server.send_message(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
