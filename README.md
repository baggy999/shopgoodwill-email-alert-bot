# ShopGoodwill Charm Email Alert Bot

This Coolify worker **does not scrape or log into ShopGoodwill**. It reads official
ShopGoodwill Personal Shopper emails from your own IMAP mailbox, extracts item
links and an auction end time, stores the listing in SQLite, and emails
`admin@planenews.net` when the item has 60 minutes or less remaining.

## Why this mode

ShopGoodwill's Terms of Use prohibit unauthorized bots, scrapers, scripts, and
third-party applications that access the site. The supported input for this
project is therefore the notification email sent to you by ShopGoodwill.

## 1. Configure ShopGoodwill

1. Sign in to ShopGoodwill.
2. Open **My ShopGoodwill → Personal Shopper**.
3. Create a search for `charm`.
4. Confirm the Personal Shopper messages arrive in an IMAP-accessible mailbox.
5. Inspect one real message and confirm it contains:
   - a ShopGoodwill item URL;
   - the auction end date and time.

The bot never needs your ShopGoodwill username or password.

## 2. Local parser test

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python test_parser.py
```

If a real email formats its end date differently, copy a redacted example into
`sample_shopgoodwill_email.txt` and adjust `DATE_FRAGMENT_RE` in `bot.py`.

## 3. Deploy with Coolify

1. Put this directory in a new private GitHub repository.
2. In Coolify, choose **New Resource → Application → Public/Private Repository**.
3. Select the repository and use the included `Dockerfile`.
4. Add a persistent storage mount:
   - destination: `/data`
5. Add the environment variables from `.env.example`.
6. Set container port to `8080`.
7. Use `/health` for the health check.
8. Deploy. A public domain is not required; the service only needs outbound
   access to IMAP and SMTP.

## 4. Required environment variables

| Variable | Purpose |
|---|---|
| `IMAP_HOST` | Incoming mail server |
| `IMAP_USERNAME` | Mailbox login |
| `IMAP_PASSWORD` | Mailbox password or app password |
| `SMTP_HOST` | Outgoing SMTP server |
| `SMTP_USERNAME` | SMTP login |
| `SMTP_PASSWORD` | SMTP password |
| `ALERT_FROM_EMAIL` | Sender address |
| `ALERT_TO_EMAIL` | Destination; defaults to `admin@planenews.net` |

For SMTP2GO, the usual values are:

```text
SMTP_HOST=mail.smtp2go.com
SMTP_PORT=2525
SMTP_USE_TLS=true
SMTP_USE_SSL=false
```

## 5. Useful Coolify settings

- Restart policy: `unless-stopped`
- Health endpoint: `/health`
- Health port: `8080`
- Persistent directory: `/data`
- Poll interval: `POLL_SECONDS=120`
- Auction timezone: `America/Los_Angeles`
- Alert window: `ALERT_MINUTES_BEFORE=60`

## 6. Logs and behavior

Expected log lines:

```text
Queued https://shopgoodwill.com/item/123456789 ending at ...
Sent due alert for https://shopgoodwill.com/item/123456789
```

The SQLite database prevents duplicate alerts. Already-ended auctions are
silently marked complete.

## Limitation

The bot can schedule a one-hour warning only when the official notification
email contains a recognizable auction end date/time. ShopGoodwill documents
item-ending-soon alerts as mobile notifications for Favorite items, not as
email alerts. If Personal Shopper messages omit the end time, request an
authorized API or written automation permission from ShopGoodwill before
adding direct site access.
