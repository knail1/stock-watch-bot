"""`stockwatch` command line (spec sections 6, 9 and 13).

    stockwatch run                         run the service (launchd runs this)
    stockwatch check <watch_id> [--dry-run]
    stockwatch check --url URL             dry check of one pasted product URL
    stockwatch doctor [--offline]          PASS/WARN/FAIL health table
    stockwatch test-alert                  fire a test alert through every channel
    stockwatch simulate [watch_id]         fake a restock through the real alert pipeline
    stockwatch init                        create ~/StockWatch/config.yaml and .env
    stockwatch setup-profile               sign in to retailers in the bot's Chrome profile
    stockwatch install | uninstall         launchd agent
    stockwatch status                      last check per target from the database
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

import httpx

from . import __version__, paths
from .adapters.registry import build_adapters, identifiers_from_url
from .config import Config, ConfigError, ConfigStore, WatchCfg
from .models import RETAILERS, Target

log = logging.getLogger("stockwatch.cli")

EXAMPLE_CONFIG = paths.assets_dir().parent / "config.example.yaml"
CHROME_APPS = (Path("/Applications/Google Chrome.app"), Path.home() / "Applications/Google Chrome.app")
#: Retailers whose stock checks need the browser (others use JSON APIs).
BROWSER_RETAILERS = ("walmart", "gamestop", "nintendo")

ENV_TEMPLATE = """\
# StockWatch secrets. Referenced from config.yaml as ${NAME}. Never commit this file (chmod 600).
# Alternatively store them in the macOS Keychain:
#   security add-generic-password -s stockwatch -a PUSHOVER_USER_KEY -w '<value>'
PUSHOVER_USER_KEY=
PUSHOVER_APP_TOKEN=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DASHBOARD_PIN=
HEARTBEAT_URL=
BESTBUY_API_KEY=
"""


# ------------------------------------------------------------------ helpers

def _store(args: argparse.Namespace) -> ConfigStore:
    return ConfigStore(Path(args.config).expanduser() if args.config else None)


def _load(args: argparse.Namespace) -> tuple[ConfigStore, Config]:
    store = _store(args)
    return store, store.load()


def _new_http() -> httpx.AsyncClient:
    from .service import HTTP_TIMEOUT

    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True)


def _new_browser(config: Config) -> Any:
    from .browser import BrowserManager

    return BrowserManager(config.browser)


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# ------------------------------------------------------------------ run

def cmd_run(args: argparse.Namespace) -> int:
    from .db import Database
    from .logging_setup import collect_secrets, setup_logging
    from .service import StockWatchService

    paths.ensure_dirs()
    redactor = setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    try:
        store, config = _load(args)
    except ConfigError as e:
        log.error("config error: %s", e)
        return 2
    redactor.add_secrets(collect_secrets(config))
    if config.missing_secrets:
        log.warning("missing secrets (set them in %s or the Keychain): %s",
                    paths.env_path(), ", ".join(config.missing_secrets))

    async def _main() -> None:
        db = Database(paths.db_path())
        try:
            await StockWatchService(store, db, redactor=redactor).run()
        finally:
            db.close()

    asyncio.run(_main())
    return 0


# ------------------------------------------------------------------ check

def _url_watch(url: str) -> WatchCfg:
    retailer, idents = identifiers_from_url(url)
    return WatchCfg(id="url-check", name="URL check", targets=[Target(retailer, idents, watch_id="url-check")])


async def dry_run_watch(config: Config, watch: WatchCfg, *, out: TextIO | None = None) -> int:
    """Check every target once and print StockResult JSON. No DB writes, no alerts, no cart."""
    out = out or sys.stdout
    from .service import dry_check

    http = _new_http()
    browser = _new_browser(config)
    try:
        adapters = build_adapters(config, http, browser)
        rows = []
        for t in watch.targets:
            adapter = adapters.get(t.retailer)
            if adapter is None:
                rows.append({"retailer": t.retailer, "identifiers": t.identifiers,
                             "result": {"error": "config", "error_detail": "no adapter"}})
                continue
            result = await dry_check(adapter, t, config.effective_home(watch))
            try:
                url = adapter.product_url(t)
            except Exception:  # noqa: BLE001
                url = t.get("url")
            rows.append({"watch_id": watch.id, "retailer": t.retailer, "identifiers": t.identifiers,
                         "product_url": url, "result": result.to_dict()})
        print(json.dumps(rows, indent=2, default=str), file=out)
        return 1 if any(r["result"].get("error") for r in rows) else 0
    finally:
        if hasattr(browser, "close"):
            await browser.close()
        await http.aclose()


async def _live_check(store: ConfigStore, watch_id: str) -> int:
    """Real check through the service: records to the DB and sends alerts like the scheduler would."""
    from .db import Database
    from .service import StockWatchService

    paths.ensure_dirs()
    db = Database(paths.db_path())
    svc = StockWatchService(store, db, enable_dashboard=False, enable_telegram=False,
                            enable_health=False, enable_power=False)
    try:
        print(await svc.check_now(watch_id))
        await svc.wait_background(timeout=60)
        return 0
    finally:
        await svc.stop()
        db.close()


def cmd_check(args: argparse.Namespace) -> int:
    if not args.watch_id and not args.url:
        _err("give a watch id or --url URL")
        return 2
    try:
        store, config = _load(args)
    except ConfigError as e:
        _err(str(e))
        return 2
    if args.url:
        try:
            watch = _url_watch(args.url)
        except ValueError as e:
            _err(str(e))
            return 2
        return asyncio.run(dry_run_watch(config, watch))
    watch = config.watch(args.watch_id)
    if watch is None:
        _err(f"no watch {args.watch_id!r} (known: {', '.join(w.id for w in config.watches) or 'none'})")
        return 2
    if args.dry_run:
        return asyncio.run(dry_run_watch(config, watch))
    return asyncio.run(_live_check(store, watch.id))


# ------------------------------------------------------------------ doctor

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class DoctorRow:
    status: str
    check: str
    detail: str = ""


def _mode_ok(path: Path, mode: int) -> bool:
    return stat.S_IMODE(path.stat().st_mode) & ~mode == 0


async def run_doctor(store: ConfigStore, *, network: bool = True, browser: bool = True,
                     checks: bool = True) -> list[DoctorRow]:
    """Collect doctor rows (spec 9). Never prints secrets: only variable names."""
    rows: list[DoctorRow] = []
    add = lambda s, c, d="": rows.append(DoctorRow(s, c, d))  # noqa: E731

    try:
        config = store.load()
    except (ConfigError, ValueError, OSError) as e:
        add(FAIL, "config", str(e))
        return rows
    except Exception as e:  # noqa: BLE001 - e.g. YAML syntax error
        add(FAIL, "config", f"{type(e).__name__}: {e}")
        return rows
    ntargets = sum(len(w.targets) for w in config.watches)
    add(PASS, "config", f"{store.path}: {len(config.watches)} watches, {ntargets} targets")

    # ---- secrets and channel config
    if config.missing_secrets:
        add(WARN, "secrets", "not set: " + ", ".join(config.missing_secrets))
    else:
        add(PASS, "secrets", "all ${VAR} references resolved")
    n = config.notify
    if n.pushover.enabled:
        ok = bool(n.pushover.user_key and n.pushover.app_token)
        add(PASS if ok else FAIL, "pushover config", "" if ok else "user_key/app_token missing")
    else:
        add(WARN, "pushover config", "disabled: no alert can break through silent mode")
    if n.telegram.enabled:
        ok = bool(n.telegram.bot_token and n.telegram.chat_id)
        add(PASS if ok else FAIL, "telegram config", "" if ok else "bot_token/chat_id missing")
    used = sorted({t.retailer for w in config.watches for t in w.targets})
    if "bestbuy" in used:
        rc = config.retailers.get("bestbuy")
        ok = bool(rc and rc.api_key)
        add(PASS if ok else FAIL, "bestbuy api key", "" if ok else "retailers.bestbuy.api_key not set")
    add(PASS if config.dashboard.pin else WARN, "dashboard pin",
        "" if config.dashboard.pin else "no PIN: dashboard listens on 127.0.0.1 only")
    add(PASS if config.health.heartbeat_url else WARN, "heartbeat",
        "" if config.health.heartbeat_url else "health.heartbeat_url not set: a dead Mac goes unnoticed")
    bad_ids = [f"{w.id}/{t.retailer}" for w in config.watches for t in w.targets
               if any("FILL_IN" in v or (v.isupper() and "_" in v) for v in t.identifiers.values())]
    if bad_ids:
        add(WARN, "placeholders", "placeholder identifiers still in config: " + ", ".join(bad_ids))

    # ---- file permissions
    env = paths.env_path()
    if env.exists():
        ok = _mode_ok(env, 0o600)
        add(PASS if ok else FAIL, ".env permissions",
            f"{env}" if ok else f"{env} is readable by others: chmod 600 {env}")
    else:
        add(WARN, ".env permissions", f"{env} not found (secrets must then come from env or Keychain)")
    prof = paths.chrome_profile_dir()
    if prof.exists():
        ok = _mode_ok(prof, 0o700)
        add(PASS if ok else FAIL, "chrome profile permissions",
            str(prof) if ok else f"chmod 700 {prof}")
    else:
        add(WARN, "chrome profile permissions", f"{prof} missing: run `stockwatch setup-profile`")

    needs_browser = any(r in BROWSER_RETAILERS for r in used) or any(w.add_to_cart for w in config.watches)
    if config.browser.channel == "chrome":
        found = any(p.exists() for p in CHROME_APPS)
        add(PASS if found else (FAIL if needs_browser else WARN), "google chrome",
            "installed" if found else "Google Chrome not found in /Applications")

    # ---- network reachability
    if not network:
        add(SKIP, "pushover/telegram reachability", "--offline")
    else:
        from .notify.pushover import PushoverClient
        from .notify.telegram import TelegramClient, TelegramError

        async with _new_http() as http:
            if n.pushover.enabled and n.pushover.user_key and n.pushover.app_token:
                ok, detail = await PushoverClient(n.pushover, http).validate()
                add(PASS if ok else FAIL, "pushover validate", detail)
            if n.telegram.enabled and n.telegram.bot_token:
                try:
                    me = await TelegramClient(n.telegram, http).get_me()
                    add(PASS, "telegram getMe", f"@{me.get('username', '?')}")
                except TelegramError as e:
                    add(FAIL, "telegram getMe", str(e))

    # ---- sign-in state and dry checks (these use the network and possibly Chrome)
    br = None
    try:
        if network and browser and used:
            br = _new_browser(config)
            try:
                status = await br.signin_status(used)
            except Exception as e:  # noqa: BLE001 - Chrome missing, Playwright not installed, ...
                add(FAIL, "sign-in status", f"{type(e).__name__}: {e}")
            else:
                for r in used:
                    s = status.get(r)
                    add(PASS if s else WARN, f"signed in: {r}",
                        "yes" if s else ("no: run `stockwatch setup-profile`" if s is False else "unknown"))
        else:
            add(SKIP, "sign-in status", "--offline/--no-browser" if used else "no targets")
        if network and checks and used:
            from .service import describe_result, dry_check

            async with _new_http() as http:
                if br is None:
                    br = _new_browser(config)
                adapters = build_adapters(config, http, br)
                for w in config.watches:
                    for t in w.targets:
                        adapter = adapters.get(t.retailer)
                        if adapter is None:
                            add(FAIL, f"check {w.id}/{t.retailer}", "no adapter")
                            continue
                        res = await dry_check(adapter, t, config.effective_home(w))
                        status_ = PASS if not res.error else (WARN if res.error == "blocked" else FAIL)
                        add(status_, f"check {w.id}/{t.retailer}", describe_result(res))
        else:
            add(SKIP, "dry checks", "--offline/--no-checks" if used else "no targets")
    finally:
        if br is not None and hasattr(br, "close"):
            await br.close()
    return rows


def print_table(rows: list[DoctorRow], out: TextIO | None = None) -> None:
    out = out or sys.stdout
    width = max((len(r.check) for r in rows), default=10)
    for r in rows:
        print(f"{r.status:<4}  {r.check:<{width}}  {r.detail}".rstrip(), file=out)
    counts = {s: sum(1 for r in rows if r.status == s) for s in (PASS, WARN, FAIL, SKIP)}
    print(f"\n{counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail, {counts[SKIP]} skipped", file=out)


def cmd_doctor(args: argparse.Namespace) -> int:
    rows = asyncio.run(run_doctor(_store(args), network=not args.offline,
                                  browser=not args.no_browser, checks=not args.no_checks))
    print_table(rows)
    return 1 if any(r.status == FAIL for r in rows) else 0


# ------------------------------------------------------------------ test-alert

def cmd_test_alert(args: argparse.Namespace) -> int:
    from .db import Database
    from .notify.notifier import Notifier

    try:
        _, config = _load(args)
    except ConfigError as e:
        _err(str(e))
        return 2

    async def _main() -> int:
        paths.ensure_dirs()
        db = Database(paths.db_path())
        async with _new_http() as http:
            notifier = Notifier(config, http, db)
            outcomes = await notifier.test_all()
            await notifier.mac_sound.wait()
        db.close()
        if not outcomes:
            print("No notification channels are enabled.")
            return 1
        for o in outcomes:
            print(f"{'ok' if o.ok else 'FAILED':<6} {o.channel}{': ' + o.detail if o.detail and not o.ok else ''}")
        return 0 if all(o.ok for o in outcomes) else 1

    return asyncio.run(_main())


# ------------------------------------------------------------------ simulate

def simulated_result(watch: WatchCfg, retailer: str, max_price: float | None) -> "StockResult":
    """An in-stock StockResult that qualifies as critical for `watch` (first fulfillment channel)."""
    from .models import ChannelAvail, StockResult

    channel = watch.fulfillment[0] if watch.fulfillment else "store_pickup"
    local = channel != "ship"
    price = round(max_price - 50, 2) if max_price and max_price > 60 else 449.99
    return StockResult(
        retailer=retailer, in_stock=True, price=price, raw_status="IN_STOCK",
        product_name=watch.name,
        channels=[ChannelAvail(
            channel=channel, available=True, low_stock=True,
            store_id="SIM-1" if local else None,
            store_name="Simulated Store" if local else None,
            distance_miles=1.0 if local else None,
        )],
    )


def cmd_simulate(args: argparse.Namespace) -> int:
    """Push a fake restock through the real dedupe -> alert pipeline (in-memory DB, nothing persisted)."""
    import dataclasses

    from .db import Database
    from .models import StockResult
    from .service import StockWatchService

    try:
        store, config = _load(args)
    except ConfigError as e:
        _err(str(e))
        return 2
    watch = config.watch(args.watch_id) if args.watch_id else (config.watches[0] if config.watches else None)
    if watch is None:
        _err(f"no watch {args.watch_id!r}" if args.watch_id else "config has no watches")
        return 2
    targets = [t for t in watch.targets if not args.retailer or t.retailer == args.retailer]
    if not targets:
        _err(f"watch {watch.id} has no {args.retailer or ''} target".replace("  ", " "))
        return 2
    target = targets[0]
    sim_watch = dataclasses.replace(watch, name=f"[TEST] {watch.name}", add_to_cart=bool(args.cart))

    async def _main() -> int:
        db = Database(":memory:")
        async with _new_http() as http:
            svc = StockWatchService(store, db, http=http, enable_dashboard=False, enable_telegram=False,
                                    enable_health=False, enable_power=False)
            try:
                print(f"simulating restock: {watch.id} @ {target.retailer} "
                      f"(cart attempt: {'YES, real browser' if args.cart else 'no'})")
                await svc._on_result(target, sim_watch, StockResult(
                    retailer=target.retailer, in_stock=False, raw_status="OUT_OF_STOCK"))
                await svc._on_result(target, sim_watch, simulated_result(
                    sim_watch, target.retailer, config.effective_max_price(watch)))
                await svc.wait_background(timeout=config.browser.cart_timeout_seconds + 15)
                await svc.notifier.mac_sound.wait()
            finally:
                if args.cart:
                    await svc.browser.close()
        rows = db.recent_alerts(20)
        db.close()
        if not rows:
            print("No alert was sent: no notification channel is enabled, or the watch's settings "
                  "made the simulated stock non-qualifying.")
            return 1
        for r in reversed(rows):
            print(f"{'ok' if r['status'] == 'sent' else r['status']:<8} {r['channel']}")
        return 0 if all(r["status"] == "sent" for r in rows) else 1

    return asyncio.run(_main())


# ------------------------------------------------------------------ init / setup-profile

def cmd_init(args: argparse.Namespace) -> int:
    paths.ensure_dirs()
    cfg_path = Path(args.config).expanduser() if args.config else paths.config_path()
    if cfg_path.exists():
        print(f"keeping existing {cfg_path}")
    elif not EXAMPLE_CONFIG.exists():
        _err(f"{EXAMPLE_CONFIG} not found (is the package installed from the repo with -e?)")
        return 1
    else:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE_CONFIG, cfg_path)
        print(f"created {cfg_path}: set home.zip and the watches' identifiers")
    env = paths.env_path()
    if env.exists():
        print(f"keeping existing {env}")
    else:
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(ENV_TEMPLATE)
        print(f"created {env} (chmod 600): fill in your secrets")
    os.chmod(env, 0o600)
    return 0


def cmd_setup_profile(args: argparse.Namespace,
                      wait: Callable[[str], Any] = input) -> int:
    try:
        _, config = _load(args)
        used = sorted({t.retailer for w in config.watches for t in w.targets})
    except ConfigError:
        from .config import BrowserCfg, Defaults, DashboardCfg, HealthCfg, NotifyCfg
        from .models import Home

        config = Config(Home("00000"), Defaults(), NotifyCfg(), DashboardCfg(), HealthCfg(), BrowserCfg(), {}, [])
        used = []
    unknown = [r for r in args.retailers if r not in RETAILERS]
    if unknown:
        _err(f"unknown retailer(s): {', '.join(unknown)} (known: {', '.join(RETAILERS)})")
        return 2
    retailers = args.retailers or used or list(RETAILERS)

    async def _main() -> int:
        paths.ensure_dirs()
        br = _new_browser(config)
        try:
            urls = await br.open_signin_pages(retailers)
            print("Opened the StockWatch Chrome profile on:")
            for u in urls:
                print(f"  {u}")
            print("Sign in to each retailer (including any 2FA). The bot never sees or stores passwords.")
            await asyncio.get_running_loop().run_in_executor(None, wait, "Press Enter when you are done... ")
            status = await br.signin_status(retailers)
            for r in retailers:
                s = status.get(r)
                print(f"  {r:<9} {'signed in' if s else ('NOT signed in' if s is False else 'unknown')}")
            return 0
        finally:
            await br.close()

    return asyncio.run(_main())


# ------------------------------------------------------------------ launchd / status

def cmd_install(args: argparse.Namespace) -> int:
    from . import launchd

    res = launchd.install(python=sys.executable)
    print(f"{res.plist}: {res.detail}")
    return 0 if res.loaded else 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    from . import launchd

    res = launchd.uninstall()
    print(f"{res.plist}: {res.detail}")
    return 0


def cmd_status(args: argparse.Namespace, out: TextIO | None = None) -> int:
    from .db import Database

    out = out or sys.stdout
    try:
        _, config = _load(args)
    except ConfigError as e:
        _err(str(e))
        return 2
    if sys.platform == "darwin":
        from . import launchd

        print(f"launchd agent: {'loaded' if launchd.is_loaded() else 'not loaded'}", file=out)
    dbp = paths.db_path()
    if not dbp.exists():
        print(f"no database yet at {dbp} (the service has not run)", file=out)
        return 0
    db = Database(dbp)
    try:
        for w in config.watches:
            print(f"\n{w.id} ({w.name}){'' if w.enabled else ' [paused]'}", file=out)
            for t in w.targets:
                found = db.conn.execute(
                    "SELECT id FROM targets WHERE watch_id=? AND retailer=? AND identifiers_json=?",
                    (w.id, t.retailer, json.dumps(t.identifiers, sort_keys=True))).fetchone()
                tid = found["id"] if found else None
                row = db.last_check(tid) if tid else None
                if row is None:
                    print(f"  {t.retailer:<9} not checked yet", file=out)
                    continue
                state = row["error"].upper() if row["error"] else (row["raw_status"] or
                                                                     ("IN_STOCK" if row["in_stock"] else "OUT"))
                price = f" ${row['price']:,.2f}" if row["price"] is not None else ""
                opened = [k for k in ("restock", "blocked", "error") if db.get_open_event(tid, k)]
                ev = f" [open: {', '.join(opened)}]" if opened else ""
                print(f"  {t.retailer:<9} {state}{price} at {row['checked_at']}{ev}", file=out)
    finally:
        db.close()
    return 0


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stockwatch", description="Local stock tracker and alert bot.")
    p.add_argument("--version", action="version", version=f"stockwatch {__version__}")
    config_help = "config.yaml path (default: ~/StockWatch/config.yaml or $STOCKWATCH_CONFIG)"
    p.add_argument("--config", help=config_help)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    # Same options after the subcommand; SUPPRESS keeps a top-level value from being overwritten.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=config_help)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=lambda **kw: argparse.ArgumentParser(
        parents=[common], **kw))

    sub.add_parser("run", help="run the service until SIGTERM/SIGINT").set_defaults(func=cmd_run)

    c = sub.add_parser("check", help="check one watch now, or dry-check a product URL")
    c.add_argument("watch_id", nargs="?", help="watch id from config.yaml")
    c.add_argument("--url", help="product URL to dry-check (implies --dry-run)")
    c.add_argument("--dry-run", action="store_true",
                   help="print StockResult JSON per target; no database writes, alerts or cart")
    c.set_defaults(func=cmd_check)

    d = sub.add_parser("doctor", help="verify config, secrets, channels, Chrome profile and adapters")
    d.add_argument("--offline", action="store_true", help="skip everything that touches the network or Chrome")
    d.add_argument("--no-browser", action="store_true", help="skip the sign-in status check")
    d.add_argument("--no-checks", action="store_true", help="skip the dry check per target")
    d.set_defaults(func=cmd_doctor)

    sub.add_parser("test-alert", help="fire a test alert through every channel").set_defaults(func=cmd_test_alert)
    sm = sub.add_parser("simulate", help="fake a restock to test the alert pipeline end to end (nothing saved)")
    sm.add_argument("watch_id", nargs="?", help="watch id (default: first watch)")
    sm.add_argument("--retailer", help="which target of the watch to simulate (default: first)")
    sm.add_argument("--cart", action="store_true",
                    help="also run the REAL add-to-cart in the Chrome profile (never checks out)")
    sm.set_defaults(func=cmd_simulate)
    sub.add_parser("init", help="create ~/StockWatch/config.yaml and .env templates").set_defaults(func=cmd_init)

    s = sub.add_parser("setup-profile", help="open the bot's Chrome profile on each retailer's sign-in page")
    s.add_argument("retailers", nargs="*", metavar="RETAILER",
                   help=f"subset of: {', '.join(RETAILERS)} (default: those in config)")
    s.set_defaults(func=cmd_setup_profile)

    sub.add_parser("install", help="install and load the launchd agent").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="unload and remove the launchd agent").set_defaults(func=cmd_uninstall)
    sub.add_parser("status", help="last check per target and open events").set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    if args.command != "run":
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                            format="%(levelname)s %(name)s: %(message)s")
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
