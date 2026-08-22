"""
auto_settle_runs.py
===================
Background auto-settlement worker state and cron run history.

Tables: shared_settle_jobs, auto_settle_runs, auto_settle_run_details
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

import bet_tracking

AUTH_SOURCE_COOKIE = bet_tracking.AUTH_SOURCE_COOKIE

_SETTLE_JOB_SKIP_STATUSES = frozenset({"needs_review", "not_settleable"})


def init_tables(con: sqlite3.Connection) -> None:
    """Create auto-settle tables (called from bet_tracking.init_db)."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS shared_settle_jobs (
            shared_bet_id      TEXT PRIMARY KEY REFERENCES shared_bets(shared_bet_id),
            job_status         TEXT NOT NULL DEFAULT 'pending',
            error_count        INTEGER NOT NULL DEFAULT 0,
            next_retry_at      TEXT,
            last_checked_at    TEXT,
            last_note          TEXT,
            last_source        TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
        """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_settle_jobs_status "
        "ON shared_settle_jobs(job_status)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_settle_jobs_next_retry "
        "ON shared_settle_jobs(next_retry_at)"
    )

    con.execute("""
        CREATE TABLE IF NOT EXISTS auto_settle_runs (
            run_id             TEXT PRIMARY KEY,
            started_at         TEXT NOT NULL,
            finished_at        TEXT,
            run_status         TEXT NOT NULL DEFAULT 'running',
            dry_run            INTEGER NOT NULL DEFAULT 0,
            limit_n            INTEGER NOT NULL,
            max_attempts       INTEGER NOT NULL,
            candidates         INTEGER NOT NULL DEFAULT 0,
            settled            INTEGER NOT NULL DEFAULT 0,
            pending            INTEGER NOT NULL DEFAULT 0,
            skipped_future     INTEGER NOT NULL DEFAULT 0,
            not_settleable     INTEGER NOT NULL DEFAULT 0,
            needs_review       INTEGER NOT NULL DEFAULT 0,
            error              INTEGER NOT NULL DEFAULT 0,
            note               TEXT
        )
        """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_auto_settle_runs_started_at "
        "ON auto_settle_runs(started_at)"
    )
    con.execute("""
        CREATE TABLE IF NOT EXISTS auto_settle_run_details (
            detail_id          TEXT PRIMARY KEY,
            run_id             TEXT NOT NULL REFERENCES auto_settle_runs(run_id),
            shared_bet_id      TEXT,
            event              TEXT,
            market             TEXT,
            pick               TEXT,
            line               REAL,
            bucket             TEXT NOT NULL,
            outcome            TEXT,
            note               TEXT,
            created_at         TEXT NOT NULL
        )
        """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_auto_settle_run_details_run_id "
        "ON auto_settle_run_details(run_id)"
    )


def list_due_shared_bets_for_cookie_users(
    *,
    now_iso: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    One representative pending user_bet per shared_bet_id for cookie users.

    Skips canonical settlements already terminal, jobs flagged needs_review /
    not_settleable, and rows whose next_retry_at is still in the future.
    """
    now = now_iso or datetime.now(timezone.utc).isoformat()
    limit = max(1, min(int(limit), 500))
    skip = tuple(_SETTLE_JOB_SKIP_STATUSES)
    placeholders = ",".join("?" * len(skip))
    with bet_tracking._conn() as con:
        rows = con.execute(
            f"""
            WITH cookie_pending AS (
                SELECT ub.*,
                       COALESCE(sj.error_count, 0) AS settle_error_count,
                       sj.next_retry_at AS settle_next_retry_at,
                       sj.job_status AS settle_job_status,
                       sj.last_note AS settle_job_note,
                       ROW_NUMBER() OVER (
                           PARTITION BY ub.shared_bet_id
                           ORDER BY ub.created_at ASC
                       ) AS _rn
                FROM user_bets ub
                JOIN users u ON u.user_id = ub.user_id
                LEFT JOIN shared_settlements ss
                       ON ss.shared_bet_id = ub.shared_bet_id
                LEFT JOIN shared_settle_jobs sj
                       ON sj.shared_bet_id = ub.shared_bet_id
                WHERE ub.status = 'pending'
                  AND ub.shared_bet_id IS NOT NULL
                  AND u.auth_source = ?
                  AND (
                        ss.shared_bet_id IS NULL
                        OR COALESCE(ss.settled, 0) = 0
                      )
                  AND (
                        sj.shared_bet_id IS NULL
                        OR (
                            sj.job_status NOT IN ({placeholders})
                            AND (
                                sj.next_retry_at IS NULL
                                OR sj.next_retry_at <= ?
                            )
                        )
                      )
            )
            SELECT *
            FROM cookie_pending
            WHERE _rn = 1
            ORDER BY date ASC, created_at ASC
            LIMIT ?
            """,
            (AUTH_SOURCE_COOKIE, *skip, now, limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = bet_tracking._row_to_dict(r)
        if d is not None:
            d.pop("_rn", None)
            out.append(d)
    return out


def list_shared_settle_jobs_needing_review(
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Jobs flagged for debug / manual settle after failed retries."""
    limit = max(1, min(int(limit), 500))
    with bet_tracking._conn() as con:
        rows = con.execute(
            """
            SELECT sj.*,
                   sb.sport, sb.league, sb.date, sb.event, sb.market,
                   sb.pick, sb.line, sb.player, sb.home_team, sb.away_team
            FROM shared_settle_jobs sj
            JOIN shared_bets sb ON sb.shared_bet_id = sj.shared_bet_id
            WHERE sj.job_status = 'needs_review'
            ORDER BY sj.last_checked_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [bet_tracking._row_to_dict(r) for r in rows]


def list_shared_settlements_needing_review(
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Backward-compatible alias."""
    return list_shared_settle_jobs_needing_review(limit=limit)


def record_shared_settle_job(
    shared_bet_id: str,
    *,
    job_status: str,
    note: str | None = None,
    error_count: int = 0,
    next_retry_at: str | None = None,
    source: str | None = None,
) -> None:
    """Upsert worker state for an unsettled shared bet (not canonical settlement)."""
    if not shared_bet_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    status_norm = (job_status or "pending").strip().lower()
    with bet_tracking._conn() as con:
        settled = con.execute(
            """
            SELECT 1 FROM shared_settlements
            WHERE shared_bet_id = ?
              AND COALESCE(settled, 0) = 1
            LIMIT 1
            """,
            (shared_bet_id,),
        ).fetchone()
        if settled:
            con.execute(
                "DELETE FROM shared_settle_jobs WHERE shared_bet_id = ?",
                (shared_bet_id,),
            )
            return

        con.execute(
            """
            INSERT INTO shared_settle_jobs (
                shared_bet_id, job_status, error_count, next_retry_at,
                last_checked_at, last_note, last_source, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(shared_bet_id) DO UPDATE SET
                job_status = excluded.job_status,
                error_count = excluded.error_count,
                next_retry_at = excluded.next_retry_at,
                last_checked_at = excluded.last_checked_at,
                last_note = excluded.last_note,
                last_source = excluded.last_source,
                updated_at = excluded.updated_at
            """,
            (
                shared_bet_id,
                status_norm,
                max(0, int(error_count)),
                next_retry_at,
                now,
                note,
                source,
                now,
                now,
            ),
        )


def record_shared_settlement_check(
    shared_bet_id: str,
    *,
    status: str,
    note: str | None = None,
    error_count: int = 0,
    next_retry_at: str | None = None,
    source: str | None = None,
) -> None:
    """Backward-compatible alias."""
    record_shared_settle_job(
        shared_bet_id,
        job_status=status,
        note=note,
        error_count=error_count,
        next_retry_at=next_retry_at,
        source=source,
    )


def requeue_shared_settle_job(
    shared_bet_id: str,
    *,
    note: str | None = None,
    source: str = "admin_requeue",
) -> bool:
    """Reset worker state so cron will retry. Returns False if already settled."""
    if not shared_bet_id:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with bet_tracking._conn() as con:
        settled = con.execute(
            """
            SELECT 1 FROM shared_settlements
            WHERE shared_bet_id = ?
              AND COALESCE(settled, 0) = 1
            LIMIT 1
            """,
            (shared_bet_id,),
        ).fetchone()
        if settled:
            con.execute(
                "DELETE FROM shared_settle_jobs WHERE shared_bet_id = ?",
                (shared_bet_id,),
            )
            return False

        existing = con.execute(
            "SELECT created_at FROM shared_settle_jobs WHERE shared_bet_id = ?",
            (shared_bet_id,),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        con.execute(
            """
            INSERT INTO shared_settle_jobs (
                shared_bet_id, job_status, error_count, next_retry_at,
                last_checked_at, last_note, last_source, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(shared_bet_id) DO UPDATE SET
                job_status = 'pending',
                error_count = 0,
                next_retry_at = NULL,
                last_checked_at = excluded.last_checked_at,
                last_note = excluded.last_note,
                last_source = excluded.last_source,
                updated_at = excluded.updated_at
            """,
            (
                shared_bet_id,
                "pending",
                0,
                None,
                now,
                note or "Requeued for auto-settlement.",
                source,
                created_at,
                now,
            ),
        )
    return True


def clear_shared_settle_job(
    shared_bet_id: str,
    *,
    con: sqlite3.Connection | None = None,
) -> None:
    """Remove worker row after canonical settlement succeeds."""
    if not shared_bet_id:
        return
    sql = "DELETE FROM shared_settle_jobs WHERE shared_bet_id = ?"
    if con is not None:
        con.execute(sql, (shared_bet_id,))
        return
    with bet_tracking._conn() as c:
        c.execute(sql, (shared_bet_id,))


def start_auto_settle_run(
    *,
    dry_run: bool = False,
    limit_n: int = 50,
    max_attempts: int = 8,
) -> str:
    """Create a run log row; returns run_id."""
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    with bet_tracking._conn() as con:
        con.execute(
            """
            INSERT INTO auto_settle_runs (
                run_id, started_at, run_status, dry_run,
                limit_n, max_attempts
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                run_id,
                started_at,
                "running",
                1 if dry_run else 0,
                max(1, int(limit_n)),
                max(1, int(max_attempts)),
            ),
        )
    return run_id


def record_auto_settle_run_detail(
    run_id: str,
    *,
    shared_bet_id: str | None,
    event: str | None,
    market: str | None,
    pick: str | None,
    line: float | None,
    bucket: str,
    outcome: str | None = None,
    note: str | None = None,
) -> None:
    if not run_id:
        return
    with bet_tracking._conn() as con:
        con.execute(
            """
            INSERT INTO auto_settle_run_details (
                detail_id, run_id, shared_bet_id, event, market, pick, line,
                bucket, outcome, note, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                run_id,
                shared_bet_id,
                event,
                market,
                pick,
                line,
                bucket,
                outcome,
                note,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def finish_auto_settle_run(
    run_id: str,
    summary: dict[str, Any],
    *,
    run_status: str = "completed",
    note: str | None = None,
) -> None:
    if not run_id:
        return
    finished_at = datetime.now(timezone.utc).isoformat()
    with bet_tracking._conn() as con:
        con.execute(
            """
            UPDATE auto_settle_runs
            SET finished_at = ?,
                run_status = ?,
                candidates = ?,
                settled = ?,
                pending = ?,
                skipped_future = ?,
                not_settleable = ?,
                needs_review = ?,
                error = ?,
                note = ?
            WHERE run_id = ?
            """,
            (
                finished_at,
                run_status,
                int(summary.get("candidates") or 0),
                int(summary.get("settled") or 0),
                int(summary.get("pending") or 0),
                int(summary.get("skipped_future") or 0),
                int(summary.get("not_settleable") or 0),
                int(summary.get("needs_review") or 0),
                int(summary.get("error") or 0),
                note,
                run_id,
            ),
        )


def list_auto_settle_runs(*, limit: int = 30) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with bet_tracking._conn() as con:
        rows = con.execute(
            """
            SELECT *
            FROM auto_settle_runs
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [bet_tracking._row_to_dict(r) for r in rows]


def get_auto_settle_run_details(run_id: str) -> list[dict[str, Any]]:
    with bet_tracking._conn() as con:
        rows = con.execute(
            """
            SELECT *
            FROM auto_settle_run_details
            WHERE run_id = ?
            ORDER BY created_at ASC
            """,
            (run_id,),
        ).fetchall()
    return [bet_tracking._row_to_dict(r) for r in rows]
