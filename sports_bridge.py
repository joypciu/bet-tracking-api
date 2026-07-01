"""
sports_bridge.py (bet-tracking-api edition)
============================================
Lightweight bridge to the internal Stats API (realtime_data_fetch/stats_api.py).
No Redis dependency — this service is pure SQLite + HTTP.

Environment variables
---------------------
STATS_API_URL      Base URL of stats_api.py (e.g. http://localhost:8001).
STATS_API_TOKEN    Bearer token for stats_api (optional).
STATS_API_TIMEOUT  Max seconds per request (default 8.0).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_STATS_URL = os.getenv("STATS_API_URL", "http://localhost:8001").rstrip("/")
_TOKEN     = os.getenv("STATS_API_TOKEN", "").strip()
_TIMEOUT   = float(os.getenv("STATS_API_TIMEOUT", "15.0"))


class StatsBridgeHTTPError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _headers() -> dict:
    h = {"User-Agent": "bet-tracking-api/1.0"}
    if _TOKEN:
        h["Authorization"] = f"Bearer {_TOKEN}"
    return h


async def _get(path: str, params: dict) -> dict:
    if not _STATS_URL:
        raise StatsBridgeHTTPError(503, "STATS_API_URL not configured")
    url = f"{_STATS_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, params=params, headers=_headers())
    except httpx.TimeoutException as exc:
        raise StatsBridgeHTTPError(504, f"stats_api timeout after {_TIMEOUT:.1f}s") from exc
    if r.status_code == 404:
        raise StatsBridgeHTTPError(404, r.text)
    if r.status_code != 200:
        raise StatsBridgeHTTPError(r.status_code, r.text)
    return r.json()


async def market_check(
    event_id: str | None,
    date:     str | None,
    sport:    str | None,
    team:     str | None,
    opponent: str | None,
    player:   str | None,
    market:   str,
    pick:     str,
    line:     float | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"market": market, "pick": pick}
    if event_id: params["event_id"] = event_id
    if date:     params["date"]     = date
    if sport:    params["sport"]    = sport
    if team:     params["team"]     = team
    if opponent: params["opponent"] = opponent
    if player:   params["player"]   = player
    if line is not None: params["line"] = line
    return await _get("/stats/market-check", params)


async def fetch_prop_check(
    player:   str,
    market:   str,
    pick:     str,
    line:     float,
    event_id: str | None,
    date:     str | None,
    sport:    str | None,
    team:     str | None,
    opponent: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "player": player,
        "market": market,
        "pick":   pick,
        "line":   line,
    }
    if event_id: params["event_id"] = event_id
    if date:     params["date"]     = date
    if sport:    params["sport"]    = sport
    if team:     params["team"]     = team
    if opponent: params["opponent"] = opponent
    return await _get("/stats/prop-check", params)


async def fetch_live(team: str | None = None) -> dict[str, Any]:
    params = {}
    if team:
        params["team"] = team
    return await _get("/stats/live", params)


def is_available() -> bool:
    return bool(_STATS_URL)
