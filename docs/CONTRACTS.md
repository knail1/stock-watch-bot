# Internal module contracts

Spec: `docs/spec/stock-watch-bot-spec.md`. This file records the interfaces as built.
Foundation modules (change with care; other modules depend on them):
`stockwatch/models.py`, `config.py`, `paths.py`, `db.py`, `adapters/base.py`, `adapters/registry.py`, `cart/safety.py`.

## Cross-cutting rules

- Never purchase or check out. Every click in a retailer page goes through `stockwatch.cart.safety.safe_click`
  (enforced by a static scan in `tests/test_cart_safety.py`). `safe_click` fails closed: it refuses when the
  element's label matches the denylist **or** no label can be read.
- No CAPTCHA solving, UA/fingerprint spoofing or proxies. HTTP 403/429/challenge -> `BlockedError`, one attempt, no retry.
- Secrets never logged. `logging_setup.setup_logging()` installs a redacting filter on our handlers and sets the
  `httpx`/`httpcore` loggers to WARNING (their INFO lines contain full URLs, e.g. Best Buy's `apiKey=`).
  `bestbuy.py` additionally filters `apiKey=` on those loggers. Error details pass through `redact_detail`
  (scheduler) / `cart.helper.redact`.
- HTML parsing uses `selectolax.lexbor.LexborHTMLParser` (selectolax >= 1.0 removed the Modest backend).

## Models (`models.py`)

`Target(retailer, identifiers, store_ids, watch_id, db_id)` with `.key` and `.get(name)`;
`StockResult(retailer, in_stock, channels, price, checked_at, raw_status, error, error_detail, duration_ms, product_name)`
with `StockResult.failure(...)`; `ChannelAvail`; `CartResult(retailer, success, message, cart_url, signed_out, ...)`;
`Severity` CRITICAL/WARNING/INFO/LOW -> Pushover 2/1/0/-1; `Alert`; `AlertOutcome`; `ErrorKind` values.

## Config (`config.py`)

`ConfigStore(path=None, resolver=None)`: `load()`, `save()`, `upsert_watch(raw)`, `set_enabled(id, bool)`,
`delete_watch(id)`, `set_value(dotted, v)`, `set_values({dotted: v})` (one atomic save; rolls back on invalid).
The raw YAML keeps `${VAR}` references; only the resolved `Config` holds secret values.
`Config.effective_interval/effective_max_price/effective_home/min_interval/watch(id)`.

## Adapters (`stockwatch/adapters/<retailer>.py`)

`<Name>Adapter(BaseAdapter)`, constructed as `cls(http=, browser=, settings=RetailerCfg|None)` by
`registry.build_adapters(config, http, browser)`.

| Retailer | min interval | identifiers | source |
|---|---|---|---|
| bestbuy | 60 | `sku` | official API (`retailers.bestbuy.api_key`); missing key -> CONFIG failure result |
| target | 90 | `tcin` | redsky fulfillment JSON (nearby stores, `extra.max_stores`=3), page fallback via browser on ParseError |
| walmart | 180 | `item_id` | product page via `browser.fetch_html` (`__NEXT_DATA__`, then visible text) |
| gamestop | 120 | `product_id`, `url` | product page via browser |
| nintendo | 120 | `product_id`, `url` | product page via browser; QUEUE / INVITE_ONLY reported as `raw_status` with `error=None` |

Each: `identifiers_from_url(url)` (ValueError if the id is missing; `BaseAdapter.required_identifiers` gives
the same check declaratively), `matches_url`, `async check(target, home) -> StockResult`
(raises `BlockedError`/`ParseError`/`AdapterError(kind)`), `async add_to_cart(target, result, browser, channel=None) -> CartResult`
(`channel` = the matched `ChannelAvail` (option + store) to use, None = best available; pickup adds that cannot select the
matched store say so in the message; signed out -> `signed_out=True`; refused unsafe click -> `success=False`),
`product_url(target)` (GameStop/Nintendo re-validate the stored URL), `cart_url()`,
module-level pure parsers and an `async is_signed_in(page) -> bool | None` hook used by `BrowserManager.signin_status`.
`registry.identifiers_from_url(url) -> (retailer, identifiers)` raises ValueError for unsupported URLs. Hosts are
checked on the parsed URL (`base.safe_split_url`: http(s) only, no userinfo/backslash); `identifiers['url']` is the
canonical URL; cart/checkout/account paths and `${` are rejected.

## Browser (`browser.py`)

`BrowserManager(cfg.browser)` implements `BrowserCtx`. Lazy: Chrome starts on the first `page_for`/`cart_page`/
`fetch_html`/`signin_status`/`open_signin_pages`, so API-only setups never launch it. The launch is one shared task
awaited via `asyncio.shield` (a caller timeout never orphans Chrome); after `close()` it never starts again
(`AdapterError(CONFIG)`).
`fetch_html(retailer, url, wait_selector=None, timeout_ms=30000) -> (status, html)` (BlockedError on 403/429/challenge,
`AdapterError(TIMEOUT)`), `open_signin_pages(retailers) -> urls` (headed until `end_signin()`),
`signin_status(retailers=None, *, max_age=600, launch=True) -> {retailer: bool|None}` (cached; probes go through
`request_gate(retailer)` when set), `cached_signin_status(retailers)`, `signin_stale(retailers)`, `close()`,
`restart(stale=None)` (only replaces `stale` if it is still current), `lock_for(retailer)`.

## Engine (`engine/`)

`StateEngine(db, failure_threshold=config.health.failure_warn_threshold, record_checks=True)`.
`process(target, result, watch, max_price, home, *, online_severity=None) -> list[Decision]`. **It writes the `checks` row itself**; callers must not.
Restock decisions (`restock_alert`/`price_info`) are returned before `recovered` ones. `online_severity` comes from
`Config.online_only_severity(watch, retailer)` (watch override -> `retailers.<r>.online_severity` -> Nintendo critical ->
info when a local channel precedes `ship` in `fulfillment`, else critical).
`Decision(kind, severity, event_id, channel, result, reason, realert, channels)`, `.is_critical`.
Kinds: `restock_alert` (CRITICAL, or the online-only severity for ship-only stock, and INFO for re-alerts after the
critical), `price_info`, `blocked_notice`, `broken_notice` (parse_failed), `failure_warning` (N in a row), `recovered`.
Only `restock_alert` with `is_critical` triggers the cart. Queue/invite results are not failures and count as out of stock.

`Scheduler(config, adapters, on_result, *, clock, sleep, rng, retailer_spacing=5.0, retailer_concurrency=1,
check_timeout=90.0, max_backoff=1800.0)`: `start()`, `stop()`, `reload(config)`, `pause/resume(watch_id)` (KeyError),
`pause_all/resume_all()`, `async check_now(watch_id) -> list[StockResult]`, `seed_result(target_key, result)`
(counts an external check, e.g. the `/add` dry check, as the latest), `gate(retailer)`, `status() -> list[dict]`.
Interval = effective interval x U(0.8, 1.2), never below the retailer floor; blocked doubles to 1800 s, success resets.

## Notifier (`notify/`)

`Notifier(config, http, db, *, pushover=None, telegram=None, mac_sound=None)`: `async send(alert) -> list[AlertOutcome]`
(concurrent, never raises, one `alerts` row per channel), `test_all()`, `update_config(config)`, attributes
`pushover`/`telegram`/`mac_sound`. Quiet hours: non-critical Pushover at priority -1, no Mac sound; critical unaffected.
`format_restock_alert(watch, target, result, product_url, cart_url=None, added=None, *, channel, severity, event_id)`,
`format_cart_followup(watch, target, cart, product_url, *, event_id)` (success INFO with cart URL; signed out WARNING
"Sign in needed for <Retailer>" with product link; failure INFO "Added to cart: NO (<reason>)").
`TelegramClient.send_text/get_updates/get_me`, `PushoverClient.validate/receipt_status`, `MacSound.play/wait/stop`.

## Cart (`cart/`)

`CartHelper(browser, adapters, timeout_seconds=config.browser.cart_timeout_seconds)`:
`async try_add(target, watch, result, *, channel=None) -> CartResult` (`channel` = `Decision.channel`; when None the
best available channel honouring `watch.fulfillment` and `target.store_ids`), never raises, 25 s hard timeout including the per-retailer lock,
maps SignedOutError / `signed_out=True` / ForbiddenClickError / BlockedError (no retry). Skips (adapter not called) when
`watch.add_to_cart` is false, `result` is missing/not in stock, or there is no browser.

## Service (`service.py`)

`StockWatchService(store: ConfigStore, db: Database, *, http=None, browser=None, adapters=None, notifier=None,
cart_helper=None, scheduler_kwargs=None, enable_dashboard=True, enable_telegram=True, enable_health=True,
enable_power=True, redactor=None)`.

- `async run(install_signal_handlers=True)` (start, wait for SIGTERM/SIGINT or `request_stop()`, stop), `start()`, `stop()`,
  `reload_config()`, `apply_config(config)`, `wait_background(timeout)`, `product_url(target)`.
- On each result: `StateEngine.process` -> per decision. Restock: the alert is **awaited first**, then (critical and
  `add_to_cart`) a background cart task, then the follow-up alert; the cart result is also stored in the restock
  event's `details.cart`. A critical alert whose delivery failed (`critical_delivered(outcomes)` is False) is re-armed
  via `StateEngine.critical_delivery_failed` and re-sent on the next in-stock check (max 3 attempts). Blocked: one `Severity.LOW` "paused, check manually" notice with the product link (spec 6).
  Broken: WARNING "<Retailer> adapter broken". Failure warning: WARNING. Price info / recovered: INFO.
  Nintendo QUEUE/INVITE_ONLY: one INFO alert per episode (in-memory).
- Watches/targets are mirrored into SQLite on start and on every config change.
- Implements the Telegram `ServiceControl` (`status_text`, `pause`, `resume`, `pause_all`, `resume_all`,
  `check_now -> str`, `add_watch_from_url -> watch_id`, `test_alert -> str`) and the dashboard `DashboardControl`
  (`watches`, `recent_checks`, `recent_events`, `settings`, `signin_status`, `open_signin_browser`, `send_test`,
  `update_settings`, `add_watch`, `update_watch`, `set_paused`, `delete_watch`, `check_now`).
  Pause/resume persist as `enabled` in config.yaml; pause_all/resume_all are runtime only.
- `add_watch_from_url`: id `<retailer>-<id>` (unique), name from the dry check's `product_name` if any; the dry check runs
  through the retailer gate and seeds the scheduler; ValueError for unsupported URLs or an already-watched product.
- Dashboard: `create_app(service, pin=config.dashboard.pin, session_secret=<~/StockWatch/.dashboard_secret>)`,
  served by two `supervise()` loops: 127.0.0.1 alone, and the remote (Tailscale) hosts from `resolve_bind_hosts(config)`,
  re-resolved every 60 s and retried every 30 s on a missing address or bind failure. Never stops stock checks.
  `spawn_background(coro, name)` runs dashboard fire-and-forget work as service-owned tasks (cancelled by `stop()`).
  `signin_status()` answers from the browser's cache and refreshes stale entries in the background (never launches Chrome).
- Telegram bot runs only when Telegram is enabled with a token and chat id. Health monitor gets
  `send_text=telegram.send_text` for the daily summary. `PowerAssertion(enabled=health.prevent_sleep)`.
- Module helpers: `dry_check(adapter, target, home, timeout=90) -> StockResult` (never raises), `describe_result(result)`.

## Telegram bot (`telegram_bot.py`)

`TelegramBot(control: ServiceControl, client: UpdateSource, chat_id, *, poll_timeout=30, ...)`: `run()`, `stop()`,
`execute(text) -> reply`. Only the configured chat; KeyError -> "Unknown watch", ValueError -> message shown.

## Dashboard (`dashboard/`)

`create_app(control: DashboardControl, *, pin=None, session_secret=None)`, `resolve_bind_hosts(config)`,
`check_bind_safety(hosts, pin_configured)` (only loopback and Tailscale 100.64.0.0/10 / fd7a:115c:a1e0::/48),
`async serve(app, hosts, port)`, `async supervise(app, resolve, port, *, retry_seconds, refresh_seconds)`.
`?token=` auth shares the login form's `LoginThrottle`. View models: `WatchStatus`,
`TargetStatus`, `CheckView`, `EventView`, `SettingsView`, `NotifyChannelView`, `WatchSpec.to_raw()`.

## Health and ops

`HealthMonitor(config, db, http, send_text=None)`: `start/stop/update_config`, heartbeat every
`health.heartbeat_interval_seconds`, daily summary + prune at `health.daily_summary_time`.
`PowerAssertion(enabled)`: `caffeinate -i -w <pid>`. `logging_setup.setup_logging(level) -> RedactingFilter`,
`collect_secrets(config)`. `launchd.install(python)/uninstall()/is_loaded()/restart()`; the plist runs
`python -m stockwatch run`.

## CLI (`cli.py`, `__main__.py`)

`stockwatch run | check <watch_id> [--dry-run] | check --url URL | doctor [--offline --no-browser --no-checks] |
test-alert | init | setup-profile [retailer...] | install | uninstall | status`, plus global `--config` and `-v`
(accepted before or after the subcommand). `doctor` exits 1 on any FAIL. `check --dry-run` makes no DB writes,
sends no alerts and never touches the cart.
