"""Adapter registry. Adding a retailer = add one module and one line here."""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import BaseAdapter, BrowserCtx

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config


def adapter_classes() -> dict[str, type[BaseAdapter]]:
    from .bestbuy import BestBuyAdapter
    from .gamestop import GameStopAdapter
    from .nintendo import NintendoAdapter
    from .target import TargetAdapter
    from .walmart import WalmartAdapter

    return {c.name: c for c in (BestBuyAdapter, TargetAdapter, WalmartAdapter, GameStopAdapter, NintendoAdapter)}


def build_adapters(config: "Config", http: httpx.AsyncClient, browser: BrowserCtx | None) -> dict[str, BaseAdapter]:
    out: dict[str, BaseAdapter] = {}
    for name, cls in adapter_classes().items():
        out[name] = cls(http=http, browser=browser, settings=config.retailers.get(name))
    return out


def retailer_for_url(url: str) -> str | None:
    for name, cls in adapter_classes().items():
        if cls.matches_url(url):
            return name
    return None


def identifiers_from_url(url: str) -> tuple[str, dict[str, str]]:
    """Return (retailer, identifiers) for a pasted product URL. Raises ValueError if unsupported."""
    retailer = retailer_for_url(url)
    if not retailer:
        raise ValueError(f"unsupported retailer URL: {url}")
    return retailer, adapter_classes()[retailer].identifiers_from_url(url)
