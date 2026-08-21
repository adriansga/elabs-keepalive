#!/usr/bin/env python3
"""Niezależny alarm workera ELABS: e-mail przy awarii i po odzyskaniu."""
import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from email.mime.text import MIMEText

BASE = "https://elabs-jrch.onrender.com"
CRON_KEY = os.environ["ELABS_CRON_KEY"]
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")


def api(payload):
    req = urllib.request.Request(
        BASE + "/api/reminders/alert-state", data=json.dumps(payload).encode(), method="POST",
        headers={"X-ELABS-Cron": CRON_KEY, "Content-Type": "application/json",
                 "User-Agent": "ELABS-reminder-watchdog/1.0"})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def send_alert(kind, health):
    if not (SMTP_USER and SMTP_PASS):
        raise RuntimeError("smtp_not_configured")
    if kind == "failure":
        subject = "ALERT ELABS: automat przypomnien SMS wymaga uwagi"
        body = ("Automatyczny monitoring wykryl problem z przypomnieniami SMS.\n\n"
                f"Stan: {health.get('status')}\n"
                f"Telefon online: {health.get('gateway_online')}\n"
                f"Ostatni sygnal (min): {health.get('age_minutes')}\n\n"
                "Otworz pulpit ELABS. Czerwony komunikat wskaze problem.")
    else:
        subject = "ELABS: automat przypomnien SMS znow dziala"
        body = "Monitoring potwierdzil powrot workera i telefonu SMS do prawidlowej pracy."
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = SMTP_USER
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30,
                          context=ssl.create_default_context()) as client:
        client.login(SMTP_USER, SMTP_PASS)
        client.send_message(msg)


def main():
    state = api({"action": "check"})
    action = state.get("action") or "none"
    print(f"ELABS watchdog action={action} status={(state.get('health') or {}).get('status')}")
    if action in ("failure", "recovery"):
        send_alert(action, state.get("health") or {})
        api({"action": "ack", "kind": action})
        print(f"ELABS watchdog alert_sent={action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
