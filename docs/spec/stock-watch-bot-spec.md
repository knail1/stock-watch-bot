# StockWatch: Local Stock Tracker and Alert Bot (Spec v1)

Target builder: Claude Code
Status: Ready to build (decisions marked OPEN need a quick answer before Phase 2)

---

## 1. Goal

A background service on the user's Mac that watches retailers for a configurable item (first target: Nintendo Switch) at nearby stores and online. When stock appears it:

1. Fires a loud, repeating alert on the iPhone that breaks through silent mode and Focus.
2. Plays a loud sound on the Mac.
3. Tries to add the item to the cart in the user's own signed-in account (never purchases).
4. Includes a one-tap link in the alert that opens the product or cart page so the user can reserve or buy in seconds.

Everything runs locally. No public website, no cloud server owned by the user.

## 2. Non-goals (v1)

- No automatic purchase or checkout submission.
- No CAPTCHA solving, browser fingerprint spoofing, proxy rotation, or any technique for getting around a retailer's bot protection. If a retailer blocks or challenges the bot, it backs off and alerts the user instead. Reason: these violate retailer terms, get accounts banned (losing the account is worse than missing one drop), and add fragile maintenance. The edge this bot gives is speed of notice plus a pre-loaded cart, not out-scripting checkout bots.
- No native iOS app, no App Store, no Apple developer account.
- No multi-user support.

## 3. Platform decisions

| Area | Decision | Why |
|---|---|---|
| Host | macOS, always-on Mac | User's choice |
| Language | Python 3.12+, asyncio | Mature HTTP and browser automation libraries |
| HTTP | httpx (async) | For retailers with JSON endpoints |
| Browser | Playwright driving a dedicated Chrome profile (headed or headless toggle) | For page checks and add-to-cart in the user's signed-in session |
| Storage | SQLite (single file) | Watch history, events, dedupe |
| Config | YAML file + local dashboard edits | Human-editable, diffable |
| Dashboard | FastAPI + simple HTML (HTMX or vanilla JS), bound to localhost and Tailscale only | Manage watches from Mac or phone |
| Service | launchd LaunchAgent with KeepAlive | Starts at login, restarts on crash |
| Phone alerts | Pushover (primary), Telegram bot (secondary, also used for remote commands) | See section 7 |
| Phone access to dashboard | Tailscale (private network between Mac and iPhone) | Dashboard reachable from phone without being on the public internet |

## 4. Architecture

```
                 ┌───────────────────────── Mac (launchd) ─────────────────────────┐
                 │                                                                  │
 config.yaml ───►│  Scheduler ──► Watch jobs ──► Retailer adapters ──► Normalizer   │
                 │      ▲                              │                   │        │
                 │      │                       (httpx / Playwright)        ▼        │
                 │  Dashboard (FastAPI) ◄──── SQLite ◄──── State + dedupe engine    │
                 │      ▲                                              │            │
                 │      │                                   ┌──────────┴─────────┐  │
                 │      │                                   ▼                    ▼  │
                 │      │                             Cart helper          Notifier  │
                 │      │                          (Playwright, own      (Pushover,  │
                 │      │                           signed-in profile)   Telegram,   │
                 │      │                                                Mac sound)  │
                 └──────┼──────────────────────────────────────────────────────┼─────┘
                        │ Tailscale                                            │ push
                    iPhone Safari  ◄────────────────────────────────── iPhone Pushover / Telegram
```

### Components

1. **Scheduler**: runs each watch on its interval with random jitter (plus or minus 20 percent), enforces per-retailer request budgets, applies backoff.
2. **Retailer adapters**: one plugin per retailer behind a common interface (section 6). Adding Amazon or Costco later means adding one file.
3. **Normalizer**: converts adapter output into a standard `StockResult`.
4. **State and dedupe engine**: detects transitions (out of stock to in stock) so the user gets one alert per restock event, not one per poll.
5. **Cart helper**: on a transition, opens the product in the signed-in Chrome profile and clicks add-to-cart for the chosen fulfillment (store pickup at the matched store when possible). Stops before checkout.
6. **Notifier**: fans out to Pushover, Telegram, and the Mac speaker.
7. **Dashboard**: list, add, edit, pause watches; see last check time, last result, error state; test alerts; view event log.
8. **Health monitor**: heartbeat and self-checks (section 9).

## 5. Configuration

`~/StockWatch/config.yaml` is the source of truth. Dashboard edits write back to it.

```yaml
home:
  zip: "75001"            # OPEN: the user's ZIP
  radius_miles: 25

defaults:
  interval_seconds: 90    # per watch, before jitter
  quiet_hours: null       # e.g. "23:00-06:00"; alerts still fire if severity=critical
  max_price: null

notify:
  pushover:
    enabled: true
    user_key: ${PUSHOVER_USER_KEY}
    app_token: ${PUSHOVER_APP_TOKEN}
    priority: 2           # emergency: repeats until acknowledged
    retry_seconds: 30
    expire_seconds: 900
    sound: "siren"
  telegram:
    enabled: true
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
  mac_sound:
    enabled: true
    file: "assets/alarm.aiff"
    repeat: 5
    speak: true           # macOS "say" reads store name and price

watches:
  - id: switch2-local
    name: "Nintendo Switch 2"
    enabled: true
    max_price: 499.99
    fulfillment: [store_pickup, in_store, ship]   # in priority order
    add_to_cart: true
    targets:
      - retailer: bestbuy
        sku: "BESTBUY_SKU"          # placeholder, fill from product URL
      - retailer: target
        tcin: "TARGET_TCIN"
      - retailer: walmart
        item_id: "WALMART_ITEM_ID"
      - retailer: gamestop
        url: "https://www.gamestop.com/..."
      - retailer: nintendo
        url: "https://www.nintendo.com/us/store/products/..."
```

Rules:
- Secrets live in macOS Keychain or a `.env` file excluded from git, referenced as `${VAR}`.
- Adding a new item = add a watch with its retailer IDs. The dashboard should accept a pasted product URL and extract the ID.
- Store filter: optional `store_ids` per target to pin specific stores; otherwise use ZIP plus radius.

## 6. Retailer adapters

### Common interface

```python
class RetailerAdapter(Protocol):
    name: str
    min_interval_seconds: int           # floor the scheduler will not go below
    async def check(self, target: Target, home: Home) -> StockResult: ...
    async def add_to_cart(self, target: Target, result: StockResult, browser: BrowserCtx) -> CartResult: ...
    def product_url(self, target: Target) -> str: ...
    def cart_url(self) -> str: ...

@dataclass
class StockResult:
    retailer: str
    in_stock: bool
    channels: list[ChannelAvail]        # store_pickup / in_store / ship, each with store info
    price: float | None
    checked_at: datetime
    raw_status: str                     # e.g. "IN_STOCK", "LIMITED", "OUT_OF_STOCK"
    error: str | None                   # "blocked", "timeout", "parse_failed", ...

@dataclass
class ChannelAvail:
    channel: Literal["store_pickup", "in_store", "ship"]
    available: bool
    store_id: str | None
    store_name: str | None
    distance_miles: float | None
    low_stock: bool | None
    quantity: int | None
```

### Per-retailer approach

Priority order for data source: official API, then the same public JSON the retailer's own website uses, then reading the product page in the real browser profile. Always at human-scale request rates.

| Retailer | Source | Local stock | Notes |
|---|---|---|---|
| Best Buy | Official Developer API (free key from developer.bestbuy.com). `GET /v1/products/{sku}/stores.json?postalCode=ZIP` | Yes, per store, with `lowStock` and distance | Cleanest source. Best Buy documents it as near real time, but some third parties report store stock lags the website. Phase 0 must compare API vs site during a real restock and fall back to page check if it lags. |
| Target | The fulfillment JSON Target's own website calls (redsky `pdp_fulfillment` style endpoint keyed by TCIN and store ID). Fallback: product page in browser profile. | Yes, per store | Unofficial and can change without notice. Adapter must fail loud (alert "Target adapter broken") rather than silently report out of stock. |
| Walmart | Product page in browser profile, read pickup availability for the set store | Partial (Walmart shows your selected store) | No public consumer stock API. Walmart is the most aggressive at bot detection. Use the longest interval (180s+) and back off immediately on any challenge page. |
| GameStop | Product page in browser profile, "pick up in store" availability for set store | Partial | Same pattern as Walmart. |
| Nintendo Store | Product page in browser profile (online only, ships) | No (online only) | Alert means "online stock", deep link to product page. Nintendo has used invite and queue systems for Switch 2 sales; adapter should detect queue or invite-only text and report it as such. |

Each adapter ships with:
- A recorded fixture (saved JSON or HTML) for both in-stock and out-of-stock states, and unit tests against them.
- A `--dry-run` CLI to check one target and print the `StockResult`.

### Request etiquette (hard requirements)

- Respect `min_interval_seconds` per retailer. Defaults: Best Buy API 60s, Target 90s, GameStop 120s, Walmart 180s, Nintendo 120s.
- Jitter every interval.
- On HTTP 403, 429, or a challenge/CAPTCHA page: mark the target `blocked`, double the interval (cap 30 min), send one low-priority "paused, check manually" notice with the product link. Never retry immediately.
- One shared browser context per retailer, reused across checks (no fresh sessions per poll).
- Identify as a normal Chrome on macOS by virtue of being a real Chrome. Do not spoof.

## 7. Alerts to iPhone

### Why Pushover as primary

- Emergency priority (2) repeats the alert every `retry` seconds until the user taps acknowledge, up to `expire`.
- The iOS app supports Apple Critical Alerts, which sound even with the mute switch on and Do Not Disturb or Focus enabled, at a volume set inside the app. The user must turn on "Critical Alerts for emergency priority" once in Pushover settings.
- Messages carry a `url` and `url_title`, so the alert opens the cart or product page in one tap.
- One-time purchase per platform after a free trial. No server to run.

ntfy was considered. Its iOS app relays through ntfy.sh even when self-hosted, and critical alert support on iOS is still listed as a top open request, so it is not reliable enough to be the alarm.

### Telegram as secondary and remote control

Telegram gets every alert as a backup channel (normal priority, it cannot bypass silent mode) and also serves as a command channel:

| Command | Action |
|---|---|
| `/status` | Summary of all watches, last check, errors |
| `/pause <id>` / `/resume <id>` | Toggle a watch |
| `/pauseall` / `/resumeall` | Global toggle |
| `/check <id>` | Force an immediate check |
| `/add <product url>` | Create a watch from a URL with defaults |
| `/test` | Fire a test alert through every channel |

Only messages from the configured `chat_id` are accepted; everything else is ignored.

### Alert content

Title: `IN STOCK: Nintendo Switch 2 @ Best Buy`
Body: `Pickup at Best Buy Frisco (6.2 mi), $449.99, low stock. Added to cart: YES`
Primary link: cart URL if add-to-cart succeeded, otherwise product URL. Second link (Telegram only): store map link.

### Severity levels

| Level | When | Pushover priority |
|---|---|---|
| critical | In stock at a target within radius and under max price | 2 (emergency, repeating) |
| info | Price change, online-only stock when local is preferred | 0 |
| warning | Adapter blocked, broken, or service unhealthy | 1 |

## 8. Add-to-cart behavior

- Uses a dedicated Chrome profile at `~/StockWatch/chrome-profile`. Setup step: the dashboard opens it once and the user signs in to each retailer manually (including any 2FA). The bot never stores passwords.
- On a critical event, the cart helper opens the product page in that profile, selects the matched fulfillment option and store, clicks add to cart, and confirms the cart count changed. Hard timeout 25 seconds. The alert is sent first, before the cart attempt, then a follow-up message reports the cart result. The alert must never wait on the cart.
- The helper must never click any element whose label matches checkout, place order, buy now, or pay. Enforce with a denylist check before every click, plus a unit test.
- If the retailer session has expired, report "Sign in needed for <retailer>" as a warning and include the product link.
- Honest limitation to surface in the README: at Target, Walmart, and Best Buy a cart does not reserve inventory. The item is only held once an order is placed. The cart saves the user the search and option clicks; they still have to check out fast on the phone.
- For the phone checkout to be one tap, the user should be signed in to each retailer in iPhone Safari (or their apps) with payment saved.

## 9. Running reliably on the Mac

- **launchd**: `~/Library/LaunchAgents/com.local.stockwatch.plist` with `RunAtLoad` and `KeepAlive`. Logs to `~/StockWatch/logs/` with daily rotation.
- **Prevent sleep**: the service holds a power assertion while running (`caffeinate -i` as a child process or the IOKit equivalent). The README tells the user to set Energy settings so the Mac does not sleep on power; a sleeping Mac checks nothing.
- **Heartbeat / dead man's switch**: every 5 minutes the service pings a free external heartbeat monitor (for example healthchecks.io). If pings stop, that service alerts the user's phone. This is the only way to learn that the Mac itself died.
- **Self-checks**: if any adapter has failed 5 times in a row, send a warning. Daily 9:00 summary to Telegram: checks run, errors, watches active.
- **Updates**: `stockwatch doctor` CLI verifies config, secrets, Pushover and Telegram reachability, browser profile sign-in state per retailer, and runs one dry check per target.

## 10. Phone access to the dashboard (no public website)

- Install Tailscale on the Mac and iPhone (free personal plan). The dashboard binds to `127.0.0.1` and the Mac's Tailscale address only.
- On the phone: open `http://<mac-name>:8787` in Safari and add it to the Home Screen. It works anywhere the phone has internet, and nobody outside the user's Tailscale network can reach it.
- Dashboard requires a simple PIN or token even over Tailscale.
- Telegram commands cover the same essentials when the user does not want to open the dashboard.

### Dashboard screens

1. **Watches**: cards with name, status dot (green checking, yellow blocked, red error, grey paused), last check, last in-stock time, toggle.
2. **Watch detail**: targets per retailer, interval, max price, recent results, "check now".
3. **Add watch**: paste product URLs; adapter extracts IDs and price; choose fulfillment and radius.
4. **Events**: in-stock events, alerts sent, cart results.
5. **Settings**: notification channels with "send test", home ZIP, quiet hours, sign-in status per retailer.

## 11. Data model (SQLite)

- `watches(id, name, enabled, config_json, created_at, updated_at)`
- `targets(id, watch_id, retailer, identifiers_json, store_ids_json)`
- `checks(id, target_id, checked_at, in_stock, price, channels_json, error, duration_ms)` with 14-day retention
- `events(id, target_id, kind, started_at, ended_at, details_json)` where kind is `restock`, `blocked`, `error`
- `alerts(id, event_id, channel, sent_at, status, ack_at)`

Dedupe rule: a `restock` event opens on the first in-stock check after an out-of-stock check, and closes when stock is gone for 2 consecutive checks. One critical alert per open event. A new alert only fires if a different store or a lower price appears.

## 12. Security and privacy

- Secrets in Keychain or `.env` with `chmod 600`; never logged.
- Chrome profile directory `chmod 700`.
- Dashboard not bound to `0.0.0.0`.
- No payment data handled by the bot.
- Telegram bot only answers the configured chat ID.

## 13. Build phases for Claude Code

**Phase 0: Spike (validate data sources)**
- Best Buy API key, call the store availability endpoint for a known SKU and the user's ZIP.
- Target fulfillment JSON for one TCIN and a nearby store.
- Page checks for Walmart, GameStop, Nintendo with the real Chrome profile.
- Pushover emergency alert with URL to the iPhone, confirm critical alert sounds on mute.
- Output: a short findings note per retailer (works / lags / blocked).

**Phase 1: Core loop**
- Config loader, scheduler with jitter and backoff, Best Buy and Target adapters, SQLite, dedupe, Pushover and Mac sound, CLI (`run`, `check`, `doctor`, `test-alert`).

**Phase 2: Remaining adapters and cart helper**
- Walmart, GameStop, Nintendo adapters; cart helper with checkout denylist; signed-in profile setup flow.

**Phase 3: Remote control**
- Telegram bot commands, FastAPI dashboard, Tailscale binding, PIN.

**Phase 4: Hardening**
- launchd plist and installer script, power assertion, heartbeat, daily summary, log rotation, fixtures and tests for every adapter.

### Acceptance criteria

1. With the Mac locked and the iPhone on silent with a Focus mode on, a simulated restock produces an audible repeating iPhone alert within 10 seconds of detection.
2. Tapping the alert opens the cart (if add succeeded) or product page in Safari.
3. The Mac plays the alarm and speaks the store name.
4. Killing the process causes launchd to restart it within 30 seconds.
5. Stopping the Mac entirely triggers the heartbeat alert on the phone within 15 minutes.
6. A 403 or challenge page pauses that target with backoff and sends one warning, not a stream.
7. The cart helper's test suite proves it cannot click any checkout or purchase control.
8. Adding a new item from a pasted Best Buy or Target URL takes under one minute from the phone.

## 14. Open decisions

1. Home ZIP and search radius.
2. Exact item(s) and SKUs (Switch 2 base vs bundle, any color or edition limits).
3. Max price per item.
4. Quiet hours: should critical alerts ever be suppressed overnight? Default: never.
5. Should Nintendo online stock trigger a critical alert or info only?

## 15. Future ideas (not v1)

- More retailers (Amazon, Costco, Sam's Club, Micro Center) as adapters.
- Stock pattern log to learn restock days and times per store, then poll faster in those windows.
- Apple Watch complication via Pushover's watch support.
- Calling the user's phone via Twilio as a last-resort alarm if the Pushover alert is not acknowledged in 2 minutes.
