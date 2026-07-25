import email
import html
import imaplib
import logging
import os
import re
import signal
import smtplib
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("shopgoodwill-alert-bot")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "state.db"

IMAP_HOST = os.environ["IMAP_HOST"]
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USERNAME = os.environ["IMAP_USERNAME"]
IMAP_PASSWORD = os.environ["IMAP_PASSWORD"]
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
IMAP_SENDER_MATCH = os.getenv("IMAP_SENDER_MATCH", "shopgoodwill")
IMAP_LOOKBACK_DAYS = int(os.getenv("IMAP_LOOKBACK_DAYS", "14"))

SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.getenv("SMTP_PORT", "2525"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}

ALERT_FROM_EMAIL = os.environ["ALERT_FROM_EMAIL"]
ALERT_TO_EMAIL = os.getenv("ALERT_TO_EMAIL", "admin@planenews.net")

SEARCH_TERMS = [
    item.strip().lower()
    for item in os.getenv("SEARCH_TERMS", "charm,charms").split(",")
    if item.strip()
]
ALERT_MINUTES_BEFORE = int(os.getenv("ALERT_MINUTES_BEFORE", "60"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))
AUCTION_TIMEZONE = ZoneInfo(os.getenv("AUCTION_TIMEZONE", "America/Los_Angeles"))
HEALTH_PORT = int(os.getenv("PORT", "8080"))
SEND_PARSE_FAILURES = os.getenv("SEND_PARSE_FAILURES", "false").lower() in {
    "1", "true", "yes", "on"
}

ITEM_URL_RE = re.compile(
    r"https?://(?:www\.)?shopgoodwill\.com/item/\d+(?:[^\s<>\"']*)?",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"[ \t]+")

END_LABEL_RE = re.compile(
    r"\b(?:auction\s+)?(?:end|ends|ending|end\s+time|closing|closes)\b",
    re.IGNORECASE,
)

# Common date/time fragments found in marketplace emails.
DATE_FRAGMENT_RE = re.compile(
    r"""
    (
      (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?[,]?\s*
      (?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|
         Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|
         Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)
      \s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+\d{4}
      (?:\s+(?:at\s+)?)?
      \d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?
      (?:\s*(?:PT|PST|PDT|ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|UTC|GMT))?
    |
      \d{1,2}[/-]\d{1,2}[/-]\d{2,4}
      (?:\s+(?:at\s+)?)?
      \d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?
      (?:\s*(?:PT|PST|PDT|ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|UTC|GMT))?
    |
      \d{4}-\d{2}-\d{2}[ T]
      \d{1,2}:\d{2}(?::\d{2})?
      (?:Z|[+-]\d{2}:?\d{2})?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

TZINFOS = {
    "PT": AUCTION_TIMEZONE,
    "PST": ZoneInfo("America/Los_Angeles"),
    "PDT": ZoneInfo("America/Los_Angeles"),
    "ET": ZoneInfo("America/New_York"),
    "EST": ZoneInfo("America/New_York"),
    "EDT": ZoneInfo("America/New_York"),
    "CT": ZoneInfo("America/Chicago"),
    "CST": ZoneInfo("America/Chicago"),
    "CDT": ZoneInfo("America/Chicago"),
    "MT": ZoneInfo("America/Denver"),
    "MST": ZoneInfo("America/Denver"),
    "MDT": ZoneInfo("America/Denver"),
    "UTC": timezone.utc,
    "GMT": timezone.utc,
}

shutdown_event = threading.Event()
last_poll_ok_at: datetime | None = None
last_error: str | None = None


def env_summary() -> str:
    return (
        f"terms={SEARCH_TERMS}, alert={ALERT_MINUTES_BEFORE}m, "
        f"poll={POLL_SECONDS}s, destination={ALERT_TO_EMAIL}"
    )


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                message_key TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auctions (
                item_url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                end_time_utc TEXT NOT NULL,
                source_subject TEXT,
                discovered_at TEXT NOT NULL,
                alerted_at TEXT
            )
        """)
        conn.commit()


def message_processed(message_key: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE message_key = ?",
            (message_key,),
        ).fetchone()
        return row is not None


def mark_message_processed(message_key: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO messages(message_key, processed_at) VALUES (?, ?)",
            (message_key, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def save_auction(item_url: str, title: str, end_time: datetime, subject: str) -> bool:
    end_utc = end_time.astimezone(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT end_time_utc FROM auctions WHERE item_url = ?",
            (item_url,),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE auctions
                SET title = ?, end_time_utc = ?, source_subject = ?
                WHERE item_url = ?
                """,
                (title, end_utc, subject, item_url),
            )
            conn.commit()
            return False

        conn.execute(
            """
            INSERT INTO auctions(
                item_url, title, end_time_utc, source_subject, discovered_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                item_url,
                title,
                end_utc,
                subject,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return True


def plain_text_from_message(msg: email.message.EmailMessage) -> str:
    parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if content_type not in {"text/plain", "text/html"}:
                continue
            try:
                payload = part.get_content()
            except Exception:
                raw = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                payload = raw.decode(charset, errors="replace")
            if content_type == "text/html":
                # Preserve URLs used only as HTML button/link targets.
                hrefs = re.findall(
                    r'href=["\'](https?://[^"\']+)["\']',
                    payload,
                    flags=re.IGNORECASE,
                )
                payload = html.unescape(TAG_RE.sub(" ", payload))
                if hrefs:
                    payload += "\n" + "\n".join(html.unescape(url) for url in hrefs)
            parts.append(payload)
    else:
        try:
            payload = msg.get_content()
        except Exception:
            raw = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            payload = raw.decode(charset, errors="replace")
        if msg.get_content_type() == "text/html":
            hrefs = re.findall(
                r'href=["\'](https?://[^"\']+)["\']',
                payload,
                flags=re.IGNORECASE,
            )
            payload = html.unescape(TAG_RE.sub(" ", payload))
            if hrefs:
                payload += "\n" + "\n".join(html.unescape(url) for url in hrefs)
        parts.append(payload)

    text = "\n".join(parts)
    text = text.replace("\r", "\n")
    text = "\n".join(
        WHITESPACE_RE.sub(" ", line).strip()
        for line in text.splitlines()
        if line.strip()
    )
    return text


def contains_search_term(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in SEARCH_TERMS)


def clean_item_url(url: str) -> str:
    match = re.search(r"(https?://(?:www\.)?shopgoodwill\.com/item/\d+)", url, re.I)
    return match.group(1) if match else url.rstrip(".,);]")


def extract_item_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in ITEM_URL_RE.finditer(text):
        url = clean_item_url(html.unescape(match.group(0)))
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def infer_title(subject: str, text: str) -> str:
    for line in text.splitlines():
        if contains_search_term(line) and 3 <= len(line) <= 180:
            return line
    return subject.strip() or "ShopGoodwill charm auction"


def parse_end_time(text: str) -> datetime | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidate_lines = [line for line in lines if END_LABEL_RE.search(line)]

    # Prefer dates on lines explicitly labeled as ending/closing.
    for line in candidate_lines:
        match = DATE_FRAGMENT_RE.search(line)
        if not match:
            continue
        parsed = parse_datetime_fragment(match.group(1))
        if parsed:
            return parsed

    # Fallback: scan all lines, useful when the email uses a separate label and value.
    for index, line in enumerate(lines):
        if END_LABEL_RE.search(line):
            nearby = " ".join(lines[index:index + 3])
            match = DATE_FRAGMENT_RE.search(nearby)
            if match:
                parsed = parse_datetime_fragment(match.group(1))
                if parsed:
                    return parsed
    return None


def parse_datetime_fragment(value: str) -> datetime | None:
    value = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value, flags=re.I)
    try:
        parsed = date_parser.parse(value, fuzzy=True, tzinfos=TZINFOS)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=AUCTION_TIMEZONE)
    return parsed


def send_email(subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = ALERT_FROM_EMAIL
    message["To"] = ALERT_TO_EMAIL
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    if SMTP_USE_SSL:
        with smtplib.SMTP_SSL(
            SMTP_HOST, SMTP_PORT, timeout=30, context=context
        ) as smtp:
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
        return

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.ehlo()
        if SMTP_USE_TLS:
            smtp.starttls(context=context)
            smtp.ehlo()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(message)


def fetch_messages() -> Iterable[tuple[str, email.message.EmailMessage]]:
    since = datetime.now().astimezone() - timedelta(days=IMAP_LOOKBACK_DAYS)
    since_token = since.strftime("%d-%b-%Y")

    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as mailbox:
        mailbox.login(IMAP_USERNAME, IMAP_PASSWORD)
        status, _ = mailbox.select(IMAP_FOLDER, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not select IMAP folder {IMAP_FOLDER!r}")

        status, data = mailbox.uid("search", None, f'(SINCE "{since_token}")')
        if status != "OK":
            raise RuntimeError("IMAP search failed")

        uids = (data[0] or b"").split()
        for uid in uids[-250:]:
            status, message_data = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not message_data:
                log.warning("Could not fetch IMAP UID %s", uid.decode())
                continue

            raw = next(
                (
                    part[1]
                    for part in message_data
                    if isinstance(part, tuple) and len(part) > 1
                ),
                None,
            )
            if not raw:
                continue

            msg = email.message_from_bytes(raw, policy=default)
            message_id = (msg.get("Message-ID") or "").strip()
            message_key = message_id or f"imap:{IMAP_FOLDER}:{uid.decode()}"
            yield message_key, msg


def process_inbox() -> None:
    global last_poll_ok_at, last_error

    new_count = 0
    for message_key, msg in fetch_messages():
        if message_processed(message_key):
            continue

        sender = str(msg.get("From") or "")
        subject = str(msg.get("Subject") or "")
        text = plain_text_from_message(msg)
        searchable = f"{subject}\n{text}"

        # Only inspect mail that appears to originate from ShopGoodwill.
        if IMAP_SENDER_MATCH.lower() not in sender.lower():
            mark_message_processed(message_key)
            continue

        if not contains_search_term(searchable):
            mark_message_processed(message_key)
            continue

        urls = extract_item_urls(searchable)
        end_time = parse_end_time(searchable)
        title = infer_title(subject, text)

        if not urls:
            log.warning("Matching email has no ShopGoodwill item URL: %s", subject)
        elif not end_time:
            log.warning("Matching email has no parseable auction end time: %s", subject)
            if SEND_PARSE_FAILURES:
                send_email(
                    "[ShopGoodwill bot] Could not read auction end time",
                    (
                        f"A ShopGoodwill email matched {SEARCH_TERMS}, but the bot "
                        "could not find a recognizable end date/time.\n\n"
                        f"Subject: {subject}\n"
                        f"Item: {urls[0]}\n\n"
                        "Check the container logs and update the parser if the "
                        "email format has changed."
                    ),
                )
        else:
            for url in urls:
                created = save_auction(url, title, end_time, subject)
                if created:
                    new_count += 1
                    log.info(
                        "Queued %s ending at %s",
                        url,
                        end_time.astimezone(AUCTION_TIMEZONE).isoformat(),
                    )

        mark_message_processed(message_key)

    last_poll_ok_at = datetime.now(timezone.utc)
    last_error = None
    log.info("Inbox poll complete; queued %d new auction(s)", new_count)


def send_due_alerts() -> None:
    now = datetime.now(timezone.utc)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT item_url, title, end_time_utc
            FROM auctions
            WHERE alerted_at IS NULL
            ORDER BY end_time_utc
            """
        ).fetchall()

        for row in rows:
            end_time = datetime.fromisoformat(row["end_time_utc"])
            minutes_left = (end_time - now).total_seconds() / 60

            # Do not send for already-ended auctions. A five-minute grace handles
            # clock skew without creating stale alerts.
            if minutes_left < -5:
                conn.execute(
                    "UPDATE auctions SET alerted_at = ? WHERE item_url = ?",
                    (now.isoformat(), row["item_url"]),
                )
                continue

            if 0 <= minutes_left <= ALERT_MINUTES_BEFORE:
                local_end = end_time.astimezone(AUCTION_TIMEZONE)
                rounded = max(1, int(round(minutes_left)))
                send_email(
                    f"[ShopGoodwill] Charm ending in {rounded} minutes",
                    (
                        f"{row['title']}\n\n"
                        f"Ends: {local_end.strftime('%B %d, %Y at %I:%M %p %Z')}\n"
                        f"Time remaining: about {rounded} minutes\n"
                        f"Open item: {row['item_url']}\n\n"
                        "This alert was generated from an official ShopGoodwill "
                        "notification email; the bot does not scrape or log into "
                        "ShopGoodwill."
                    ),
                )
                conn.execute(
                    "UPDATE auctions SET alerted_at = ? WHERE item_url = ?",
                    (now.isoformat(), row["item_url"]),
                )
                log.info("Sent due alert for %s", row["item_url"])

        conn.commit()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/", "/health"}:
            self.send_response(404)
            self.end_headers()
            return


        poll_healthy = (
            last_poll_ok_at is not None
            and (datetime.now(timezone.utc) - last_poll_ok_at).total_seconds()
            < max(POLL_SECONDS * 3, 600)
        )

        # Keep the container healthy while reporting mailbox problems.
        status = 200
        payload = {
            "status": "ok" if poll_healthy else "degraded",




            "last_poll_ok_at": last_poll_ok_at.isoformat() if last_poll_ok_at else None,
            "last_error": last_error,
            "configuration": env_summary(),
        }
        raw = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:
        return


def json_bytes(value: dict) -> bytes:
    import json
    return json.dumps(value, indent=2).encode("utf-8")


def run_health_server() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Health endpoint listening on port %d", HEALTH_PORT)


def handle_shutdown(signum, frame) -> None:
    log.info("Received signal %s; stopping", signum)
    shutdown_event.set()


def main() -> None:
    global last_error

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    init_db()
    run_health_server()
    log.info("Starting bot: %s", env_summary())

    while not shutdown_event.is_set():
        started = time.monotonic()
        try:
            process_inbox()
            send_due_alerts()
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.exception("Bot cycle failed")

        elapsed = time.monotonic() - started
        shutdown_event.wait(max(5, POLL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
