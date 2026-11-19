import os
from unittest.mock import patch

os.environ.setdefault("IMAP_HOST", "example.invalid")
os.environ.setdefault("IMAP_USERNAME", "test")
os.environ.setdefault("IMAP_PASSWORD", "test")
os.environ.setdefault("SMTP_HOST", "example.invalid")
os.environ.setdefault("ALERT_FROM_EMAIL", "alerts@example.com")

from bot import (
    build_alert_html,
    contains_search_term,
    extract_item_urls,
    parse_end_time,
    resolve_auction_details,
)
from get_end_date import _extract_first_image_url

SAMPLE = """
Vintage Sterling Silver Heart Charm
Auction Ends: July 25, 2026 at 07:45 PM PT
https://shopgoodwill.com/item/123456789
"""

# 1. Existing email parsing assertions
assert contains_search_term(SAMPLE)
assert extract_item_urls(SAMPLE) == ["https://shopgoodwill.com/item/123456789"]
parsed = parse_end_time(SAMPLE)
assert parsed is not None
assert parsed.year == 2026
assert parsed.month == 7
assert parsed.day == 25
assert parsed.hour == 19

# 2. Image URL extraction from API payload variations
assert (
    _extract_first_image_url({"imageUrl": "//cdn.shopgoodwill.com/img1.jpg"})
    == "https://cdn.shopgoodwill.com/img1.jpg"
)
assert (
    _extract_first_image_url(
        {
            "imageServer": "https://cdn.shopgoodwill.com/",
            "imageUrlString": "items/first.jpg;items/second.jpg",
        }
    )
    == "https://cdn.shopgoodwill.com/items/first.jpg"
)
assert (
    _extract_first_image_url(
        {"itemImages": [{"url": "https://cdn.shopgoodwill.com/arr1.jpg"}]}
    )
    == "https://cdn.shopgoodwill.com/arr1.jpg"
)
assert _extract_first_image_url({}) is None

# 3. 3-tuple return from resolve_auction_details & HTML email embedding
mock_img = "https://cdn.shopgoodwill.com/items/charm.jpg"
with patch(
    "bot.get_goodwill_end_date",
    return_value=(parsed, "API Vintage Sterling Silver Heart Charm", mock_img),
):
    end_dt, title, img_url = resolve_auction_details(
        "https://shopgoodwill.com/item/123456789",
        parsed,
        "Fallback Title",
    )
    assert end_dt == parsed
    assert title == "API Vintage Sterling Silver Heart Charm"
    assert img_url == mock_img

html_with_img = build_alert_html(
    title=title,
    item_url="https://shopgoodwill.com/item/123456789",
    local_end_str="July 25, 2026 at 07:45 PM PDT",
    rounded_minutes=45,
    image_url=img_url,
)
assert f'<img src="{mock_img}"' in html_with_img

html_without_img = build_alert_html(
    title=title,
    item_url="https://shopgoodwill.com/item/123456789",
    local_end_str="July 25, 2026 at 07:45 PM PDT",
    rounded_minutes=45,
    image_url=None,
)
assert "<img " not in html_without_img

print("Parser & image alert tests passed:", parsed.isoformat())