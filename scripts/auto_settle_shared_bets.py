#!/usr/bin/env python3
"""
Background auto-settlement for shared bets tracked by cookie (non API-key) users.

Settles once per shared_bet_id, then fan-out happens via bet_tracking.settle_bet.
Uses shared_settle_jobs for retry/backoff; flags needs_review after too many
unresolved attempts. Canonical outcomes stay in shared_settlements only.

Each cron run is persisted to auto_settle_runs + auto_settle_run_details for debug.

Designed for VPS cron (every 5–15 minutes). Overlap-safe via flock when available.

Usage:
  python scripts/auto_settle_shared_bets.py
  python scripts/auto_settle_shared_bets.py --dry-run
  python scripts/auto_settle_shared_bets.py --limit 25 --max-attempts 8
  python scripts/auto_settle_shared_bets.py --list-needs-review
  python scripts/auto_settle_shared_bets.py --list-runs
  python scripts/auto_settle_shared_bets.py --show-run RUN_ID
  python scripts/auto_settle_shared_bets.py --requeue SHARED_BET_ID

Cron example (Linux):
  */10 * * * * cd /path/to/bet-tracking-api && \\
    flock -n /tmp/auto_settle_shared_bets.lock \\
    .venv/bin/python scripts/auto_settle_shared_bets.py >> /var/log/auto_settle_shared_bets.log 2>&1

Environment:
  BET_DB_PATH   SQLite path (default from bet_tracking)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import bet_tracking  # noqa: E402
import auto_settle_runs  # noqa: E402
from main import (  # noqa: E402
    _build_settlement,
    _calculate_clv_for_bet,
    _is_future_game,
)

log = logging.getLogger("auto_settle_shared_bets")

SETTLED_OUTCOMES = frozenset({"win", "loss", "push", "void"})
# Minutes of backoff after attempt N fails / stays pending (0-indexed).
_BACKOFF_MINUTES = (5, 15, 30, 60, 120, 240, 360, 720)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _backoff_minutes(error_count: int) -> int:
    idx = max(0, min(int(error_count), len(_BACKOFF_MINUTES) - 1))
    return _BACKOFF_MINUTES[idx]


def _label(bet: dict) -> str:
    shared = (bet.get("shared_bet_id") or "")[:8]
    return (
        f"{shared}… | {bet.get('event')} | {bet.get('market')} "
        f"{bet.get('pick') or ''} {bet.get('line') if bet.get('line') is not None else ''}"
    ).strip()


def _result(
    bet: dict | None,
    bucket: str,
    *,
    outcome: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    b = bet or {}
    line = b.get("line")
    return {
        "bucket": bucket,
        "shared_bet_id": b.get("shared_bet_id"),
        "event": b.get("event"),
        "market": b.get("market"),
        "pick": b.get("pick"),
        "line": float(line) if isinstance(line, (int, float)) else None,
        "outcome": outcome,
        "note": str(note) if note is not None else None,
    }


def _acquire_lock(lock_path: Path):
    """Return an open lock file handle, or None if another run holds the lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        import fcntl  # Unix

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return None
        return fh
    except ImportError:
        # Windows / no fcntl: best-effort pid file (not perfect, ok for local runs).
        if lock_path.stat().st_size > 0:
            fh.seek(0)
            old = (fh.read() or "").strip()
            if old.isdigit():
                pass
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        return fh


def _release_lock(fh) -> None:
    if fh is None:
        return
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except ImportError:
        pass
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


async def _maybe_clv_for_shared(shared_bet_id: str) -> int:
    """Fire CLV for newly settled tickets that still need it."""
    with bet_tracking._conn() as con:
        rows = con.execute(
            """
            SELECT bet_id
            FROM user_bets
            WHERE shared_bet_id = ?
              AND status IN ('win', 'loss', 'push', 'void')
              AND historics_context IS NOT NULL
              AND clv_calculated_at IS NULL
            """,
            (shared_bet_id,),
        ).fetchall()
    count = 0
    for row in rows:
        await _calculate_clv_for_bet(row["bet_id"])
        count += 1
    return count


async def _process_one(
    bet: dict,
    *,
    dry_run: bool,
    max_attempts: int,
) -> dict[str, Any]:
    """
    Returns a detail dict with bucket:
      settled | skipped_future | pending | not_settleable | needs_review | error
    """
    shared_bet_id = bet.get("shared_bet_id")
    if not shared_bet_id:
        return _result(bet, "error", note="Missing shared_bet_id")

    label = _label(bet)
    prev_errors = int(bet.get("settle_error_count") or 0)

    if _is_future_game(bet):
        log.info("SKIP future: %s", label)
        return _result(bet, "skipped_future", note="Game has not started yet.")

    if dry_run:
        log.info(
            "DRY-RUN would settle: %s (errors=%s status=%s)",
            label,
            prev_errors,
            bet.get("settle_job_status") or "none",
        )
        return _result(bet, "pending", outcome="dry_run")

    try:
        settlement = await _build_settlement(bet)
    except Exception as exc:
        new_errors = prev_errors + 1
        err_note = f"Settlement exception: {exc}"
        if new_errors >= max_attempts:
            auto_settle_runs.record_shared_settle_job(
                shared_bet_id,
                job_status="needs_review",
                note=f"Settlement exception after {new_errors} attempts: {exc}",
                error_count=new_errors,
                next_retry_at=None,
                source="auto_settle_cron",
            )
            log.error("NEEDS_REVIEW (exception) %s: %s", label, exc)
            return _result(bet, "needs_review", outcome="exception", note=err_note)

        delay = _backoff_minutes(new_errors - 1)
        auto_settle_runs.record_shared_settle_job(
            shared_bet_id,
            job_status="pending",
            note=err_note,
            error_count=new_errors,
            next_retry_at=_iso(_utcnow() + timedelta(minutes=delay)),
            source="auto_settle_cron",
        )
        log.error("ERROR %s (retry in %sm): %s", label, delay, exc)
        return _result(bet, "error", outcome="exception", note=err_note)

    outcome = str(settlement.get("outcome") or "pending").lower()
    note = settlement.get("note")
    note_str = str(note) if note is not None else None
    settled = bool(settlement.get("settled")) and outcome in SETTLED_OUTCOMES

    if settled:
        log.info("SETTLED %s -> %s", label, outcome)
        try:
            await _maybe_clv_for_shared(shared_bet_id)
        except Exception as exc:
            log.warning("CLV follow-up failed for %s: %s", shared_bet_id[:8], exc)
        return _result(bet, "settled", outcome=outcome, note=note_str)

    if outcome == "not_settleable":
        auto_settle_runs.record_shared_settle_job(
            shared_bet_id,
            job_status="not_settleable",
            note=str(note or "Market is not auto-settleable."),
            error_count=prev_errors + 1,
            next_retry_at=None,
            source="auto_settle_cron",
        )
        log.info("NOT_SETTLEABLE %s", label)
        return _result(bet, "not_settleable", outcome=outcome, note=note_str)

    new_errors = prev_errors + 1
    unknown_review_at = max(3, max_attempts // 2)
    should_flag = new_errors >= max_attempts or (
        outcome == "unknown" and new_errors >= unknown_review_at
    )
    if should_flag:
        flag_note = str(
            note or f"Unresolved after {new_errors} attempts (last outcome={outcome})."
        )
        auto_settle_runs.record_shared_settle_job(
            shared_bet_id,
            job_status="needs_review",
            note=flag_note,
            error_count=new_errors,
            next_retry_at=None,
            source="auto_settle_cron",
        )
        log.warning(
            "NEEDS_REVIEW %s (outcome=%s attempts=%s)", label, outcome, new_errors
        )
        return _result(bet, "needs_review", outcome=outcome, note=flag_note)

    delay = _backoff_minutes(new_errors - 1)
    pending_note = str(note or f"Still {outcome} after auto-settle check.")
    auto_settle_runs.record_shared_settle_job(
        shared_bet_id,
        job_status="unknown" if outcome == "unknown" else "pending",
        note=pending_note,
        error_count=new_errors,
        next_retry_at=_iso(_utcnow() + timedelta(minutes=delay)),
        source="auto_settle_cron",
    )
    log.info("PENDING %s (%s) retry in %sm", label, outcome, delay)
    return _result(bet, "pending", outcome=outcome, note=pending_note)


def _empty_summary() -> dict[str, int]:
    return {
        "candidates": 0,
        "settled": 0,
        "pending": 0,
        "skipped_future": 0,
        "not_settleable": 0,
        "needs_review": 0,
        "error": 0,
    }


def _record_detail(run_id: str | None, detail: dict[str, Any]) -> None:
    if not run_id:
        return
    auto_settle_runs.record_auto_settle_run_detail(
        run_id,
        shared_bet_id=detail.get("shared_bet_id"),
        event=detail.get("event"),
        market=detail.get("market"),
        pick=detail.get("pick"),
        line=detail.get("line"),
        bucket=str(detail.get("bucket") or "error"),
        outcome=detail.get("outcome"),
        note=detail.get("note"),
    )


async def run_once(
    *,
    dry_run: bool,
    limit: int,
    max_attempts: int,
    run_id: str | None = None,
) -> dict[str, int]:
    summary = _empty_summary()

    bets = auto_settle_runs.list_due_shared_bets_for_cookie_users(
        now_iso=_iso(_utcnow()),
        limit=limit,
    )
    summary["candidates"] = len(bets)
    log.info(
        "Loaded %d due shared bet(s) for cookie users (limit=%s dry_run=%s run_id=%s)",
        len(bets),
        limit,
        dry_run,
        (run_id or "")[:8] if run_id else "none",
    )

    for bet in bets:
        detail = await _process_one(
            bet, dry_run=dry_run, max_attempts=max_attempts
        )
        bucket = str(detail.get("bucket") or "error")
        if bucket in summary:
            summary[bucket] += 1
        else:
            summary["error"] += 1
        _record_detail(run_id, detail)

    return summary


def _print_run_row(row: dict[str, Any]) -> None:
    dry = "dry-run" if row.get("dry_run") else "live"
    print(
        f"  {row.get('run_id')} | {row.get('started_at')} | {row.get('run_status')} | "
        f"{dry} | candidates={row.get('candidates')} settled={row.get('settled')} "
        f"pending={row.get('pending')} skipped_future={row.get('skipped_future')} "
        f"needs_review={row.get('needs_review')} error={row.get('error')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Auto-settle pending shared bets tracked by non API-key (cookie) users"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List/check candidates without writing settlements or retry state",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("AUTO_SETTLE_LIMIT", "50")),
        help="Max shared bets to process this run (default 50)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=int(os.environ.get("AUTO_SETTLE_MAX_ATTEMPTS", "8")),
        help="Flag needs_review after this many unresolved attempts (default 8)",
    )
    parser.add_argument(
        "--list-needs-review",
        action="store_true",
        help="Print shared settle jobs flagged needs_review and exit",
    )
    parser.add_argument(
        "--list-runs",
        action="store_true",
        help="Print recent auto-settle run summaries from the DB",
    )
    parser.add_argument(
        "--show-run",
        metavar="RUN_ID",
        help="Print one run summary plus per-bet detail rows",
    )
    parser.add_argument(
        "--requeue",
        metavar="SHARED_BET_ID",
        help="Reset job state so cron retries auto-settlement for this shared bet",
    )
    parser.add_argument(
        "--lock-file",
        default=os.environ.get(
            "AUTO_SETTLE_LOCK_FILE",
            str(Path(tempfile.gettempdir()) / "auto_settle_shared_bets.lock"),
        ),
        help="Path for exclusive run lock",
    )
    parser.add_argument(
        "--skip-lock",
        action="store_true",
        help="Do not take a lock file (not recommended for cron)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    bet_tracking.init_db()

    if args.requeue:
        ok = auto_settle_runs.requeue_shared_settle_job(
            args.requeue.strip(),
            note="Requeued via auto_settle_shared_bets.py CLI.",
            source="cli_requeue",
        )
        if ok:
            print(f"Requeued shared bet {args.requeue.strip()}")
        else:
            print(
                f"Not requeued: {args.requeue.strip()} is already settled "
                "(job row cleared)."
            )
        return

    if args.list_needs_review:
        rows = auto_settle_runs.list_shared_settle_jobs_needing_review(limit=200)
        print(f"needs_review count: {len(rows)}")
        for row in rows:
            print(
                f"  {row.get('shared_bet_id')} | {row.get('date')} | "
                f"{row.get('event')} | {row.get('market')} | "
                f"errors={row.get('error_count')} | {row.get('last_note')}"
            )
        return

    if args.list_runs:
        rows = auto_settle_runs.list_auto_settle_runs(limit=30)
        print(f"Recent runs: {len(rows)}")
        for row in rows:
            _print_run_row(row)
        return

    if args.show_run:
        run_id = args.show_run.strip()
        runs = [
            r for r in auto_settle_runs.list_auto_settle_runs(limit=200)
            if r.get("run_id") == run_id
        ]
        if not runs:
            print(f"Run not found: {run_id}")
            return
        _print_run_row(runs[0])
        if runs[0].get("note"):
            print(f"  note: {runs[0].get('note')}")
        details = auto_settle_runs.get_auto_settle_run_details(run_id)
        print(f"  details ({len(details)}):")
        for d in details:
            line = d.get("line")
            line_s = f" {line}" if line is not None else ""
            print(
                f"    [{d.get('bucket')}] {d.get('shared_bet_id')} | "
                f"{d.get('event')} | {d.get('market')} {d.get('pick') or ''}{line_s} "
                f"| outcome={d.get('outcome')} | {d.get('note') or ''}"
            )
        return

    max_attempts = max(1, args.max_attempts)
    run_id = auto_settle_runs.start_auto_settle_run(
        dry_run=args.dry_run,
        limit_n=args.limit,
        max_attempts=max_attempts,
    )

    lock_fh = None
    if not args.skip_lock:
        lock_fh = _acquire_lock(Path(args.lock_file))
        if lock_fh is None:
            log.warning("Another auto-settle run holds the lock; exiting.")
            auto_settle_runs.finish_auto_settle_run(
                run_id,
                _empty_summary(),
                run_status="skipped_lock",
                note="Another process holds the lock file.",
            )
            sys.exit(0)

    summary = _empty_summary()
    try:
        summary = asyncio.run(
            run_once(
                dry_run=args.dry_run,
                limit=args.limit,
                max_attempts=max_attempts,
                run_id=run_id,
            )
        )
        auto_settle_runs.finish_auto_settle_run(run_id, summary, run_status="completed")
    except Exception as exc:
        log.exception("Auto-settle run failed: %s", exc)
        auto_settle_runs.finish_auto_settle_run(
            run_id,
            summary,
            run_status="failed",
            note=str(exc),
        )
        raise
    finally:
        _release_lock(lock_fh)

    print(f"\nRun logged: {run_id}")
    print("\n=== Auto-settle summary ===")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
