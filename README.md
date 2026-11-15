# StockWatch

A background service for an always-on Mac. It watches Best Buy, Target, Walmart, GameStop
and the Nintendo Store for an item (first target: Nintendo Switch 2) at nearby stores and
online. When stock appears it:

1. Sends a loud, repeating Pushover alert that breaks through silent mode and Focus, plus a Telegram backup message.
2. Plays an alarm on the Mac and speaks the store name and price.
3. Tries to add the item to the cart in **your own** signed-in Chrome profile. It never checks out.
4. Follows up with "Added to cart: YES/NO" and a one-tap link to the cart or product page.

Everything runs locally. Nothing is exposed to the public internet. The full design is in
[`docs/spec/stock-watch-bot-spec.md`](docs/spec/stock-watch-bot-spec.md); module interfaces are in
[`docs/CONTRACTS.md`](docs/CONTRACTS.md). For a guided tour of how the app works, see
[`docs/OVERVIEW.md`](docs/OVERVIEW.md).

## Project status (v1 implementation)

Spec phases 1–4 are implemented. Phase 0, the live data-source checks, needs your real keys
and accounts; see the checklist below.

- **Built:** Best Buy (official API), Target (fulfillment JSON) and Walmart, GameStop and
  Nintendo (product page in the Chrome profile) adapters. Also the scheduler (jitter,
  per-retailer floors, backoff up to 30 min), the restock dedupe engine, Pushover, Telegram
  and Mac sound alerts, and the add-to-cart helper with the checkout denylist. Plus the
  Telegram commands, the PIN-protected dashboard on localhost and Tailscale only, launchd,
  the heartbeat, the daily summary, log rotation, the `doctor` checks and the CLI.
- **Tests:** 710 passing (`.venv/bin/pytest -q`), with no network access. They cover every
  adapter fixture, the dedupe state machine, the scheduler, the notifiers, the dashboard,
  Telegram, the CLI and end-to-end service flows. `tests/test_cart_safety.py` covers
  acceptance criterion 7.
- **Reviewed:** an independent review of spec compliance, alert correctness, async bugs,
  safety and security, and the adapters confirmed 21 defects. All are fixed, with regression
  tests in `tests/test_review_fixes.py`.
- **Smoke-tested:** `stockwatch run` started, served the dashboard login page and shut down
  cleanly on SIGTERM. `doctor` and `check --dry-run` reported bad credentials and network
  errors as FAIL rows, not crashes. No secrets appeared in logs or the database.
- **Still to do before relying on it:**
  1. Run Phase 0 against the live sites. Fixtures are synthetic, and page selectors and the
     unofficial Target endpoint are not yet verified.
  2. Fill in the open decisions: ZIP, SKUs and max price. The example watch stays disabled
     until you do.
  3. Run `scripts/install.sh` and `stockwatch install`, which were not run during
     development.
- **Known quirks:** add-to-cart uses the pickup store saved in your retailer account, not
  the store picker. An invalid Best Buy API key shows as "blocked", because Best Buy
  returns 403. `run` warns about missing secrets even for channels that are turned off.

## Honest limitations

- **A cart does not reserve inventory** at Target, Walmart or Best Buy. The item is only held
  once an order is placed. The cart saves you the search and the option clicks; you still have
  to check out fast on your phone.
- For one-tap phone checkout, sign in to each retailer in **iPhone Safari** (or the retailer's
  app) with a payment method saved.
- A sleeping Mac checks nothing. See "Keep the Mac awake" below.
- Retailer page selectors and the unofficial Target/Walmart/GameStop/Nintendo data sources were
  built from synthetic fixtures. They must be confirmed against the live sites (Phase 0, below).
  If a page changes shape, the adapter fails loud ("Target adapter broken") rather than
  silently reporting out of stock.

## What it will not do (non-goals)

- No automatic purchase or checkout. Every click goes through a denylist
  (`stockwatch/cart/safety.py`) that refuses anything labelled checkout, place order, buy now,
  pay, PayPal and similar; the test suite proves it.
- No CAPTCHA solving, fingerprint or user-agent spoofing, proxies, or any other way around bot
  protection. On HTTP 403/429 or a challenge page the target is paused with backoff (doubling,
  capped at 30 minutes) and you get one "paused, check manually" notice. It never retries
  immediately.
- No native iOS app, no multi-user support.

## Setup

Requirements: macOS, Python 3.12+, Google Chrome (the real one; no bundled browser is downloaded).

```bash
git clone <this repo> && cd stock-watch-bot
scripts/install.sh          # venv + package, ~/StockWatch, config + .env templates, launchd agent
```

Or by hand: `uv venv && uv pip install -e '.[dev]'`, then `stockwatch init`.

Then work through these steps.

### 1. Pushover (primary alarm)

1. Install Pushover on the iPhone and create an account (free trial, then a one-time purchase).
2. Your **user key** is on the pushover.net dashboard. Create an **application** to get an **API token**.
3. In the iPhone app: Settings > **Critical Alerts** > turn on for emergency priority, and set the
   volume. Without this the alarm will not sound on mute or in Focus.
4. Put `PUSHOVER_USER_KEY` and `PUSHOVER_APP_TOKEN` in `~/StockWatch/.env`.

### 2. Telegram bot (backup alerts and remote control)

1. Message `@BotFather`, send `/newbot`, and copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<token>/getUpdates` and copy `message.chat.id` into `TELEGRAM_CHAT_ID`.
3. Only that chat is answered. Commands: `/status`, `/pause <id>`, `/resume <id>`, `/pauseall`,
   `/resumeall`, `/check <id>`, `/add <product url>`, `/test`, `/help`.

### 3. Best Buy API key

Create a free key at <https://developer.bestbuy.com> and set `BESTBUY_API_KEY`. Target uses the
public key its own website uses (built in; override with `retailers.target.api_key` if it rotates).

### 4. Tailscale (dashboard on the phone)

1. Install Tailscale on the Mac and iPhone (free personal plan) and sign in to both.
2. Set `DASHBOARD_PIN` (6+ characters). Without a PIN the dashboard only listens on 127.0.0.1.
3. On the phone, open `http://<mac-name>:8787` in Safari, enter the PIN, and use Share >
   **Add to Home Screen**. The dashboard binds to 127.0.0.1 and the Mac's Tailscale address
   only, never 0.0.0.0.

### 5. Keep the Mac awake

System Settings > Battery (or Energy) > **Prevent automatic sleeping when the display is off**
(on power adapter), and turn off "Put hard disks to sleep". The service also holds a
`caffeinate -i` assertion while running, but that does not stop sleep from a closed lid or a
manual sleep.

### 6. Heartbeat (dead man's switch)

Create a free check at <https://healthchecks.io> with a 5-minute period and a 10-minute grace
time, connect it to the Pushover integration or the iOS app, and put its ping URL in
`HEARTBEAT_URL`. If the Mac dies, healthchecks.io alerts your phone within about 15 minutes.

### 7. Sign in to the bot's Chrome profile

```bash
stockwatch setup-profile        # or: dashboard > Settings > Open sign-in browser
```

This opens the dedicated profile at `~/StockWatch/chrome-profile` (chmod 700) on each retailer's
sign-in page. Sign in by hand, including any 2FA, then press Enter. The bot never sees or stores
passwords. Repeat whenever you get a "Sign in needed for <retailer>" warning.

### 8. Configure watches and verify

Edit `~/StockWatch/config.yaml`: set `home.zip`, `radius_miles`, each target's identifier, and set
the watch to `enabled: true`. Or paste product URLs in the dashboard (Add watch) or send
`/add <url>` to the bot.

```bash
stockwatch doctor        # PASS/WARN/FAIL table; exit code 1 on any FAIL
stockwatch test-alert    # fires through every channel; acknowledge it on the phone
stockwatch install       # (re)load the launchd agent; it restarts within ~10 s if killed
```

Secrets are `${VAR}` references resolved from the environment, then `~/StockWatch/.env`
(chmod 600), then the macOS Keychain:
`security add-generic-password -s stockwatch -a PUSHOVER_USER_KEY -w '<value>'`.
They are redacted from logs.

## CLI

```
stockwatch run                              run the service (what launchd runs)
stockwatch check <watch_id>                 check now; records history and sends alerts like a scheduled check
stockwatch check <watch_id> --dry-run       print StockResult JSON per target; no DB writes, alerts or cart
stockwatch check --url <product url>        dry-check one pasted URL
stockwatch doctor [--offline] [--no-browser] [--no-checks]
stockwatch test-alert
stockwatch init                             create ~/StockWatch/config.yaml and .env if missing
stockwatch setup-profile [retailer ...]
stockwatch install | uninstall              launchd agent com.local.stockwatch
stockwatch status                           last check per target and open events, from the DB
```

`--config PATH` (or `$STOCKWATCH_CONFIG`) selects another config; `$STOCKWATCH_HOME` moves
`~/StockWatch`. Logs are in `~/StockWatch/logs/stockwatch.log` (rotated daily, kept 14 days).

## How alerts behave

| Situation | Alert |
|---|---|
| In stock within radius, under max price | **critical**: Pushover emergency (repeats until acknowledged), Telegram, Mac alarm and voice. Then the cart attempt, then a follow-up "Added to cart: YES/NO" |
| Same restock, new store or lower price | info |
| In stock but over max price, or online-only (ship-only) stock when a local channel is listed ahead of `ship` in `fulfillment` (spec 7) | info |
| 403 / 429 / challenge page | one low-priority "paused, check manually" notice per episode; backoff doubles up to 30 min |
| Page or API shape changed | warning "<Retailer> adapter broken" (once) |
| 5 failed checks in a row | warning (once) |
| Nintendo queue or invite-only sale | info, once per episode |
| Session expired during add-to-cart | warning "Sign in needed for <Retailer>" with the product link |

One critical alert per restock event. An event closes after two out-of-stock checks in a row.
Quiet hours (`defaults.quiet_hours`) soften only non-critical alerts.

## Phase 0: validate the data sources (manual, needs real keys)

Do this once before relying on the bot, and again whenever an adapter reports "broken".
Record a short note per retailer: **works / lags / blocked**.

1. **Best Buy**: `stockwatch check --url "https://www.bestbuy.com/site/<...>/<sku>.p?skuId=<sku>"`.
   Confirm nearby stores, distances and `low_stock` look right. During a real restock compare the
   API result with the website; if the API lags, note it (fallback to page checks is future work).
2. **Target**: dry-check a TCIN URL. Confirm the nearby stores and per-store status. If it fails
   with "Target adapter broken", the redsky parameters or the public key changed.
3. **Walmart, GameStop, Nintendo**: after `stockwatch setup-profile`, dry-check one product URL
   each. If you get `blocked`, stop and try later; do not hammer.
4. **Add-to-cart selectors**: for each retailer, run `stockwatch check <id>` on an in-stock,
   inexpensive item with `add_to_cart: true` and `browser.headless: false`, and watch the
   Chrome window. Confirm it picks the fulfillment option, adds to cart, and stops. Remove the
   item from the cart afterwards.
5. **Pushover**: `stockwatch test-alert` with the phone on mute and a Focus mode on. Confirm the
   critical alert sounds and repeats until acknowledged, and that the link opens.
6. **Heartbeat**: stop the service (`stockwatch uninstall`) and confirm healthchecks.io alerts you.

Selectors and fixtures to update live in `stockwatch/adapters/<retailer>.py` and
`tests/fixtures/<retailer>/`.

## Open decisions (spec section 14)

Defaults are in `config.example.yaml`; change them there.

1. Home ZIP and search radius: placeholders `00000` / 25 mi.
2. Exact item(s) and SKUs (base vs bundle, editions): placeholders; the example watch is disabled until filled in.
3. Max price per item: example 499.99.
4. Should critical alerts ever be suppressed overnight? Default **never** (`quiet_hours: null`; quiet hours never affect critical alerts).
5. Should Nintendo (online-only) stock be critical or info? Default **critical** for Nintendo only (`retailers.nintendo.online_severity`). Other retailers follow spec 7: ship-only stock is info when you prefer local pickup; override per watch with `online_only_severity`.

## Development

```bash
.venv/bin/pytest -q
```

Tests never touch the network (respx and fakes). Each adapter has fixtures in
`tests/fixtures/<retailer>/`. `tests/test_cart_safety.py` covers acceptance criterion 7,
including a static scan that fails if any module clicks outside `safe_click`.
