"""
historics_bridge.py
=====================
Fetches per-book and nvig odds timelines from bettor-odds GET /api/historics.

The bet row stores only the JWT `context` token (data-historics). At CLV time we
call:  {HISTORICS_API_URL}?context={historics_context}

Environment variables
---------------------
HISTORICS_API_URL   Base URL (default: https://app.keepbetting.co/api/historics).
HISTORICS_API_KEY   Optional auth header if the historics proxy requires it.
HISTORICS_TIMEOUT   Request timeout seconds (default 12.0).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_HISTORICS_URL = os.getenv(
    "HISTORICS_API_URL",
    "https://app.keepbetting.co/api/historics",
).rstrip("/")
_HISTORICS_KEY = os.getenv("HISTORICS_API_KEY", "").strip()
_TIMEOUT = float(os.getenv("HISTORICS_TIMEOUT", "12.0"))


class HistoricsBridgeHTTPError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _headers() -> dict[str, str]:
    h = {"User-Agent": "bet-tracking-api/1.0"}
    if _HISTORICS_KEY:
        h["analytical-auth-key"] = _HISTORICS_KEY
    return h


async def fetch_historics(context: str) -> dict[str, Any]:
    """Return historics payload: books, nvig, limit, title, ..."""
    if not context or not context.strip():
        raise HistoricsBridgeHTTPError(400, "historics context is required")
    if not _HISTORICS_URL:
        raise HistoricsBridgeHTTPError(503, "HISTORICS_API_URL not configured")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                _HISTORICS_URL,
                params={"context": context.strip()},
                headers=_headers(),
            )
    except httpx.TimeoutException as exc:
        raise HistoricsBridgeHTTPError(
            504, f"historics API timeout after {_TIMEOUT:.1f}s"
        ) from exc

    if r.status_code != 200:
        raise HistoricsBridgeHTTPError(r.status_code, r.text[:500])

    data = r.json()
    if not isinstance(data, dict):
        raise HistoricsBridgeHTTPError(502, "historics API returned non-object JSON")
    return data


def is_available() -> bool:
    return bool(_HISTORICS_URL)
