"""Local alarm on the Mac: `afplay` the alarm file N times, then `say` the alert.

Playback runs in a background task so callers never wait on it. Commands are
spawned with argv lists (no shell); the spoken text is a single argv element
after `--` so it can never be interpreted as options.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

from .. import paths
from ..config import MacSoundCfg
from ..models import Alert, AlertOutcome

log = logging.getLogger(__name__)

CHANNEL = "mac_sound"
FALLBACK_SOUND = Path("/System/Library/Sounds/Sosumi.aiff")
MAX_SPEAK = 300
PLAY_TIMEOUT = 60.0


def resolve_sound_file(file: str) -> Path | None:
    """Find the configured alarm file, falling back to the system Sosumi sound.

    Relative paths are tried against the repo root, then ~/StockWatch.
    """
    candidates: list[Path] = []
    if file:
        p = Path(file).expanduser()
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates += [paths.assets_dir().parent / p, paths.home_dir() / p]
    candidates.append(FALLBACK_SOUND)
    return next((c for c in candidates if c.is_file()), None)


class MacSound:
    def __init__(self, cfg: MacSoundCfg, *, platform: str | None = None):
        self.cfg = cfg
        self.platform = platform or sys.platform
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def available(self) -> bool:
        return self.platform == "darwin"

    async def play(self, alert: Alert) -> AlertOutcome:
        """Start playback in the background and return immediately. Never raises."""
        if not self.available:
            return AlertOutcome(CHANNEL, False, "skipped: not macOS")
        sound = resolve_sound_file(self.cfg.file)
        speak = (alert.speak or alert.title) if self.cfg.speak else None
        task = asyncio.create_task(self._run(sound, max(0, int(self.cfg.repeat)), speak))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return AlertOutcome(CHANNEL, True, f"playing {sound.name if sound else 'speech only'}")

    async def wait(self) -> None:
        """Wait for any in-flight playback (tests, shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        """Cancel pending playback and kill the current sound."""
        for t in list(self._tasks):
            t.cancel()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        await self.wait()

    async def _run(self, sound: Path | None, repeat: int, speak: str | None) -> None:
        async with self._lock:  # one alarm at a time; later alerts queue behind
            try:
                if sound and shutil.which("afplay"):
                    for _ in range(repeat):
                        await self._exec("afplay", str(sound))
                if speak and shutil.which("say"):
                    await self._exec("say", "--", speak[:MAX_SPEAK])
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - sound is best effort
                log.warning("mac sound failed: %s", e)

    async def _exec(self, *argv: str) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(self._proc.wait(), PLAY_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if self._proc.returncode is None:
                self._proc.kill()
            raise
