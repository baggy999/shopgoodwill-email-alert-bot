"""
Looks up the authoritative auction end time for a shopgoodwill item
directly from shopgoodwill's own buyer API, instead of relying on
regex-parsing dates out of notification emails.
"""

import re
import random
import time
import logging
from datetime import datetime, timezone

import requests
from dateutil import parser as date_parser

log = logging.getLogger("shopgoodwill-alert-bot")

API_URL_TEMPLATE = "https://buyerapi.shopgoodwill.com/api/ItemDetail/GetItemDetailModelByItemId/{item_id}"

# Random delay (seconds) inserted before each outgoing API request, to avoid
# hammering shopgoodwill with back-to-back requests when resolving several
# item URLs in a row.
REQUEST_DELAY_MIN_SECONDS = 10
REQUEST_DELAY_MAX_SECONDS = 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

ITEM_ID_RE = re.compile(r"/item/(\d+)")

# Timestamp (time.monotonic()) of the last outgoing API request. None until
# the first request has been made, so that first request goes out immediately.
_last_request_at: float | None = None


def _throttle_before_request() -> None:
    """
    Sleeps a random amount of time before the next API request, so
    consecutive lookups (e.g. several item URLs in one email) aren't fired
    back-to-back. The very first request in a run is not delayed.
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


def get_goodwill_end_date(url: str, timeout: int = 10) -> datetime | None:
    """
    Fetch the authoritative end time for a shopgoodwill item.

    Returns a timezone-aware datetime in UTC, or None if it could not be
    determined (bad URL, network error, non-200 response, missing/unparseable
    'endTime' field).
    """
    item_id = extract_item_id(url)
    if not item_id:
        log.debug(f"[get_end_date] Could not extract item id from URL: {url}")
        return None

    api_url = API_URL_TEMPLATE.format(item_id=item_id)
    log.debug(f"[get_end_date] Querying {api_url}")

    _throttle_before_request()

    try:
        response = requests.get(api_url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as e:
        log.warning(f"[get_end_date] Network error fetching {api_url}: {e}")
        return None

    if response.status_code != 200:
        log.warning(f"[get_end_date] Unexpected status {response.status_code} for {api_url}")
        return None

    try:
        data = response.json()
    except ValueError as e:
        log.warning(f"[get_end_date] Could not decode JSON from {api_url}: {e}")
        return None

    raw_end_time = data.get("endTime")
    if not raw_end_time:
        log.warning(f"[get_end_date] No 'endTime' field in API response for {api_url}")
        return None

    try:
        parsed = date_parser.parse(raw_end_time)
    except (ValueError, TypeError, OverflowError) as e:
        log.warning(f"[get_end_date] Could not parse endTime '{raw_end_time}': {e}")
        return None

    # shopgoodwill's API returns endTime as a UTC timestamp with no offset
    # marker; treat naive values as UTC, and normalize anything else to UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    log.debug(f"[get_end_date] Resolved end time for item {item_id}: {parsed.isoformat()}")
    return parsed


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [DEBUG] %(message)s", datefmt="%H:%M:%S")
    target_url = "https://shopgoodwill.com/item/274593484"
    end_date = get_goodwill_end_date(target_url)
    print(f"Ends On: {end_date}")
