#!/usr/bin/env python3
"""Worker przypomnień ELABS: kolejka przez HTTPS, wysyłka Gmail SMTP poza Renderem."""
import json
import os
import smtplib
import ssl
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

BASE = "https://elabs-jrch.onrender.com"
CRON_KEY = os.environ["ELABS_CRON_KEY"]
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Laboratorium JRCH Nowy Sącz")


def api(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(BASE + path, data=data, method="POST",
        headers={"X-ELABS-Cron": CRON_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.loads(response.read().decode())


queue = api("/api/reminders/pending").get("messages", [])
print(f"ELABS reminders pending={len(queue)}")
if not queue:
    raise SystemExit(0)

smtp = None
try:
    smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30,
                            context=ssl.create_default_context())
    smtp.login(SMTP_USER, SMTP_PASS)
    for item in queue:
        ok = False
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = item["subject"]
            msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
            msg["To"] = item["to"]
            msg.attach(MIMEText("Przypomnienie o poborze próbek. Otwórz wiadomość w wersji HTML, aby uzupełnić kartę poboru.", "plain", "utf-8"))
            msg.attach(MIMEText(item["html"], "html", "utf-8"))
            smtp.send_message(msg)
            ok = True
            print(f"sent id={item['id']}")
        except Exception as exc:
            print(f"send failed id={item['id']} type={type(exc).__name__}")
        finally:
            api("/api/reminders/complete", {"id": item["id"], "claim": item["claim"],
                "ok": ok, "source": item.get("source") or "github_actions_smtp"})
finally:
    if smtp:
        try:
            smtp.quit()
        except Exception:
            pass
