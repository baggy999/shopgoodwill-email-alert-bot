"""
Looks up the authoritative auction end time and title for a shopgoodwill item
directly from shopgoodwill's own buyer API.
"""

import html
import logging
import random
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dateutil import parser as date_parser
import requests

log = logging.getLogger("shopgoodwill-alert-bot")

# ShopGoodwill's API returns timestamps in Pacific Time (PT)
SG_TZ = ZoneInfo("America/Los_Angeles")

API_URL_TEMPLATE = "https://buyerapi.shopgoodwill.com/api/ItemDetail/GetItemDetailModelByItemId/{item_id}"

REQUEST_DELAY_MIN_SECONDS = 10
REQUEST_DELAY_MAX_SECONDS = 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

ITEM_ID_RE = re.compile(r"/item/(\d+)")

_last_request_at: float | None = None


def _throttle_before_request() -> None:
    """
    Sleeps a random amount of time before the next API request so
    consecutive lookups aren't fired back-to-back.
    """
    global _last_request_at

    if _last_request_at is not None:
        delay = random.uniform(REQUEST_DELAY_MIN_SECONDS, REQUEST_DELAY_MAX_SECONDS)
        log.info(f"[get_end_date] Waiting {delay:.1f}s before next API request")
        time.sleep(delay)

    _last_request_at = time.monotonic()


def extract_item_id(url: str) -> str | None:
    match = ITEM_ID_RE.search(url)
    return match.group(1) if match else None


def get_goodwill_end_date(url: str, timeout: int = 10) -> tuple[datetime | None, str | None]:
    """
    Fetch the authoritative end time and listing title for a shopgoodwill item.
    Returns (end_time_utc, item_title).
    """
    item_id = extract_item_id(url)
    if not item_id:
        log.debug(f"[get_end_date] Could not extract item id from URL: {url}")
        return None, None

    api_url = API_URL_TEMPLATE.format(item_id=item_id)
    log.debug(f"[get_end_date] Querying {api_url}")

    _throttle_before_request()

    try:
        response = requests.get(api_url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as e:
        log.warning(f"[get_end_date] Network error fetching {api_url}: {e}")
        return None, None

    if response.status_code != 200:
        log.warning(f"[get_end_date] Unexpected status {response.status_code} for {api_url}")
        return None, None

    try:
        data = response.json()
    except ValueError as e:
        log.warning(f"[get_end_date] Could not decode JSON from {api_url}: {e}")
        return None, None

    # 1. Authoritative title extraction
    api_title = data.get("title") or data.get("itemTitle")
    if api_title:
        api_title = html.unescape(api_title).strip()

    # 2. Authoritative end date extraction
    raw_end_time = data.get("endTime")
    if not raw_end_time:
        log.warning(f"[get_end_date] No 'endTime' field in API response for {api_url}")
        return None, api_title

    try:
        parsed = date_parser.parse(raw_end_time)
    except (ValueError, TypeError, OverflowError) as e:
        log.warning(f"[get_end_date] Could not parse endTime '{raw_end_time}': {e}")
        return None, api_title

    # ShopGoodwill API timestamps represent Pacific Time (PT)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SG_TZ)
    else:
        parsed = parsed.astimezone(SG_TZ)

    # Convert to UTC for internal storage
    parsed_utc = parsed.astimezone(timezone.utc)

    log.debug(f"[get_end_date] Item {item_id}: Title='{api_title}' -> EndTime(UTC)={parsed_utc.isoformat()}")
    return parsed_utc, api_title


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [DEBUG] %(message)s", datefmt="%H:%M:%S")
    target_url = "https://shopgoodwill.com/item/274593484"
    end_date, title = get_goodwill_end_date(target_url)
    print(f"Title: {title}")
    print(f"Ends On: {end_date}")