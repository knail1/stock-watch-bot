from __future__ import annotations

import asyncio
from typing import Any

import pytest

from stockwatch.telegram_bot import HELP_TEXT, TelegramBot

CHAT = "12345"


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.watches = {"switch2-local"}

    def _known(self, watch_id: str) -> None:
        if watch_id not in self.watches:
            raise KeyError(watch_id)

    async def status_text(self) -> str:
        self.calls.append(("status", None))
        return "switch2-local: ok"

    async def pause(self, watch_id: str) -> None:
        self._known(watch_id)
        self.calls.append(("pause", watch_id))

    async def resume(self, watch_id: str) -> None:
        self._known(watch_id)
        self.calls.append(("resume", watch_id))

    async def pause_all(self) -> None:
        self.calls.append(("pause_all", None))

    async def resume_all(self) -> None:
        self.calls.append(("resume_all", None))

    async def check_now(self, watch_id: str) -> str:
        self._known(watch_id)
        self.calls.append(("check", watch_id))
        return "bestbuy: out of stock"

    async def add_watch_from_url(self, url: str) -> str:
        if "unsupported" in url:
            raise ValueError("no adapter for this URL")
        self.calls.append(("add", url))
        return "new-watch"

    async def test_alert(self) -> str:
        self.calls.append(("test", None))
        return "pushover: ok"


class FakeClient:
    def __init__(self, batches: list[Any] | None = None) -> None:
        self.batches = list(batches or [])
        self.offsets: list[int | None] = []
        self.sent: list[str] = []

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        item = self.batches.pop(0) if self.batches else []
        if isinstance(item, Exception):
            raise item
        return item

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def msg(update_id: int, text: str, chat_id: Any = CHAT) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def make_bot(batches: list[Any] | None = None) -> tuple[TelegramBot, FakeControl, FakeClient, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    control, client = FakeControl(), FakeClient(batches)
    bot = TelegramBot(control, client, int(CHAT), sleep=fake_sleep)
    return bot, control, client, sleeps


async def test_offset_tracking_and_dispatch() -> None:
    bot, control, client, _ = make_bot([[msg(10, "/status"), msg(11, "/pause switch2-local")],
                                        [msg(12, "/resumeall")]])
    await bot.poll_once()
    assert bot.offset == 12
    await bot.poll_once()
    assert client.offsets == [None, 12]
    assert bot.offset == 13
    assert control.calls == [("status", None), ("pause", "switch2-local"), ("resume_all", None)]
    assert client.sent == ["switch2-local: ok", "Paused switch2-local.", "All watches resumed."]


async def test_unauthorised_chat_dropped(caplog: pytest.LogCaptureFixture) -> None:
    bot, control, client, _ = make_bot([[msg(5, "/pauseall", chat_id=999)]])
    await bot.poll_once()
    assert control.calls == []
    assert client.sent == []
    assert bot.offset == 6  # still consumed, so it is not redelivered
    assert "unauthorised chat 999" in caplog.text


async def test_non_command_and_non_message_updates_ignored() -> None:
    bot, control, client, _ = make_bot([[msg(1, "hello"), {"update_id": 2, "edited_message": {}}]])
    await bot.poll_once()
    assert control.calls == [] and client.sent == []
    assert bot.offset == 3


@pytest.mark.parametrize(
    ("text", "call", "reply_part"),
    [
        ("/resume switch2-local", ("resume", "switch2-local"), "Resumed switch2-local."),
        ("/pauseall", ("pause_all", None), "All watches paused."),
        ("/check switch2-local", ("check", "switch2-local"), "bestbuy: out of stock"),
        ("/add https://www.bestbuy.com/site/x/123.p?skuId=123",
         ("add", "https://www.bestbuy.com/site/x/123.p?skuId=123"), "Added watch new-watch."),
        ("/test", ("test", None), "pushover: ok"),
        ("/status@StockWatchBot", ("status", None), "switch2-local: ok"),
    ],
)
async def test_commands(text: str, call: tuple[str, Any], reply_part: str) -> None:
    bot, control, _, _ = make_bot()
    reply = await bot.execute(text)
    assert control.calls == [call]
    assert reply_part in reply


async def test_help_and_unknown() -> None:
    bot, _, _, _ = make_bot()
    assert await bot.execute("/help") == HELP_TEXT
    for cmd in ("/status", "/pause", "/check", "/add", "/test"):
        assert cmd in HELP_TEXT
    assert "Unknown command /foo" in await bot.execute("/foo")


async def test_argument_and_error_handling() -> None:
    bot, control, _, _ = make_bot()
    assert await bot.execute("/pause") == "Usage: /pause <id>"
    assert await bot.execute("/check") == "Usage: /check <id>"
    assert (await bot.execute("/add not-a-url")).startswith("Usage: /add")
    assert await bot.execute("/pause nope") == "Unknown watch: nope"
    assert await bot.execute("/add https://example.com/unsupported") == \
        "/add failed: no adapter for this URL"
    assert control.calls == []


async def test_unexpected_error_does_not_leak_message() -> None:
    bot, control, _, _ = make_bot()

    async def boom() -> str:
        raise RuntimeError("token=SECRET")

    control.status_text = boom  # type: ignore[method-assign]
    reply = await bot.execute("/status")
    assert reply == "/status failed: RuntimeError"
    assert "SECRET" not in reply


async def test_network_error_backs_off_exponentially_then_resets() -> None:
    bot, control, client, sleeps = make_bot(
        [OSError("down"), OSError("down"), OSError("down"), [msg(1, "/pauseall")], OSError("x")])
    for _ in range(5):
        await bot.poll_once()
    assert len(sleeps) == 4
    assert 4.0 <= sleeps[0] <= 6.0
    assert 8.0 <= sleeps[1] <= 12.0
    assert 16.0 <= sleeps[2] <= 24.0
    assert 4.0 <= sleeps[3] <= 6.0  # reset after a successful poll
    assert control.calls == [("pause_all", None)]
    assert client.offsets == [None, None, None, None, 2]


def test_backoff_capped() -> None:
    bot, _, _, _ = make_bot()
    assert all(bot.backoff_delay(n) <= bot.backoff_max for n in range(1, 30))


async def test_run_stops_and_handler_failure_still_advances_offset() -> None:
    bot, control, client, _ = make_bot()

    async def failing_send(text: str) -> None:
        raise RuntimeError("send failed")

    client.send_text = failing_send  # type: ignore[method-assign]
    client.batches = [[msg(7, "/pauseall")]]
    original = client.get_updates

    async def get_updates(offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        if offset == 8:
            bot.stop()
        await asyncio.sleep(0)
        return await original(offset, timeout)

    client.get_updates = get_updates  # type: ignore[method-assign]
    await asyncio.wait_for(bot.run(), timeout=2)
    assert bot.offset == 8
    assert control.calls == [("pause_all", None)]


async def test_run_without_chat_id_returns() -> None:
    bot = TelegramBot(FakeControl(), FakeClient(), "")
    await asyncio.wait_for(bot.run(), timeout=1)


async def test_stop_interrupts_backoff_sleep() -> None:
    client = FakeClient([OSError("down")])
    bot = TelegramBot(FakeControl(), client, CHAT, backoff_initial=1000, backoff_max=1000)
    task = asyncio.create_task(bot.poll_once())
    await asyncio.sleep(0.01)
    bot.stop()
    await asyncio.wait_for(task, timeout=1)
