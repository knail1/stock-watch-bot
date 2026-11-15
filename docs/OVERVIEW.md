# StockWatch: how the app works

StockWatch is a background service for an always-on Mac. It watches retailers for an item
(first target: Nintendo Switch 2) at stores near your ZIP and online. The moment stock
appears it gets your attention and gets you as close to checkout as it safely can:

1. **Loud phone alarm.** A Pushover emergency alert repeats until you acknowledge it and
   uses iOS Critical Alerts, so it sounds even on silent or in a Focus mode. Telegram gets
   the same alert as a backup.
2. **Mac alarm.** A siren plays and macOS `say` reads out the store and price.
3. **Pre-loaded cart.** In a dedicated Chrome profile where you are signed in, it opens the
   product, picks pickup or shipping, and clicks *Add to cart*. It **never** checks out.
4. **One-tap link.** A follow-up message says "Added to cart: YES/NO" and links to the cart
   or product page.

Everything runs on your Mac. There is no cloud server and nothing on the public internet.
You manage it from a phone-friendly dashboard over Tailscale, or with Telegram commands.

The spec is [`spec/stock-watch-bot-spec.md`](spec/stock-watch-bot-spec.md). Module
interfaces are in [`CONTRACTS.md`](CONTRACTS.md). Setup steps are in the
[README](../README.md).

---

## How a check flows through the system

```
config.yaml ─► Scheduler ─► Retailer adapter ─► StockResult ─► State engine ─► Decisions
                  ▲          (httpx / Chrome)                   (SQLite)          │
                  │                                                               ▼
  Dashboard / Telegram ◄──────────── Service ◄─────────────────────────────── Notifier
                                       │                       (Pushover, Telegram, Mac sound)
                                       └─► Cart helper (after the alert, never before)
```

1. **Scheduler** (`engine/scheduler.py`) runs each target on its interval, randomised by
   ±20%, and never faster than the retailer's minimum. Requests to the same retailer are
   spaced out. If a retailer blocks the bot, that target's interval doubles each time, up to
   30 minutes, and resets after a good check.
2. **Adapter** (`adapters/<retailer>.py`) fetches the data and parses it into a standard
   `StockResult`: in stock or not, price, and per-channel availability (store pickup,
   in store, ship) with store name, distance and low-stock flags.
3. **State engine** (`engine/state.py`) compares the result with history in SQLite and
   decides whether anything is worth telling you (the dedupe rules are below).
4. **Service** (`service.py`) acts on each decision. It sends the alert first. For a
   critical restock it then starts the cart attempt in the background, with a 25-second
   timeout, and sends a follow-up with the result.

## Retailers

| Retailer | Data source | Local stock | Min interval |
|---|---|---|---|
| Best Buy | Official Products API (free key) | Per store, with distance and low stock | 60 s |
| Target | The fulfillment JSON Target's own site uses; product page as fallback | Per store | 90 s |
| Walmart | Product page in the Chrome profile | Your selected store only | 180 s |
| GameStop | Product page in the Chrome profile | Your set store only | 120 s |
| Nintendo Store | Product page in the Chrome profile | Online only; detects queue and invite-only sales | 120 s |

**Best Buy API terms.** The Best Buy adapter enforces the API's terms itself: at most
4 calls/s, a rolling daily budget under the 50,000/day limit, Best Buy content deleted
within 72 h, game/music/movie titles refused, and Best Buy's logo and attribution on the
dashboard. See [`policies/bestbuy-compliance.md`](policies/bestbuy-compliance.md).

Every adapter separates *fetching* from *parsing*. Parsing is a pure function tested
against saved fixtures in `tests/fixtures/<retailer>/`. If a page or API changes shape, the
adapter raises an error and you get "<Retailer> adapter broken". It never quietly reports
"out of stock".

To add a retailer (Amazon, Costco…), add one adapter file and one line in
`adapters/registry.py`.

## Alerts and dedupe

| Situation | Severity | What you get |
|---|---|---|
| In stock within radius, under max price | critical | Repeating Pushover emergency alert, Telegram, Mac alarm and voice, then a cart attempt and its follow-up |
| Same restock, but a new store or lower price | critical | Another alert |
| Over max price, or ship-only stock when you prefer local | info | Normal-priority push |
| 403 / 429 / CAPTCHA page | low | One "paused, check manually" notice per episode, with backoff |
| Page or API shape changed | warning | "<Retailer> adapter broken", once |
| 5 failed checks in a row | warning | Once |
| Nintendo queue or invite-only sale | info | Once per episode |
| Signed out during add-to-cart | warning | "Sign in needed for <Retailer>" plus the product link |

- A **restock event** opens on the first in-stock check after an out-of-stock one. It
  closes after two out-of-stock checks in a row. You get one critical alert per event.
- If a critical alert fails to deliver, the next in-stock check sends it again, up to 3
  times.
- Quiet hours only soften non-critical alerts. Critical alerts always sound.

Example alert:

```
IN STOCK: Nintendo Switch 2 @ Best Buy
Pickup at Best Buy Frisco (6.2 mi), $449.99, low stock. Added to cart: trying…
```

## Safety rules (built in, not configurable)

- **Never purchases.** Every click in a retailer page goes through `cart/safety.py`. It
  reads the element's text, aria-label, id, class and href, plus those of the button or
  link around it, and refuses anything that looks like checkout, place order, buy now, pay,
  PayPal, Apple/Google Pay or pay-later wallets. It also refuses any element whose label it
  can't read. A test scans the codebase and fails if anything clicks without going through
  this check.
- **No bot-protection evasion.** No CAPTCHA solving, user-agent or fingerprint spoofing, or
  proxies. Chrome runs as itself. A block pauses the target and backs off; it never retries
  right away.
- **No stored passwords.** You sign in to each retailer yourself, once, in the dedicated
  Chrome profile.
- **Private dashboard.** It listens only on `127.0.0.1` and your Tailscale address, and
  refuses `0.0.0.0` or LAN addresses. It needs a PIN, uses CSRF tokens and rate-limits
  login attempts.
- **Secrets stay out of files and logs.** They live in `.env` (chmod 600) or the macOS
  Keychain and are referenced as `${VAR}` in config. Log output is filtered to redact them.
  Saving config never writes resolved secrets back to disk, and watch fields may not
  contain `${…}` references.
- **Telegram** only answers the configured chat ID.

## Running reliably

- **launchd** agent `com.local.stockwatch` starts at login and restarts within about
  10 seconds of a crash.
- **caffeinate** keeps the Mac from idle-sleeping while the service runs.
- **Heartbeat**: every 5 minutes it pings healthchecks.io. If the Mac dies, healthchecks.io
  alerts your phone.
- **Daily summary** at 09:00 on Telegram: checks run, errors, active watches and restocks.
- **Logs** rotate daily in `~/StockWatch/logs/` and are kept for 14 days. Check history is
  kept for 14 days.

## Remote control

**Dashboard** (`http://<mac-name>:8787` over Tailscale; add it to the Home Screen):
- *Watches*: status dot, last check and last in stock for each watch.
- *Watch detail*: recent results and a *Check now* button.
- *Add watch*: paste product URLs.
- *Events*: restocks, alerts and cart results.
- *Settings*: test alerts, ZIP, quiet hours and per-retailer sign-in status.

**Telegram:**

| Command | What it does |
|---|---|
| `/status` | Summary of all watches |
| `/pause <id>`, `/resume <id>` | Pause or resume one watch |
| `/pauseall`, `/resumeall` | Pause or resume everything |
| `/check <id>` | Check a watch now |
| `/add <url>` | Create a watch from a product URL |
| `/test` | Fire a test alert on every channel |
| `/help` | List commands |

## Files on disk

| Path | What it is |
|---|---|
| `~/StockWatch/config.yaml` | Source of truth; dashboard edits are written back here |
| `~/StockWatch/.env` | Secrets (chmod 600) |
| `~/StockWatch/stockwatch.db` | SQLite: watches, targets, checks, events, alerts |
| `~/StockWatch/chrome-profile/` | Dedicated signed-in Chrome profile (chmod 700) |
| `~/StockWatch/logs/` | Rotating logs |
| `~/Library/LaunchAgents/com.local.stockwatch.plist` | launchd agent |

## Code map

| Module | Responsibility |
|---|---|
| `models.py` | `StockResult`, `ChannelAvail`, `CartResult`, `Alert`, severities |
| `config.py`, `paths.py` | YAML config, `${VAR}` secret resolution (env → `.env` → Keychain), safe write-back |
| `db.py` | SQLite schema and queries |
| `adapters/` | One adapter per retailer, plus the base protocol and URL parsing |
| `engine/scheduler.py` | Intervals, jitter, per-retailer spacing, backoff, pause/resume, check now |
| `engine/state.py` | Restock, blocked and error events; deciding what to alert |
| `notify/` | Pushover, Telegram, Mac sound, fan-out and message formatting |
| `cart/safety.py` | Checkout denylist and `safe_click` |
| `cart/helper.py` | Cart attempt with a hard timeout and per-retailer lock |
| `browser.py` | Playwright on real Chrome with the persistent profile; sign-in status |
| `service.py` | Wires all components together |
| `telegram_bot.py` | Telegram command channel |
| `dashboard/` | FastAPI app, PIN auth, Tailscale-only binding, mobile templates |
| `health.py`, `launchd.py`, `logging_setup.py` | Heartbeat, daily summary, sleep prevention, launchd, logs |
| `cli.py` | `stockwatch run / check / doctor / test-alert / init / setup-profile / install / uninstall / status` |

## Known limitations

- **A cart does not reserve inventory.** You still have to check out fast on your phone.
  For a one-tap checkout, sign in to each retailer in iPhone Safari with a saved payment
  method.
- **Pickup store.** Add-to-cart uses the pickup store saved in your retailer account; it
  does not drive the store picker. The follow-up message warns you when the matched store
  may differ.
- **Unverified against live sites.** Fixtures are synthetic, modelled on the known payload
  shapes. Page selectors and the unofficial Target endpoint must be confirmed in Phase 0
  (see the README).
- **Best Buy API lag.** Store stock from the API may lag the website. Phase 0 should
  compare the two during a real restock.
- **Bad Best Buy key.** An invalid API key returns HTTP 403, so it shows up as "blocked"
  rather than as a config error.
