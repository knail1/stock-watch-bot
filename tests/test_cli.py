"""CLI tests: check --dry-run with a fake adapter, doctor on a temp config, init. No network."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from stockwatch import cli, paths
from stockwatch.adapters.base import BlockedError
from stockwatch.models import ChannelAvail, StockResult


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "StockWatch"
    h.mkdir()
    monkeypatch.setenv("STOCKWATCH_HOME", str(h))
    monkeypatch.delenv("STOCKWATCH_CONFIG", raising=False)
    for var in ("PUSHOVER_USER_KEY", "PUSHOVER_APP_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "DASHBOARD_PIN", "HEARTBEAT_URL", "BESTBUY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # Keep the doctor away from the real Keychain and the real Chrome install.
    monkeypatch.setattr("stockwatch.config._keychain_lookup", lambda name: None)
    chrome = tmp_path / "Google Chrome.app"
    chrome.mkdir()
    monkeypatch.setattr(cli, "CHROME_APPS", (chrome,))
    return h


def write_config(home: Path, *, pushover: bool = False, telegram: bool = False) -> Path:
    raw = {
        "home": {"zip": "75001", "radius_miles": 25},
        "notify": {
            "pushover": {"enabled": pushover, "user_key": "${PUSHOVER_USER_KEY}",
                         "app_token": "${PUSHOVER_APP_TOKEN}"},
            "telegram": {"enabled": telegram, "bot_token": "${TELEGRAM_BOT_TOKEN}", "chat_id": "42"},
            "mac_sound": {"enabled": False},
        },
        "dashboard": {"pin": "${DASHBOARD_PIN}"},
        "health": {"heartbeat_url": "${HEARTBEAT_URL}"},
        "retailers": {"bestbuy": {"api_key": "${BESTBUY_API_KEY}"}},
        "watches": [{"id": "w1", "name": "Switch 2", "targets": [{"retailer": "bestbuy", "sku": "6614313"}]}],
    }
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    env = home / ".env"
    env.write_text("PUSHOVER_USER_KEY=uuuuuuuuuuuuuuuuuuuuuuuuuuuuuu\nPUSHOVER_APP_TOKEN=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                   "TELEGRAM_BOT_TOKEN=123:secret-token\nDASHBOARD_PIN=482913\n"
                   "HEARTBEAT_URL=https://hc-ping.com/x\nBESTBUY_API_KEY=bbkey\n")
    os.chmod(env, 0o600)
    prof = home / "chrome-profile"
    prof.mkdir(exist_ok=True)
    os.chmod(prof, 0o700)
    return path


class FakeAdapter:
    name = "bestbuy"
    min_interval_seconds = 60

    def __init__(self, result: Any):
        self.result = result
        self.calls = 0

    async def check(self, target: Any, home: Any) -> StockResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def product_url(self, target: Any) -> str:
        return f"https://www.bestbuy.com/site/{target.get('sku')}.p?skuId={target.get('sku')}"


class FakeBrowser:
    closed = False

    async def close(self) -> None:
        FakeBrowser.closed = True

    async def signin_status(self, retailers: Any = None) -> dict[str, bool | None]:
        return {"bestbuy": True}


def patch_adapters(monkeypatch: pytest.MonkeyPatch, result: Any) -> FakeAdapter:
    fake = FakeAdapter(result)
    monkeypatch.setattr(cli, "build_adapters", lambda config, http, browser: {"bestbuy": fake})
    monkeypatch.setattr(cli, "_new_browser", lambda config: FakeBrowser())
    return fake


def test_check_dry_run_prints_json_without_db_or_alerts(home: Path, monkeypatch, capsys) -> None:
    write_config(home)
    result = StockResult("bestbuy", True, price=449.99, raw_status="IN_STOCK",
                         channels=[ChannelAvail("store_pickup", True, "281", "Frisco", 6.2)])
    fake = patch_adapters(monkeypatch, result)
    with respx.mock(assert_all_called=False) as mock:  # any real HTTP (alerts) would fail the test
        assert cli.main(["check", "w1", "--dry-run"]) == 0
        assert not mock.calls
    rows = json.loads(capsys.readouterr().out)
    assert fake.calls == 1
    assert rows[0]["retailer"] == "bestbuy" and rows[0]["identifiers"] == {"sku": "6614313"}
    assert rows[0]["result"]["in_stock"] is True
    assert rows[0]["result"]["channels"][0]["store_name"] == "Frisco"
    assert rows[0]["product_url"].startswith("https://www.bestbuy.com/site/6614313.p")
    assert not paths.db_path().exists()
    assert FakeBrowser.closed


def test_check_dry_run_reports_blocked_with_exit_1(home: Path, monkeypatch, capsys) -> None:
    write_config(home)
    patch_adapters(monkeypatch, BlockedError("HTTP 403"))
    assert cli.main(["--config", str(home / "config.yaml"), "check", "w1", "--dry-run"]) == 1
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["result"]["error"] == "blocked"


def test_check_url_and_bad_input(home: Path, monkeypatch, capsys) -> None:
    write_config(home)
    patch_adapters(monkeypatch, StockResult("bestbuy", False, raw_status="OUT_OF_STOCK"))
    assert cli.main(["check", "--url", "https://www.bestbuy.com/site/x/6620000.p?skuId=6620000"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["identifiers"]["sku"] == "6620000"
    assert cli.main(["check", "nope", "--dry-run"]) == 2
    assert cli.main(["check", "--url", "https://example.com/x"]) == 2
    assert cli.main(["check"]) == 2


def run_doctor(*args: str) -> tuple[int, dict[str, cli.DoctorRow], str]:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["doctor", *args])
    out = buf.getvalue()
    rows: dict[str, cli.DoctorRow] = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in ("PASS", "WARN", "FAIL", "SKIP"):
            name = parts[1].split("  ")[0].strip()
            rows[name] = cli.DoctorRow(parts[0], name, parts[1])
    return code, rows, out


def test_doctor_offline_on_good_config(home: Path) -> None:
    write_config(home)
    code, rows, out = run_doctor("--offline")
    assert code == 0, out
    assert rows["config"].status == "PASS"
    assert rows["secrets"].status == "PASS"
    assert rows[".env permissions"].status == "PASS"
    assert rows["chrome profile permissions"].status == "PASS"
    assert rows["bestbuy api key"].status == "PASS"
    assert rows["google chrome"].status == "PASS"
    assert rows["dry checks"].status == "SKIP"
    # Secrets never appear in the output.
    for secret in ("secret-token", "bbkey", "482913", "hc-ping.com/x"):
        assert secret not in out


def test_doctor_fails_on_loose_env_perms_and_missing_secrets(home: Path) -> None:
    write_config(home, pushover=True)
    env = home / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=x\n")
    os.chmod(env, 0o644)
    code, rows, out = run_doctor("--offline")
    assert code == 1
    assert rows[".env permissions"].status == "FAIL"
    assert rows["pushover config"].status == "FAIL"
    assert rows["bestbuy api key"].status == "FAIL"
    assert rows["secrets"].status == "WARN" and "PUSHOVER_USER_KEY" in out


def test_doctor_bad_config(home: Path) -> None:
    (home / "config.yaml").write_text("home: {}\n")
    code, rows, _ = run_doctor("--offline")
    assert code == 1 and rows["config"].status == "FAIL"


@respx.mock
def test_doctor_network_checks_and_dry_check(home: Path, monkeypatch) -> None:
    write_config(home, pushover=True, telegram=True)
    respx.post("https://api.pushover.net/1/users/validate.json").mock(
        return_value=httpx.Response(200, json={"status": 1, "devices": ["iphone"]}))
    respx.post(url__regex=r"https://api\.telegram\.org/bot.*/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "sw_bot"}}))
    patch_adapters(monkeypatch, StockResult("bestbuy", False, raw_status="OUT_OF_STOCK"))
    code, rows, out = run_doctor()
    assert code == 0, out
    assert rows["pushover validate"].status == "PASS"
    assert rows["telegram getMe"].status == "PASS" and "@sw_bot" in out
    assert rows["signed in: bestbuy"].status == "PASS"
    assert rows["check w1/bestbuy"].status == "PASS" and "OUT_OF_STOCK" in out


def test_init_creates_config_and_env(home: Path, capsys) -> None:
    assert cli.main(["init"]) == 0
    cfg = home / "config.yaml"
    assert cfg.exists() and "${PUSHOVER_USER_KEY}" in cfg.read_text()
    env = home / ".env"
    assert env.stat().st_mode & 0o777 == 0o600
    assert "BESTBUY_API_KEY=" in env.read_text()
    assert (home / "chrome-profile").stat().st_mode & 0o777 == 0o700
    assert cli.main(["init"]) == 0  # idempotent
    assert "keeping existing" in capsys.readouterr().out


def test_env_example_matches_template() -> None:
    repo = Path(__file__).resolve().parent.parent
    assert (repo / ".env.example").read_text() == cli.ENV_TEMPLATE


def test_example_config_is_valid() -> None:
    from stockwatch.config import SecretResolver, build_config

    raw = yaml.safe_load(cli.EXAMPLE_CONFIG.read_text())
    cfg = build_config(raw, SecretResolver(env_file=Path("/nonexistent"), use_keychain=False))
    assert cfg.defaults.quiet_hours is None
    assert cfg.watches[0].online_only_severity is None  # spec 7 default: info when local preferred
    assert cfg.online_only_severity(cfg.watches[0], "bestbuy") == "info"
    assert cfg.online_only_severity(cfg.watches[0], "nintendo") == "critical"
    assert cfg.watches[0].enabled is False  # placeholders until the user fills them in


def test_status_without_db(home: Path, capsys, monkeypatch) -> None:
    write_config(home)
    monkeypatch.setattr("sys.platform", "linux")
    assert cli.main(["status"]) == 0
    assert "no database yet" in capsys.readouterr().out


def test_help_and_no_command(capsys) -> None:
    assert cli.main([]) == 2
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "--help"])
    assert e.value.code == 0
    assert "--dry-run" in capsys.readouterr().out


@respx.mock
def test_simulate_sends_critical_restock_without_persisting(home: Path, capsys) -> None:
    write_config(home, telegram=True)
    route = respx.post(url__regex=r"https://api\.telegram\.org/bot.*/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
    assert cli.main(["simulate", "w1"]) == 0
    assert route.call_count == 1  # one critical alert, no cart follow-up without --cart
    text = json.loads(route.calls[0].request.content)["text"]
    assert "IN STOCK: [TEST] Switch 2 @ Best Buy" in text
    assert "Simulated Store" in text
    out = capsys.readouterr().out
    assert "ok       telegram" in out
    assert not (home / "stockwatch.db").exists()


def test_simulate_unknown_watch(home: Path, capsys) -> None:
    write_config(home)
    assert cli.main(["simulate", "nope"]) == 2


def test_simulated_result_is_local_and_under_max_price() -> None:
    from stockwatch.config import WatchCfg

    r = cli.simulated_result(WatchCfg(id="w", name="n", fulfillment=["store_pickup", "ship"]), "target", 499.99)
    assert r.in_stock and r.price == 449.99
    assert r.channels[0].channel == "store_pickup" and r.channels[0].distance_miles == 1.0
