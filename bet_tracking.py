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


def _norm_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _norm_num(value: float | int | str | None) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _norm_text(str(value))


def build_shared_fingerprint(
    *,
    event_id: str | None,
    sport: str | None,
    league: str | None,
    date: str | None,
    home_team: str | None,
    away_team: str | None,
    market: str,
    pick: str | None,
    line: float | None,
    selection_line: str | None,
    player: str | None,
) -> str:
    # Prefer upstream event_id; fallback to matchup identity.
    event_key = _norm_text(event_id)
    if not event_key:
        event_key = "|".join(
            [
                _norm_text(date),
                _norm_text(sport),
                _norm_text(league),
                _norm_text(home_team),
                _norm_text(away_team),
            ]
        )

    raw = "|".join(
        [
            event_key,
            _norm_text(market),
            _norm_text(pick),
            _norm_num(line),
            _norm_text(selection_line),
            _norm_text(player),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _shared_settlement_to_bet_fields(settlement: dict[str, Any]) -> tuple:
    outcome = settlement.get("outcome") or settlement.get("status")
    settled_at = settlement.get("settled_at")
    source = settlement.get("source")
    return (
        outcome,
        outcome,
        settled_at,
        source,
        settlement.get("home_score"),
        settlement.get("away_score"),
        settlement.get("player_stat_value"),
        settlement.get("stat_name"),
    )


def _migrate_users_auth_source(con: sqlite3.Connection) -> None:
    """Add auth_source and tag api_users mirrors (safe inside a transaction)."""
    user_cols = {
        row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()
    }
    if "auth_source" not in user_cols:
        con.execute(
            "ALTER TABLE users ADD COLUMN auth_source TEXT NOT NULL DEFAULT 'cookie'"
        )

    con.execute(
        """
        UPDATE users
        SET auth_source = CASE
            WHEN user_id IN (SELECT user_id FROM api_users) THEN 'api_key'
            ELSE 'cookie'
        END
        """
    )


def _rebuild_users_table_for_auth_source() -> None:
    """
    Drop legacy global UNIQUE(email) so the same email can exist once per
    auth_source. Must run outside an open transaction (FK pragma requirement).
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        # FK mode can only change when no transaction is open.
        con.execute("PRAGMA foreign_keys=OFF")

        idx_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        create_sql = (idx_sql["sql"] or "") if idx_sql else ""
        # Legacy schema (possibly after ALTER ADD auth_source) still has
        # column-level UNIQUE on email alone — that blocks cookie+api separation.
        legacy_global_email_unique = bool(
            re.search(r"\bemail\s+TEXT\s+UNIQUE\b", create_sql, re.IGNORECASE)
        )

        if legacy_global_email_unique:
            con.execute("""
                CREATE TABLE users_v2 (
                    user_id     TEXT PRIMARY KEY,
                    email       TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    auth_source TEXT NOT NULL DEFAULT 'cookie',
                    UNIQUE (email, auth_source)
                )
            """)
            con.execute("""
                INSERT INTO users_v2 (user_id, email, created_at, auth_source)
                SELECT user_id, email, created_at,
                       COALESCE(NULLIF(TRIM(auth_source), ''), 'cookie')
                FROM users
            """)
            con.execute("DROP TABLE users")
            con.execute("ALTER TABLE users_v2 RENAME TO users")

        con.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_auth_source "
            "ON users(email, auth_source)"
        )
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     TEXT PRIMARY KEY,
                email       TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                auth_source TEXT NOT NULL DEFAULT 'cookie',
                UNIQUE (email, auth_source)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        # Composite unique index is ensured in _rebuild_users_table_for_auth_source.

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

        cols = {
            row["name"] for row in con.execute("PRAGMA table_info(bets)").fetchall()
        }
        for col in (
            "league",
            "event",
            "event_datetime",
            "selection_line",
            "player",
            "user_id",
            "book",
        ):
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
            ("counterpart_odds", "INTEGER"),
            ("nvig_at_placement", "REAL"),
            ("book_clv", "REAL"),
            ("nvig_clv", "REAL"),
            ("clv_calculated_at", "TEXT"),
            ("historics_context", "TEXT"),
        ]:
            if col not in cols:
                con.execute(f"ALTER TABLE bets ADD COLUMN {col} {typ}")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bets_player  ON bets(player)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bets_user_id ON bets(user_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bets_book    ON bets(book)")

        # Shared bet definition (dedup) + canonical settlement cache.
        con.execute("""
            CREATE TABLE IF NOT EXISTS shared_bets (
                shared_bet_id    TEXT PRIMARY KEY,
                fingerprint      TEXT NOT NULL UNIQUE,
                created_at       TEXT NOT NULL,
                event_id         TEXT,
                sport            TEXT,
                league           TEXT,
                date             TEXT,
                event            TEXT,
                event_datetime   TEXT,
                team             TEXT,
                home_team        TEXT,
                away_team        TEXT,
                player           TEXT,
                market           TEXT NOT NULL,
                pick             TEXT NOT NULL DEFAULT '',
                selection_line   TEXT,
                line             REAL
            )
            """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_bets_event_id ON shared_bets(event_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_bets_date ON shared_bets(date)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_bets_market ON shared_bets(market)"
        )

        con.execute("""
            CREATE TABLE IF NOT EXISTS shared_settlements (
                shared_bet_id      TEXT PRIMARY KEY REFERENCES shared_bets(shared_bet_id),
                status             TEXT NOT NULL DEFAULT 'pending',
                outcome            TEXT,
                settled            INTEGER NOT NULL DEFAULT 0,
                source             TEXT,
                settled_at         TEXT,
                home_score         REAL,
                away_score         REAL,
                player_stat_value  REAL,
                stat_name          TEXT,
                note               TEXT,
                checked_at         TEXT,
                next_retry_at      TEXT,
                error_count        INTEGER NOT NULL DEFAULT 0
            )
            """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_settlements_status ON shared_settlements(status)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_settlements_next_retry ON shared_settlements(next_retry_at)"
        )

        import auto_settle_runs

        auto_settle_runs.init_tables(con)

        if "shared_bet_id" not in cols:
            con.execute(
                "ALTER TABLE bets ADD COLUMN shared_bet_id TEXT REFERENCES shared_bets(shared_bet_id)"
            )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_bets_shared_bet_id ON bets(shared_bet_id)"
        )

        # Ticket table (new read source): explicit user_id <-> shared_bet_id reference.
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_bets (
                bet_id             TEXT PRIMARY KEY,
                created_at         TEXT NOT NULL,
                user_id            TEXT REFERENCES users(user_id),
                shared_bet_id      TEXT REFERENCES shared_bets(shared_bet_id),
                sport              TEXT,
                league             TEXT,
                date               TEXT,
                event              TEXT,
                event_datetime     TEXT,
                event_id           TEXT,
                team               TEXT,
                home_team          TEXT,
                away_team          TEXT,
                player             TEXT,
                market             TEXT NOT NULL,
                pick               TEXT NOT NULL DEFAULT '',
                selection_line     TEXT,
                line               REAL,
                odds               INTEGER,
                stake              REAL,
                notes              TEXT,
                status             TEXT NOT NULL DEFAULT 'pending',
                outcome            TEXT,
                settled_at         TEXT,
                settlement_source  TEXT,
                book               TEXT,
                home_score         INTEGER,
                away_score         INTEGER,
                player_stat_value  REAL,
                stat_name          TEXT,
                counterpart_odds   INTEGER,
                nvig_at_placement  REAL,
                book_clv           REAL,
                nvig_clv           REAL,
                clv_calculated_at  TEXT,
                historics_context  TEXT,
                book_closing_odds  INTEGER,
                nvig_closing_odds  INTEGER,
                nvig_odds_at_placement INTEGER
            )
            """)
        ub_cols = {
            row["name"]
            for row in con.execute("PRAGMA table_info(user_bets)").fetchall()
        }
        if "historics_context" not in ub_cols:
            con.execute("ALTER TABLE user_bets ADD COLUMN historics_context TEXT")
        for col in ("book_closing_odds", "nvig_closing_odds"):
            if col not in ub_cols:
                con.execute(f"ALTER TABLE user_bets ADD COLUMN {col} INTEGER")
        if "nvig_odds_at_placement" not in ub_cols:
            con.execute(
                "ALTER TABLE user_bets ADD COLUMN nvig_odds_at_placement INTEGER"
            )

        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_bets_user_id ON user_bets(user_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_bets_shared_bet_id ON user_bets(shared_bet_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_bets_status ON user_bets(status)"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_date ON user_bets(date)")

        # Drop synchronization triggers if they exist.
        con.execute("DROP TRIGGER IF EXISTS trg_bets_ai_sync_user_bets")
        con.execute("DROP TRIGGER IF EXISTS trg_bets_au_sync_user_bets")
        con.execute("DROP TRIGGER IF EXISTS trg_bets_ad_sync_user_bets")

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
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_users_email    ON api_users(email)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_users_key_hash ON api_users(api_key_hash)"
        )

        # Must run after api_users exists so mirrors can be tagged correctly.
        _migrate_users_auth_source(con)

        # Backfill existing bets into shared_bets on first startup after migration.
        rows = con.execute("""
            SELECT bet_id, event_id, sport, league, date, event, event_datetime,
                   team, home_team, away_team, player, market, pick,
                   selection_line, line
            FROM bets
            WHERE shared_bet_id IS NULL
            """).fetchall()
        for r in rows:
            fp = build_shared_fingerprint(
                event_id=r["event_id"],
                sport=r["sport"],
                league=r["league"],
                date=r["date"],
                home_team=r["home_team"],
                away_team=r["away_team"],
                market=r["market"],
                pick=r["pick"],
                line=r["line"],
                selection_line=r["selection_line"],
                player=r["player"],
            )
            shared = con.execute(
                "SELECT shared_bet_id FROM shared_bets WHERE fingerprint = ?",
                (fp,),
            ).fetchone()
            if shared:
                shared_bet_id = shared["shared_bet_id"]
            else:
                shared_bet_id = str(uuid.uuid4())
                con.execute(
                    """
                    INSERT INTO shared_bets (
                        shared_bet_id, fingerprint, created_at,
                        event_id, sport, league, date, event, event_datetime,
                        team, home_team, away_team, player, market, pick,
                        selection_line, line
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        shared_bet_id,
                        fp,
                        datetime.now(timezone.utc).isoformat(),
                        r["event_id"],
                        r["sport"],
                        r["league"],
                        r["date"],
                        r["event"],
                        r["event_datetime"],
                        r["team"],
                        r["home_team"],
                        r["away_team"],
                        r["player"],
                        r["market"],
                        r["pick"],
                        r["selection_line"],
                        r["line"],
                    ),
                )
            con.execute(
                "UPDATE bets SET shared_bet_id = ? WHERE bet_id = ?",
                (shared_bet_id, r["bet_id"]),
            )

        # Backfill canonical settlements from already-settled legacy bet rows.
        settled_rows = con.execute("""
            SELECT b.shared_bet_id, b.status, b.outcome, b.settlement_source,
                   b.settled_at, b.home_score, b.away_score, b.player_stat_value, b.stat_name
            FROM bets b
            WHERE b.shared_bet_id IS NOT NULL
              AND b.status IN ('win','loss','push','void')
            """).fetchall()
        for r in settled_rows:
            con.execute(
                """
                INSERT INTO shared_settlements (
                    shared_bet_id, status, outcome, settled, source, settled_at,
                    home_score, away_score, player_stat_value, stat_name, checked_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(shared_bet_id) DO NOTHING
                """,
                (
                    r["shared_bet_id"],
                    r["status"],
                    r["outcome"] or r["status"],
                    1,
                    r["settlement_source"],
                    r["settled_at"],
                    r["home_score"],
                    r["away_score"],
                    r["player_stat_value"],
                    r["stat_name"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    # Legacy UNIQUE(email) → UNIQUE(email, auth_source); must be outside the
    # init transaction so PRAGMA foreign_keys=OFF takes effect.
    _rebuild_users_table_for_auth_source()
    sync_api_user_mirrors()


def _create_or_get_shared_bet(
    con: sqlite3.Connection,
    *,
    event_id: str | None,
    sport: str | None,
    league: str | None,
    date: str | None,
    event: str | None,
    event_datetime: str | None,
    team: str | None,
    home_team: str | None,
    away_team: str | None,
    player: str | None,
    market: str,
    pick: str,
    selection_line: str | None,
    line: float | None,
) -> str:
    fp = build_shared_fingerprint(
        event_id=event_id,
        sport=sport,
        league=league,
        date=date,
        home_team=home_team,
        away_team=away_team,
        market=market,
        pick=pick,
        line=line,
        selection_line=selection_line,
        player=player,
    )
    row = con.execute(
        "SELECT shared_bet_id FROM shared_bets WHERE fingerprint = ?",
        (fp,),
    ).fetchone()
    if row:
        return str(row["shared_bet_id"])

    shared_bet_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO shared_bets (
            shared_bet_id, fingerprint, created_at,
            event_id, sport, league, date, event, event_datetime,
            team, home_team, away_team, player, market, pick,
            selection_line, line
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            shared_bet_id,
            fp,
            datetime.now(timezone.utc).isoformat(),
            event_id,
            sport,
            league,
            date,
            event,
            event_datetime,
            team,
            home_team,
            away_team,
            player,
            market,
            pick,
            selection_line,
            line,
        ),
    )
    return shared_bet_id


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


AUTH_SOURCE_COOKIE = "cookie"
AUTH_SOURCE_API_KEY = "api_key"


def create_or_get_user(
    email: str,
    *,
    auth_source: str = AUTH_SOURCE_COOKIE,
) -> dict[str, Any]:
    """Create/get a users row scoped to one login identity (cookie vs api_key)."""
    if auth_source not in (AUTH_SOURCE_COOKIE, AUTH_SOURCE_API_KEY):
        raise ValueError(f"Invalid auth_source: {auth_source!r}")
    normalised = validate_email(email)
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE email = ? AND auth_source = ?",
            (normalised, auth_source),
        ).fetchone()
        if row:
            return _row_to_dict(row)  # type: ignore[return-value]
        user_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT INTO users (user_id, email, created_at, auth_source) "
            "VALUES (?,?,?,?)",
            (user_id, normalised, created_at, auth_source),
        )
    return {
        "user_id": user_id,
        "email": normalised,
        "created_at": created_at,
        "auth_source": auth_source,
    }


def ensure_api_user_mirrored(user_id: str, email: str) -> dict[str, Any]:
    """
    Ensure an api_users identity also exists in users so user_bets FK is satisfied.

    Uses api_users.user_id as the canonical id and auth_source='api_key'.
    Does not collide with Bettor Odds cookie users that share the same email.
    """
    normalised = validate_email(email)
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            if row["auth_source"] != AUTH_SOURCE_API_KEY:
                con.execute(
                    "UPDATE users SET auth_source = ? WHERE user_id = ?",
                    (AUTH_SOURCE_API_KEY, user_id),
                )
                row = con.execute(
                    "SELECT * FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()
            return _row_to_dict(row)  # type: ignore[return-value]

        # Only conflict if another api_key mirror already owns this email.
        email_row = con.execute(
            "SELECT user_id FROM users WHERE email = ? AND auth_source = ?",
            (normalised, AUTH_SOURCE_API_KEY),
        ).fetchone()
        if email_row and email_row["user_id"] != user_id:
            old_id = email_row["user_id"]
            has_bets = con.execute(
                "SELECT 1 FROM user_bets WHERE user_id = ? LIMIT 1", (old_id,)
            ).fetchone()
            if has_bets:
                raise ValueError(
                    f"Email {normalised!r} is already linked to a different "
                    f"api_key users record with existing bets; contact support."
                )
            con.execute("DELETE FROM users WHERE user_id = ?", (old_id,))

        created_at = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT INTO users (user_id, email, created_at, auth_source) "
            "VALUES (?,?,?,?)",
            (user_id, normalised, created_at, AUTH_SOURCE_API_KEY),
        )
    return {
        "user_id": user_id,
        "email": normalised,
        "created_at": created_at,
        "auth_source": AUTH_SOURCE_API_KEY,
    }


def sync_api_user_mirrors() -> None:
    """Ensure every api_users row has a matching users row (startup backfill)."""
    with _conn() as con:
        rows = con.execute("SELECT user_id, email FROM api_users").fetchall()
    for row in rows:
        ensure_api_user_mirrored(row["user_id"], row["email"])


def get_user_by_email(
    email: str,
    *,
    auth_source: str = AUTH_SOURCE_COOKIE,
) -> dict[str, Any] | None:
    """Lookup by email within one login identity (default: Bettor Odds cookie)."""
    normalised = email.strip().lower()
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE email = ? AND auth_source = ?",
            (normalised, auth_source),
        ).fetchone()
    return _row_to_dict(row)


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_tracked_historics_contexts(user_id: str) -> list[str]:
    """Return distinct historics_context JWTs for this user's pending bets.

    The EV feed matches rows via data-historics (same JWT saved at track time).
    """
    with _conn() as con:
        rows = con.execute(
            """
            SELECT DISTINCT historics_context
            FROM user_bets
            WHERE user_id = ?
              AND status = 'pending'
              AND historics_context IS NOT NULL
              AND TRIM(historics_context) != ''
            """,
            (user_id,),
        ).fetchall()
    return [r["historics_context"] for r in rows]


def get_tracked_fingerprints(user_id: str) -> list[str]:
    """Return fingerprints for this user's pending bets only.

    Settled bets (win/loss/push/void) are excluded — the EV feed only surfaces
    future markets, so those rows would never reappear anyway.
    """
    with _conn() as con:
        rows = con.execute(
            """
            SELECT DISTINCT sb.fingerprint
            FROM user_bets ub
            JOIN shared_bets sb ON ub.shared_bet_id = sb.shared_bet_id
            WHERE ub.user_id = ? AND ub.status = 'pending'
            """,
            (user_id,),
        ).fetchall()
    return [r["fingerprint"] for r in rows]


def list_users(limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    with _conn() as con:
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = con.execute(
            """
            SELECT u.user_id, u.email, u.created_at,
                 COUNT(ub.bet_id)                                        AS total_bets,
                 SUM(CASE WHEN ub.status = 'pending' THEN 1 ELSE 0 END) AS pending_bets,
                 SUM(CASE WHEN ub.status = 'win'     THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN ub.status = 'loss'    THEN 1 ELSE 0 END) AS losses
            FROM users u
             LEFT JOIN user_bets ub ON ub.user_id = u.user_id
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
    raw = "btk_" + secrets.token_urlsafe(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:16] + "..."
    return raw, hashed, prefix


def create_api_user(
    name: str,
    email: str,
    organization: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a new API user and return the record including the plaintext key (shown once)."""
    normalised = validate_email(email)
    user_id = str(uuid.uuid4())
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
            (
                user_id,
                name.strip(),
                normalised,
                organization,
                notes,
                key_hash,
                key_prefix,
                created_at,
            ),
        )
    ensure_api_user_mirrored(user_id, normalised)
    return {
        "user_id": user_id,
        "name": name.strip(),
        "email": normalised,
        "organization": organization,
        "notes": notes,
        "api_key": raw_key,  # plaintext — returned once only
        "api_key_prefix": key_prefix,
        "is_active": True,
        "created_at": created_at,
    }


def list_api_users() -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute("""
            SELECT user_id, name, email, organization, notes,
                   api_key_prefix, is_active, created_at
            FROM api_users
            ORDER BY created_at DESC
            """).fetchall()
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
    rec["api_key"] = raw_key
    rec["api_key_prefix"] = key_prefix
    return rec


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_bet(
    market: str,
    pick: str | None = None,
    user_id: str | None = None,
    sport: str | None = None,
    league: str | None = None,
    date: str | None = None,
    event: str | None = None,
    event_datetime: str | None = None,
    event_id: str | None = None,
    team: str | None = None,
    home_team: str | None = None,
    away_team: str | None = None,
    player: str | None = None,
    selection_line: str | None = None,
    line: float | None = None,
    odds: int | None = None,
    stake: float | None = None,
    notes: str | None = None,
    book: str | None = None,
    counterpart_odds: int | None = None,
    nvig_at_placement: float | None = None,
    nvig_odds_at_placement: int | None = None,
    historics_context: str | None = None,
) -> dict[str, Any]:
    bet_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    pick_stored = pick if pick is not None else ""
    with _conn() as con:
        shared_bet_id = _create_or_get_shared_bet(
            con,
            event_id=event_id,
            sport=sport,
            league=league,
            date=date,
            event=event,
            event_datetime=event_datetime,
            team=team,
            home_team=home_team,
            away_team=away_team,
            player=player,
            market=market,
            pick=pick_stored,
            selection_line=selection_line,
            line=line,
        )
        con.execute(
            """
            INSERT INTO user_bets
                (bet_id, created_at, user_id, sport, league, date, event, event_datetime,
                 event_id, team, home_team, away_team, player, market, pick,
                 selection_line, line, odds, stake, notes, book,
                 counterpart_odds, nvig_at_placement, historics_context,
                 nvig_odds_at_placement, shared_bet_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                bet_id,
                created_at,
                user_id,
                sport,
                league,
                date,
                event,
                event_datetime,
                event_id,
                team,
                home_team,
                away_team,
                player,
                market,
                pick_stored,
                selection_line,
                line,
                odds,
                stake,
                notes,
                book,
                counterpart_odds,
                nvig_at_placement,
                historics_context,
                nvig_odds_at_placement,
                shared_bet_id,
            ),
        )

        # If this shared bet already has a canonical settled result, apply it to
        # the newly tracked ticket immediately (no external settlement call).
        ss = con.execute(
            "SELECT * FROM shared_settlements WHERE shared_bet_id = ?",
            (shared_bet_id,),
        ).fetchone()
        if ss and (
            ss["status"] in ("win", "loss", "push", "void") or ss["settled"] == 1
        ):
            con.execute(
                """
                UPDATE user_bets
                SET outcome = ?, status = ?, settled_at = ?, settlement_source = ?,
                    home_score = ?, away_score = ?, player_stat_value = ?, stat_name = ?
                WHERE bet_id = ? AND status = 'pending'
                """,
                _shared_settlement_to_bet_fields(dict(ss)) + (bet_id,),
            )
    return get_bet(bet_id)  # type: ignore[return-value]


def get_bet(bet_id: str) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute(
            """
            SELECT ub.*, u.email
            FROM user_bets ub
            LEFT JOIN users u ON u.user_id = ub.user_id
            WHERE ub.bet_id = ?
            """,
            (bet_id,),
        ).fetchone()
    return _row_to_dict(row)


def list_bets(
    status: str | None = None,
    sport: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    market: str | None = None,
    player: str | None = None,
    user_id: str | None = None,
    book: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []

    if status:
        where.append("b.status = ?")
        params.append(status)
    if sport:
        where.append("LOWER(b.sport) = LOWER(?)")
        params.append(sport)
    if market:
        where.append("LOWER(b.market) = LOWER(?)")
        params.append(market)
    if player:
        where.append("LOWER(b.player) LIKE LOWER(?)")
        params.append(f"%{player}%")
    if date_from:
        where.append("b.date >= ?")
        params.append(date_from)
    if date_to:
        where.append("b.date <= ?")
        params.append(date_to)
    if user_id:
        where.append("b.user_id = ?")
        params.append(user_id)
    if book:
        where.append("LOWER(b.book) = LOWER(?)")
        params.append(book)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    base = "FROM user_bets b LEFT JOIN users u ON u.user_id = b.user_id"

    with _conn() as con:
        total = con.execute(f"SELECT COUNT(*) {base} {clause}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT b.*, u.email {base} {clause} ORDER BY b.created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

    return [_row_to_dict(r) for r in rows], total  # type: ignore[misc]


def list_bet_filter_options(user_id: str | None = None) -> dict[str, list[str]]:
    """Distinct markets and books for filter dropdowns (no bet row payload)."""
    where = "WHERE market IS NOT NULL AND TRIM(market) != ''"
    book_where = "WHERE book IS NOT NULL AND TRIM(book) != ''"
    params: list[Any] = []
    if user_id:
        where += " AND user_id = ?"
        book_where += " AND user_id = ?"
        params = [user_id]

    with _conn() as con:
        markets = [
            row["market"]
            for row in con.execute(
                f"SELECT DISTINCT market FROM user_bets {where} ORDER BY market COLLATE NOCASE",
                params,
            ).fetchall()
        ]
        books = [
            row["book"]
            for row in con.execute(
                f"SELECT DISTINCT book FROM user_bets {book_where} ORDER BY book COLLATE NOCASE",
                params,
            ).fetchall()
        ]

    return {"markets": markets, "books": books}


def settle_bet(
    bet_id: str,
    outcome: str,
    source: str,
    home_score: int | None = None,
    away_score: int | None = None,
    player_stat_value: float | None = None,
    stat_name: str | None = None,
) -> bool:
    if outcome == "pending":
        return False
    settled_at = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        bet_row = con.execute(
            "SELECT bet_id, shared_bet_id, status FROM user_bets WHERE bet_id = ?",
            (bet_id,),
        ).fetchone()
        if not bet_row:
            return False

        shared_bet_id = bet_row["shared_bet_id"]

        # User/manual overrides are ticket-scoped by policy (must not fan out).
        if source == "manual" or not shared_bet_id:
            cur = con.execute(
                """
                UPDATE user_bets
                SET    outcome = ?, status = ?, settled_at = ?,
                       settlement_source = ?, home_score = ?, away_score = ?,
                       player_stat_value = ?, stat_name = ?
                WHERE  bet_id = ? AND status = 'pending'
                """,
                (
                    outcome,
                    outcome,
                    settled_at,
                    source,
                    home_score,
                    away_score,
                    player_stat_value,
                    stat_name,
                    bet_id,
                ),
            )
            return cur.rowcount > 0

        # Canonical auto-settlement: persist once and apply to all pending user bets
        # linked to the same shared definition.
        con.execute(
            """
            INSERT INTO shared_settlements (
                shared_bet_id, status, outcome, settled, source, settled_at,
                home_score, away_score, player_stat_value, stat_name, checked_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(shared_bet_id) DO UPDATE SET
                status = excluded.status,
                outcome = excluded.outcome,
                settled = excluded.settled,
                source = excluded.source,
                settled_at = excluded.settled_at,
                home_score = excluded.home_score,
                away_score = excluded.away_score,
                player_stat_value = excluded.player_stat_value,
                stat_name = excluded.stat_name,
                checked_at = excluded.checked_at
            """,
            (
                shared_bet_id,
                outcome,
                outcome,
                1,
                source,
                settled_at,
                home_score,
                away_score,
                player_stat_value,
                stat_name,
                settled_at,
            ),
        )
        import auto_settle_runs

        auto_settle_runs.clear_shared_settle_job(shared_bet_id, con=con)

        cur = con.execute(
            """
            UPDATE user_bets
            SET    outcome = ?, status = ?, settled_at = ?,
                   settlement_source = ?, home_score = ?, away_score = ?,
                   player_stat_value = ?, stat_name = ?
            WHERE  shared_bet_id = ? AND status = 'pending'
            """,
            (
                outcome,
                outcome,
                settled_at,
                source,
                home_score,
                away_score,
                player_stat_value,
                stat_name,
                shared_bet_id,
            ),
        )
        return cur.rowcount > 0


def get_shared_settlement(shared_bet_id: str | None) -> dict[str, Any] | None:
    if not shared_bet_id:
        return None
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM shared_settlements WHERE shared_bet_id = ?",
            (shared_bet_id,),
        ).fetchone()
    return _row_to_dict(row)


def update_bet_clv(
    bet_id: str,
    book_clv: float | None,
    nvig_clv: float | None,
    *,
    book_closing_odds: int | None = None,
    nvig_closing_odds: int | None = None,
) -> None:
    clv_calculated_at = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            """
            UPDATE user_bets
            SET book_clv = ?, nvig_clv = ?, clv_calculated_at = ?,
                book_closing_odds = ?, nvig_closing_odds = ?
            WHERE bet_id = ?
            """,
            (
                book_clv,
                nvig_clv,
                clv_calculated_at,
                book_closing_odds,
                nvig_closing_odds,
                bet_id,
            ),
        )


def update_bet_nvig_odds_at_placement(
    bet_id: str,
    nvig_odds_at_placement: int | None,
    nvig_at_placement: float | None = None,
) -> None:
    with _conn() as con:
        con.execute(
            """
            UPDATE user_bets
            SET nvig_odds_at_placement = ?, nvig_at_placement = ?
            WHERE bet_id = ?
            """,
            (nvig_odds_at_placement, nvig_at_placement, bet_id),
        )


def void_bet(bet_id: str) -> bool:
    settled_at = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            """
            UPDATE user_bets
            SET status = 'void', outcome = 'void', settled_at = ?
            WHERE bet_id = ? AND status = 'pending'
            """,
            (settled_at, bet_id),
        )
        return cur.rowcount > 0


def delete_bet(bet_id: str) -> bool:
    with _conn() as con:
        cur = con.execute("DELETE FROM user_bets WHERE bet_id = ?", (bet_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Summary / analytics
# ---------------------------------------------------------------------------


def get_summary(user_id: str | None = None) -> dict[str, Any]:
    with _conn() as con:
        if user_id:
            rows = con.execute(
                "SELECT status, COUNT(*) AS n, SUM(stake) AS staked "
                "FROM user_bets WHERE user_id = ? GROUP BY status",
                (user_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT status, COUNT(*) AS n, SUM(stake) AS staked FROM user_bets GROUP BY status"
            ).fetchall()

    by_status: dict[str, int] = {}
    total_staked = 0.0
    for r in rows:
        by_status[r["status"]] = r["n"]
        total_staked += r["staked"] or 0.0

    return {
        "total_bets": sum(by_status.values()),
        "by_status": by_status,
        "total_staked": round(total_staked, 2),
        "pending": by_status.get("pending", 0),
        "settled": by_status.get("win", 0)
        + by_status.get("loss", 0)
        + by_status.get("push", 0),
    }


def _clv_valid_for_analytics(book_clv: object, odds: object) -> bool:
    """Skip corrupt odds/CLV rows so averages are not skewed by bad data."""
    if book_clv is None:
        return False
    try:
        clv = float(book_clv)
        if abs(clv) > 2.0:
            return False
    except (TypeError, ValueError):
        return False
    if odds is None:
        return False
    try:
        american = int(odds)
    except (TypeError, ValueError):
        return False
    return 100 <= abs(american) <= 50000


def _analytics_from_rows(rows: list) -> dict[str, Any]:
    settled_wins = settled_losses = settled_pushes = 0
    total_staked = total_returned = 0.0
    book_clv_list: list[float] = []

    by_market: dict[str, dict] = {}
    by_sport: dict[str, dict] = {}
    by_book: dict[str, dict] = {}
    by_league: dict[str, dict] = {}

    def _bucket(d: dict, key: str) -> dict:
        if key not in d:
            d[key] = {
                "wins": 0,
                "losses": 0,
                "pushes": 0,
                "pending": 0,
                "staked": 0.0,
                "returned": 0.0,
            }
        return d[key]

    def _payout(american_odds: float | None, stake: float) -> float:
        if american_odds is None:
            return stake
        if american_odds >= 100:
            return stake + stake * (american_odds / 100)
        return stake + stake * (100 / abs(american_odds))

    def _unit_profit(status: str, american_odds: float | None, stake: float) -> float | None:
        if status == "win":
            return _payout(american_odds, stake) - stake
        if status == "loss":
            return -stake
        if status == "push":
            return 0.0
        return None

    by_date_daily: dict[str, float] = {}

    for r in rows:
        status = r["status"]
        market = r["market"] or "unknown"
        sport = r["sport"] or "unknown"
        league = r["league"] or sport or "unknown"
        book = r["book"] or "unknown"
        odds = r["odds"]
        stake = r["stake"] or 0.0
        mb = _bucket(by_market, market)
        sb = _bucket(by_sport, sport)
        bb = _bucket(by_book, book)
        lb = _bucket(by_league, league)

        if status == "win":
            settled_wins += 1
            payout = _payout(odds, stake)
            total_staked += stake
            total_returned += payout
            mb["wins"] += 1
            mb["staked"] += stake
            mb["returned"] += payout
            sb["wins"] += 1
            sb["staked"] += stake
            sb["returned"] += payout
            bb["wins"] += 1
            bb["staked"] += stake
            bb["returned"] += payout
            lb["wins"] += 1
            lb["staked"] += stake
            lb["returned"] += payout
        elif status == "loss":
            settled_losses += 1
            total_staked += stake
            mb["losses"] += 1
            mb["staked"] += stake
            sb["losses"] += 1
            sb["staked"] += stake
            bb["losses"] += 1
            bb["staked"] += stake
            lb["losses"] += 1
            lb["staked"] += stake
        elif status == "push":
            settled_pushes += 1
            total_staked += stake
            total_returned += stake
            mb["pushes"] += 1
            mb["staked"] += stake
            mb["returned"] += stake
            sb["pushes"] += 1
            sb["staked"] += stake
            sb["returned"] += stake
            bb["pushes"] += 1
            bb["staked"] += stake
            bb["returned"] += stake
            lb["pushes"] += 1
            lb["staked"] += stake
            lb["returned"] += stake
        elif status == "pending":
            mb["pending"] += 1
            sb["pending"] += 1
            bb["pending"] += 1
            lb["pending"] += 1

        if status in ("win", "loss", "push") and _clv_valid_for_analytics(
            r["book_clv"], r["odds"]
        ):
            book_clv_list.append(float(r["book_clv"]))

        unit_profit = _unit_profit(status, odds, stake)
        event_date = r["date"]
        if unit_profit is not None and event_date:
            by_date_daily[event_date] = by_date_daily.get(event_date, 0.0) + unit_profit

    settled = settled_wins + settled_losses + settled_pushes
    decided = settled_wins + settled_losses
    win_rate = round(settled_wins / decided * 100, 1) if decided else None
    roi = (
        round((total_returned - total_staked) / total_staked * 100, 2)
        if total_staked
        else None
    )
    avg_clv = (
        round(sum(book_clv_list) / len(book_clv_list), 4) if book_clv_list else None
    )

    def _clean(d: dict) -> dict:
        for v in d.values():
            v["staked"] = round(v["staked"], 2)
            v["returned"] = round(v.get("returned", 0.0), 2)
            v["net"] = round(v["returned"] - v["staked"], 2)
            decided_g = v["wins"] + v["losses"]
            v["win_rate"] = (
                round(v["wins"] / decided_g * 100, 1) if decided_g else None
            )
        return d

    cumulative_units = 0.0
    by_date: list[dict[str, Any]] = []
    for day in sorted(by_date_daily.keys()):
        daily_units = round(by_date_daily[day], 2)
        cumulative_units = round(cumulative_units + daily_units, 2)
        by_date.append(
            {
                "date": day,
                "daily_units": daily_units,
                "cumulative_units": cumulative_units,
            }
        )

    return {
        "settled_bets": settled,
        "pending_bets": sum(v.get("pending", 0) for v in by_market.values()),
        "win_rate_pct": win_rate,
        "roi_pct": roi,
        "avg_clv": avg_clv,
        "total_staked": round(total_staked, 2),
        "total_returned": round(total_returned, 2),
        "net_profit": round(total_returned - total_staked, 2),
        "by_status": {
            "win": settled_wins,
            "loss": settled_losses,
            "push": settled_pushes,
        },
        "by_market": _clean(by_market),
        "by_sport": _clean(by_sport),
        "by_book": _clean(by_book),
        "by_league": _clean(by_league),
        "by_date": by_date,
    }


def _payout_from_odds(american_odds: float | None, stake: float) -> float:
    if american_odds is None:
        return stake
    if american_odds >= 100:
        return stake + stake * (american_odds / 100)
    return stake + stake * (100 / abs(american_odds))


def _unit_profit_from_bet(status: str, american_odds: float | None, stake: float) -> float | None:
    if status == "win":
        return _payout_from_odds(american_odds, stake) - stake
    if status == "loss":
        return -stake
    if status == "push":
        return 0.0
    return None


def _recent_graded_bets(
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    user_id: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    where = ["status IN ('win', 'loss', 'push')"]
    params: list[Any] = []
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    if date_from or date_to:
        where.append("date IS NOT NULL AND date != ''")
    if date_from:
        where.append("date >= ?")
        params.append(date_from)
    if date_to:
        where.append("date <= ?")
        params.append(date_to)

    clause = " AND ".join(where)
    sql = f"""
        SELECT bet_id, status, sport, league, player, market, pick,
               selection_line, event, date, book, odds, counterpart_odds,
               stake, nvig_at_placement, settled_at, created_at
        FROM user_bets
        WHERE {clause}
        ORDER BY COALESCE(settled_at, created_at) DESC
        LIMIT ?
    """
    params.append(limit)

    with _conn() as con:
        rows = con.execute(sql, params).fetchall()

    recent: list[dict[str, Any]] = []
    for r in rows:
        stake = r["stake"] or 0.0
        unit_profit = _unit_profit_from_bet(r["status"], r["odds"], stake)
        recent.append(
            {
                "bet_id": r["bet_id"],
                "status": r["status"],
                "sport": r["sport"],
                "league": r["league"],
                "player": r["player"],
                "market": r["market"],
                "pick": r["pick"],
                "selection_line": r["selection_line"],
                "event": r["event"],
                "date": r["date"],
                "book": r["book"],
                "odds": r["odds"],
                "counterpart_odds": r["counterpart_odds"],
                "stake": round(stake, 2),
                "nvig_at_placement": r["nvig_at_placement"],
                "unit_profit": round(unit_profit, 2) if unit_profit is not None else None,
            }
        )
    return recent


def _analytics_date_clause(
    date_from: str | None,
    date_to: str | None,
    *,
    user_id: str | None = None,
) -> tuple[str, list[Any]]:
    sql = (
        "SELECT status, market, sport, league, book, odds, stake, book_clv, date "
        "FROM user_bets WHERE 1=1"
    )
    params: list[Any] = []
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    if date_from or date_to:
        sql += " AND date IS NOT NULL AND date != ''"
    if date_from:
        sql += " AND date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND date <= ?"
        params.append(date_to)
    return sql, params


def get_analytics(
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    sql, params = _analytics_date_clause(date_from, date_to)
    with _conn() as con:
        rows = con.execute(sql, params).fetchall()
    result = _analytics_from_rows(rows)
    result["recent_bets"] = _recent_graded_bets(date_from, date_to)
    result["period"] = {
        "from": date_from,
        "to": date_to,
    }
    return result


def get_user_summary(user_id: str) -> dict[str, Any]:
    return get_summary(user_id)


def get_user_analytics(
    user_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    sql, params = _analytics_date_clause(date_from, date_to, user_id=user_id)
    with _conn() as con:
        rows = con.execute(sql, params).fetchall()
    result = _analytics_from_rows(rows)
    result["recent_bets"] = _recent_graded_bets(
        date_from, date_to, user_id=user_id
    )
    result["period"] = {
        "from": date_from,
        "to": date_to,
    }
    return result


def empty_user_analytics(
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Zeroed analytics payload when a scoped user has no bets yet."""
    result = _analytics_from_rows([])
    result["recent_bets"] = []
    result["period"] = {
        "from": date_from,
        "to": date_to,
    }
    return result


# Auto-settle worker + cron run logs (see auto_settle_runs.py)
from auto_settle_runs import (  # noqa: E402
    clear_shared_settle_job,
    finish_auto_settle_run,
    get_auto_settle_run_details,
    list_auto_settle_runs,
    list_due_shared_bets_for_cookie_users,
    list_shared_settle_jobs_needing_review,
    list_shared_settlements_needing_review,
    record_auto_settle_run_detail,
    record_shared_settle_job,
    record_shared_settlement_check,
    requeue_shared_settle_job,
    start_auto_settle_run,
)
