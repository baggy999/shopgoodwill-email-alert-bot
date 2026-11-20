"""
Looks up the authoritative auction end time, title, first image URL,
and current bid price for a shopgoodwill item directly from shopgoodwill's own buyer API.
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


def _normalize_image_url(raw_url: str | None, server_prefix: str = "") -> str | None:
    if not raw_url or not isinstance(raw_url, str):
        return None
    cleaned = raw_url.strip().replace("\\", "/")
    if not cleaned:
        return None
    if cleaned.startswith("//"):
        return f"https:{cleaned}"
    if cleaned.startswith(("http://", "https://")):
        return cleaned
    if server_prefix:
        prefix = server_prefix.strip().rstrip("/")
        if prefix.startswith("//"):
            prefix = f"https:{prefix}"
        return f"{prefix}/{cleaned.lstrip('/')}"
    return None


def _extract_first_image_url(data: dict) -> str | None:
    """
    Extracts the first listing image URL from ShopGoodwill's ItemDetail JSON payload.
    Handles direct fields, itemImages arrays, and semicolon-delimited imageUrlString.
    """
    server_prefix = data.get("imageServer") or ""

    # 1. Direct primary image field
    direct = data.get("imageUrl") or data.get("imageURL")
    normalized = _normalize_image_url(direct, server_prefix)
    if normalized:
        return normalized

    # 2. List of image objects or URLs (e.g., itemImages / images)
    images = data.get("itemImages") or data.get("images") or []
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            candidate = (
                first.get("url")
                or first.get("imageUrl")
                or first.get("imageURL")
                or first.get("fullImageUrl")
            )
            normalized = _normalize_image_url(candidate, server_prefix)
            if normalized:
                return normalized
        elif isinstance(first, str):
            normalized = _normalize_image_url(first, server_prefix)
            if normalized:
                return normalized

    # 3. Semicolon-separated imageUrlString (common in ItemDetailModel)
    url_string = data.get("imageUrlString")
    if isinstance(url_string, str) and url_string.strip():
        first_part = url_string.split(";")[0].strip()
        normalized = _normalize_image_url(first_part, server_prefix)
        if normalized:
            return normalized

    return None


def _extract_current_price(data: dict) -> str | None:
    """
    Extracts and formats the current bid price from ShopGoodwill's ItemDetail JSON payload.
    """
    for key in ("currentPrice", "CurrentPrice", "minimumBid", "startingPrice"):
        val = data.get(key)
        if val is not None and val != "":
            try:
                amount = float(str(val).replace("$", "").replace(",", "").strip())
                return f"${amount:,.2f}"
            except (ValueError, TypeError):
                continue
    return None


def get_goodwill_end_date(
    url: str, timeout: int = 10
) -> tuple[datetime | None, str | None, str | None, str | None]:
    """
    Fetch the authoritative end time, listing title, first image URL, and current bid price
    for a shopgoodwill item.
    Returns (end_time_utc, item_title, first_image_url, current_price).
    """
    item_id = extract_item_id(url)
    if not item_id:
        log.debug(f"[get_end_date] Could not extract item id from URL: {url}")
        return None, None, None, None

    api_url = API_URL_TEMPLATE.format(item_id=item_id)
    log.debug(f"[get_end_date] Querying {api_url}")

    _throttle_before_request()

    try:
        response = requests.get(api_url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as e:
        log.warning(f"[get_end_date] Network error fetching {api_url}: {e}")
        return None, None, None, None

    if response.status_code != 200:
        log.warning(f"[get_end_date] Unexpected status {response.status_code} for {api_url}")
        return None, None, None, None

    try:
        data = response.json()
    except ValueError as e:
        log.warning(f"[get_end_date] Could not decode JSON from {api_url}: {e}")
        return None, None, None, None

    # 1. Authoritative title extraction
    api_title = data.get("title") or data.get("itemTitle")
    if api_title:
        api_title = html.unescape(api_title).strip()

    # 2. First listing image & current bid price extraction
    first_image_url = _extract_first_image_url(data)
    current_price = _extract_current_price(data)

    # 3. Authoritative end date extraction
    raw_end_time = data.get("endTime")
    if not raw_end_time:
        log.warning(f"[get_end_date] No 'endTime' field in API response for {api_url}")
        return None, api_title, first_image_url, current_price

    try:
        parsed = date_parser.parse(raw_end_time)
    except (ValueError, TypeError, OverflowError) as e:
        log.warning(f"[get_end_date] Could not parse endTime '{raw_end_time}': {e}")
        return None, api_title, first_image_url, current_price

    # ShopGoodwill API timestamps represent Pacific Time (PT)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SG_TZ)
    else:
        parsed = parsed.astimezone(SG_TZ)

    # Convert to UTC for internal storage
    parsed_utc = parsed.astimezone(timezone.utc)

    log.debug(
        f"[get_end_date] Item {item_id}: Title='{api_title}', Price={current_price} -> "
        f"EndTime(UTC)={parsed_utc.isoformat()}, Image={first_image_url}"
    )
    return parsed_utc, api_title, first_image_url, current_price


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [DEBUG] %(message)s", datefmt="%H:%M:%S")
    target_url = "https://shopgoodwill.com/item/274593484"
    end_date, title, img_url, price = get_goodwill_end_date(target_url)
    print(f"Title: {title}")
    print(f"Current Bid: {price}")
    print(f"Ends On: {end_date}")
    print(f"First Image: {img_url}")