#!/usr/bin/env python3
"""Worker ELABS: szyfrowany SMS z Androida, a e-mail tylko jako fallback."""
import base64
import json
import os
import smtplib
import ssl
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

BASE = "https://elabs-jrch.onrender.com"
CRON_KEY = os.environ["ELABS_CRON_KEY"]
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Laboratorium JRCH Nowy Sącz")
SMSGATE_USER = os.environ.get("SMSGATE_USERNAME", "")
SMSGATE_PASS = os.environ.get("SMSGATE_PASSWORD", "")
SMSGATE_PHRASE = os.environ.get("SMSGATE_PASSPHRASE", "")
SMSGATE_SIM = os.environ.get("SMSGATE_SIM_NUMBER", "").strip()
SMSGATE_URL = "https://api.sms-gate.app/3rdparty/v1/messages"


def api(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(BASE + path, data=data, method="POST",
        headers={"X-ELABS-Cron": CRON_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.loads(response.read().decode())


def encrypt_field(value):
    """Format E2E zgodny z SMS Gateway for Android (AES-256-CBC/PBKDF2-SHA1)."""
    from cryptography.hazmat.primitives import hashes, padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    iterations = 300_000
    salt = os.urandom(16)
    key = PBKDF2HMAC(algorithm=hashes.SHA1(), length=32, salt=salt,
                    iterations=iterations).derive(SMSGATE_PHRASE.encode("utf-8"))
    padder = padding.PKCS7(128).padder()
    padded = padder.update(value.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(salt)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return (f"$aes-256-cbc/pbkdf2-sha1$i={iterations}$"
            f"{base64.b64encode(salt).decode()}${base64.b64encode(encrypted).decode()}")


def send_sms(item):
    payload = {
        "textMessage": {"text": encrypt_field(item["sms_text"])},
        "phoneNumbers": [encrypt_field(item["phone"])],
        "isEncrypted": True,
        "ttl": 86400,
        "withDeliveryReport": True,
    }
    if SMSGATE_SIM:
        payload["simNumber"] = int(SMSGATE_SIM)
    auth = base64.b64encode(f"{SMSGATE_USER}:{SMSGATE_PASS}".encode()).decode()
    req = urllib.request.Request(SMSGATE_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as response:
        if response.status not in (200, 201, 202):
            raise RuntimeError(f"SMSGate HTTP {response.status}")


def smtp_client():
    if not (SMTP_USER and SMTP_PASS):
        return None
    client = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30,
                              context=ssl.create_default_context())
    client.login(SMTP_USER, SMTP_PASS)
    return client


def send_email(client, item):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = item["subject"]
    msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"] = item["to"]
    msg.attach(MIMEText("Przypomnienie o poborze próbek. Otwórz wiadomość w wersji HTML, aby uzupełnić kartę poboru.", "plain", "utf-8"))
    msg.attach(MIMEText(item["html"], "html", "utf-8"))
    client.send_message(msg)


def main():
    queue = api("/api/reminders/pending").get("messages", [])
    print(f"ELABS reminders pending={len(queue)}")
    if not queue:
        return 0

    smtp = None
    try:
        for item in queue:
            ok = False
            transport = ""
            try:
                sms_ready = bool(SMSGATE_USER and SMSGATE_PASS and SMSGATE_PHRASE and item.get("phone"))
                if sms_ready:
                    try:
                        send_sms(item)
                        ok, transport = True, "android_sms_e2e"
                        print(f"sent id={item['id']} via=sms text_len={len(item.get('sms_text', ''))}")
                    except Exception as exc:
                        print(f"sms failed id={item['id']} type={type(exc).__name__}; trying fallback")
                if not ok and item.get("to") and SMTP_USER and SMTP_PASS:
                    smtp = smtp or smtp_client()
                    send_email(smtp, item)
                    ok, transport = True, "email_fallback"
                    print(f"sent id={item['id']} via=email_fallback")
                if not ok:
                    email_ready = bool(item.get("to") and SMTP_USER and SMTP_PASS)
                    print(f"send unavailable id={item['id']} sms_ready={sms_ready} email_ready={email_ready}")
            except Exception as exc:
                print(f"send failed id={item['id']} type={type(exc).__name__}")
            finally:
                api("/api/reminders/complete", {"id": item["id"], "claim": item["claim"],
                    "ok": ok, "source": f"{item.get('source') or 'automatic'}:{transport or 'failed'}"})
    finally:
        if smtp:
            try:
                smtp.quit()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
