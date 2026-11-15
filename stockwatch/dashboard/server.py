"""Bind-address resolution and multi-host uvicorn serving (spec sections 10, 12).

The dashboard listens on 127.0.0.1 and, when configured, the Mac's Tailscale
IPv4 address. It must never listen on a wildcard address (0.0.0.0 / ::).
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import shutil
import socket
import subprocess
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import uvicorn

log = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"
TAILSCALE_APP_BIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
#: Tailscale assigns node addresses from the CGNAT range (IPv4) and this ULA prefix (IPv6).
TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


class BindError(ValueError):
    """Raised for a bind address that would expose the dashboard too widely."""


def tailscale_ipv4(timeout: float = 5.0) -> str | None:
    """Return the first IPv4 address from ``tailscale ip -4``, or None if unavailable."""
    exe = shutil.which("tailscale") or (TAILSCALE_APP_BIN if os.path.exists(TAILSCALE_APP_BIN) else None)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.split():
        try:
            addr = ipaddress.IPv4Address(line.strip())
        except ValueError:
            continue
        return str(addr)
    return None


def _validate_host(host: str) -> str:
    if host == "localhost":
        return LOOPBACK
    try:
        addr = ipaddress.ip_address(host)
    except ValueError as e:
        raise BindError(f"dashboard bind address must be an IP, 'localhost' or 'tailscale': {host!r}") from e
    if addr.is_unspecified:
        raise BindError(f"refusing to bind the dashboard to wildcard address {host}")
    if not (addr.is_loopback or any(addr in net for net in TAILSCALE_NETS)):
        # Spec 10/12: 127.0.0.1 and the Tailscale address only (plain HTTP is fine only
        # because WireGuard encrypts the tailnet link), never a LAN or public interface.
        raise BindError(f"refusing to bind the dashboard to {host}: only loopback and Tailscale "
                        "(100.64.0.0/10, fd7a:115c:a1e0::/48) addresses are allowed")
    return str(addr)


def resolve_bind_hosts(cfg: Any, *, tailscale_ip: Callable[[], str | None] = tailscale_ipv4) -> list[str]:
    """Turn ``dashboard.bind`` into concrete addresses.

    Accepts a Config or a DashboardCfg. Always includes 127.0.0.1 (first).
    ``"tailscale"`` expands to the ``tailscale ip -4`` result (skipped with a
    warning if Tailscale is not running). Wildcards raise BindError.
    """
    dash = getattr(cfg, "dashboard", cfg)
    hosts = [LOOPBACK]
    for entry in getattr(dash, "bind", None) or []:
        entry = str(entry).strip()
        if not entry:
            continue
        if entry.lower() == "tailscale":
            ip = tailscale_ip()
            if not ip:
                log.info("dashboard: 'tailscale' bind requested but no Tailscale IPv4 found yet")
                continue
            entry = ip
        host = _validate_host(entry)
        if host not in hosts:
            hosts.append(host)
    return hosts


def check_bind_safety(hosts: list[str], pin_configured: bool) -> None:
    """Without a PIN the dashboard may only listen on 127.0.0.1."""
    for h in hosts:
        _validate_host(h)
    if not pin_configured and any(h != LOOPBACK for h in hosts):
        raise BindError("dashboard PIN is not configured; refusing to listen on anything but 127.0.0.1 "
                        "(set dashboard.pin, e.g. ${DASHBOARD_PIN})")


class _EmbeddedServer(uvicorn.Server):
    """uvicorn server that leaves signal handling to the host service."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:  # type: ignore[override]
        yield


def _bind_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


async def serve(app: Any, hosts: list[str], port: int, *, log_level: str = "warning") -> None:
    """Run one uvicorn server per host until cancelled (or any server stops).

    Access logs are disabled because request URLs may carry ``?token=``.
    """
    if not hosts:
        raise BindError("no dashboard bind hosts")
    check_bind_safety(hosts, bool(getattr(app.state, "pin_configured", False)))
    sockets = []
    try:
        for h in hosts:
            sockets.append(_bind_socket(h, port))
    except OSError:
        for s in sockets:
            s.close()
        raise
    servers = [
        _EmbeddedServer(uvicorn.Config(app, log_level=log_level, access_log=False, server_header=False,
                                       proxy_headers=False, lifespan="off"))
        for _ in hosts
    ]
    tasks = [asyncio.create_task(srv.serve(sockets=[sock])) for srv, sock in zip(servers, sockets)]
    log.info("dashboard listening on %s port %d", ", ".join(hosts), port)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for srv in servers:
            srv.should_exit = True
        await asyncio.wait(tasks, timeout=10)
        for t in tasks:
            t.cancel()
        for s in sockets:
            s.close()
    for t in tasks:
        if t.done() and not t.cancelled() and t.exception():
            raise t.exception()  # type: ignore[misc]


#: Supervisor timing: retry a failed/missing bind after this long; re-resolve addresses this often.
SUPERVISE_RETRY_SECONDS = 30.0
SUPERVISE_REFRESH_SECONDS = 60.0


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


async def supervise(app: Any, resolve: Callable[[], Awaitable[list[str]]], port: int, *,
                    label: str = "dashboard",
                    retry_seconds: float = SUPERVISE_RETRY_SECONDS,
                    refresh_seconds: float = SUPERVISE_REFRESH_SECONDS,
                    serve_fn: Callable[[Any, list[str], int], Awaitable[None]] | None = None) -> None:
    """Keep ``app`` listening on the hosts ``resolve()`` returns, until cancelled.

    * nothing to bind yet (e.g. Tailscale not up at login): retry every ``retry_seconds``;
    * a bind/serve failure: log it and retry after ``retry_seconds`` (never gives up);
    * every ``refresh_seconds`` the hosts are re-resolved and the server rebinds when
      they change (Tailscale IP appeared, changed or went away).

    Only cancellation propagates; this coroutine never ends on its own.
    """
    serve_fn = serve_fn or serve
    task: asyncio.Task[None] | None = None
    hosts: list[str] = []
    try:
        while True:
            try:
                wanted = list(await resolve())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep what we have, try again later
                log.warning("%s: could not resolve bind addresses: %s", label, e)
                wanted = hosts if task is not None and not task.done() else []
            if task is not None and not task.done() and wanted != hosts:
                log.info("%s: bind addresses changed (%s -> %s); rebinding", label,
                         ", ".join(hosts) or "none", ", ".join(wanted) or "none")
                await _cancel(task)
                task = None
            if task is None and wanted:
                hosts = wanted
                task = asyncio.create_task(serve_fn(app, hosts, port), name=f"{label}-serve")
            if task is None:
                hosts = []
                await asyncio.sleep(retry_seconds)
                continue
            done, _ = await asyncio.wait({task}, timeout=refresh_seconds)
            if done:
                exc = None if task.cancelled() else task.exception()
                why = f"{type(exc).__name__}: {exc}" if exc else "server exited"
                log.error("%s on %s stopped (%s); retrying in %.0fs", label, ", ".join(hosts), why, retry_seconds)
                task = None
                await asyncio.sleep(retry_seconds)
    finally:
        if task is not None:
            await _cancel(task)
