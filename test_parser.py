import os
os.environ.setdefault("IMAP_HOST", "example.invalid")
os.environ.setdefault("IMAP_USERNAME", "test")
os.environ.setdefault("IMAP_PASSWORD", "test")
os.environ.setdefault("SMTP_HOST", "example.invalid")
os.environ.setdefault("ALERT_FROM_EMAIL", "alerts@example.com")

from bot import contains_search_term, extract_item_urls, parse_end_time

SAMPLE = """
Vintage Sterling Silver Heart Charm
Auction Ends: July 25, 2026 at 07:45 PM PT
https://shopgoodwill.com/item/123456789
"""

assert contains_search_term(SAMPLE)
assert extract_item_urls(SAMPLE) == ["https://shopgoodwill.com/item/123456789"]
parsed = parse_end_time(SAMPLE)
assert parsed is not None
assert parsed.year == 2026
assert parsed.month == 7
assert parsed.day == 25
assert parsed.hour == 19

print("Parser test passed:", parsed.isoformat())
