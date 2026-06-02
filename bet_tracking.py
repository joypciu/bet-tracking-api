"""
bet_tracking.py
===============
Persistent bet tracking backed by SQLite.

Schema (bets table)
-------------------
bet_id            TEXT PK
created_at        TEXT      — ISO-8601 UTC
sport             TEXT
league            TEXT
date              TEXT      — YYYY-MM-DD of the game
event             TEXT      — display label, e.g. "A vs B"
event_datetime    TEXT      — aware ISO-8601 datetime string
event_id          TEXT      — ESPN event_id
team              TEXT
home_team         TEXT
away_team         TEXT
market            TEXT
pick              TEXT
selection_line    TEXT
line              REAL
odds              INTEGER
stake             REAL
notes             TEXT
status            TEXT      — pending | win | loss | push | void
outcome           TEXT
settled_at        TEXT
settlement_source TEXT
book              TEXT
home_score        INTEGER
away_score        INTEGER
player_stat_value REAL
stat_name         TEXT
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

DB_PATH = os.getenv("BET_DB_PATH", os.path.join(os.path.dirname(__file__), "bets.db"))

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    TEXT PRIMARY KEY,
                email      TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")

        con.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                bet_id            TEXT PRIMARY KEY,
                created_at        TEXT NOT NULL,
                user_id           TEXT REFERENCES users(user_id),
                sport             TEXT,
                league            TEXT,
                date              TEXT,
                event             TEXT,
                event_datetime    TEXT,
                event_id          TEXT,
                team              TEXT,
                home_team         TEXT,
                away_team         TEXT,
                player            TEXT,
                market            TEXT NOT NULL,
                pick              TEXT NOT NULL DEFAULT '',
                selection_line    TEXT,
                line              REAL,
                odds              INTEGER,
                stake             REAL,
                notes             TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',
                outcome           TEXT,
                settled_at        TEXT,
                settlement_source TEXT
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_bets_status   ON bets(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bets_date     ON bets(date)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bets_event_id ON bets(event_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bets_sport    ON bets(sport)")

        cols = {row["name"] for row in con.execute("PRAGMA table_info(bets)").fetchall()}
        for col in ("league", "event", "event_datetime", "selection_line", "player", "user_id", "book"):
            if col not in cols:
                ref = " REFERENCES users(user_id)" if col == "user_id" else ""
                con.execute(f"ALTER TABLE bets ADD COLUMN {col} TEXT{ref}")
        for col in ("home_score", "away_score"):
            if col not in cols:
                con.execute(f"ALTER TABLE bets ADD COLUMN {col} INTEGER")
        for col in ("player_stat_value", "stat_name"):
            if col not in cols:
                con.execute(f"ALTER TABLE bets ADD COLUMN {col} TEXT")
        for col, typ in [
            ("counterpart_odds",  "INTEGER"),
            ("nvig_at_placement", "REAL"),
            ("book_clv",          "REAL"),
            ("nvig_clv",          "REAL"),
            ("clv_calculated_at", "TEXT"),
        ]:
            if col not in cols:
                con.execute(f"ALTER TABLE bets ADD COLUMN {col} {typ}")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bets_player  ON bets(player)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bets_user_id ON bets(user_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bets_book    ON bets(book)")

        con.execute("""
            CREATE TABLE IF NOT EXISTS api_users (
                user_id        TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                email          TEXT UNIQUE NOT NULL,
                organization   TEXT,
                notes          TEXT,
                api_key_hash   TEXT NOT NULL,
                api_key_prefix TEXT NOT NULL,
                is_active      INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_api_users_email    ON api_users(email)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_api_users_key_hash ON api_users(api_key_hash)")


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def validate_email(email: str) -> str:
    normalised = email.strip().lower()
    if not _EMAIL_RE.match(normalised):
        raise ValueError(
            f"'{email}' is not a valid email address. "
            "Provide a real address in the form user@domain.tld."
        )
    return normalised


def create_or_get_user(email: str) -> dict[str, Any]:
    normalised = validate_email(email)
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE email = ?", (normalised,)).fetchone()
        if row:
            return _row_to_dict(row)  # type: ignore[return-value]
        user_id    = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT INTO users (user_id, email, created_at) VALUES (?,?,?)",
            (user_id, normalised, created_at),
        )
    return {"user_id": user_id, "email": normalised, "created_at": created_at}


def get_user_by_email(email: str) -> dict[str, Any] | None:
    normalised = email.strip().lower()
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE email = ?", (normalised,)).fetchone()
    return _row_to_dict(row)


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return _row_to_dict(row)


def list_users(limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    with _conn() as con:
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows  = con.execute(
            """
            SELECT u.user_id, u.email, u.created_at,
                   COUNT(b.bet_id)                                        AS total_bets,
                   SUM(CASE WHEN b.status = 'pending' THEN 1 ELSE 0 END) AS pending_bets,
                   SUM(CASE WHEN b.status = 'win'     THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN b.status = 'loss'    THEN 1 ELSE 0 END) AS losses
            FROM users u
            LEFT JOIN bets b ON b.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY u.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


# ---------------------------------------------------------------------------
# API user management (api_key-based external users)
# ---------------------------------------------------------------------------

def _generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, sha256_hash, display_prefix)."""
    raw    = "btk_" + secrets.token_urlsafe(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:16] + "..."
    return raw, hashed, prefix


def create_api_user(
    name:         str,
    email:        str,
    organization: str | None = None,
    notes:        str | None = None,
) -> dict[str, Any]:
    """Create a new API user and return the record including the plaintext key (shown once)."""
    normalised = validate_email(email)
    user_id    = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    raw_key, key_hash, key_prefix = _generate_api_key()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO api_users
                (user_id, name, email, organization, notes,
                 api_key_hash, api_key_prefix, is_active, created_at)
            VALUES (?,?,?,?,?,?,?,1,?)
            """,
            (user_id, name.strip(), normalised, organization, notes,
             key_hash, key_prefix, created_at),
        )
    return {
        "user_id":      user_id,
        "name":         name.strip(),
        "email":        normalised,
        "organization": organization,
        "notes":        notes,
        "api_key":      raw_key,      # plaintext — returned once only
        "api_key_prefix": key_prefix,
        "is_active":    True,
        "created_at":   created_at,
    }


def list_api_users() -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT user_id, name, email, organization, notes,
                   api_key_prefix, is_active, created_at
            FROM api_users
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_api_user_by_id(user_id: str) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute(
            "SELECT user_id, name, email, organization, notes, "
            "api_key_prefix, is_active, created_at "
            "FROM api_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_dict(row)


def get_api_user_by_key_hash(key_hash: str) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute(
            "SELECT user_id, name, email, organization, api_key_prefix, is_active "
            "FROM api_users WHERE api_key_hash = ?",
            (key_hash,),
        ).fetchone()
    return _row_to_dict(row)


def set_api_user_active(user_id: str, active: bool) -> bool:
    with _conn() as con:
        cur = con.execute(
            "UPDATE api_users SET is_active = ? WHERE user_id = ?",
            (1 if active else 0, user_id),
        )
    return cur.rowcount > 0


def delete_api_user(user_id: str) -> bool:
    with _conn() as con:
        cur = con.execute("DELETE FROM api_users WHERE user_id = ?", (user_id,))
    return cur.rowcount > 0


def regenerate_api_key(user_id: str) -> dict[str, Any] | None:
    """Replace the API key for user_id. Returns record with plaintext key (once only)."""
    raw_key, key_hash, key_prefix = _generate_api_key()
    with _conn() as con:
        cur = con.execute(
            "UPDATE api_users SET api_key_hash = ?, api_key_prefix = ? WHERE user_id = ?",
            (key_hash, key_prefix, user_id),
        )
        if cur.rowcount == 0:
            return None
        row = con.execute(
            "SELECT user_id, name, email, organization, notes, is_active, created_at "
            "FROM api_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    rec = dict(row)
    rec["api_key"]        = raw_key
    rec["api_key_prefix"] = key_prefix
    return rec


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_bet(
    market:            str,
    pick:              str | None       = None,
    user_id:           str | None       = None,
    sport:             str | None       = None,
    league:            str | None       = None,
    date:              str | None       = None,
    event:             str | None       = None,
    event_datetime:    str | None       = None,
    event_id:          str | None       = None,
    team:              str | None       = None,
    home_team:         str | None       = None,
    away_team:         str | None       = None,
    player:            str | None       = None,
    selection_line:    str | None       = None,
    line:              float | None     = None,
    odds:              int | None       = None,
    stake:             float | None     = None,
    notes:             str | None       = None,
    book:              str | None       = None,
    counterpart_odds:  int | None       = None,
    nvig_at_placement: float | None     = None,
) -> dict[str, Any]:
    bet_id      = str(uuid.uuid4())
    created_at  = datetime.now(timezone.utc).isoformat()
    pick_stored = pick if pick is not None else ""
    with _conn() as con:
        con.execute(
            """
            INSERT INTO bets
                (bet_id, created_at, user_id, sport, league, date, event, event_datetime,
                 event_id, team, home_team, away_team, player, market, pick,
                 selection_line, line, odds, stake, notes, book,
                 counterpart_odds, nvig_at_placement)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (bet_id, created_at, user_id, sport, league, date, event, event_datetime,
             event_id, team, home_team, away_team, player, market, pick_stored,
             selection_line, line, odds, stake, notes, book,
             counterpart_odds, nvig_at_placement),
        )
    return get_bet(bet_id)  # type: ignore[return-value]


def get_bet(bet_id: str) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute(
            """
            SELECT b.*, u.email
            FROM bets b
            LEFT JOIN users u ON u.user_id = b.user_id
            WHERE b.bet_id = ?
            """,
            (bet_id,),
        ).fetchone()
    return _row_to_dict(row)


def list_bets(
    status:    str | None = None,
    sport:     str | None = None,
    date_from: str | None = None,
    date_to:   str | None = None,
    market:    str | None = None,
    player:    str | None = None,
    user_id:   str | None = None,
    book:      str | None = None,
    limit:     int        = 50,
    offset:    int        = 0,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []

    if status:
        where.append("b.status = ?");               params.append(status)
    if sport:
        where.append("LOWER(b.sport) = LOWER(?)");  params.append(sport)
    if market:
        where.append("LOWER(b.market) = LOWER(?)"); params.append(market)
    if player:
        where.append("LOWER(b.player) LIKE LOWER(?)"); params.append(f"%{player}%")
    if date_from:
        where.append("b.date >= ?");                params.append(date_from)
    if date_to:
        where.append("b.date <= ?");                params.append(date_to)
    if user_id:
        where.append("b.user_id = ?");              params.append(user_id)
    if book:
        where.append("LOWER(b.book) = LOWER(?)");   params.append(book)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    base   = "FROM bets b LEFT JOIN users u ON u.user_id = b.user_id"

    with _conn() as con:
        total = con.execute(f"SELECT COUNT(*) {base} {clause}", params).fetchone()[0]
        rows  = con.execute(
            f"SELECT b.*, u.email {base} {clause} ORDER BY b.created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

    return [_row_to_dict(r) for r in rows], total  # type: ignore[misc]


def settle_bet(
    bet_id:              str,
    outcome:             str,
    source:              str,
    home_score:          int | None = None,
    away_score:          int | None = None,
    player_stat_value:   float | None = None,
    stat_name:           str | None = None,
) -> bool:
    if outcome == "pending":
        return False
    settled_at = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            """
            UPDATE bets
            SET    outcome = ?, status = ?, settled_at = ?,
                   settlement_source = ?, home_score = ?, away_score = ?,
                   player_stat_value = ?, stat_name = ?
            WHERE  bet_id = ? AND status = 'pending'
            """,
            (outcome, outcome, settled_at, source, home_score, away_score,
             player_stat_value, stat_name, bet_id),
        )
        return cur.rowcount > 0


def update_bet_clv(
    bet_id:   str,
    book_clv: float | None,
    nvig_clv: float | None,
) -> None:
    clv_calculated_at = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            """
            UPDATE bets
            SET book_clv = ?, nvig_clv = ?, clv_calculated_at = ?
            WHERE bet_id = ?
            """,
            (book_clv, nvig_clv, clv_calculated_at, bet_id),
        )


def void_bet(bet_id: str) -> bool:
    settled_at = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            """
            UPDATE bets
            SET status = 'void', outcome = 'void', settled_at = ?
            WHERE bet_id = ? AND status = 'pending'
            """,
            (settled_at, bet_id),
        )
        return cur.rowcount > 0


def delete_bet(bet_id: str) -> bool:
    with _conn() as con:
        cur = con.execute("DELETE FROM bets WHERE bet_id = ?", (bet_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Summary / analytics
# ---------------------------------------------------------------------------

def get_summary(user_id: str | None = None) -> dict[str, Any]:
    with _conn() as con:
        if user_id:
            rows = con.execute(
                "SELECT status, COUNT(*) AS n, SUM(stake) AS staked "
                "FROM bets WHERE user_id = ? GROUP BY status",
                (user_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT status, COUNT(*) AS n, SUM(stake) AS staked FROM bets GROUP BY status"
            ).fetchall()

    by_status: dict[str, int] = {}
    total_staked = 0.0
    for r in rows:
        by_status[r["status"]] = r["n"]
        total_staked += r["staked"] or 0.0

    return {
        "total_bets":   sum(by_status.values()),
        "by_status":    by_status,
        "total_staked": round(total_staked, 2),
        "pending":      by_status.get("pending", 0),
        "settled":      by_status.get("win", 0) + by_status.get("loss", 0) + by_status.get("push", 0),
    }


def _analytics_from_rows(rows: list) -> dict[str, Any]:
    settled_wins = settled_losses = settled_pushes = 0
    total_staked = total_returned = 0.0
    odds_list: list[float] = []

    by_market: dict[str, dict] = {}
    by_sport:  dict[str, dict] = {}
    by_book:   dict[str, dict] = {}

    def _bucket(d: dict, key: str) -> dict:
        if key not in d:
            d[key] = {"wins": 0, "losses": 0, "pushes": 0, "pending": 0, "staked": 0.0, "returned": 0.0}
        return d[key]

    def _payout(american_odds: float | None, stake: float) -> float:
        if american_odds is None:
            return stake
        if american_odds >= 100:
            return stake + stake * (american_odds / 100)
        return stake + stake * (100 / abs(american_odds))

    for r in rows:
        status = r["status"]
        market = r["market"] or "unknown"
        sport  = r["sport"]  or "unknown"
        book   = r["book"]   or "unknown"
        odds   = r["odds"]
        stake  = r["stake"] or 0.0
        mb = _bucket(by_market, market)
        sb = _bucket(by_sport,  sport)
        bb = _bucket(by_book,   book)

        if status == "win":
            settled_wins += 1
            payout = _payout(odds, stake)
            total_staked += stake; total_returned += payout
            mb["wins"] += 1; mb["staked"] += stake; mb["returned"] += payout
            sb["wins"] += 1; sb["staked"] += stake; sb["returned"] += payout
            bb["wins"] += 1; bb["staked"] += stake; bb["returned"] += payout
            if odds is not None: odds_list.append(float(odds))
        elif status == "loss":
            settled_losses += 1
            total_staked += stake
            mb["losses"] += 1; mb["staked"] += stake
            sb["losses"] += 1; sb["staked"] += stake
            bb["losses"] += 1; bb["staked"] += stake
            if odds is not None: odds_list.append(float(odds))
        elif status == "push":
            settled_pushes += 1
            total_staked += stake; total_returned += stake
            mb["pushes"] += 1; mb["staked"] += stake; mb["returned"] += stake
            sb["pushes"] += 1; sb["staked"] += stake; sb["returned"] += stake
            bb["pushes"] += 1; bb["staked"] += stake; bb["returned"] += stake
            if odds is not None: odds_list.append(float(odds))
        elif status == "pending":
            mb["pending"] += 1; sb["pending"] += 1; bb["pending"] += 1

    settled  = settled_wins + settled_losses + settled_pushes
    win_rate = round(settled_wins / settled * 100, 1) if settled else None
    roi      = round((total_returned - total_staked) / total_staked * 100, 2) if total_staked else None
    avg_odds = round(sum(odds_list) / len(odds_list), 1) if odds_list else None

    def _clean(d: dict) -> dict:
        for v in d.values():
            v["staked"]   = round(v["staked"], 2)
            v["returned"] = round(v.get("returned", 0.0), 2)
            v["net"]      = round(v["returned"] - v["staked"], 2)
            total_g = v["wins"] + v["losses"] + v["pushes"]
            v["win_rate"] = round(v["wins"] / total_g * 100, 1) if total_g else None
        return d

    return {
        "settled_bets":   settled,
        "pending_bets":   sum(v.get("pending", 0) for v in by_market.values()),
        "win_rate_pct":   win_rate,
        "roi_pct":        roi,
        "avg_odds":       avg_odds,
        "total_staked":   round(total_staked, 2),
        "total_returned": round(total_returned, 2),
        "net_profit":     round(total_returned - total_staked, 2),
        "by_status":  {"win": settled_wins, "loss": settled_losses, "push": settled_pushes},
        "by_market":  _clean(by_market),
        "by_sport":   _clean(by_sport),
        "by_book":    _clean(by_book),
    }


def get_analytics() -> dict[str, Any]:
    with _conn() as con:
        rows = con.execute("SELECT status, market, sport, book, odds, stake FROM bets").fetchall()
    return _analytics_from_rows(rows)


def get_user_summary(user_id: str) -> dict[str, Any]:
    return get_summary(user_id)


def get_user_analytics(user_id: str) -> dict[str, Any]:
    with _conn() as con:
        rows = con.execute(
            "SELECT status, market, sport, book, odds, stake FROM bets WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return _analytics_from_rows(rows)
