#!/usr/bin/env python3
"""
Backfill book_closing_odds and nvig_closing_odds on settled user_bets.

For rows with historics_context, fetches the historics API, reads the last
book and nvig american_odds, and writes closing odds plus CLV (keeps metrics
in sync). Targets bets missing either closing-odds column unless --force.

Dry-run by default. Use --apply to write.

Environment (bet-tracking-api/.env or shell):
  BET_DB_PATH           SQLite path (default: ../bets.db)
  HISTORICS_API_URL     default https://app.keepbetting.co/api/historics
  HISTORICS_API_KEY     optional
  HISTORICS_TIMEOUT     optional

Examples:
  python scripts/backfill_closing_odds.py
  python scripts/backfill_closing_odds.py --apply
  python scripts/backfill_closing_odds.py --apply --limit 10
  python scripts/backfill_closing_odds.py --apply --force
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

load_dotenv(ROOT / ".env")

import bet_tracking  # noqa: E402
from backfill_historics_and_clv import RunStats, run_clv_batch  # noqa: E402

log = logging.getLogger("backfill_closing_odds")

SETTLED_STATUSES = ("win", "loss", "push", "void")


def fetch_candidates(
    con: sqlite3.Connection,
    *,
    force: bool,
    limit: int | None,
) -> list[sqlite3.Row]:
    missing_filter = ""
    if not force:
        missing_filter = """
          AND (
            book_closing_odds IS NULL
            OR nvig_closing_odds IS NULL
          )
        """
    sql = f"""
        SELECT bet_id, historics_context, odds, book, status,
               book_clv, nvig_clv, book_closing_odds, nvig_closing_odds
        FROM user_bets
        WHERE status IN ({",".join("?" * len(SETTLED_STATUSES))})
          AND historics_context IS NOT NULL AND historics_context != ''
          AND odds IS NOT NULL
          {missing_filter}
        ORDER BY settled_at DESC NULLS LAST, created_at DESC
    """
    params: list[object] = list(SETTLED_STATUSES)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return con.execute(sql, params).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill closing book/nvig odds on settled user_bets"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default is dry-run)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch even when closing odds are already set",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max rows to process (for testing)",
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

    bet_tracking.DB_PATH = str(db_path)
    bet_tracking.init_db()

    log.info("Database: %s", db_path)
    log.info(
        "Historics URL: %s",
        os.getenv(
            "HISTORICS_API_URL",
            "https://app.keepbetting.co/api/historics",
        ),
    )
    log.info("Mode: %s", "APPLY" if args.apply else "DRY-RUN")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    t0 = time.perf_counter()
    candidates = fetch_candidates(con, force=args.force, limit=args.limit)
    log.info("Closing-odds candidates: %d", len(candidates))

    stats = RunStats()
    if candidates:
        stats = asyncio.run(
            run_clv_batch(
                candidates,
                apply=args.apply,
                concurrency=args.concurrency,
                delay=args.delay,
            )
        )

    log.info(
        "Closing odds: ok=%d failed=%d updated=%d",
        stats.clv_ok,
        stats.clv_failed,
        stats.clv_updated,
    )
    if stats.clv_errors[:10]:
        log.info("Sample errors:")
        for err in stats.clv_errors[:10]:
            log.info("  %s", err)

    elapsed = time.perf_counter() - t0
    log.info("Done in %.1fs", elapsed)

    if not args.apply:
        log.info("Dry-run only. Re-run with --apply to write to the database.")

    con.close()


if __name__ == "__main__":
    main()
