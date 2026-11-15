"""Telegram remote-control bot (spec section 7, "Telegram as secondary and remote control").

Long-polls ``getUpdates`` through :class:`stockwatch.notify.telegram.TelegramClient`,
tracks the update offset so each update is handled once, and answers only the
configured chat. Messages from any other chat are logged and dropped without a reply.
Network/API failures back off exponentially; the loop never hot-retries.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

HELP_TEXT = (
    "StockWatch commands:\n"
    "/status - summary of all watches\n"
    "/pause <id> - pause a watch\n"
    "/resume <id> - resume a watch\n"
    "/pauseall - pause every watch\n"
    "/resumeall - resume every watch\n"
    "/check <id> - force an immediate check\n"
    "/add <product url> - create a watch from a URL with defaults\n"
    "/test - fire a test alert through every channel\n"
    "/help - this message"
)


class ServiceControl(Protocol):
    """Operations the bot needs from the service. Implemented by StockWatchService.

    Conventions: unknown watch ids raise ``KeyError``; invalid input (e.g. an
    unsupported URL) raises ``ValueError`` whose message is safe to show the user.
    """

    async def status_text(self) -> str: ...
    async def pause(self, watch_id: str) -> None: ...
    async def resume(self, watch_id: str) -> None: ...
    async def pause_all(self) -> None: ...
    async def resume_all(self) -> None: ...
    async def check_now(self, watch_id: str) -> str:
        """Run an immediate check; return a short human-readable result summary."""
        ...
    async def add_watch_from_url(self, url: str) -> str:
        """Create a watch with defaults from a product URL; return the new watch id."""
        ...
    async def test_alert(self) -> str:
        """Fire a test alert through every channel; return a short per-channel summary."""
        ...


class UpdateSource(Protocol):
    """The subset of TelegramClient the bot uses."""

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]: ...
    async def send_text(self, text: str) -> Any: ...


Handler = Callable[[str], Awaitable[str]]


class TelegramBot:
    """Long-polling command bot bound to a single authorised chat."""

    def __init__(
        self,
        control: ServiceControl,
        client: UpdateSource,
        chat_id: str | int,
        *,
        poll_timeout: int = 30,
        backoff_initial: float = 5.0,
        backoff_max: float = 300.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.control = control
        self.client = client
        self.chat_id = str(chat_id).strip()
        self.poll_timeout = poll_timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self._sleep = sleep
        self.offset: int | None = None
        self._failures = 0
        self._stopping = asyncio.Event()
        self._commands: dict[str, Handler] = {
            "status": self._cmd_status,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "pauseall": self._cmd_pauseall,
            "resumeall": self._cmd_resumeall,
            "check": self._cmd_check,
            "add": self._cmd_add,
            "test": self._cmd_test,
            "help": self._cmd_help,
            "start": self._cmd_help,
        }

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        """Poll until :meth:`stop` is called. Never raises except on cancellation."""
        if not self.chat_id:
            log.warning("telegram bot disabled: chat_id not configured")
            return
        log.info("telegram bot polling started")
        while not self._stopping.is_set():
            await self.poll_once()
        log.info("telegram bot polling stopped")

    def stop(self) -> None:
        self._stopping.set()

    async def poll_once(self) -> None:
        """One getUpdates round: fetch, advance offset, handle; back off on failure."""
        try:
            updates = await self.client.get_updates(self.offset, self.poll_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # network/API error: TelegramError already redacts the token
            self._failures += 1
            delay = self.backoff_delay(self._failures)
            log.warning("telegram getUpdates failed (%d in a row), retrying in %.0fs: %s",
                        self._failures, delay, e)
            await self._sleep_or_stop(delay)
            return
        self._failures = 0
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Advance before handling so a failing handler can't cause a redelivery loop.
                self.offset = max(self.offset or 0, update_id + 1)
            try:
                await self.handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram update %s handling failed", update_id)

    def backoff_delay(self, failures: int) -> float:
        """Exponential backoff with +/-20% jitter, capped at ``backoff_max``."""
        base = min(self.backoff_max, self.backoff_initial * 2 ** max(0, failures - 1))
        return min(self.backoff_max, base * random.uniform(0.8, 1.2))

    async def _sleep_or_stop(self, delay: float) -> None:
        stop_task = asyncio.ensure_future(self._stopping.wait())
        sleep_task = asyncio.ensure_future(self._sleep(delay))
        try:
            await asyncio.wait({stop_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (stop_task, sleep_task):
                t.cancel()

    # -------------------------------------------------------------- dispatch

    async def handle_update(self, update: dict[str, Any]) -> None:
        """Authorise and dispatch a single update; replies go to the configured chat only."""
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        sender_chat = str(chat.get("id", "")).strip()
        if sender_chat != self.chat_id:
            log.warning("telegram: ignoring message from unauthorised chat %s", sender_chat or "?")
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return
        reply = await self.execute(text)
        if reply:
            await self.client.send_text(reply)

    async def execute(self, text: str) -> str:
        """Run a command line such as ``/pause switch2-local`` and return the reply text."""
        head, _, rest = text.strip().partition(" ")
        name = head[1:].split("@", 1)[0].lower()
        arg = rest.strip()
        handler = self._commands.get(name)
        if handler is None:
            return f"Unknown command /{name}. Send /help for the list."
        try:
            return await handler(arg)
        except KeyError as e:
            missing = e.args[0] if e.args else arg
            return f"Unknown watch: {missing}"
        except ValueError as e:
            return f"/{name} failed: {e}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("telegram command /%s failed", name)
            return f"/{name} failed: {type(e).__name__}"

    # -------------------------------------------------------------- commands

    @staticmethod
    def _require(arg: str, usage: str) -> str | None:
        return None if arg else f"Usage: {usage}"

    async def _cmd_status(self, _: str) -> str:
        return await self.control.status_text() or "No watches configured."

    async def _cmd_pause(self, arg: str) -> str:
        if err := self._require(arg, "/pause <id>"):
            return err
        await self.control.pause(arg)
        return f"Paused {arg}."

    async def _cmd_resume(self, arg: str) -> str:
        if err := self._require(arg, "/resume <id>"):
            return err
        await self.control.resume(arg)
        return f"Resumed {arg}."

    async def _cmd_pauseall(self, _: str) -> str:
        await self.control.pause_all()
        return "All watches paused."

    async def _cmd_resumeall(self, _: str) -> str:
        await self.control.resume_all()
        return "All watches resumed."

    async def _cmd_check(self, arg: str) -> str:
        if err := self._require(arg, "/check <id>"):
            return err
        summary = await self.control.check_now(arg)
        return f"Checked {arg}:\n{summary}" if summary else f"Checked {arg}."

    async def _cmd_add(self, arg: str) -> str:
        if err := self._require(arg, "/add <product url>"):
            return err
        url = arg.split()[0]
        if not url.lower().startswith(("http://", "https://")):
            return "Usage: /add <product url> (must start with http:// or https://)"
        watch_id = await self.control.add_watch_from_url(url)
        return f"Added watch {watch_id}."

    async def _cmd_test(self, _: str) -> str:
        summary = await self.control.test_alert()
        return f"Test alert sent.\n{summary}" if summary else "Test alert sent."

    async def _cmd_help(self, _: str) -> str:
        return HELP_TEXT
