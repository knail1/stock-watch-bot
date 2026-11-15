# Best Buy API compliance

How StockWatch follows the Best Buy API Terms of Service (version "Last updated, February 23,
2021"). The full text is in [`bestbuy-api-terms.md`](bestbuy-api-terms.md). Tests live in
`tests/test_bestbuy_compliance.py` and `tests/test_dashboard.py`.

This is an engineering checklist, not legal advice.

## Enforced in code

| Term | What StockWatch does | Where |
|---|---|---|
| **Rate limit: 5 calls/second per key** | A shared limiter spaces every Best Buy API call at least 0.25 s apart, so at most 4/s. A misconfiguration can never push it past 5/s. | `adapters/bestbuy.py` `ApiRateLimiter` |
| **Rate limit: 50,000 calls/day per key** | Rolling 24-hour budget, 45,000 by default and capped at 50,000. Once it's used up, calls are refused locally instead of being sent. The target is treated as blocked: backoff and one notice. A rolling window also keeps every calendar day under the limit. | `ApiRateLimiter`, `retailers.bestbuy.daily_call_budget` |
| Budget planning | `stockwatch doctor` estimates daily calls (2 per check) for enabled Best Buy targets and FAILs if the estimate exceeds the budget. One SKU checked every 60 s is about 2,880 calls/day. | `cli.py` doctor row "bestbuy api budget" |
| **403 after the limit** | A 403 or 429 is never retried right away. The target backs off, doubling up to 30 min, with one notice. Best Buy's error message (quota vs. key) is shown, with the key removed. | `BestBuyAdapter._get_json`, scheduler backoff |
| **Store or cache Content no longer than 72 hours** | Every hour, Best Buy check rows older than 71 h are deleted. Prices, store IDs and cart messages are removed from Best Buy events older than 71 h; only the event kind and timestamps remain. The longest any Best Buy content can live is 71 h plus a 1 h sweep interval, which is 72 h. Other retailers keep the normal 14-day history. | `db.prune_restricted_content`, `HealthMonitor._content_loop` |
| Dedupe after the purge | An open restock event that lost its content re-baselines from the next check, so the purge doesn't cause a duplicate alert. | `engine/state.py` |
| **No rights to Content specific to Games, CDs, DVDs, Blu-ray titles** | The adapter requests the product `type`. For `Game`, `Music` or `Movie` it stops, makes no stores call, and reports a config error telling you to remove the SKU. Consoles and hardware (`HardGood`) are fine. | `UNLICENSED_PRODUCT_TYPES`, `BestBuyAdapter.check` |
| **Keys: do not disclose** | The key lives only in `~/StockWatch/.env` (chmod 600) or the Keychain, referenced as `${BESTBUY_API_KEY}`. It is redacted from httpx logs, error messages and `doctor` output. It is never written to config, the database, the dashboard or git. | `config.py`, `bestbuy.redact`, `logging_setup.py` |
| **Attribution: clearly attribute Content to Best Buy** | Alerts name the source ("IN STOCK: … @ Best Buy", "Pickup at Best Buy Frisco"). The dashboard footer states "Best Buy stock and price data is provided by the Best Buy Developer API." | `notify/notifier.py`, `dashboard/templates/base.html` |
| **Branding: Best Buy logo wherever the API has a presence, at the bottom** | Every dashboard page shows Best Buy's own logo, loaded from Best Buy's own URL exactly as in their snippet (so it isn't redistributed in this repo) and linked to developer.bestbuy.com, at the bottom, whenever any Best Buy target is configured. The CSP allows images from `developer.bestbuy.com` for this. | `dashboard/templates/base.html`, `dashboard/app.py` |
| Logo not larger than our branding, not the most prominent element | The logo is 24 px tall in the footer. The "StockWatch" name in the header is the main brand. | `dashboard/static/style.css` |
| No Best Buy blue and yellow as primary colors | The dashboard accent is teal (`#0f766e`). Amber is used only for the "blocked" status dot. | `style.css`, test `test_dashboard_does_not_use_best_buy_blue_as_accent` |
| **Naming: no "Best Buy" in the application name** | The app is "StockWatch". | — |
| **Forge headers / disguise origin** | Requests go straight from your Mac with the HTTP client's normal headers. No spoofed user agent and no proxies. | `service.py` HTTP client |
| **No third-party access to the API** | The dashboard listens only on localhost and your Tailscale address, behind a PIN. The Telegram bot answers only your chat ID. Nobody else can trigger API calls. | `dashboard/server.py`, `telegram_bot.py` |
| **Not for the benefit of other retailers** | Best Buy data goes only to you. It isn't exported or shared, and isn't compared against other retailers' prices for anyone else. | design |

## Things you are responsible for (not enforceable in code)

1. **Accurate registration data.** The terms require registration details that are "accurate,
   current and complete", and the key holder is "fully responsible for all activities" using
   it. Register under the person or organisation that actually runs this tool, and describe
   the use as it really is: a personal stock alert for a few SKUs.
2. **Don't share the key.** Don't paste it into issues, chats or screenshots. If you think it
   leaked, report it at <https://developer.bestbuy.com/contact-us> and get a new one.
3. **Check the terms for updates.** They can change at any time. Re-read them at
   developer.bestbuy.com now and then, and update [`bestbuy-api-terms.md`](bestbuy-api-terms.md)
   when they do.
4. **Comply with Best Buy's requests.** If Best Buy asks you to change or stop how you
   display its content, or wants to verify compliance, do so.
5. **Keep the number of SKUs modest.** `stockwatch doctor` shows the estimated daily call count.

## Outside these API terms: add-to-cart on bestbuy.com

The API terms cover only the API. The add-to-cart helper drives the **bestbuy.com website**
in your own signed-in Chrome profile, which is governed by Best Buy's website **Terms and
Conditions**, not the API terms above. Read those before turning on `add_to_cart` for a
Best Buy target. If you'd rather keep Best Buy to API use only, set `add_to_cart: false`
on watches that include Best Buy. You still get the alert with a one-tap product link.

## Not used

- **Commerce Gateway API.** Not used. StockWatch never checks out; it links you to
  bestbuy.com.
- **The `sku in(...)` batching tip.** Not used yet. Each check makes 2 calls, product then
  stores, which stays far below the limits for a handful of SKUs.
