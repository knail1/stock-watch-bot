# StockWatch: status and findings

_As of 2026-11-14. Branch `stockwatch-v1`._

## TL;DR

- **Built:** StockWatch v1 is implemented from the spec (phases 1–4). That's about 9,500 lines of Python
  with **746 passing tests**, none of which touch the network.
- **Mac alarm proven:** a simulated restock played the siren 3× and read out the store and price.
- **Configured:** three Zelda 40th Anniversary items, each at every retailer that carries it
  (14 retailer listings), for the home ZIP.
- **Best Buy:** the code follows the Best Buy API Terms of Service. The API key request is **pending**.
- **Main finding: Target blocks StockWatch.** Its bot protection (PerimeterX) rejects both
  Target's stock data feed and an automated Chrome, even when a person completes the challenge.
  By spec, StockWatch does not try to get around it.
- **Live checks (2026-11-14):** **GameStop and Nintendo work** end to end. **Walmart works at first,
  but its PerimeterX starts a "Robot or human?" challenge** after about 7 page loads in a few minutes.
  **Target is blocked.** Best Buy is waiting for its key.
- **Not done yet:** the phone side (Pushover, Telegram, Tailscale) isn't set up.

---

## 1. What has been built

| Area | What exists |
|---|---|
| Retailer adapters | Best Buy (official API), Target (product page in Chrome by default; its JSON feed is optional), Walmart, GameStop, Nintendo (product page in the bot's Chrome profile). Each turns the data into the same stock result, has saved test pages, and reports itself "broken" instead of quietly saying out of stock when a page changes shape. |
| Scheduling | Per-watch intervals with ±20% jitter and per-retailer minimum gaps. On a block, the interval doubles up to 30 min and you get one notice per block, not a stream. |
| Restock alerts | SQLite database; one critical alert per restock, a new alert only for a new store or a lower price. A restock ends after 2 out-of-stock checks. |
| Alerts | Pushover emergency alert (repeats until acknowledged), Telegram backup, Mac siren plus voice. The alert always goes out before the cart attempt. |
| Add to cart | In the bot's Chrome profile, with a hard 25 s timeout. **Every click** goes through a denylist that refuses checkout, place order, buy now, pay and similar. A test scans the code for any other kind of click. |
| Remote control | Dashboard behind a PIN, reachable only on localhost and Tailscale, with CSRF protection. Telegram commands: `/status /pause /resume /check /add /test`. |
| Running on the Mac | launchd agent (restarts within about 10 s), `caffeinate`, healthchecks.io heartbeat, daily summary, log rotation. |
| CLI | `run`, `check [--dry-run \| --url]`, `doctor`, `test-alert`, **`simulate`**, `init`, `setup-profile`, `install`/`uninstall`, `status`. |

How it was built: I wrote the shared core myself. A workflow then ran 12 agents in parallel, one per
module, followed by integration, a smoke test, and a review by 5 reviewers. Each finding was
then passed to a separate agent tasked with disproving it. **21 findings held up and were fixed, 8 were rejected.**

## 2. Changes since the first build

| Change | Why |
|---|---|
| `stockwatch simulate` | Tests the whole alert path on the laptop with no phone. Fakes a restock through the real alert code, using a throwaway database. |
| Mac alarm repeats 3× (was 5) | Your preference. |
| **Best Buy API Terms compliance** | See section 4. |
| Target: PerimeterX handling | See section 5. |
| Docs | `README.md` (status, laptop-first setup), `docs/OVERVIEW.md`, `docs/policies/`, and a setup guide at `~/Documents/claude_docs/stockwatch-setup-guide/index.html`. |

## 3. What's configured (`~/StockWatch/config.yaml`)

Home ZIP set in the local config, 25-mile radius. Laptop test mode: Pushover and Telegram off, dashboard on
`127.0.0.1` only.

| Watch | Target | Best Buy | Walmart | GameStop | Nintendo | Max |
|---|---|---|---|---|---|---|
| Switch 2 – Zelda 40th console (`zelda40-console`) | 1013322047 | 6691841 | 21002656445 | 20037854 | 121642 | $519.99 |
| Pro Controller – Zelda 40th (`zelda40-pro-controller`) | 1013213521 | 6691849 | 20954470204 | 20037855 | 127076 (with stand) | $109.99 |
| HORI Electric Ocarina of Time (`hori-ocarina`) | 1013288947 | 6696766 | 21122355365 | not listed | 129987 | $49.99 |

The Walmart console ID is Walmart's own listing. Marketplace resellers list it at $775–$989
under other IDs. The previous config is backed up at `~/StockWatch/config.yaml.bak`.

## 4. Best Buy API

- **Key status:** requested, waiting for approval. Best Buy checks report "key missing" until
  `BESTBUY_API_KEY` is in `~/StockWatch/.env`.
- **What the code enforces:**
  - at most 4 calls/s
  - a rolling 24 h budget of 45,000 calls (the limit is 50,000)
  - Best Buy data deleted from the database within 72 h
  - game, music and movie titles refused
  - Best Buy's 403 reason shown, with the key removed
  - Best Buy's logo and attribution in the dashboard footer
  - a non-blue color scheme
- **Expected usage:** about 5,760 calls/day for the three items, from `stockwatch doctor`.
- The full terms and a clause-by-clause checklist are in `docs/policies/`.
- **Registration:** answer honestly. You registered with the WAADA address because free
  email domains aren't accepted, so say that, and that this is a personal tool. Don't present
  it as part of WAADA's mental-wellness work.
- Add-to-cart on bestbuy.com is governed by the **website** terms, not the API terms.

## 5. Findings: retailer access

### Target: blocked

| Attempt | Result |
|---|---|
| Target's stock data feed (`nearby_stores_v1`) | HTTP **435** with a PerimeterX `blockScript` (captcha.px-cdn.net). This is bot protection for non-browser clients. StockWatch had reported it as `http_error`; **it now counts as blocked.** |
| Product page in the bot's Chrome | The page loaded (HTTP 200, correct title, product data), but a **full-screen PerimeterX "press & hold" overlay** covered it. |
| You signing in and completing the challenge yourself | **"Please try again"**: PerimeterX rejects the challenge because it detects that Chrome is controlled by automation, whoever does the pressing. |

**Conclusion:** Target can't be checked without hiding the automation (stealth patches,
spoofed fingerprints, captcha solvers, proxies). That's bot-protection evasion. The spec rules
it out, and it risks your Target account. **I declined to build it.**

**Code changes from this:**
- **Blocked handling:** HTTP 435 and PerimeterX block responses count as **blocked**, which means
  backoff and one notice, never an immediate retry.
- **New setting:** `retailers.target.source: page` (the default) or `api`.
- **More precise challenge detection:** it now looks for a challenge that's actually showing (the PerimeterX
  overlay, challenge elements, visible "press & hold" wording) and ignores words like "captcha"
  inside normal pages' scripts. Before, Target's `GLOBAL_CAPTCHA_ENABLED` flag would have
  flagged every Target page.

### Live checks of the other retailers (2026-11-14, home ZIP)

| Retailer | Result | What I found and fixed |
|---|---|---|
| **GameStop** | **Works.** Console: out of stock, $519.99. | (1) **False "challenge":** Cloudflare loads `/cdn-cgi/challenge-platform/...` on *normal* pages. It now only counts on a small "Just a moment…" page. (2) **A false "in stock" that would have set off the alarm:** the parser read a "GameStop Pro" promo blob ("Available") and a JSON-LD offer for a *different* SKU ("InStock"). It now trusts only the main product's form flags (`data-available` / `data-ready-to-order`) and data matching the page's product id. |
| **Nintendo** | **Works.** Console out of stock, $519.99; Ocarina out of stock, $49.99. | The store page changed layout: product data now sits in an Apollo cache, with keys like `prices({"personalized":false})` and an `isSalableQty` flag. The parser now reads that, and `isSalableQty` decides stock. |
| **Walmart** | **Works intermittently.** Ocarina: shippable at **$148.50** (a reseller; above the $49.99 max, so an info alert), pickup at the nearest store unavailable. | (1) The console ID `21002656445` was a business.walmart.com-only listing (404 on walmart.com). The config now uses `21396365421`. (2) After about 7 loads in a few minutes, PerimeterX served **"Robot or human?"**. StockWatch correctly reports blocked and backs off. Normal running is 3 Walmart items every 180 s or more, which may still trigger it, so expect occasional "paused, check manually" notices. |
| **Best Buy** | Waiting for the key | Official API, so no bot protection should apply. |

Add-to-cart flows have **not** been tested live on any retailer yet.

## 6. Legitimate ways to cover Target

1. Alerts from the other retailers are a cue to check Target by hand. Restocks often land at
   several retailers in the same window.
2. Restock-alert services that cover Target: apps such as HotStock or NowInStock, X posters
   such as Wario64, Discord stock-alert servers. Check one against a known drop first.
3. Possible feature: forward a trusted Telegram restock channel into StockWatch's Pushover
   alarm. This uses public posts, not Target's site.
4. Add the three items to a list in the Target app so checkout is one tap.

## 7. Open items and next steps

| # | Item | Who |
|---|---|---|
| 1 | Comment out the 3 Target entries in the config, so StockWatch stops triggering Target's bot protection | waiting for your OK |
| 2 | Consider raising Walmart's interval (e.g. `retailers.walmart.min_interval_seconds: 600`) or watching fewer Walmart items to stay under its challenge threshold | your call |
| 3 | Test add-to-cart on a cheap in-stock item (GameStop/Nintendo/Walmart) | after sign-in to each in `setup-profile` |
| 4 | Add `BESTBUY_API_KEY` when Best Buy approves it, then `stockwatch doctor` | you |
| 5 | Phone setup: Pushover with Critical Alerts, Telegram bot, Tailscale and dashboard PIN, healthchecks.io | you (guide part B) |
| 6 | `stockwatch install` (launchd) once checks look good | you or me |
| 7 | Push commit `a5448b8`, which is local only, and merge `stockwatch-v1` into `main` or open a pull request | waiting for your OK |

## 8. Repository state

```
a5448b8  Target: treat PerimeterX blocks as blocked; page check by default   (local, not pushed)
31bf711  Ignore macOS .DS_Store
68bbf38  Comply with Best Buy API Terms of Service
4905812  Add 'stockwatch simulate' for laptop-only testing; alarm repeats 3x
8df1445  Implement StockWatch v1 (spec phases 1-4)
c15001d  Add StockWatch v1 spec                                              (main)
```
