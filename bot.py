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
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo
from get_end_date import get_goodwill_end_date

from dateutil import parser as date_parser

# --- LOGGING CONFIGURATION ---
ENABLE_LOGGING = True  # Set to False to disable all logging output
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
LOG_FILE = os.getenv("LOG_FILE", str(DATA_DIR / "logging.txt"))

log = logging.getLogger("shopgoodwill-alert-bot")
log.setLevel(logging.DEBUG)
log.handlers.clear()

if ENABLE_LOGGING:
    class FlushingFileHandler(logging.FileHandler):
        """A file handler that forces a flush to disk after every log entry."""
        def emit(self, record):
            super().emit(record)
            self.flush()

    detailed_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    log.addHandler(console_handler)

    try:
        log_path = Path(LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = FlushingFileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(detailed_formatter)
        log.addHandler(file_handler)
        print(f"Successfully initialized log file at: {LOG_FILE}")
    except Exception as e:
        print(f"CRITICAL: Could not create file logger at {LOG_FILE}: {e}")
else:
    log.addHandler(logging.NullHandler())
# -----------------------------

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

log.debug(f"Configuration loaded: IMAP_HOST={IMAP_HOST}, SMTP_HOST={SMTP_HOST}")
log.debug(f"Search terms: {SEARCH_TERMS}, Alert window: {ALERT_MINUTES_BEFORE}m")

ITEM_URL_RE = re.compile(
    r"https?://(?:www\.)?shopgoodwill\.com/item/\d+(?:[^\s<>\"']*)?",
    re.IGNORECASE,
)

EMAIL_TITLE_RE = re.compile(
    r"^[ \t]*Title:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE
)

TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"[ \t]+")

END_LABEL_RE = re.compile(
    r"\b(?:auction\s+)?(?:end|ends|ending|end\s+time|closing|closes)\b",
    re.IGNORECASE,
)

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
    log.info("Initializing database...")
    log.debug(f"Database path: {DB_PATH}")
    try:
        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        log.debug(f"Data directory created/verified: {DATA_DIR}")
    except Exception as e:
        log.error(f"Failed to create data directory: {e}")
        raise
    
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
    log.info("Database initialization complete")


def cleanup_expired_auctions() -> None:
    """Deletes auctions whose end time passed more than 30 minutes ago."""
    cutoff_utc = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                "DELETE FROM auctions WHERE end_time_utc < ?",
                (cutoff_utc,),
            )
            deleted_count = cursor.rowcount
            conn.commit()
            if deleted_count > 0:
                log.info(f"🗑️ Cleaned up {deleted_count} expired auction(s) from database")
    except Exception as e:
        log.error(f"Failed to cleanup expired auctions: {e}")


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
    log.debug(f"Attempting to save auction - URL: {item_url}, Title: {title[:50]}...")
    end_utc = end_time.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)

    if end_utc <= now_utc:
        log.info(f"Skipping auction that already ended in the past: {item_url}")
        return False

    end_utc_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT end_time_utc FROM auctions WHERE item_url = ?",
            (item_url,),
        ).fetchone()

        if existing:
            log.info(f"Auction already exists, updating: {item_url}")
            conn.execute(
                """
                UPDATE auctions
                SET title = ?, end_time_utc = ?, source_subject = ?
                WHERE item_url = ?
                """,
                (title, end_utc_str, subject, item_url),
            )
            conn.commit()
            return False

        log.info(f"New auction saved: {item_url}")
        conn.execute(
            """
            INSERT INTO auctions(
                item_url, title, end_time_utc, source_subject, discovered_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                item_url,
                title,
                end_utc_str,
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
            
            if "attachment" in disposition or content_type not in {"text/plain", "text/html"}:
                continue
            
            try:
                payload = part.get_content()
            except Exception:
                raw = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                payload = raw.decode(charset, errors="replace")
            
            if content_type == "text/html":
                hrefs = re.findall(r'href=["\'](https?://[^"\']+)["\']', payload, flags=re.I)
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
            hrefs = re.findall(r'href=["\'](https?://[^"\']+)["\']', payload, flags=re.I)
            payload = html.unescape(TAG_RE.sub(" ", payload))
            if hrefs:
                payload += "\n" + "\n".join(html.unescape(url) for url in hrefs)
        parts.append(payload)

    text = "\n".join(parts).replace("\r", "\n")
    return "\n".join(
        WHITESPACE_RE.sub(" ", line).strip()
        for line in text.splitlines()
        if line.strip()
    )


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
    """Fallback title inference from the raw email text."""
    title_match = EMAIL_TITLE_RE.search(text)
    if title_match:
        extracted = title_match.group(1).strip()
        extracted = re.sub(r"<[^>]+>", "", extracted).strip()
        if extracted:
            return extracted

    # Scan for search terms while ignoring metadata/keyword lines
    for line in text.splitlines():
        cleaned_line = line.strip()
        if re.search(r"\b(?:search|keyword|keywords|category|ends?|time|item)\b", cleaned_line, re.I):
            continue
        if contains_search_term(cleaned_line) and 3 <= len(cleaned_line) <= 180:
            return cleaned_line

    return subject.strip() or "shopgoodwill auction"


def parse_end_time(text: str) -> datetime | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidate_lines = [line for line in lines if END_LABEL_RE.search(line)]

    for line in candidate_lines:
        match = DATE_FRAGMENT_RE.search(line)
        if match:
            parsed = parse_datetime_fragment(match.group(1))
            if parsed:
                return parsed

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


def resolve_auction_details(
    item_url: str, 
    fallback_dt: datetime | None, 
    fallback_title: str
) -> tuple[datetime | None, str]:
    """
    Attempts to pull authoritative end time and title from the API.
    Falls back gracefully to values parsed from the email.
    """
    api_end_time, api_title = get_goodwill_end_date(item_url)
    
    final_title = api_title if api_title else fallback_title
    final_end_time = api_end_time if api_end_time else fallback_dt

    if api_end_time:
        log.info(f"[AUCTION-DETAILS] Using API data for {item_url}: Title='{final_title}', Ends='{final_end_time.isoformat()}'")
    else:
        log.warning(f"[AUCTION-DETAILS] API lookup incomplete for {item_url}; using fallback data")

    return final_end_time, final_title


def extract_item_pairs(text: str) -> dict[str, str]:
    """
    Extracts {item_url: title} blocks when multiple items are listed in one email.
    """
    url_to_title = {}
    blocks = re.split(r"(?:\r?\n\s*\r?\n|(?=Item\s*#:))", text)
    for block in blocks:
        title_m = EMAIL_TITLE_RE.search(block)
        url_m = ITEM_URL_RE.search(block)
        if title_m and url_m:
            url = clean_item_url(html.unescape(url_m.group(0)))
            title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
            if url and title:
                url_to_title[url] = title

    return url_to_title


def send_email(subject: str, body: str) -> None:
    log.info(f"Sending email: {subject}")
    
    message = EmailMessage()
    message["From"] = ALERT_FROM_EMAIL
    message["To"] = ALERT_TO_EMAIL
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    try:
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=context) as smtp:
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(message)
                log.info("Email sent successfully via SMTP_SSL")
            return

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            if SMTP_USE_TLS:
                smtp.starttls(context=context)
                smtp.ehlo()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
            log.info("Email sent successfully via SMTP")
    except Exception as e:
        log.error(f"Failed to send email: {type(e).__name__}: {e}")
        raise


def fetch_messages() -> Iterable[tuple[str, email.message.EmailMessage]]:
    since = datetime.now().astimezone() - timedelta(days=IMAP_LOOKBACK_DAYS)
    since_token = since.strftime("%d-%b-%Y")

    try:
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
                    continue

                raw = next((part[1] for part in message_data if isinstance(part, tuple) and len(part) > 1), None)
                if not raw:
                    continue

                msg = email.message_from_bytes(raw, policy=default)
                message_id = (msg.get("Message-ID") or "").strip()
                message_key = message_id or f"imap:{IMAP_FOLDER}:{uid.decode()}"
                yield message_key, msg
    except Exception as e:
        log.error(f"IMAP fetch failed: {type(e).__name__}: {e}")
        raise


def process_inbox() -> None:
    global last_poll_ok_at, last_error

    log.info("=" * 60)
    log.info("Starting inbox processing cycle")
    new_count = 0
    processed_count = 0
    skipped_count = 0
    
    try:
        messages = list(fetch_messages())
        failed_filters = {"sender": 0, "search_term": 0, "no_urls": 0, "no_end_time": 0}
        
        for message_key, msg in messages:
            processed_count += 1
            
            if message_processed(message_key):
                skipped_count += 1
                continue

            sender = str(msg.get("From") or "")
            subject = str(msg.get("Subject") or "")
            text = plain_text_from_message(msg)
            searchable = f"{subject}\n{text}"

            if IMAP_SENDER_MATCH.lower() not in sender.lower():
                mark_message_processed(message_key)
                failed_filters["sender"] += 1
                skipped_count += 1
                continue

            if not contains_search_term(searchable):
                mark_message_processed(message_key)
                failed_filters["search_term"] += 1
                skipped_count += 1
                continue

            urls = extract_item_urls(searchable)
            email_end_time = parse_end_time(searchable)
            item_pairs = extract_item_pairs(text)
            fallback_title = infer_title(subject, text)

            if not urls:
                failed_filters["no_urls"] += 1
                mark_message_processed(message_key)
                continue

            for url in urls:
                item_fallback_title = item_pairs.get(url, fallback_title)
                end_time, title = resolve_auction_details(url, email_end_time, item_fallback_title)
                
                if not end_time:
                    failed_filters["no_end_time"] += 1
                    if SEND_PARSE_FAILURES:
                        try:
                            send_email(
                                "[Shopshopgoodwill bot] Parse failure",
                                f"Could not determine end time.\n\nSubject: {subject}\nItem: {url}\n\nBody:\n{text[:500]}",
                            )
                        except Exception as e:
                            log.error(f"Failed to send parse failure email: {e}")
                    continue

                created = save_auction(url, title, end_time, subject)
                if created:
                    new_count += 1
                    log.info(f"✅ Auction SAVED: {url} | Title: {title}")
                else:
                    log.info(f"⟳ Updated existing: {url} | Title: {title}")

            mark_message_processed(message_key)

        last_poll_ok_at = datetime.now(timezone.utc)
        last_error = None
        log.info(f"📊 POLL SUMMARY - Total: {len(messages)}, Processed: {processed_count}, Saved: {new_count}✅")
        log.info("=" * 60)
    except Exception as e:
        last_error = f"{type(e).__name__}: {e}"
        log.error(f"Process inbox failed: {last_error}")
        raise


def send_due_alerts() -> None:
    now = datetime.now(timezone.utc)
    alerts_sent = 0
    
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

            if minutes_left < -5:
                conn.execute(
                    "UPDATE auctions SET alerted_at = ? WHERE item_url = ?",
                    (now.isoformat(), row["item_url"]),
                )
                continue

            if 0 <= minutes_left <= ALERT_MINUTES_BEFORE:
                log.info(f"⏰ ALERT DUE: {row['title']} - {int(minutes_left)} minutes remaining")
                local_end = end_time.astimezone(AUCTION_TIMEZONE)
                rounded = max(1, int(round(minutes_left)))
                try:
                    send_email(
                        f"[shopgoodwill] Item ending in {rounded} minutes",
                        (
                            f"{row['title']}\n\n"
                            f"Ends: {local_end.strftime('%B %d, %Y at %I:%M %p %Z')}\n"
                            f"Time remaining: about {rounded} minutes\n"
                            f"Open item: {row['item_url']}\n\n"
                            "This alert was generated from an official shopgoodwill "
                            "notification email; the bot does not scrape or log into "
                            "shopgoodwill."
                        ),
                    )
                    alerts_sent += 1
                except Exception as e:
                    log.error(f"Failed to send alert for {row['item_url']}: {e}")
                
                conn.execute(
                    "UPDATE auctions SET alerted_at = ? WHERE item_url = ?",
                    (now.isoformat(), row["item_url"]),
                )
                log.info(f"Alert sent and recorded for {row['item_url']}")

        conn.commit()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            poll_healthy = (
                last_poll_ok_at is not None
                and (datetime.now(timezone.utc) - last_poll_ok_at).total_seconds()
                < max(POLL_SECONDS * 3, 600)
            )

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
            return

        if self.path in {"/", "/auctions"}:
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        """
                        SELECT item_url, title, end_time_utc, alerted_at
                        FROM auctions
                        ORDER BY end_time_utc ASC
                        """
                    ).fetchall()
            except Exception as e:
                rows = []
                log.error(f"Failed to fetch auctions for webpage: {e}")

            table_rows = ""
            for r in rows:
                title = html.escape(r["title"] or "No Title")
                url = html.escape(r["item_url"])
                end_time = r["end_time_utc"]
                alerted = "✅ Yes" if r["alerted_at"] else "⏳ Pending"

                table_rows += f"""
                <tr>
                    <td><a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a></td>
                    <td><code>{end_time}</code></td>
                    <td>{alerted}</td>
                </tr>
                """

            html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tracked Auctions</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; margin: 0; }}
        h1 {{ margin-bottom: 0.5rem; }}
        p {{ color: #94a3b8; margin-bottom: 1.5rem; }}
        table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }}
        th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; }}
        th {{ background: #334155; font-weight: 600; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em; }}
        tr:hover {{ background: #283548; }}
        a {{ color: #38bdf8; text-decoration: none; font-weight: 500; }}
        a:hover {{ text-decoration: underline; }}
        code {{ background: #0f172a; padding: 2px 6px; border-radius: 4px; font-size: 0.85em; color: #facc15; }}
    </style>
</head>
<body>
    <h1>Tracked Auctions ({len(rows)})</h1>
    <p>Live inventory from <code>state.db</code></p>
    <table>
        <thead>
            <tr>
                <th>Item Title & Link</th>
                <th>End Time (UTC)</th>
                <th>Alert Status</th>
            </tr>
        </thead>
        <tbody>
            {table_rows or '<tr><td colspan="3" style="text-align:center; color:#94a3b8;">No active auctions found in database.</td></tr>'}
        </tbody>
    </table>
</body>
</html>"""

            encoded = html_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


def json_bytes(value: dict) -> bytes:
    import json
    return json.dumps(value, indent=2).encode("utf-8")


def run_health_server() -> None:
    log.info(f"Starting web server on port {HEALTH_PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def handle_shutdown(signum, frame) -> None:
    log.info(f"Received signal {signum}; initiating shutdown")
    shutdown_event.set()


def main() -> None:
    global last_error

    log.info("╔" + "=" * 58 + "╗")
    log.info("║" + " BOT STARTUP ".center(58) + "║")
    log.info("╚" + "=" * 58 + "╝")

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    init_db()
    cleanup_expired_auctions()
    run_health_server()
    log.info(f"Starting bot: {env_summary()}")

    cycle_count = 0
    while not shutdown_event.is_set():
        cycle_count += 1
        started = time.monotonic()
        try:
            cleanup_expired_auctions()
            process_inbox()
            send_due_alerts()
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.exception(f"Bot cycle failed: {last_error}")

        elapsed = time.monotonic() - started
        wait_time = max(5, POLL_SECONDS - elapsed)
        shutdown_event.wait(wait_time)
    
    log.info("╔" + "=" * 58 + "╗")
    log.info("║" + " BOT SHUTDOWN ".center(58) + "║")
    log.info("╚" + "=" * 58 + "╝")


if __name__ == "__main__":
    main()