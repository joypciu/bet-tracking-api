#!/usr/bin/env python3
"""
Re-settle World Cup (FIFA) bets (e.g. after SofaScore UTC date fix).

1. Clears shared_settlements cache for World Cup shared bets
2. Resets settled user_bets back to pending
3. Re-runs auto-settlement via stats_api for each past World Cup bet

Usage:
  python scripts/resettle_world_cup.py
  python scripts/resettle_world_cup.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import bet_tracking  # noqa: E402
from main import _build_settlement, _is_future_game  # noqa: E402

log = logging.getLogger("resettle_world_cup")
LEAGUE = "World Cup (FIFA)"
SETTLED = ("win", "loss", "push")


def _reset_world_cup_settlements() -> tuple[int, int]:
    """Clear shared cache and reset settled World Cup user bets to pending."""
    with bet_tracking._conn() as con:
        shared_deleted = con.execute(
            """
            DELETE FROM shared_settlements
            WHERE shared_bet_id IN (
                SELECT shared_bet_id FROM shared_bets WHERE league = ?
            )
            """,
            (LEAGUE,),
        ).rowcount

        bets_reset = con.execute(
            """
            UPDATE user_bets
            SET status = 'pending',
                outcome = 'pending',
                settled_at = NULL,
                settlement_source = NULL,
                home_score = NULL,
                away_score = NULL,
                player_stat_value = NULL,
                stat_name = NULL,
                clv_calculated_at = NULL,
                book_clv = NULL,
                nvig_clv = NULL,
                book_closing_odds = NULL,
                nvig_closing_odds = NULL
            WHERE league = ? AND status IN ('win', 'loss', 'push')
            """,
            (LEAGUE,),
        ).rowcount

    return shared_deleted, bets_reset


def _world_cup_bets() -> list[dict]:
    with bet_tracking._conn() as con:
        rows = con.execute(
            """
            SELECT b.*
            FROM user_bets b
            WHERE b.league = ?
            ORDER BY b.date, b.event, b.created_at
            """,
            (LEAGUE,),
        ).fetchall()
    return [bet_tracking._row_to_dict(r) for r in rows]


async def _resettle_all(*, apply: bool) -> dict[str, int | list]:
    summary: dict[str, int | list] = {
        "shared_cache_cleared": 0,
        "bets_reset": 0,
        "skipped_future": 0,
        "settled": 0,
        "still_pending": 0,
        "errors": 0,
        "changes": [],
    }

    if apply:
        cleared, reset = _reset_world_cup_settlements()
        summary["shared_cache_cleared"] = cleared
        summary["bets_reset"] = reset
        log.info("Cleared %d shared settlements; reset %d bets", cleared, reset)

    bets = _world_cup_bets()
    for bet in bets:
        label = f"{bet['bet_id'][:8]}… {bet.get('event')} | {bet.get('market')}"
        if _is_future_game(bet):
            summary["skipped_future"] += 1
            log.info("SKIP (future): %s", label)
            continue

        if not apply:
            log.info("DRY-RUN would settle: %s (status=%s)", label, bet.get("status"))
            continue

        try:
            settlement = await _build_settlement(bet)
        except Exception as exc:
            summary["errors"] += 1
            log.error("ERROR %s: %s", label, exc)
            continue

        outcome = settlement.get("outcome", "pending")
        if settlement.get("settled") and outcome in SETTLED:
            summary["settled"] += 1
            fresh = bet_tracking.get_bet(bet["bet_id"]) or bet
            old_status = bet.get("status")
            new_status = fresh.get("status")
            if old_status != new_status or bet.get("status") == "pending":
                summary["changes"].append(
                    {
                        "bet_id": bet["bet_id"],
                        "event": bet.get("event"),
                        "market": bet.get("market"),
                        "pick": bet.get("pick"),
                        "line": bet.get("line"),
                        "outcome": new_status,
                        "score": settlement.get("score"),
                        "stat_value": settlement.get("stat_value"),
                    }
                )
            log.info("SETTLED %s -> %s", label, new_status)
        else:
            summary["still_pending"] += 1
            log.info("PENDING %s (%s)", label, settlement.get("note") or outcome)

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-settle World Cup (FIFA) bets")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Reset and re-settle (default is dry-run listing only)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    summary = asyncio.run(_resettle_all(apply=args.apply))
    print("\n=== Summary ===")
    for key, value in summary.items():
        if key != "changes":
            print(f"  {key}: {value}")

    changes = summary.get("changes") or []
    if changes:
        print("\n=== Outcomes ===")
        for row in changes:
            score = row.get("score") or {}
            stat = row.get("stat_value")
            extra = f" score={score.get('home')}-{score.get('away')}" if score else ""
            if stat is not None:
                extra += f" stat={stat}"
            print(
                f"  {row['outcome'].upper():4} | {row['event']} | {row['market']} "
                f"{row.get('pick')} {row.get('line') or ''}{extra}"
            )


if __name__ == "__main__":
    main()
