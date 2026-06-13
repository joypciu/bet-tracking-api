#!/usr/bin/env python3
"""
Backfill nvig_odds_at_placement on user_bets from historics timelines.

For rows with historics_context but no nvig_odds_at_placement, fetches historics
and picks the nvig point at or just before created_at (best proxy for track time).
Also recomputes nvig_at_placement EV% when bet odds are present.

Dry-run by default. Use --apply to write.

Environment (bet-tracking-api/.env or shell):
  BET_DB_PATH           SQLite path (default: ../bets.db)
  HISTORICS_API_URL     default https://app.keepbetting.co/api/historics
  HISTORICS_API_KEY     optional
  HISTORICS_TIMEOUT     optional

Examples:
  python scripts/backfill_nvig_odds_at_placement.py
  python scripts/backfill_nvig_odds_at_placement.py --apply
  python scripts/backfill_nvig_odds_at_placement.py --apply --limit 20
  python scripts/backfill_nvig_odds_at_placement.py --apply --force
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

import bet_tracking  # noqa: E402
import historics_bridge  # noqa: E402

log = logging.getLogger("backfill_nvig_odds_at_placement")


def _american_to_decimal(american: int) -> float:
    if american > 0:
        return (american / 100) + 1.0
    return (100 / abs(american)) + 1.0


def _compute_nvig_at_placement(bet_odds: int, nvig_odds: int) -> float | None:
    try:
        bet_dec = _american_to_decimal(bet_odds)
        fair_prob = 1.0 / _american_to_decimal(nvig_odds)
        return round(fair_prob * bet_dec - 1.0, 4)
    except (ZeroDivisionError, ValueError, TypeError):
        return None


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _first_american_odds(series: list | None) -> int | None:
    if not series:
        return None
    for point in series:
        if not isinstance(point, dict):
            continue
        try:
            val = point.get("american_odds")
            if val is not None:
                return int(val)
        except (ValueError, TypeError):
            continue
    return None


def _pick_nvig_at_placement(
    nvig_series: list | None, created_at: str | None
) -> int | None:
    if not nvig_series:
        return None

    created = _parse_iso_dt(created_at)
    if created is None:
        return _first_american_odds(nvig_series)

    best_odds: int | None = None
    best_dt: datetime | None = None
    for point in nvig_series:
        if not isinstance(point, dict):
            continue
        dt = _parse_iso_dt(point.get("datetime"))
        try:
            odds = int(point["american_odds"])
        except (KeyError, TypeError, ValueError):
            continue
        if dt is None:
            continue
        if dt <= created and (best_dt is None or dt > best_dt):
            best_dt = dt
            best_odds = odds

    if best_odds is not None:
        return best_odds
    return _first_american_odds(nvig_series)


@dataclass
class RunStats:
    ok: int = 0
    failed: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)


def fetch_candidates(
    con: sqlite3.Connection,
    *,
    force: bool,
    limit: int | None,
) -> list[sqlite3.Row]:
    missing_filter = ""
    if not force:
        missing_filter = "AND nvig_odds_at_placement IS NULL"
    sql = f"""
        SELECT bet_id, historics_context, odds, created_at, nvig_odds_at_placement
        FROM user_bets
        WHERE historics_context IS NOT NULL AND historics_context != ''
          {missing_filter}
        ORDER BY created_at DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql).fetchall()


async def process_row(row: sqlite3.Row) -> tuple[bool, int | None, float | None, str | None]:
    bet_id = row["bet_id"]
    context = (row["historics_context"] or "").strip()
    if not context:
        return False, None, None, "no historics_context"

    try:
        historics_data = await historics_bridge.fetch_historics(context)
    except historics_bridge.HistoricsBridgeHTTPError as exc:
        return False, None, None, f"historics HTTP {exc.status_code}"
    except Exception as exc:
        return False, None, None, str(exc)[:200]

    nvig_odds = _pick_nvig_at_placement(
        historics_data.get("nvig"), row["created_at"]
    )
    if nvig_odds is None:
        return False, None, None, "no nvig points in historics"

    nvig_ev: float | None = None
    if row["odds"] is not None:
        try:
            nvig_ev = _compute_nvig_at_placement(int(row["odds"]), nvig_odds)
        except (ValueError, TypeError):
            pass

    return True, nvig_odds, nvig_ev, None


async def run_batch(
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
            ok, nvig_odds, nvig_ev, err = await process_row(row)
            if delay > 0:
                await asyncio.sleep(delay)
            if ok:
                stats.ok += 1
                if apply:
                    bet_tracking.update_bet_nvig_odds_at_placement(
                        row["bet_id"], nvig_odds, nvig_ev
                    )
                    stats.updated += 1
                log.info(
                    "nvig placement %s odds=%s ev=%s",
                    row["bet_id"][:8],
                    nvig_odds,
                    nvig_ev,
                )
            else:
                stats.failed += 1
                msg = f"{row['bet_id'][:8]}... {err}"
                stats.errors.append(msg)
                log.warning("nvig placement fail %s", msg)

    await asyncio.gather(*(one(row) for row in rows))
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill nvig_odds_at_placement from historics timelines"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default is dry-run)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch even when nvig_odds_at_placement is already set",
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
    log.info("Nvig-at-placement candidates: %d", len(candidates))

    stats = RunStats()
    if candidates:
        stats = asyncio.run(
            run_batch(
                candidates,
                apply=args.apply,
                concurrency=args.concurrency,
                delay=args.delay,
            )
        )

    log.info(
        "Nvig at placement: ok=%d failed=%d updated=%d",
        stats.ok,
        stats.failed,
        stats.updated,
    )
    if stats.errors[:10]:
        log.info("Sample errors:")
        for err in stats.errors[:10]:
            log.info("  %s", err)

    elapsed = time.perf_counter() - t0
    log.info("Done in %.1fs", elapsed)

    if not args.apply:
        log.info("Dry-run only. Re-run with --apply to write to the database.")

    con.close()


if __name__ == "__main__":
    main()
