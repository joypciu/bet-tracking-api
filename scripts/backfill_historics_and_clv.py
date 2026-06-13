#!/usr/bin/env python3
"""
Backfill historics_context and CLV on user_bets (production-safe).

Phase 1 — Reconstruct historics JWT from existing row fields (same payload as
          bettor-odds data-historics) for rows missing historics_context.

Phase 2 — For settled bets (win/loss/push/void) with historics_context + odds,
          fetch historics API and write book_clv / nvig_clv.

Dry-run by default. Use --apply to write.

Environment (bet-tracking-api/.env or shell):
  BET_DB_PATH           SQLite path (default: ../bets.db)
  API_CONTEXT_SECRET    JWT secret (same as bettor-odds) — required for phase 1
  HISTORICS_API_URL     default https://app.keepbetting.co/api/historics
  HISTORICS_API_KEY     optional
  HISTORICS_TIMEOUT     optional

Examples:
  python scripts/backfill_historics_and_clv.py
  python scripts/backfill_historics_and_clv.py --apply
  python scripts/backfill_historics_and_clv.py --apply --clv-only
  python scripts/backfill_historics_and_clv.py --apply --historics-only
  python scripts/backfill_historics_and_clv.py --apply --force-clv --limit 10
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import jwt
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

import bet_tracking  # noqa: E402
import historics_bridge  # noqa: E402

log = logging.getLogger("backfill_historics_clv")

SETTLED_STATUSES = ("win", "loss", "push", "void")

# fmtMarket-style display names for historics JWT (spread/total need aliases)
_DB_TO_DISPLAY: dict[str, str] = {
    "spread": "Game Spread",
    "total": "Game Total",
    "moneyline": "Moneyline",
    "puck_line": "Puck Line",
    "run_line": "Run Line",
}


# ---------------------------------------------------------------------------
# CLV math (mirrors main.py — kept local to avoid loading FastAPI)
# ---------------------------------------------------------------------------


def _american_to_decimal(american: int) -> float:
    if american > 0:
        return (american / 100) + 1.0
    return (100 / abs(american)) + 1.0


def _compute_nvig_clv_from_fair(bet_odds: int, nvig_odds: int) -> float | None:
    try:
        bet_dec = _american_to_decimal(bet_odds)
        fair_prob = 1.0 / _american_to_decimal(nvig_odds)
        return round(fair_prob * bet_dec - 1.0, 4)
    except (ZeroDivisionError, ValueError, TypeError):
        return None


def _compute_book_clv(bet_odds: int, closing_pick_odds: int) -> float | None:
    try:
        bet_dec = _american_to_decimal(bet_odds)
        closing_dec = _american_to_decimal(closing_pick_odds)
        if closing_dec <= 0:
            return None
        return round(bet_dec / closing_dec - 1.0, 4)
    except (ZeroDivisionError, ValueError, TypeError):
        return None


def _normalize_book_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _last_american_odds(series: list | None) -> int | None:
    if not series:
        return None
    for point in reversed(series):
        if not isinstance(point, dict):
            continue
        try:
            val = point.get("american_odds")
            if val is not None:
                return int(val)
        except (ValueError, TypeError):
            continue
    return None


def _find_book_series(books: dict, book_name: str | None) -> list | None:
    if not books or not book_name:
        return None
    target = _normalize_book_key(book_name)
    for key, series in books.items():
        if _normalize_book_key(str(key)) == target:
            return series if isinstance(series, list) else None
    return None


# ---------------------------------------------------------------------------
# historics_context reconstruction
# ---------------------------------------------------------------------------


def _load_jwt_secret() -> str:
    secret = os.environ.get("API_CONTEXT_SECRET", "").strip()
    if secret:
        return secret
    for candidate in (
        ROOT.parent / "bettor-odds-web-app-dev" / ".env",
        ROOT.parent / "bettor-odds-web-app" / ".env",
    ):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_CONTEXT_SECRET="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _db_market_to_display(market: str | None) -> str:
    """DB snake_case → historics display label (same rules as frontend fmtMarket)."""
    m = (market or "").strip().lower()
    if not m:
        return ""
    if m in _DB_TO_DISPLAY:
        return _DB_TO_DISPLAY[m]
    parts: list[str] = []
    for word in m.replace("_", " ").split():
        if re.fullmatch(r"\d+(st|nd|rd|th)", word, re.IGNORECASE):
            parts.append(word.lower())
        else:
            parts.append(word.capitalize())
    return " ".join(parts)


def _historics_date(event_datetime: str | None, date: str | None) -> str | None:
    raw = (event_datetime or "").strip()
    if raw:
        s = raw.replace("T", " ")
        if s.endswith("+00:00"):
            s = s[: -len("+00:00")] + "Z"
        elif "+" in s:
            s = s.split("+", 1)[0] + "Z"
        elif not s.endswith("Z"):
            s = s + "Z"
        return s
    if date:
        return f"{date} 00:00:00Z"
    return None


def _prop_from_row(row: sqlite3.Row) -> str | None:
    prop = (row["selection_line"] or "").strip()
    if prop:
        return prop
    market = (row["market"] or "").strip().lower()
    pick = (row["pick"] or "").strip()
    player = (row["player"] or "").strip()
    line = row["line"]
    if market == "moneyline" and pick:
        return pick
    if player and pick:
        line_s = f" {line:g}" if line is not None else ""
        return f"{player} {pick.capitalize()}{line_s}".strip()
    if pick and line is not None:
        if market in ("spread", "run_line", "puck_line"):
            return f"{pick} {line:+g}"
        if market.startswith("total") or pick.lower() in ("over", "under"):
            return f"{pick.capitalize()} {line:g}"
    return None


def _skip_reason(row: sqlite3.Row) -> str:
    reasons: list[str] = []
    if not (row["event"] or "").strip():
        reasons.append("no event")
    if not (row["league"] or "").strip():
        reasons.append("no league")
    if not (row["book"] or "").strip():
        reasons.append("no book")
    if not _prop_from_row(row):
        reasons.append("no prop/selection_line")
    if not _historics_date(row["event_datetime"], row["date"]):
        reasons.append("no date")
    return ", ".join(reasons) or "unknown"


def build_historics_context(row: sqlite3.Row, secret: str) -> str | None:
    prop = _prop_from_row(row)
    event = (row["event"] or "").strip()
    league = (row["league"] or "").strip()
    book = (row["book"] or "").strip()
    market = _db_market_to_display(row["market"] or "")
    dt = _historics_date(row["event_datetime"], row["date"])
    if not all([prop, event, league, market, dt, book]):
        return None
    payload = {
        "event": event,
        "league": league,
        "market": market,
        "prop": prop,
        "date": dt,
        "books": [book],
    }
    return jwt.encode(payload, secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# CLV fetch + compute
# ---------------------------------------------------------------------------


@dataclass
class ClvResult:
    bet_id: str
    ok: bool
    book_clv: float | None = None
    nvig_clv: float | None = None
    error: str | None = None


async def compute_clv_for_row(row: sqlite3.Row) -> ClvResult:
    bet_id = row["bet_id"]
    context = (row["historics_context"] or "").strip()
    if not context:
        return ClvResult(bet_id, False, error="no historics_context")
    if row["odds"] is None:
        return ClvResult(bet_id, False, error="no odds")
    try:
        bet_odds_int = int(row["odds"])
    except (ValueError, TypeError):
        return ClvResult(bet_id, False, error="invalid odds")

    try:
        historics_data = await historics_bridge.fetch_historics(context)
    except historics_bridge.HistoricsBridgeHTTPError as exc:
        return ClvResult(bet_id, False, error=f"historics HTTP {exc.status_code}")
    except Exception as exc:
        return ClvResult(bet_id, False, error=str(exc)[:200])

    books = historics_data.get("books") or {}
    book_series = _find_book_series(books, row["book"])
    book_closing = _last_american_odds(book_series)
    nvig_closing = _last_american_odds(historics_data.get("nvig"))

    book_clv = (
        _compute_book_clv(bet_odds_int, book_closing)
        if book_closing is not None
        else None
    )
    nvig_clv = (
        _compute_nvig_clv_from_fair(bet_odds_int, nvig_closing)
        if nvig_closing is not None
        else None
    )
    if book_clv is None and nvig_clv is None:
        return ClvResult(bet_id, False, error="no closing lines in historics")
    return ClvResult(bet_id, True, book_clv=book_clv, nvig_clv=nvig_clv)


@dataclass
class RunStats:
    historics_built: int = 0
    historics_skipped: int = 0
    historics_updated: int = 0
    clv_ok: int = 0
    clv_failed: int = 0
    clv_updated: int = 0
    clv_errors: list[str] = field(default_factory=list)


async def run_clv_batch(
    rows: list[sqlite3.Row],
    *,
    apply: bool,
    concurrency: int,
    delay: float,
) -> RunStats:
    stats = RunStats()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(row: sqlite3.Row) -> None:
        async with sem:
            result = await compute_clv_for_row(row)
            if delay > 0:
                await asyncio.sleep(delay)
            if result.ok:
                stats.clv_ok += 1
                if apply:
                    bet_tracking.update_bet_clv(
                        result.bet_id, result.book_clv, result.nvig_clv
                    )
                    stats.clv_updated += 1
                log.info(
                    "CLV %s book=%s nvig=%s",
                    result.bet_id[:8],
                    result.book_clv,
                    result.nvig_clv,
                )
            else:
                stats.clv_failed += 1
                msg = f"{result.bet_id[:8]}... {result.error}"
                stats.clv_errors.append(msg)
                log.warning("CLV fail %s", msg)

    await asyncio.gather(*(one(row) for row in rows))
    return stats


def backfill_historics(
    con: sqlite3.Connection,
    secret: str,
    *,
    apply: bool,
    limit: int | None,
) -> tuple[list[tuple[str, str]], RunStats]:
    stats = RunStats()
    sql = """
        SELECT bet_id, event, league, market, pick, book, event_datetime, date,
               player, selection_line, line
        FROM user_bets
        WHERE historics_context IS NULL OR historics_context = ''
        ORDER BY created_at
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = con.execute(sql).fetchall()
    built: list[tuple[str, str]] = []

    for row in rows:
        token = build_historics_context(row, secret)
        if token:
            built.append((row["bet_id"], token))
            stats.historics_built += 1
        else:
            stats.historics_skipped += 1
            log.debug("skip historics %s: %s", row["bet_id"][:8], _skip_reason(row))

    if apply and built:
        with con:
            for bet_id, token in built:
                cur = con.execute(
                    """
                    UPDATE user_bets
                    SET historics_context = ?
                    WHERE bet_id = ?
                      AND (historics_context IS NULL OR historics_context = '')
                    """,
                    (token, bet_id),
                )
                stats.historics_updated += cur.rowcount

    return built, stats


def fetch_clv_candidates(
    con: sqlite3.Connection,
    *,
    force_clv: bool,
    limit: int | None,
) -> list[sqlite3.Row]:
    if force_clv:
        clv_filter = ""
    else:
        clv_filter = "AND book_clv IS NULL"
    sql = f"""
        SELECT bet_id, historics_context, odds, book, status, book_clv, nvig_clv
        FROM user_bets
        WHERE status IN ({",".join("?" * len(SETTLED_STATUSES))})
          AND historics_context IS NOT NULL AND historics_context != ''
          AND odds IS NOT NULL
          {clv_filter}
        ORDER BY settled_at DESC NULLS LAST, created_at DESC
    """
    params: list[object] = list(SETTLED_STATUSES)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return con.execute(sql, params).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill historics_context and CLV on user_bets"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default is dry-run)",
    )
    parser.add_argument(
        "--historics-only",
        action="store_true",
        help="Only phase 1 (historics_context)",
    )
    parser.add_argument(
        "--clv-only",
        action="store_true",
        help="Only phase 2 (CLV for settled bets)",
    )
    parser.add_argument(
        "--force-clv",
        action="store_true",
        help="Recompute CLV even when book_clv is already set",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max rows per phase (for testing)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Parallel historics API calls (default 3)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="Seconds to wait after each historics API call (default 0.25)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db_path = Path(os.getenv("BET_DB_PATH", ROOT / "bets.db")).resolve()
    if not db_path.exists():
        log.error("Database not found: %s", db_path)
        sys.exit(1)

    # Ensure bet_tracking writes to the same DB as this script.
    bet_tracking.DB_PATH = str(db_path)

    log.info("Database: %s", db_path)
    log.info(
        "Historics URL: %s",
        os.getenv(
            "HISTORICS_API_URL",
            "https://app.keepbetting.co/api/historics",
        ),
    )
    log.info("Mode: %s", "APPLY" if args.apply else "DRY-RUN")

    do_historics = not args.clv_only
    do_clv = not args.historics_only

    secret = _load_jwt_secret()
    if do_historics and not secret:
        log.error("API_CONTEXT_SECRET required for historics backfill")
        sys.exit(1)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    stats = RunStats()
    t0 = time.perf_counter()

    if do_historics:
        log.info("--- Phase 1: historics_context ---")
        _, hstats = backfill_historics(
            con, secret, apply=args.apply, limit=args.limit
        )
        stats.historics_built = hstats.historics_built
        stats.historics_skipped = hstats.historics_skipped
        stats.historics_updated = hstats.historics_updated
        log.info(
            "Historics: built=%d skipped=%d updated=%d",
            stats.historics_built,
            stats.historics_skipped,
            stats.historics_updated,
        )

    if do_clv:
        log.info("--- Phase 2: CLV ---")
        candidates = fetch_clv_candidates(
            con, force_clv=args.force_clv, limit=args.limit
        )
        log.info("CLV candidates: %d", len(candidates))
        if candidates:
            clv_stats = asyncio.run(
                run_clv_batch(
                    candidates,
                    apply=args.apply,
                    concurrency=args.concurrency,
                    delay=args.delay,
                )
            )
            stats.clv_ok = clv_stats.clv_ok
            stats.clv_failed = clv_stats.clv_failed
            stats.clv_updated = clv_stats.clv_updated
            stats.clv_errors = clv_stats.clv_errors

        log.info(
            "CLV: ok=%d failed=%d updated=%d",
            stats.clv_ok,
            stats.clv_failed,
            stats.clv_updated,
        )
        if stats.clv_errors[:10]:
            log.info("Sample CLV errors:")
            for err in stats.clv_errors[:10]:
                log.info("  %s", err)

    elapsed = time.perf_counter() - t0
    log.info("Done in %.1fs", elapsed)

    if not args.apply:
        log.info("Dry-run only. Re-run with --apply to write to the database.")

    con.close()


if __name__ == "__main__":
    main()
