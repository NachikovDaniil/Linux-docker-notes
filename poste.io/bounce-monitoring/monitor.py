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

STATE_VERSION = 5

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL = max(1, int(os.getenv("CHECK_INTERVAL", "5")))
DEBUG = os.getenv("DEBUG", "0") == "1"

LOCAL_DOMAINS = {
    domain.strip().lower()
    for domain in os.getenv("LOCAL_DOMAINS", "").split(",")
    if domain.strip()
}


# ============================================================
# Regex
# ============================================================

EMAIL_RE = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.IGNORECASE,
)


# ============================================================
# Logging
# ============================================================

def log(message):
    print(
        f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
        f"{message}",
        flush=True,
    )


def debug(message):
    if DEBUG:
        log(message)


# ============================================================
# State
# ============================================================

def load_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return {
            "processed": {},
            "initialized": False,
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError("State root must be a JSON object")

        processed = state.setdefault("processed", {})

        if not isinstance(processed, dict):
            raise ValueError("'processed' must be a JSON object")

        state.setdefault("initialized", False)

        return state

    except Exception as error:
        # Не сбрасываем повреждённое состояние автоматически.
        # Иначе можно потерять историю обработки.
        raise RuntimeError(
            f"Cannot load state from {STATE_FILE}: {error}"
        ) from error


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    temporary_file = STATE_FILE.with_suffix(".tmp")

    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(
                state,
                file,
                ensure_ascii=False,
                indent=2,
            )

            file.flush()
            os.fsync(file.fileno())

        temporary_file.replace(STATE_FILE)

    except Exception as error:
        log(
            f"FATAL: cannot save state to {STATE_FILE}: {error}"
        )

        # Не продолжаем обработку без возможности сохранить историю.
        raise SystemExit(1)


# ============================================================
# Telegram
# ============================================================

def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log(
            "ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID "
            "is not configured"
        )
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    try:
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
        )

        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

            if response.status == 200 and result.get("ok"):
                return True

            log(
                "Telegram rejected the message: "
                f"{result.get('description', 'unknown error')}"
            )

    except Exception as error:
        log(f"Telegram error: {error}")

    return False


# ============================================================
# Helpers
# ============================================================

def normalize_email(value):
    if not value:
        return ""

    return str(value).strip().strip("<>").strip().lower()


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
    if not timestamp:
        return "не указано"

    try:
        date = datetime.fromisoformat(
            str(timestamp).replace("Z", "+00:00")
        )

        return date.astimezone().strftime(
            "%d.%m.%Y %H:%M:%S"
        )

    except Exception:
        return str(timestamp)


def event_id(*values):
    raw = "|".join(str(value) for value in values)

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def parse_json_objects(content):
    """
    Поддерживает один JSON-объект или JSON-объекты построчно.
    Возвращает только словари.
    """
    try:
        value = json.loads(content)

        if isinstance(value, dict):
            return [value]

        if isinstance(value, list):
            return [
                item
                for item in value
                if isinstance(item, dict)
            ]

    except json.JSONDecodeError:
        pass

    objects = []

    for line in content.splitlines():
        line = line.strip()

        if not line.startswith("{"):
            continue

        try:
            value = json.loads(line)

            if isinstance(value, dict):
                objects.append(value)

        except json.JSONDecodeError:
            continue

    return objects


def mark_processed(state, key, **details):
    state["processed"][key] = {
        "time": time.time(),
        **details,
    }


# ============================================================
# Parse tx file / tx .results
# ============================================================

def parse_tx_file(path):
    """
    Возвращает (sender, recipients).

    Основной формат:
    {
        "mail_from": {
            "address": "sender@example.com"
        },
        "rcpt_to": {
            "recipient": [
                {"address": "recipient@example.com"}
            ]
        }
    }
    """
    if not path:
        return None, []

    sender = None
    recipients = []

    try:
        content = path.read_text(
            encoding="utf-8",
            errors="replace",
        ).strip()

    except Exception as error:
        log(f"Error reading tx file {path}: {error}")
        return None, []

    if path.name.endswith(".results") or content.startswith("{"):
        for data in parse_json_objects(content):
            mail_from = data.get("mail_from")

            if isinstance(mail_from, dict):
                sender = normalize_email(
                    mail_from.get("address")
                )

            rcpt_to = data.get("rcpt_to")

            if not isinstance(rcpt_to, dict):
                continue

            recipient_list = rcpt_to.get("recipient")

            if not isinstance(recipient_list, list):
                continue

            for item in recipient_list:
                if isinstance(item, dict):
                    address = normalize_email(
                        item.get("address")
                    )

                elif isinstance(item, str):
                    address = normalize_email(item)

                else:
                    continue

                if address and address not in recipients:
                    recipients.append(address)

    # Текстовый fallback.
    need_text_recipients = not recipients

    for line in content.splitlines():
        if sender is None:
            match = re.search(
                r"sender\s+<([^>]+)>",
                line,
                re.IGNORECASE,
            )

            if match:
                sender = normalize_email(match.group(1))

        if need_text_recipients:
            match = re.search(
                r"recipient\s+<([^>]+)>",
                line,
                re.IGNORECASE,
            )

            if match:
                address = normalize_email(match.group(1))

                if address and address not in recipients:
                    recipients.append(address)

    return sender, recipients


def find_tx_file(txid):
    """
    Возможные пути:
      delivery/tx/<a>/<b>/<TXID>.1
      delivery/tx/<a>/<b>/<TXID>.1.results
    """
    base_txid = (
        txid.rsplit(".", 1)[0]
        if "." in txid
        else txid
    )

    if len(base_txid) < 2:
        return None

    directory = (
        LOG_ROOT
        / "delivery"
        / "tx"
        / base_txid[0]
        / base_txid[1]
    )

    candidates = [
        directory / f"{base_txid}.1.results",
        directory / f"{base_txid}.1",
        directory / base_txid,
        directory / txid,
    ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    try:
        matches = sorted(
            path
            for path in directory.glob(f"{base_txid}*")
            if path.is_file()
        )

        results = [
            path
            for path in matches
            if path.name.endswith(".results")
        ]

        if results:
            return results[0]

        if matches:
            return matches[0]

    except Exception as error:
        log(f"Error searching tx files for {txid}: {error}")

    return None


# ============================================================
# Sender / recipients from delivery results
# ============================================================

def extract_sender_from_event(event):
    data = event.get("data", {})

    if not isinstance(data, dict):
        data = {}

    for source in (data, event):
        for key in ("sender", "mail_from", "return_path"):
            value = source.get(key)

            if isinstance(value, dict):
                value = value.get("address")

            if isinstance(value, str):
                emails = EMAIL_RE.findall(value)

                if emails:
                    return normalize_email(emails[0])

    return None


def extract_failed_recipients(event):
    """
    Возвращает (recipients, reason, smtp_code).
    """
    recipients = []
    reason = ""
    smtp_code = ""

    data = event.get("data", {})

    if not isinstance(data, dict):
        data = {}

    def add_recipient(value):
        address = normalize_email(value)

        if (
            address
            and "@" in address
            and address not in recipients
        ):
            recipients.append(address)

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

            for key in (
                "original",
                "address",
                "rcpt",
                "recipient",
                "email",
            ):
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

    error_text = data.get("err") or event.get("err") or ""

    if not recipients and isinstance(error_text, str):
        for email in EMAIL_RE.findall(error_text):
            add_recipient(email)

    if not reason:
        reason = error_text

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

    recipients = [
        recipient
        for recipient in recipients
        if recipient
    ]

    if recipients:
        recipients_text = "\n".join(
            f"• {safe_html(recipient)}"
            for recipient in recipients
        )
    else:
        recipients_text = "не определён"

    sender_text = (
        safe_html(sender)
        if sender
        else "не определён"
    )

    reason_text = safe_html(reason or "не указана")
    time_text = safe_html(format_time(date_str))

    message = (
        "🚨 <b>POSTE.IO — ПИСЬМО НЕ ДОСТАВЛЕНО</b>\n\n"
        f"📤 <b>От:</b>\n{sender_text}\n\n"
        f"📥 <b>Кому:</b>\n{recipients_text}\n\n"
        f"🕐 <b>Время:</b>\n{time_text}\n\n"
        f"💬 <b>Причина:</b>\n{reason_text}\n\n"
        f"🆔 <b>Transaction:</b>\n"
        f"<code>{safe_html(txid)}</code>"
    )

    if result_path:
        message += (
            "\n\n🗂 <b>Result:</b>\n"
            f"<code>{safe_html(result_path)}</code>"
        )

    return telegram_send(message)


# ============================================================
# Scan delivery results
# ============================================================

def get_bounce_result_files():
    base = LOG_ROOT / "delivery" / "del"

    if not base.is_dir():
        raise RuntimeError(
            f"Delivery log directory is unavailable: {base}. "
            "Check the log volume mount."
        )

    files = []

    for path in base.glob("*/*/*.results"):
        try:
            if path.is_file():
                files.append((
                    path.stat().st_mtime_ns,
                    str(path),
                    path,
                ))

        except FileNotFoundError:
            # Файл мог исчезнуть во время обхода.
            continue

    files.sort(key=lambda item: (item[0], item[1]))

    return [item[2] for item in files]


def process_bounce_result(path, state):
    processed = state["processed"]
    file_key = str(path)

    if file_key in processed:
        debug(f"Skip already processed: {file_key}")
        return 0

    try:
        content = path.read_text(
            encoding="utf-8",
            errors="replace",
        ).strip()

    except Exception as error:
        log(f"Error reading {path}: {error}")
        return 0

    debug(f"RESULT FILE: {path}")
    debug(f"Content: {content}")

    # Пустой или пока не читающийся JSON не помечаем обработанным:
    # файл может ещё записываться.
    if not content:
        debug(f"Empty result file, retry later: {path}")
        return 0

    events = parse_json_objects(content)

    if not events:
        debug(f"No JSON events, retry later: {path}")
        return 0

    bounce_event = None

    for event in events:
        result_type = str(event.get("type") or "").lower()

        if result_type in ("bounced", "rejected"):
            bounce_event = event

    if bounce_event is None:
        # Deferred или другой промежуточный результат:
        # перечитаем файл на следующем проходе.
        debug(f"No bounced/rejected event yet: {path}")
        return 0

    result_type = str(
        bounce_event.get("type") or ""
    ).lower()

    date_str = bounce_event.get("date") or ""
    txid = path.name[:-len(".results")]

    tx_path = find_tx_file(txid)
    tx_sender, tx_recipients = parse_tx_file(tx_path)

    sender = (
        tx_sender
        or extract_sender_from_event(bounce_event)
    )

    recipients, reason, smtp_code = (
        extract_failed_recipients(bounce_event)
    )

    if not recipients:
        recipients = list(tx_recipients)

    if sender:
        recipients = [
            recipient
            for recipient in recipients
            if recipient != sender
        ]

    debug(f"BOUNCE: txid={txid}")
    debug(f"Type: {result_type}")
    debug(f"TX file: {tx_path}")
    debug(f"Sender: {sender}")
    debug(f"Recipients: {recipients}")
    debug(f"Reason: {reason}")
    debug(f"SMTP code: {smtp_code}")

    # Сохраняем прежнее поведение:
    # известный внешний отправитель пропускается,
    # неизвестный отправитель не блокирует уведомление.
    if sender and not is_local_sender(sender):
        debug(f"Skip non-local sender: {sender}")

        mark_processed(
            state,
            file_key,
            skipped="non-local",
        )
        return 0

    eid = event_id(
        txid,
        sender or "",
        ",".join(sorted(recipients)),
        reason,
        result_type,
    )

    # Совместимость с хешами, которые сохраняла старая версия:
    # в ней порядок получателей не сортировался.
    legacy_eid = event_id(
        txid,
        sender or "",
        ",".join(recipients),
        reason,
        result_type,
    )

    if eid in processed or legacy_eid in processed:
        mark_processed(
            state,
            file_key,
            eid=eid,
            skipped="duplicate-event",
        )
        return 0

    log(
        f"Delivery failure: txid={txid} "
        f"sender={sender} -> {recipients} "
        f"err={reason}"
    )

    sent = notify_bounce(
        txid=txid,
        sender=sender,
        recipients=recipients,
        reason=reason,
        date_str=date_str,
        result_path=path,
    )

    if not sent:
        # Не отмечаем обработанным:
        # повторим отправку на следующем проходе.
        return 0

    mark_processed(
        state,
        file_key,
        txid=txid,
        eid=eid,
    )

    mark_processed(
        state,
        eid,
        txid=txid,
    )

    # Сохраняем сразу после успешной отправки.
    save_state(state)

    return 1


# ============================================================
# Initialization / migration
# ============================================================

def initialize_state(state):
    if (
        state.get("initialized")
        and state.get("dedup_version") == STATE_VERSION
    ):
        return

    log(
        "Initializing v5: marking existing result files "
        "as processed without notifications..."
    )

    processed = state["processed"]
    now = time.time()
    count = 0

    # Сначала получаем список целиком.
    # Если каталог недоступен, инициализация не завершится.
    paths = get_bounce_result_files()

    for path in paths:
        file_key = str(path)

        if file_key not in processed:
            processed[file_key] = {
                "time": now,
                "boot_skip": True,
            }
            count += 1

    state["initialized"] = True
    state["dedup_version"] = STATE_VERSION
    state["baseline_time"] = now

    save_state(state)

    log(
        f"Initialization completed: "
        f"{count} existing files marked as processed."
    )
    log("Waiting for new result files.")


# ============================================================
# Main
# ============================================================

def main():
    log("=" * 60)
    log("Poste.io Bounce Monitor v5")
    log("=" * 60)

    log(f"Log root:      {LOG_ROOT}")
    log(f"State file:    {STATE_FILE}")
    log(f"Local domains: {', '.join(sorted(LOCAL_DOMAINS)) or '(empty)'}")
    log(f"Interval:      {CHECK_INTERVAL}s")
    log(f"Debug:         {DEBUG}")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "must be configured"
        )

    if not LOCAL_DOMAINS:
        raise RuntimeError(
            "LOCAL_DOMAINS is empty. "
            "Example: LOCAL_DOMAINS=example.com,example.org"
        )

    if not LOG_ROOT.is_dir():
        raise RuntimeError(
            f"Log root is unavailable: {LOG_ROOT}. "
            "Check the container volume mount."
        )

    state = load_state()
    initialize_state(state)

    log(
        f"Loaded {len(state['processed'])} "
        "processed file/event records."
    )

    while True:
        try:
            total = 0

            for path in get_bounce_result_files():
                try:
                    total += process_bounce_result(
                        path,
                        state,
                    )

                except Exception as error:
                    log(
                        f"Error processing {path}: {error}"
                    )

            # Сохраняем также отметки non-local и duplicate-event.
            # Историю по возрасту НЕ удаляем.
            save_state(state)

            if total:
                log(
                    f"Sent {total} Telegram notification(s)"
                )

        except Exception as error:
            log(f"Main loop error: {error}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        log("Stopped.")

    except Exception as error:
        log(f"FATAL: {error}")
        raise SystemExit(1)
