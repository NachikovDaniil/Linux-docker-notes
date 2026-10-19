import os
import re
import json
import time
import hashlib
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime


# ============================================================
# Configuration
# ============================================================

LOG_ROOT = Path("/poste-logs")
STATE_FILE = Path("/state/processed.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))
DEBUG = os.getenv("DEBUG", "0") == "1"

LOCAL_DOMAINS = {
    x.strip().lower()
    for x in os.getenv("LOCAL_DOMAINS", "").split(",")
    if x.strip()
}


# ============================================================
# Regex / constants
# ============================================================

EMAIL_RE = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.IGNORECASE,
)


# ============================================================
# State
# ============================================================

def load_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return {"processed": {}, "initialized": False}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("processed", {})
            data.setdefault("initialized", False)
            return data
    except Exception as e:
        print(f"Error loading state: {e}")
        return {"processed": {}, "initialized": False}


def save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(STATE_FILE)
    except Exception as e:
        print(f"Error saving state: {e}")


# ============================================================
# Telegram
# ============================================================

def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()

    try:
        request = urllib.request.Request(url, data=data, method="POST")

        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200

    except Exception as e:
        print(f"Telegram error: {e}")

    return False


# ============================================================
# Helpers
# ============================================================

def normalize_email(value):
    if not value:
        return ""

    value = str(value).strip().strip("<>").strip().lower()

    if value in ("<>", ""):
        return ""

    return value


def is_local_sender(sender):
    sender = normalize_email(sender)

    if "@" not in sender:
        return False

    domain = sender.split("@", 1)[1]
    return domain in LOCAL_DOMAINS


def safe_html(value):
    return (
        str(value)
        .replace("&", "&")
        .replace("<", "<")
        .replace(">", ">")
    )


def format_time(timestamp):
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return timestamp


def event_id(*values):
    raw = "|".join(str(x) for x in values)
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_json_objects(content):
    objects = []

    try:
        objects.append(json.loads(content))
        return objects
    except json.JSONDecodeError:
        pass

    for line in content.splitlines():
        line = line.strip()

        if not line.startswith("{"):
            continue

        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    return objects


# ============================================================
# Parse tx file / tx .results
# ============================================================

def parse_tx_file(path):
    """
    Возвращает (sender, recipients).

    Фактический формат Poste.io tx .results:

    {
      "mail_from": {
        "address": "sender@example.com"
      },
      "rcpt_to": {
        "recipient": [
          {
            "address": "recipient@example.com"
          }
        ]
      }
    }
    """
    if not path or not path.exists():
        return None, []

    sender = None
    recipients = []

    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        print(f"Error reading tx file {path}: {e}")
        return None, []

    # 1. JSON tx .results — основной и точный формат
    if path.name.endswith(".results") or content.startswith("{"):
        for data in parse_json_objects(content):
            mail_from = data.get("mail_from")

            if isinstance(mail_from, dict):
                sender = normalize_email(mail_from.get("address"))

            rcpt_to = data.get("rcpt_to")

            if isinstance(rcpt_to, dict):
                rcpt_list = rcpt_to.get("recipient")

                if isinstance(rcpt_list, list):
                    for item in rcpt_list:
                        if isinstance(item, dict):
                            address = normalize_email(item.get("address"))

                            if address and address not in recipients:
                                recipients.append(address)

                        elif isinstance(item, str):
                            address = normalize_email(item)

                            if address and address not in recipients:
                                recipients.append(address)

    # 2. Fallback для текстового tx-файла
    if sender is None or not recipients:
        try:
            for line in content.splitlines():
                if sender is None:
                    match = re.search(
                        r"sender\s+<([^>]+)>",
                        line,
                        re.IGNORECASE,
                    )

                    if match:
                        sender = normalize_email(match.group(1))

                if not recipients:
                    match = re.search(
                        r"recipient\s+<([^>]+)>",
                        line,
                        re.IGNORECASE,
                    )

                    if match:
                        recipient = normalize_email(match.group(1))

                        if recipient and recipient not in recipients:
                            recipients.append(recipient)

        except Exception as e:
            print(f"Error parsing text tx file {path}: {e}")

    return sender, recipients


def find_tx_file(txid):
    """
    Ищет tx-файл Poste.io.

    Реальные варианты:
      delivery/tx/<a>/<b>/<TXID>.1
      delivery/tx/<a>/<b>/<TXID>.1.results
    """
    if len(txid) < 2:
        return None

    base_txid = txid.rsplit(".", 1)[0] if "." in txid else txid
    directory = LOG_ROOT / "delivery" / "tx" / base_txid[0] / base_txid[1]

    candidates = [
        directory / f"{base_txid}.1.results",
        directory / f"{base_txid}.1",
        directory / base_txid,
        directory / txid,
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    try:
        matches = sorted(directory.glob(f"{base_txid}*"))

        results = [
            x for x in matches
            if x.name.endswith(".results")
        ]

        if results:
            return results[0]

        if matches:
            return matches[0]
    except Exception as e:
        print(f"Error searching tx files for {txid}: {e}")

    return None


# ============================================================
# Sender / recipients из del .results
# ============================================================

def extract_sender_from_event(event):
    """
    Fallback: sender из del .results.
    В текущем формате Poste.io там обычно его нет.
    """
    data = event.get("data", {})

    if not isinstance(data, dict):
        data = {}

    for source in (data, event):
        for key in ("sender", "mail_from", "return_path"):
            value = source.get(key)

            if isinstance(value, str):
                emails = EMAIL_RE.findall(value)

                if emails:
                    return normalize_email(emails[0])

            elif isinstance(value, dict):
                address = value.get("address")

                if isinstance(address, str):
                    emails = EMAIL_RE.findall(address)

                    if emails:
                        return normalize_email(emails[0])

    return None


def extract_failed_recipients(event):
    """
    Возвращает (recipients, reason, smtp_code).

    Получатели берутся только из bounced_rcpt / failed_rcpt /
    failed / deferred_rcpt текущего bounce-события.
    """
    recipients = []
    reason = ""
    smtp_code = ""

    data = event.get("data", {})

    if not isinstance(data, dict):
        data = {}

    def add_recipient(value):
        value = normalize_email(value)

        if value and "@" in value and value not in recipients:
            recipients.append(value)

    recipient_containers = [
        data.get("bounced_rcpt"),
        data.get("failed_rcpt"),
        data.get("failed"),
        data.get("deferred_rcpt"),
        event.get("bounced_rcpt"),
        event.get("failed_rcpt"),
        event.get("failed"),
        event.get("deferred_rcpt"),
    ]

    for container in recipient_containers:
        if not isinstance(container, list):
            continue

        for item in container:
            if isinstance(item, str):
                add_recipient(item)
                continue

            if not isinstance(item, dict):
                continue

            for key in ("original", "address", "rcpt", "recipient", "email"):
                if key in item:
                    add_recipient(item[key])

            if not reason:
                reason = (
                    item.get("reason")
                    or item.get("dsn_smtp_response")
                    or item.get("dsn_status")
                    or ""
                )

            if not smtp_code:
                smtp_code = item.get("dsn_smtp_code") or ""

    # Fallback: адрес иногда есть только в тексте ошибки
    err = data.get("err", "") or event.get("err", "")

    if not recipients and isinstance(err, str):
        for email in EMAIL_RE.findall(err):
            add_recipient(email)

    if not reason:
        reason = err

    return recipients, reason, smtp_code


# ============================================================
# Notification
# ============================================================

def notify_bounce(
    txid,
    sender,
    recipients,
    reason,
    date_str,
    result_path=None,
):
    if isinstance(recipients, str):
        recipients = [recipients] if recipients else []

    recipients = [r for r in recipients if r]

    if recipients:
        recipients_text = "\n".join(
            f"• {safe_html(recipient)}"
            for recipient in recipients
        )
    else:
        recipients_text = "не определён"

    sender_text = safe_html(sender) if sender else "не определён"
    reason_text = safe_html(reason or "не указана")
    time_text = format_time(date_str)

    message = (
        f"🚨 <b>POSTE.IO — ПИСЬМО НЕ ДОСТАВЛЕНО</b>\n\n"
        f"📤 <b>От:</b>\n{sender_text}\n\n"
        f"📥 <b>Кому:</b>\n{recipients_text}\n\n"
        f"🕐 <b>Время:</b>\n{time_text}\n\n"
        f"💬 <b>Причина:</b>\n{reason_text}\n\n"
        f"🆔 <b>Transaction:</b>\n<code>{safe_html(txid)}</code>"
    )

    if result_path:
        message += (
            f"\n\n🗂 <b>Result:</b>\n"
            f"<code>{safe_html(result_path)}</code>"
        )

    return telegram_send(message)


# ============================================================
# Scan bounce results
# ============================================================

def get_bounce_result_files():
    base = LOG_ROOT / "delivery" / "del"

    if not base.exists():
        return []

    files = list(base.glob("*/*/*.results"))

    return sorted(files, key=lambda path: path.stat().st_mtime)


def process_bounce_result(path, state):
    processed = state.setdefault("processed", {})
    file_key = str(path)

    if file_key in processed:
        if DEBUG:
            print(f"  skip (already processed): {file_key}")
        return 0

    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        print(f"Error reading {path}: {e}")
        return 0

    if DEBUG:
        print(f"RESULT FILE: {path}")
        print(f"  content: {content}")

    if not content:
        processed[file_key] = {
            "time": time.time(),
            "empty": True,
        }
        return 0

    events = parse_json_objects(content)

    if not events:
        processed[file_key] = {
            "time": time.time(),
            "unparsed": True,
        }
        return 0

    # Берём последнее событие bounced/rejected.
    # До него могут быть deferred-события.
    bounce_event = None
    date_str = ""

    for event in events:
        result_type = (event.get("type") or "").lower()

        if result_type in ("bounced", "rejected"):
            bounce_event = event
            date_str = event.get("date", date_str)

    if bounce_event is None:
        processed[file_key] = {
            "time": time.time(),
            "type": events[-1].get("type", ""),
        }
        return 0

    result_type = (bounce_event.get("type") or "").lower()
    txid = path.name[:-len(".results")]

    # 1. Sender и исходные получатели из tx
    tx_path = find_tx_file(txid)
    tx_sender, tx_recipients = parse_tx_file(tx_path)

    # 2. Sender fallback из del .results
    sender = tx_sender or extract_sender_from_event(bounce_event)

    # 3. Получатели из bounce/failed события
    recipients, reason, smtp_code = extract_failed_recipients(bounce_event)

    # 4. Если в del .results получателей нет,
    #    используем исходных получателей из tx
    if not recipients:
        recipients = list(tx_recipients)

    # 5. Отправитель не должен быть в получателях
    if sender:
        recipients = [
            recipient
            for recipient in recipients
            if recipient != sender
        ]

    if DEBUG:
        print(f"  BOUNCE: txid={txid}")
        print(f"  type={result_type}")
        print(f"  tx file={tx_path}")
        print(f"  tx sender={tx_sender}")
        print(f"  tx recipients={tx_recipients}")
        print(f"  final sender={sender}")
        print(f"  final recipients={recipients}")
        print(f"  reason={reason}")
        print(f"  smtp_code={smtp_code}")

    # Не отправляем уведомления для внешних отправителей,
    # но события без определённого sender не теряем
    if sender and not is_local_sender(sender):
        if DEBUG:
            print(f"  -> пропуск: sender не локальный ({sender})")

        processed[file_key] = {
            "time": time.time(),
            "skipped": "non-local",
        }
        return 0

    eid = event_id(
        txid,
        sender or "",
        ",".join(recipients),
        reason,
        result_type,
    )

    if eid in processed:
        processed[file_key] = {
            "time": time.time(),
            "eid": eid,
        }
        return 0

    print(
        f"Delivery failure: txid={txid} "
        f"sender={sender} -> {recipients} err={reason}"
    )

    sent = notify_bounce(
        txid=txid,
        sender=sender,
        recipients=recipients,
        reason=reason,
        date_str=date_str,
        result_path=path,
    )

    if sent:
        processed[file_key] = {
            "time": time.time(),
            "txid": txid,
        }

        processed[eid] = {
            "time": time.time(),
            "txid": txid,
        }

        return 1

    return 0


# ============================================================
# Cleanup state
# ============================================================

def cleanup_state(state):
    processed = state.get("processed", {})
    now = time.time()
    remove = []

    for key, value in processed.items():
        if not isinstance(value, dict):
            continue

        created = value.get("time")

        if created and now - created > 7 * 86400:
            remove.append(key)

    for key in remove:
        processed.pop(key, None)


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 60)
    print("Poste.io Bounce Monitor (v4 - exact tx format)")
    print("=" * 60)
    print(f"Log root:       {LOG_ROOT}")
    print(f"Local domains:  {LOCAL_DOMAINS}")
    print(f"Interval:       {CHECK_INTERVAL}s")
    print(f"Debug:          {DEBUG}")
    print(
        "Telegram:       "
        + ("configured" if TELEGRAM_BOT_TOKEN else "NOT configured")
    )

    state = load_state()

    if not state.get("initialized"):
        print("First start: marking existing bounce results as processed...")

        for path in get_bounce_result_files():
            state["processed"][str(path)] = {
                "time": time.time(),
                "boot_skip": True,
            }

        state["initialized"] = True
        save_state(state)

        print("Initial state saved. Waiting for new bounces...")

    while True:
        try:
            total = 0

            for path in get_bounce_result_files():
                total += process_bounce_result(path, state)

            cleanup_state(state)
            save_state(state)

            if total:
                print(f"Sent {total} Telegram notification(s)")

        except Exception as e:
            print(f"Main loop error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()