#!/usr/bin/env python3
"""Worker ELABS: SMS E2E z potwierdzeniem odbioru, retry i e-mail fallback.

Zasada bezpieczeństwa: rekord jest zamykany dopiero po stanie Delivered
z telefonu odbiorcy albo po zaakceptowaniu awaryjnego e-maila przez SMTP.
"""
import base64
import hashlib
import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
SMSGATE_BASE = "https://api.sms-gate.app/3rdparty/v1"
SUCCESS_STATES = {"Delivered"}
FAILED_STATES = {"Failed", "Cancelled", "Canceled"}
WAITING_STATES = {"Pending", "Processed", "Sent", "Cancelling"}


class SmsAwaiting(RuntimeError):
    pass


class SmsTerminal(RuntimeError):
    pass


def _json_request(req, retries=3, allow=()):
    """HTTP z krótkim exponential backoff; nigdy nie wypisuje body ani PII."""
    for number in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                body = response.read().decode()
                return response.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as exc:
            if exc.code in allow:
                try:
                    return exc.code, json.loads(exc.read().decode() or "{}")
                except (ValueError, TypeError):
                    return exc.code, {}
            if exc.code not in (408, 425, 429, 500, 502, 503, 504) or number == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if number == retries:
                raise
        time.sleep(2 ** number)
    raise RuntimeError("http_retry_exhausted")


def api(path, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"X-ELABS-Cron": CRON_KEY, "Content-Type": "application/json",
                 "User-Agent": "ELABS-reminder-worker/2.0"})
    status, body = _json_request(req)
    if status not in (200, 201, 202):
        raise RuntimeError(f"elabs_http_{status}")
    return body


def _gateway_auth():
    return base64.b64encode(f"{SMSGATE_USER}:{SMSGATE_PASS}".encode()).decode()


def gateway(path, method="GET", payload=None, allow=()):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        SMSGATE_BASE + path, data=data, method=method,
        headers={"Authorization": "Basic " + _gateway_auth(),
                 "Content-Type": "application/json",
                 "User-Agent": "ELABS-reminder-worker/2.0"})
    return _json_request(req, allow=allow)


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


def gateway_online(max_age_minutes=120):
    """Telefon musi być zarejestrowany i widziany przez chmurę niedawno."""
    _, devices = gateway("/devices")
    now = datetime.now(timezone.utc)
    for device in devices if isinstance(devices, list) else []:
        try:
            seen = datetime.fromisoformat(str(device.get("lastSeen") or "").replace("Z", "+00:00"))
            age = max(0.0, (now - seen.astimezone(timezone.utc)).total_seconds() / 60)
            if age <= max_age_minutes:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _message_id(item, attempt):
    # Hash nie zawiera danych klienta i jest stabilny po restarcie workera.
    raw = f"{item.get('id')}|{item.get('sms_text', '')}".encode("utf-8")
    base = "elabs-" + hashlib.sha256(raw).hexdigest()[:22]
    return base if attempt == 1 else f"{base}-r{attempt}"


def _get_message(message_id):
    status, body = gateway(f"/messages/{message_id}", allow=(404,))
    return None if status == 404 else body


def _state(message):
    return str((message or {}).get("state") or "Unknown")


def _poll_message(message_id, wait_seconds=120):
    deadline = time.monotonic() + wait_seconds
    last = "Unknown"
    while time.monotonic() < deadline:
        message = _get_message(message_id)
        last = _state(message)
        if last in SUCCESS_STATES:
            return last
        if last in FAILED_STATES:
            raise SmsTerminal("sms_terminal_" + last.lower())
        time.sleep(4)
    raise SmsAwaiting("sms_awaiting_" + last.lower())


def send_sms_confirmed(item):
    """Idempotentnie wysyła SMS; sukces dopiero po Delivered od telefonu odbiorcy."""
    attempt = max(1, int(item.get("attempt") or 1))

    # Po awarii między wysłaniem a zapisem najpierw odzyskujemy wcześniejszy sukces.
    for number in range(1, attempt + 1):
        old_id = _message_id(item, number)
        old = _get_message(old_id)
        if not old:
            if number < attempt:
                continue
            message_id = old_id
            break
        state = _state(old)
        if state in SUCCESS_STATES:
            return old_id, state
        if state in WAITING_STATES:
            return old_id, _poll_message(old_id)
        if number == attempt:
            raise SmsTerminal("sms_terminal_" + state.lower())
    else:
        raise SmsTerminal("sms_no_available_attempt")

    payload = {
        "id": message_id,
        "textMessage": {"text": encrypt_field(item["sms_text"])},
        "phoneNumbers": [encrypt_field(item["phone"])],
        "isEncrypted": True,
        "ttl": 3600,
        "priority": 100,
        "withDeliveryReport": True,
    }
    if SMSGATE_SIM:
        payload["simNumber"] = int(SMSGATE_SIM)
    status, _ = gateway("/messages?deviceActiveWithin=2", method="POST",
                        payload=payload, allow=(409,))
    if status not in (200, 201, 202, 409):
        raise RuntimeError(f"smsgate_http_{status}")
    return message_id, _poll_message(message_id)


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


def heartbeat(stats):
    try:
        api("/api/reminders/heartbeat", stats)
        return True
    except Exception as exc:
        print(f"heartbeat failed type={type(exc).__name__}")
        return False


def main():
    stats = {"state": "running", "gateway_online": False, "pending": 0,
             "sent_sms": 0, "sent_email": 0, "failed": 0,
             "last_error": "", "last_delivery_status": ""}
    sms_ready = bool(SMSGATE_USER and SMSGATE_PASS and SMSGATE_PHRASE)
    try:
        stats["gateway_online"] = bool(sms_ready and gateway_online())
    except Exception as exc:
        stats["last_error"] = "gateway_check_" + type(exc).__name__.lower()
    heartbeat(stats)

    smtp = None
    had_sms_error = not stats["gateway_online"]
    try:
        queue = api("/api/reminders/pending").get("messages", [])
        stats["pending"] = len(queue)
        print(f"ELABS reminders pending={len(queue)} gateway_online={stats['gateway_online']}")
        for item in queue:
            ok = False
            transport = ""
            gateway_id = ""
            delivery_status = ""
            error_code = ""
            if sms_ready and stats["gateway_online"] and item.get("phone"):
                try:
                    gateway_id, delivery_status = send_sms_confirmed(item)
                    ok, transport = True, "android_sms_e2e_confirmed"
                    stats["sent_sms"] += 1
                    stats["last_delivery_status"] = delivery_status
                    print(f"sent id={item['id']} via=sms status={delivery_status} text_len={len(item.get('sms_text', ''))}")
                except Exception as exc:
                    error_code = type(exc).__name__.lower()
                    had_sms_error = True
                    print(f"sms not confirmed id={item['id']} type={type(exc).__name__}; trying fallback")
            elif item.get("phone"):
                error_code = "gateway_offline"

            if not ok and item.get("to") and SMTP_USER and SMTP_PASS:
                try:
                    smtp = smtp or smtp_client()
                    send_email(smtp, item)
                    ok, transport, delivery_status = True, "email_fallback", "smtp_accepted"
                    stats["sent_email"] += 1
                    stats["last_delivery_status"] = delivery_status
                    print(f"sent id={item['id']} via=email_fallback")
                except Exception as exc:
                    error_code = "smtp_" + type(exc).__name__.lower()
                    print(f"email failed id={item['id']} type={type(exc).__name__}")

            if not ok:
                stats["failed"] += 1
                error_code = error_code or "no_transport"
                print(f"send unavailable id={item['id']}")
            api("/api/reminders/complete", {
                "id": item["id"], "claim": item["claim"], "ok": ok,
                "source": f"{item.get('source') or 'automatic'}:{transport or 'failed'}",
                "delivery_status": delivery_status, "gateway_id": gateway_id,
                "error": error_code,
            })

        stats["state"] = "error" if (had_sms_error or stats["failed"]) else "ok"
        if stats["state"] == "error" and not stats["last_error"]:
            stats["last_error"] = "gateway_offline" if not stats["gateway_online"] else "send_error"
    except Exception as exc:
        stats["state"] = "error"
        stats["failed"] += 1
        stats["last_error"] = "worker_" + type(exc).__name__.lower()
        print(f"worker failed type={type(exc).__name__}")
    finally:
        if smtp:
            try:
                smtp.quit()
            except Exception:
                pass
        heartbeat(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
