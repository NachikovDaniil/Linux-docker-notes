#!/usr/bin/env python3
"""
Одноразовый пересканировщик .results файлов Poste.io.

Шлёт в Telegram уведомления обо всех ошибках доставки за указанную дату
(или за всё время), НЕ трогая state основного монитора.

Использование внутри контейнера:
    python /app/rescan.py                # только за сегодня (UTC)
    python /app/rescan.py 2026-10-09     # за конкретную дату
    python /app/rescan.py all            # за всё время
"""

import os
import re
import sys
import json
import hashlib
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone


LOG_ROOT = Path("/poste-logs")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

LOCAL_DOMAINS = {
    x.strip().lower()
    for x in os.getenv("LOCAL_DOMAINS", "").split(",")
    if x.strip()
}

ALERT_TYPES = {"bounced", "failed", "rejected", "undelivered", "error"}
SKIP_TYPES = {"delivered", "sent", "queued", "relayed", "accepted"}


# ============================================================
# Regex
# ============================================================

EMAIL_RE = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.IGNORECASE,
)


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
    В текущем формате там обычно его нет.
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
# Telegram
# ============================================================

def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Telegram not configured")
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
# Notification
# ============================================================

def notify(
    txid,
    sender,
    recipients,
    reason,
    date_str,
    result_type,
    smtp_code="",
    result_path=None,
):
    if recipients:
        recipients_text = "\n".join(
            f"• {safe_html(recipient)}"
            for recipient in recipients
        )
    else:
        recipients_text = "не определён"

    sender_text = safe_html(sender) if sender else "не определён"

    code_line = ""

    if smtp_code:
        code_line = (
            f"❌ <b>SMTP:</b> <code>{safe_html(smtp_code)}</code>\n"
        )

    message = (
        f"🚨 <b>POSTE.IO — ПИСЬМО НЕ ДОСТАВЛЕНО</b>\n\n"
        f"📤 <b>От:</b>\n{sender_text}\n\n"
        f"📥 <b>Кому:</b>\n{recipients_text}\n\n"
        f"🕐 <b>Время:</b>\n{format_time(date_str)}\n\n"
        f"📋 <b>Тип:</b> <code>{safe_html(result_type)}</code>\n"
        f"{code_line}"
        f"💬 <b>Причина:</b>\n{safe_html(reason or 'не указана')}\n\n"
        f"🆔 <b>Transaction:</b>\n<code>{safe_html(txid)}</code>"
    )

    if result_path:
        message += (
            f"\n\n🗂 <b>Result:</b>\n"
            f"<code>{safe_html(result_path)}</code>"
        )

    return telegram_send(message)


# ============================================================
# Main
# ============================================================

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "today"

    if mode == "all":
        target_date = None
        print("Rescan mode: ALL events")

    elif mode == "today":
        target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print(f"Rescan mode: TODAY ({target_date} UTC)")

    else:
        target_date = mode
        print(f"Rescan mode: {target_date}")

    base = LOG_ROOT / "delivery" / "del"

    if not base.exists():
        print(f"ERROR: {base} not found")
        return 1

    result_files = sorted(
        base.glob("*/*/*.results"),
        key=lambda path: path.stat().st_mtime,
    )

    print(f"Total result files: {len(result_files)}")

    sent = 0
    matched = 0
    seen = set()

    for path in result_files:
        txid = path.name[:-len(".results")]

        tx_path = find_tx_file(txid)
        tx_sender, tx_recipients = parse_tx_file(tx_path)

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  ! cannot read {path}: {e}")
            continue

        events = parse_json_objects(content)

        for line_no, event in enumerate(events, 1):
            result_type = (event.get("type") or "").lower()

            if result_type in SKIP_TYPES:
                continue

            if result_type not in ALERT_TYPES:
                continue

            date_str = event.get("date", "")

            if target_date and date_str:
                if not date_str.startswith(target_date):
                    continue

            # Получатели из bounce/failed события
            recipients, reason, smtp_code = extract_failed_recipients(event)

            # Если в del .results получателя нет,
            # берём всех исходных получателей из tx
            if not recipients:
                recipients = list(tx_recipients)

            # Sender: сначала tx, затем fallback из del .results
            sender = tx_sender or extract_sender_from_event(event)

            # Не отправляем уведомления для внешних отправителей
            if sender and not is_local_sender(sender):
                continue

            # Отправитель не должен попадать в получатели
            if sender:
                recipients = [
                    recipient
                    for recipient in recipients
                    if recipient != sender
                ]

            event_key = event_id(
                txid,
                line_no,
                sender or "",
                ",".join(recipients),
                reason,
                result_type,
            )

            if event_key in seen:
                continue

            seen.add(event_key)
            matched += 1

            print(
                f"[{matched}] {date_str} type={result_type} "
                f"{sender} -> {recipients} | {reason} | smtp={smtp_code}"
            )

            if notify(
                txid=txid,
                sender=sender,
                recipients=recipients,
                reason=reason,
                date_str=date_str,
                result_type=result_type,
                smtp_code=smtp_code,
                result_path=path,
            ):
                sent += 1

    print(f"\nMatched events: {matched}")
    print(f"Telegram messages sent: {sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
