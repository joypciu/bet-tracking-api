"""
Bet Tracking API
================
Standalone FastAPI service for bet + user tracking.
Source of truth for all bets and users — the Cache API proxies to this service.

Port: 5002 (VPS-internal; not exposed via Nginx by default)
Auth: Cookie JWT (tracking_auth_token) for browser sessions;
      Bearer token (BET_API_TOKEN) for service/script access.
DB:   bets.db (SQLite, WAL mode) via BET_DB_PATH env var
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Body, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

load_dotenv()

import bet_tracking
import historics_bridge
import sports_bridge
from auth import require_auth, router as auth_router
from admin import router as admin_router

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(bet_tracking.init_db)
    yield


app = FastAPI(
    title="Bet Tracking API",
    description="Standalone bet and user tracking service. Source of truth for all bets.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(auth_router)
app.include_router(admin_router)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://tracking.keepbetting.co",
        "https://tracking.eternitylabs.co",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# CLV math helpers
# ---------------------------------------------------------------------------


def _american_to_decimal(american: int) -> float:
    if american > 0:
        return (american / 100) + 1.0
    return (100 / abs(american)) + 1.0


def _compute_nvig_ev(
    bet_odds: int, pick_odds: int, counterpart_odds: int
) -> float | None:
    """EV% of a bet using no-vig fair probability. All odds are American integers."""
    try:
        bet_dec = _american_to_decimal(bet_odds)
        pick_prob = 1.0 / _american_to_decimal(pick_odds)
        ctr_prob = 1.0 / _american_to_decimal(counterpart_odds)
        total = pick_prob + ctr_prob
        if total <= 0:
            return None
        fair_prob = pick_prob / total
        return round(fair_prob * bet_dec - 1.0, 4)
    except (ZeroDivisionError, ValueError, TypeError):
        return None


def _compute_nvig_clv_from_fair(bet_odds: int, nvig_odds: int) -> float | None:
    """EV% vs closing fair nvig line (same idea as EV feed get_ev_from_price)."""
    try:
        bet_dec = _american_to_decimal(bet_odds)
        fair_prob = 1.0 / _american_to_decimal(nvig_odds)
        return round(fair_prob * bet_dec - 1.0, 4)
    except (ZeroDivisionError, ValueError, TypeError):
        return None


def _compute_book_clv(bet_odds: int, closing_pick_odds: int) -> float | None:
    """% gain vs closing raw odds (positive = you beat the closing line)."""
    try:
        bet_dec = _american_to_decimal(bet_odds)
        closing_dec = _american_to_decimal(closing_pick_odds)
        if closing_dec <= 0:
            return None
        return round(bet_dec / closing_dec - 1.0, 4)
    except (ZeroDivisionError, ValueError, TypeError):
        return None


def _resolve_nvig_at_placement(
    bet_odds: int | None,
    nvig_odds_at_placement: int | None,
    counterpart_odds: int | None,
) -> float | None:
    if bet_odds is None:
        return None
    if nvig_odds_at_placement is not None:
        return _compute_nvig_clv_from_fair(bet_odds, nvig_odds_at_placement)
    if counterpart_odds is not None:
        return _compute_nvig_ev(bet_odds, bet_odds, counterpart_odds)
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
# Request model (mirrors cache-api CreateBetRequest exactly)
# ---------------------------------------------------------------------------


class CreateBetRequest(BaseModel):
    market: str
    pick: Optional[str] = Field(None)
    sport: Optional[str] = Field(None)
    date: Optional[str] = Field(None)
    league: Optional[str] = Field(None)
    event: Optional[str] = Field(None)
    datetime: Optional[str] = Field(None)
    player: Optional[str] = Field(None)
    team: Optional[str] = Field(None)
    home_team: Optional[str] = Field(None)
    away_team: Optional[str] = Field(None)
    event_id: Optional[str] = Field(None)
    line: Optional[str | float] = Field(None)
    odds: Optional[int] = Field(None)
    stake: Optional[float] = Field(None)
    notes: Optional[str] = Field(None)
    email: Optional[str] = Field(None)
    book: Optional[str] = Field(None)
    counterpart_odds: Optional[int] = Field(
        None,
        description="Other side's American odds at placement — enables nvig_at_placement EV calculation",
    )
    nvig_odds_at_placement: Optional[int] = Field(
        None,
        description="Fair no-vig American odds for this prop at track time (EV feed Weighted Market Average)",
    )
    historics_context: Optional[str] = Field(
        None,
        description="JWT from EV feed data-historics — used to compute CLV after settlement",
    )

    _raw_line: Optional[str] = PrivateAttr(default=None)

    @field_validator("nvig_odds_at_placement", mode="before")
    @classmethod
    def sanitize_nvig_odds_at_placement(cls, v: object) -> int | None:
        if v is None or v == "":
            return None
        try:
            n = int(v)
        except (ValueError, TypeError):
            return None
        if n == 0 or abs(n) < 100 or abs(n) > 50000:
            return None
        return n

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: str) -> str:
        norm = re.sub(r"[^a-z0-9]+", "_", v.strip().lower()).strip("_")
        game_aliases = {
            "moneyline": "moneyline",
            "ml": "moneyline",
            "spread": "spread",
            "game_spread": "spread",
            "point_spread": "spread",
            "ats": "spread",
            "puck_line": "puck_line",
            "run_line": "run_line",
            "total": "total",
            "game_total": "total",
            "ou": "total",
            "o_u": "total",
            "total_points": "total",
            "total_goals": "total_goals",
            "total_runs": "total_runs",
            "total_corners": "total_corners",
        }
        if norm in game_aliases:
            return game_aliases[norm]
        if norm.startswith("player_"):
            return norm
        return norm

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("date must be YYYY-MM-DD")
        return v

    @field_validator("datetime")
    @classmethod
    def validate_datetime(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("datetime must be timezone-aware ISO-8601")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            try:
                return bet_tracking.validate_email(v)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        return v

    @model_validator(mode="after")
    def normalize(self) -> "CreateBetRequest":
        if isinstance(self.line, str):
            self._raw_line = self.line

        if self.datetime and not self.date:
            self.date = self.datetime[:10]

        if self.event and (not self.home_team or not self.away_team):
            for sep in (" vs ", " VS ", " v ", " @ "):
                if sep in self.event:
                    left, right = self.event.split(sep, 1)
                    if not self.home_team:
                        self.home_team = left.strip()
                    if not self.away_team:
                        self.away_team = right.strip()
                    break

        # For player props: parse selection string into pick direction + numeric line.
        # Handles strings like "Devin Vassell Under 1.5" or "Over 22.5" regardless
        # of whether pick was already supplied in the payload.
        if self.market.startswith("player_") and isinstance(self.line, str):
            raw_line = self.line.strip()
            direction = re.search(r"\b(over|under)\b", raw_line, re.IGNORECASE)
            if direction and not self.pick:
                self.pick = direction.group(1).lower()
            number = re.search(r"(\d+(?:\.\d+)?)\s*$", raw_line)
            if number:
                self.line = float(number.group(1))

        # For total markets: parse "Over 221.5" / "Under 47.5" into pick + numeric line.
        _TOTAL_MARKETS = {
            "total",
            "total_goals",
            "total_runs",
            "total_corners",
            "game_total",
        }
        if self.market in _TOTAL_MARKETS and isinstance(self.line, str):
            raw_line = self.line.strip()
            direction = re.search(r"\b(over|under)\b", raw_line, re.IGNORECASE)
            if direction and not self.pick:
                self.pick = direction.group(1).lower()
            number = re.search(r"(\d+(?:\.\d+)?)\s*$", raw_line)
            if number:
                self.line = float(number.group(1))

        if self.pick is None and isinstance(self.line, str):
            raw = self.line.strip()
            if self.market == "moneyline":
                self.pick = raw
                if not self.team:
                    self.team = raw
            elif _is_spread_market(self.market):
                parsed = _parse_spread_selection(raw)
                if parsed:
                    self.pick, self.line = parsed[0], parsed[1]
                    if not self.team:
                        self.team = self.pick
            else:
                self.pick = raw

        if _is_spread_market(self.market) and self.pick:
            parsed = _parse_spread_selection(str(self.pick))
            if parsed:
                self.pick, self.line = parsed[0], parsed[1]
                if not self.team:
                    self.team = self.pick

        _CORE = {"moneyline", "spread", "total", "puck_line", "run_line"}
        _is_specialty = self.market not in _CORE
        _is_prop = self.market.startswith("player_")

        if _is_prop:
            if self.pick is None:
                raise ValueError(
                    "pick is required for player prop bets. Use 'over' or 'under'."
                )
            if self.pick.lower() not in {"over", "under"}:
                raise ValueError(
                    f"pick must be 'over' or 'under' for player prop market '{self.market}'."
                )
            self.pick = self.pick.lower()
            if not self.player:
                raise ValueError("player name is required for player prop bets.")
        elif self.pick is None and not _is_specialty:
            raise ValueError(
                "pick is required, or provide a selection line that implies the pick"
            )

        return self


# ---------------------------------------------------------------------------
# Settlement helpers
# ---------------------------------------------------------------------------

_LEAGUE_TO_ESPN_PATH: dict[str, tuple[str, str]] = {
    # Tennis
    "atp": ("tennis", "atp"),
    "wta": ("tennis", "wta"),
    "itf": ("tennis", "itf-men"),
    "challenger": ("tennis", "atp-challenger"),
    # Basketball
    "nba": ("basketball", "nba"),
    "wnba": ("basketball", "wnba"),
    "ncaab": ("basketball", "mens-college-basketball"),
    "nba g league": ("basketball", "nba-g-league"),
    # Baseball
    "mlb": ("baseball", "mlb"),
    # Hockey
    "nhl": ("hockey", "nhl"),
    # Soccer — scoreboard only (halftime via summary endpoint)
    "epl": ("soccer", "eng.1"),
    "eng.1": ("soccer", "eng.1"),
    "laliga": ("soccer", "esp.1"),
    "esp.1": ("soccer", "esp.1"),
    "bundesliga": ("soccer", "ger.1"),
    "ger.1": ("soccer", "ger.1"),
    "seriea": ("soccer", "ita.1"),
    "ita.1": ("soccer", "ita.1"),
    "ligue1": ("soccer", "fra.1"),
    "fra.1": ("soccer", "fra.1"),
    "mls": ("soccer", "usa.1"),
    "ucl": ("soccer", "uefa.champions"),
    "uel": ("soccer", "uefa.europa"),
    # MMA
    "ufc": ("mma", "ufc"),
    "mma": ("mma", "ufc"),
    "bellator": ("mma", "bellator"),
}

_AUTO_SETTLEABLE = {
    # ── Core markets (all sports) ──────────────────────────────────────────
    "moneyline",
    "spread",
    "total",
    "puck_line",
    "run_line",
    # ── Baseball ──────────────────────────────────────────────────────────
    "total_goals",
    "total_runs",
    "total_corners",
    "team_total",
    "1st_half_total_runs",
    "1st_half_team_total",
    "1st_inning_moneyline",
    "1st_3_innings_moneyline",
    "1st_7_innings_moneyline",
    "1st_half_moneyline",
    "1st_5_innings_moneyline",
    "1st_inning_run_line",
    "1st_3_innings_run_line",
    "1st_7_innings_run_line",
    "1st_half_run_line",
    "1st_5_innings_total",
    "1st_5_innings_team_total",
    "1st_inning_total_runs",
    "1st_3_innings_total_runs",
    "2nd_inning_total_runs",
    "3rd_inning_total_runs",
    "4th_inning_total_runs",
    "5th_inning_total_runs",
    "6th_inning_total_runs",
    "7th_inning_total_runs",
    "8th_inning_total_runs",
    "9th_inning_total_runs",
    "1st_inning_total_runs_odd_even",
    "1st_7_innings_total_runs",
    "total_runs_odd_even",
    # ── Soccer — full-game ─────────────────────────────────────────────────
    "both_teams_to_score",
    "draw_bet",
    "asian_total_goals",
    "total_goals_odd_even",
    "goal_both_halves",
    "team_total_corners",
    "total_corners_odd_even",
    # ── Soccer — scorer / card markets (pick = player name) ────────────────
    "anytime_goal_scorer",
    "first_goal_scorer",
    "last_goal_scorer",
    "anytime_card_receiver",
    # ── Soccer — 1st half (settled via SofaScore through stats_api) ────────
    "1st_half_both_teams_to_score",
    "1st_half_draw_bet",
    "1st_half_total_goals",
    "1st_half_asian_total_goals",
    "1st_half_team_total",
    "1st_half_total_goals_odd_even",
    "1st_half_total_corners",
    "1st_half_team_total_corners",
    # ── Soccer — 2nd half ──────────────────────────────────────────────────
    "2nd_half_both_teams_to_score",
    "2nd_half_draw_bet",
    "2nd_half_total_goals",
    "2nd_half_asian_total_goals",
    "2nd_half_team_total",
    "2nd_half_total_corners",
    "2nd_half_team_total_corners",
    # NBA period/specialty markets routed via stats_api -> nba_period_props
    "1st_quarter_total_points",
    "2nd_quarter_total_points",
    "3rd_quarter_total_points",
    "4th_quarter_total_points",
    "1st_quarter_total_points_odd_even",
    "2nd_quarter_total_points_odd_even",
    "3rd_quarter_total_points_odd_even",
    "1st_quarter_moneyline",
    "2nd_quarter_moneyline",
    "3rd_quarter_moneyline",
    "4th_quarter_moneyline",
    "1st_quarter_point_spread",
    "2nd_quarter_point_spread",
    "3rd_quarter_point_spread",
    "4th_quarter_point_spread",
    "1st_quarter_team_total",
    "2nd_quarter_team_total",
    "3rd_quarter_team_total",
    "4th_quarter_team_total",
    "1st_half_total_points",
    "1st_half_total_points_odd_even",
    "2nd_half_total_points_odd_even",
    "1st_half_point_spread",
    "1st_quarter_player_points",
    "2nd_quarter_player_points",
    "3rd_quarter_player_points",
    "4th_quarter_player_points",
    "1st_half_player_points",
    "2nd_half_player_points",
    "total_points_odd_even",
    "will_there_be_overtime",
    "team_first_basket",
    "first_basket",
    "first_basket_including_ft",
    # MMA markets
    "go_the_distance",
    "total_rounds",
    # ── Tennis — set-level and match-level markets  (settled via SofaScore) ──
    "game_spread",
    "total_games",
    "total_sets",
    "set_handicap",
    "1st_set_moneyline",
    "1st_set_game_spread",
    "1st_set_total_games",
    "1st_set_will_there_be_a_tiebreak",
    "2nd_set_moneyline",
    "2nd_set_total_games",
    # ── Hockey — full-game (settled via SofaScore through stats_api) ──────────
    "both_teams_to_score_reg_time",
    # ── Hockey — 1st period ───────────────────────────────────────────────────
    "1st_period_moneyline",  # ESPN fallback; SofaScore primary
    "1st_period_total_goals",  # ESPN fallback; SofaScore primary
    "1st_period_puck_line",
    "1st_period_total_goals_odd_even",
    "1st_period_team_total",
    "1st_period_both_teams_to_score",
    # ── Hockey — 2nd period ───────────────────────────────────────────────────
    "2nd_period_moneyline",  # ESPN fallback; SofaScore primary
    "2nd_period_total_goals",  # ESPN fallback; SofaScore primary
    "2nd_period_puck_line",
    "2nd_period_total_goals_odd_even",
    "2nd_period_team_total",
    "2nd_period_both_teams_to_score",
    # ── Hockey — 3rd period ───────────────────────────────────────────────────
    "3rd_period_moneyline",
    "3rd_period_puck_line",
    "3rd_period_total_goals",
    "3rd_period_total_goals_odd_even",
}

# MLB period markets that should settle via stats_api market-check (Savant /gf)
_MLB_MARKETCHECK_ROUTED = {
    "1st_half_total_runs",
    "team_total",
    "1st_half_moneyline",
    "run_line",
    "moneyline",
    "total_runs",
    "1st_inning_moneyline",
    "1st_3_innings_moneyline",
    "1st_7_innings_moneyline",
    "1st_inning_run_line",
    "1st_3_innings_run_line",
    "1st_7_innings_run_line",
    "1st_half_run_line",
    "1st_5_innings_moneyline",
    "1st_5_innings_total",
    "1st_5_innings_team_total",
    "1st_inning_total_runs",
    "2nd_inning_total_runs",
    "3rd_inning_total_runs",
    "4th_inning_total_runs",
    "5th_inning_total_runs",
    "6th_inning_total_runs",
    "7th_inning_total_runs",
    "8th_inning_total_runs",
    "9th_inning_total_runs",
    "1st_3_innings_total_runs",
    "1st_7_innings_total_runs",
    "1st_inning_total_runs_odd_even",
    "total_runs_odd_even",
    "1st_half_team_total",
}

# Period markets that can be auto-settled from ESPN linescore data
_PERIOD_SETTLEABLE = {
    # Basketball — quarter & half
    "1st_quarter_moneyline",
    "1st_quarter_point_spread",
    "1st_quarter_total_points",
    "1st_quarter_team_total",
    "1st_half_moneyline",
    "1st_half_point_spread",
    "1st_half_total_points",
    "1st_half_team_total",
    "1st_half_home_team_total",
    "1st_half_away_team_total",
    "2nd_half_moneyline",
    "2nd_half_total_points",
    "2nd_half_team_total",
    "2nd_half_both_teams_to_score",
    # Baseball — innings
    "1st_inning_total_runs",
    "1st_3_innings_run_line",
    "1st_3_innings_total_runs",
    "1st_5_innings_moneyline",
    "1st_5_innings_total",
    "1st_5_innings_team_total",
    # Hockey — period
    "1st_period_moneyline",
    "1st_period_total_goals",
    "2nd_period_moneyline",
    "2nd_period_total_goals",
    # Soccer halftime markets were moved to _AUTO_SETTLEABLE → SofaScore via stats_api.
    # ESPN period settlement for soccer is no longer used as primary source.
}


def _is_prop_market(market: str) -> bool:
    return market.startswith("player_")


def _normalize_settlement_market(market: str) -> str:
    """Map upstream market names to stats_api settlement names."""
    m = (market or "").strip().lower()
    if m == "total_points":
        return "total"
    return m


_SPREAD_CORE_MARKETS = frozenset(
    {"spread", "puck_line", "run_line", "set_handicap", "game_spread"}
)
_SPREAD_PICK_RE = re.compile(r"^(?P<team>.+?)\s+(?P<spread>[+-]?\d+(?:\.\d+)?)$")


def _is_spread_market(market: str) -> bool:
    m = (market or "").lower()
    return (
        m in _SPREAD_CORE_MARKETS
        or m.endswith("_point_spread")
        or m.endswith("_run_line")
    )


def _parse_spread_selection(raw: str) -> tuple[str, float] | None:
    m = _SPREAD_PICK_RE.match(raw.strip())
    if not m:
        return None
    return m.group("team").strip(), float(m.group("spread"))


def _match_team_pick(team_pick: str, team_name: str) -> bool:
    tp = team_pick.strip().lower()
    tn = team_name.strip().lower()
    if tp == tn or tp in tn or tn in tp:
        return True
    words = tn.split()
    initials = "".join(w[0] for w in words if w)
    return tp == initials


def _resolve_spread_team_pick(team_pick: str, bet: dict) -> str:
    """Map abbrevs like NYK to the full home/away team name on the bet."""
    for field in ("home_team", "away_team", "team"):
        name = bet.get(field)
        if name and _match_team_pick(team_pick, name):
            return name
    return team_pick


def _spread_pick_and_line_for_settlement(bet: dict) -> tuple[str, float | None]:
    """Split combined spread picks like 'NYK +1.5' into team + numeric line."""
    pick = str(bet.get("pick") or "").strip()
    line = _line_for_settlement(bet)
    if not _is_spread_market(bet.get("market", "")):
        return pick, line

    for raw in (pick, str(bet.get("selection_line") or "").strip()):
        if not raw:
            continue
        parsed = _parse_spread_selection(raw)
        if parsed:
            team_pick, spread_line = parsed
            return _resolve_spread_team_pick(team_pick, bet), spread_line

    return pick, line


def _line_for_settlement(bet: dict) -> float | None:
    """Return numeric line from line field or selection_line text (e.g., 'Under 3.5')."""
    raw_line = bet.get("line")
    if isinstance(raw_line, (int, float)):
        return float(raw_line)

    selection_line = str(bet.get("selection_line") or "")
    matches = re.findall(r"[-+]?\d+(?:\.\d+)?", selection_line)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def _is_future_game(bet: dict) -> bool:
    """Return True when bet is for a future game based on datetime/date fields."""
    event_dt_raw = bet.get("event_datetime") or bet.get("datetime")
    if isinstance(event_dt_raw, str) and event_dt_raw.strip():
        try:
            event_dt = datetime.fromisoformat(event_dt_raw.replace("Z", "+00:00"))
            if event_dt.tzinfo is None or event_dt.utcoffset() is None:
                event_dt = event_dt.replace(tzinfo=timezone.utc)
            return event_dt > datetime.now(timezone.utc)
        except ValueError:
            pass

    date_raw = bet.get("date")
    if isinstance(date_raw, str) and date_raw.strip():
        try:
            game_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
            return game_date > datetime.now(timezone.utc).date()
        except ValueError:
            return False

    return False


async def _resolve_game_for_bet(bet: dict) -> tuple[str | None, str | None, str | None]:
    event_id = bet.get("event_id")
    home_team = bet.get("home_team")
    away_team = bet.get("away_team")
    return event_id, home_team, away_team


async def _espn_fetch_competition(bet: dict) -> tuple[dict | None, str | None]:
    """
    Fetch the ESPN competition object for a bet.
    Returns (competition_dict, league_path) or (None, None).
    For soccer, also returns the event_id so the caller can fetch halftime via summary.
    """
    import httpx as _httpx

    league = (bet.get("league") or "").lower().strip()
    sport = (bet.get("sport") or "").lower().strip()
    date = bet.get("date")

    espn_path = _LEAGUE_TO_ESPN_PATH.get(league)
    if not espn_path:
        # Infer from sport when league is missing
        if "basketball" in sport:
            espn_path = ("basketball", "nba")
        elif "baseball" in sport:
            espn_path = ("baseball", "mlb")
        elif "hockey" in sport:
            espn_path = ("hockey", "nhl")
        elif "tennis" in sport:
            espn_path = ("tennis", "atp")
        elif "soccer" in sport or "football" in sport:
            espn_path = ("soccer", "eng.1")

    if not espn_path or not date:
        return None, None

    sport_path, league_path = espn_path
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/{league_path}/scoreboard"

    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                url,
                params={"dates": date.replace("-", ""), "limit": 100},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code != 200:
                return None, None
            data = r.json()
    except Exception:
        return None, None

    all_competitions: list[dict] = []
    for event in data.get("events", []):
        groupings = event.get("groupings")
        comps = []
        if groupings:
            for g in groupings:
                comps.extend(g.get("competitions", []))
        else:
            comps = event.get("competitions", [event])
        for c in comps:
            c["_event_id"] = event.get("id", "")
            c["_league_path"] = league_path
            c["_sport_path"] = sport_path
        all_competitions.extend(comps)

    home_want = (bet.get("home_team") or "").lower()
    away_want = (bet.get("away_team") or "").lower()
    team_want = (bet.get("team") or "").lower()

    def _comp_names(comp: dict) -> list[str]:
        out = []
        for c in comp.get("competitors", []):
            for key in ("athlete", "team"):
                obj = c.get(key) or {}
                for field in (
                    "displayName",
                    "shortDisplayName",
                    "abbreviation",
                    "lastName",
                ):
                    v = obj.get(field)
                    if v:
                        out.append(v.lower())
        return out

    def _name_matches(wanted: str, name_list: list[str]) -> bool:
        w = wanted.lower()
        return any(w in n or n in w for n in name_list)

    for comp in all_competitions:
        names = _comp_names(comp)
        if home_want and away_want:
            if _name_matches(home_want, names) and _name_matches(away_want, names):
                return comp, league_path
        elif team_want:
            if _name_matches(team_want, names):
                return comp, league_path

    return None, None


def _market_stat_from_period_detail(period_det: dict) -> float | None:
    """Extract the market-relevant numeric result from ESPN period settlement detail."""
    if not period_det:
        return None
    for key in (
        "ht_total",
        "q1_total",
        "first_half_total",
        "second_half_total",
        "p1_total",
        "p2_total",
        "1st_inn_total",
        "first_3inn_total",
        "first_5inn_total",
    ):
        if key in period_det:
            return float(period_det[key])
    pick_key = next((k for k in period_det if k.startswith("pick_")), None)
    opp_key = next((k for k in period_det if k.startswith("opp_")), None)
    if pick_key and opp_key:
        return float(period_det[pick_key]) - float(period_det[opp_key])
    if pick_key:
        return float(period_det[pick_key])
    return None


def _normalize_settlement_score(
    score: dict | None = None,
    *,
    home: float | int | None = None,
    away: float | int | None = None,
) -> dict | None:
    if isinstance(score, dict) and score:
        out = dict(score)
        if out.get("total") is None and out.get("home") is not None and out.get("away") is not None:
            out["total"] = out["home"] + out["away"]
        return out
    if home is not None or away is not None:
        total = (home + away) if home is not None and away is not None else None
        return {"home": home, "away": away, "total": total}
    return None


def _settlement_stat_fields(
    *,
    stat_value: float | None = None,
    stat_name: str | None = None,
    player_stat_value: float | None = None,
    stored_stat_name: str | None = None,
) -> dict:
    value = stat_value if stat_value is not None else player_stat_value
    name = stat_name or stored_stat_name
    fields: dict = {}
    if value is not None:
        fields["stat_value"] = value
    if name:
        fields["stat_name"] = name
    return fields


async def _espn_settle_period_bet(bet: dict) -> dict | None:
    """Settle period/half/inning markets using ESPN linescore data (free, no key needed)."""
    import httpx as _httpx

    market = (bet.get("market") or "").lower()
    team_pick, line_val = _spread_pick_and_line_for_settlement(bet)
    pick_raw = team_pick.lower()
    sport = (bet.get("sport") or "").lower()

    comp, league_path = await _espn_fetch_competition(bet)
    if not comp:
        return None

    st = comp.get("status", {})
    if st.get("type", {}).get("state") != "post":
        return {
            "outcome": "pending",
            "settled": False,
            "source": "espn_public",
            "score": None,
            "pricing": None,
            "note": f"Game not yet final — {st.get('type', {}).get('description', 'in progress')}",
        }

    competitors = comp.get("competitors", [])
    home_comp = next(
        (c for c in competitors if c.get("homeAway") == "home"),
        competitors[0] if competitors else {},
    )
    away_comp = next(
        (c for c in competitors if c.get("homeAway") == "away"),
        competitors[1] if len(competitors) > 1 else {},
    )

    # ── Soccer: fetch halftime from summary endpoint ──────────────────────────
    is_soccer = (
        "soccer" in sport
        or "football" in sport
        or "soccer" in (comp.get("_sport_path") or "")
    )
    if is_soccer:
        event_id = comp.get("_event_id", "")
        lp = comp.get("_league_path", league_path or "eng.1")
        summary_url = (
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{lp}/summary"
        )
        try:
            async with _httpx.AsyncClient(timeout=10.0) as client:
                sr = await client.get(
                    summary_url,
                    params={"event": event_id},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if sr.status_code == 200:
                    sdata = sr.json()
                    for sc in sdata.get("header", {}).get("competitions") or []:
                        for c in sc.get("competitors", []):
                            ha = c.get("homeAway", "")
                            if ha == "home":
                                home_comp = {
                                    **home_comp,
                                    "linescores": c.get("linescores", []),
                                }
                            elif ha == "away":
                                away_comp = {
                                    **away_comp,
                                    "linescores": c.get("linescores", []),
                                }
        except Exception:
            pass

    def _period_scores(comp_c: dict) -> dict[int, float]:
        out: dict[int, float] = {}
        for i, ls in enumerate(comp_c.get("linescores") or [], start=1):
            key = ls.get("period", i)
            try:
                out[key] = float(ls.get("value") or ls.get("displayValue") or 0)
            except (TypeError, ValueError):
                out[key] = 0.0
        return out

    home_scores = _period_scores(home_comp)
    away_scores = _period_scores(away_comp)

    if not home_scores and not away_scores:
        return None

    home_name = (home_comp.get("team", {}) or {}).get("displayName", "home")
    away_name = (away_comp.get("team", {}) or {}).get("displayName", "away")
    home_abbr = ((home_comp.get("team", {}) or {}).get("abbreviation") or "").lower()
    away_abbr = ((away_comp.get("team", {}) or {}).get("abbreviation") or "").lower()
    bet_home = (bet.get("home_team") or "").lower()
    bet_away = (bet.get("away_team") or "").lower()

    def _pick_side_matches(side_name: str, side_abbr: str, side_bet: str) -> bool:
        return (
            pick_raw == side_abbr
            or pick_raw in side_name
            or side_name in pick_raw
            or (side_bet and (pick_raw in side_bet or side_bet in pick_raw))
        )

    # Resolve pick side
    if pick_raw == "home":
        pick_scores, opp_scores = home_scores, away_scores
    elif pick_raw == "away":
        pick_scores, opp_scores = away_scores, home_scores
    elif _pick_side_matches(home_name.lower(), home_abbr, bet_home):
        pick_scores, opp_scores = home_scores, away_scores
    elif _pick_side_matches(away_name.lower(), away_abbr, bet_away):
        pick_scores, opp_scores = away_scores, home_scores
    else:
        pick_scores, opp_scores = away_scores, home_scores

    def _sum(scores: dict, periods: list[int]) -> float:
        return sum(scores.get(p, 0.0) for p in periods)

    def _ml(ps: float, os: float) -> str:
        return "win" if ps > os else ("loss" if ps < os else "push")

    def _spread(ps: float, os: float, line: float) -> str:
        adj = ps + line
        return "win" if adj > os else ("loss" if adj < os else "push")

    def _total(total: float, line: float, pick: str) -> str:
        if pick == "over":
            return "win" if total > line else ("push" if total == line else "loss")
        return "win" if total < line else ("push" if total == line else "loss")

    def _team_total(ps: float, line: float, pick: str) -> str:
        if pick in ("over", "home", "away"):
            pick_dir = "over" if pick == "over" else pick_raw
        pick_dir = pick_raw
        if pick_dir == "over":
            return "win" if ps > line else ("push" if ps == line else "loss")
        return "win" if ps < line else ("push" if ps == line else "loss")

    outcome = "pending"
    period_det: dict = {}

    # ── Basketball ────────────────────────────────────────────────────────────
    if market == "1st_quarter_moneyline":
        ps, os = _sum(pick_scores, [1]), _sum(opp_scores, [1])
        period_det = {"pick_q1": ps, "opp_q1": os}
        outcome = _ml(ps, os)

    elif market == "1st_quarter_point_spread" and line_val is not None:
        ps, os = _sum(pick_scores, [1]), _sum(opp_scores, [1])
        period_det = {"pick_q1": ps, "opp_q1": os}
        outcome = _spread(ps, os, float(line_val))

    elif market == "1st_quarter_total_points" and line_val is not None:
        total = _sum(home_scores, [1]) + _sum(away_scores, [1])
        period_det = {"q1_total": total}
        outcome = _total(total, float(line_val), pick_raw)

    elif market == "1st_quarter_team_total" and line_val is not None:
        ps = _sum(pick_scores, [1])
        period_det = {"pick_q1": ps}
        outcome = _team_total(ps, float(line_val), pick_raw)

    elif market == "1st_half_moneyline":
        ps, os = _sum(pick_scores, [1, 2]), _sum(opp_scores, [1, 2])
        period_det = {"pick_1h": ps, "opp_1h": os}
        outcome = _ml(ps, os)

    elif market == "1st_half_total_points" and line_val is not None:
        total = _sum(home_scores, [1, 2]) + _sum(away_scores, [1, 2])
        period_det = {"first_half_total": total}
        outcome = _total(total, float(line_val), pick_raw)

    elif market == "1st_half_point_spread" and line_val is not None:
        ps, os = _sum(pick_scores, [1, 2]), _sum(opp_scores, [1, 2])
        period_det = {"pick_1h": ps, "opp_1h": os}
        outcome = _spread(ps, os, float(line_val))

    elif (
        market
        in (
            "1st_half_team_total",
            "1st_half_home_team_total",
            "1st_half_away_team_total",
        )
        and line_val is not None
    ):
        ps = _sum(pick_scores, [1, 2])
        period_det = {"pick_1h": ps}
        outcome = _team_total(ps, float(line_val), pick_raw)

    elif market == "2nd_half_moneyline":
        ps, os = _sum(pick_scores, [3, 4]), _sum(opp_scores, [3, 4])
        period_det = {"pick_2h": ps, "opp_2h": os}
        outcome = _ml(ps, os)

    elif market == "2nd_half_total_points" and line_val is not None:
        total = _sum(home_scores, [3, 4]) + _sum(away_scores, [3, 4])
        period_det = {"second_half_total": total}
        outcome = _total(total, float(line_val), pick_raw)

    elif market == "2nd_half_team_total" and line_val is not None:
        ps = _sum(pick_scores, [3, 4])
        period_det = {"pick_2h": ps}
        outcome = _team_total(ps, float(line_val), pick_raw)

    elif market == "2nd_half_both_teams_to_score":
        hs2 = _sum(home_scores, [3, 4])
        as2 = _sum(away_scores, [3, 4])
        period_det = {"home_2h": hs2, "away_2h": as2}
        outcome = "win" if hs2 > 0 and as2 > 0 else "loss"

    # ── Baseball ─────────────────────────────────────────────────────────────
    elif market == "1st_inning_total_runs" and line_val is not None:
        total = _sum(home_scores, [1]) + _sum(away_scores, [1])
        period_det = {"1st_inn_total": total}
        outcome = _total(total, float(line_val), pick_raw)

    elif market == "1st_3_innings_run_line" and line_val is not None:
        ps, os = _sum(pick_scores, [1, 2, 3]), _sum(opp_scores, [1, 2, 3])
        period_det = {"pick_3inn": ps, "opp_3inn": os}
        outcome = _spread(ps, os, float(line_val))

    elif market == "1st_3_innings_total_runs" and line_val is not None:
        total = _sum(home_scores, [1, 2, 3]) + _sum(away_scores, [1, 2, 3])
        period_det = {"first_3inn_total": total}
        outcome = _total(total, float(line_val), pick_raw)

    elif market == "1st_5_innings_moneyline":
        ps, os = _sum(pick_scores, [1, 2, 3, 4, 5]), _sum(opp_scores, [1, 2, 3, 4, 5])
        period_det = {"pick_5inn": ps, "opp_5inn": os}
        outcome = _ml(ps, os)

    elif market == "1st_5_innings_total" and line_val is not None:
        total = _sum(home_scores, [1, 2, 3, 4, 5]) + _sum(away_scores, [1, 2, 3, 4, 5])
        period_det = {"first_5inn_total": total}
        outcome = _total(total, float(line_val), pick_raw)

    elif market == "1st_5_innings_team_total" and line_val is not None:
        ps = _sum(pick_scores, [1, 2, 3, 4, 5])
        period_det = {"pick_5inn": ps}
        outcome = _team_total(ps, float(line_val), pick_raw)

    # ── Hockey ───────────────────────────────────────────────────────────────
    elif market == "1st_period_moneyline":
        ps, os = _sum(pick_scores, [1]), _sum(opp_scores, [1])
        period_det = {"pick_p1": ps, "opp_p1": os}
        outcome = _ml(ps, os)

    elif market == "1st_period_total_goals" and line_val is not None:
        total = _sum(home_scores, [1]) + _sum(away_scores, [1])
        period_det = {"p1_total": total}
        outcome = _total(total, float(line_val), pick_raw)

    elif market == "2nd_period_moneyline":
        ps, os = _sum(pick_scores, [2]), _sum(opp_scores, [2])
        period_det = {"pick_p2": ps, "opp_p2": os}
        outcome = _ml(ps, os)

    elif market == "2nd_period_total_goals" and line_val is not None:
        total = _sum(home_scores, [2]) + _sum(away_scores, [2])
        period_det = {"p2_total": total}
        outcome = _total(total, float(line_val), pick_raw)

    # ── Soccer halftime ───────────────────────────────────────────────────────
    elif market in (
        "1st_half_both_teams_to_score",
        "1st_half_draw_bet",
        "1st_half_total_goals",
        "1st_half_asian_total_goals",
    ):
        # linescores index 0 = first half for soccer
        h1 = home_scores.get(1, 0.0)
        a1 = away_scores.get(1, 0.0)
        period_det = {"home_ht": h1, "away_ht": a1, "ht_total": h1 + a1}

        if market == "1st_half_both_teams_to_score":
            outcome = "win" if h1 > 0 and a1 > 0 else "loss"
        elif market == "1st_half_draw_bet":
            outcome = "win" if h1 == a1 else "loss"
        elif (
            market in ("1st_half_total_goals", "1st_half_asian_total_goals")
            and line_val is not None
        ):
            total = h1 + a1
            outcome = _total(total, float(line_val), pick_raw)

    else:
        return None

    if outcome == "pending":
        return None

    stat_value = _market_stat_from_period_detail(period_det)

    return {
        "outcome": outcome,
        "settled": True,
        "source": "espn_public",
        "stat_value": stat_value,
        "stat_name": market,
        "score": {
            "home": float(home_comp.get("score", 0) or 0),
            "away": float(away_comp.get("score", 0) or 0),
            "home_team": home_name,
            "away_team": away_name,
            "period_detail": period_det,
        },
        "pricing": None,
        "note": f"Auto-settled from ESPN period data ({market})",
    }


async def _espn_settle_bet(bet: dict) -> dict | None:
    import httpx as _httpx

    league = (bet.get("league") or "").lower().strip()
    sport = (bet.get("sport") or "").lower().strip()
    date = bet.get("date")

    espn_path = _LEAGUE_TO_ESPN_PATH.get(league)
    if not espn_path and "tennis" in sport:
        espn_path = ("tennis", "atp")
    if not espn_path or not date:
        return None

    sport_path, league_path = espn_path
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/{league_path}/scoreboard"

    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                url,
                params={"dates": date.replace("-", ""), "limit": 100},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
    except Exception as exc:
        print(f"[WARN] ESPN fallback fetch failed for {league_path}/{date}: {exc}")
        return None

    all_competitions: list[dict] = []
    for event in data.get("events", []):
        groupings = event.get("groupings")
        if groupings:
            for g in groupings:
                all_competitions.extend(g.get("competitions", []))
        else:
            all_competitions.extend(event.get("competitions", [event]))

    home_want = (bet.get("home_team") or "").lower()
    away_want = (bet.get("away_team") or "").lower()
    team_want = (bet.get("team") or bet.get("pick") or "").lower()

    def _comp_names(comp: dict) -> list[str]:
        out = []
        for c in comp.get("competitors", []):
            for key in ("athlete", "team"):
                obj = c.get(key) or {}
                for field in (
                    "displayName",
                    "shortDisplayName",
                    "abbreviation",
                    "lastName",
                ):
                    v = obj.get(field)
                    if v:
                        out.append(v.lower())
        return out

    def _name_matches(wanted: str, name_list: list[str]) -> bool:
        w = wanted.lower()
        return any(w in n or n in w for n in name_list)

    target_comp = None
    for comp in all_competitions:
        all_names = _comp_names(comp)
        if home_want and away_want:
            if _name_matches(home_want, all_names) and _name_matches(
                away_want, all_names
            ):
                target_comp = comp
                break
        elif team_want:
            if _name_matches(team_want, all_names):
                target_comp = comp
                break

    if not target_comp:
        return None

    st = target_comp.get("status", {})
    stt = st.get("type", {})
    is_final = stt.get("state") == "post"

    if not is_final:
        return {
            "outcome": "pending",
            "settled": False,
            "source": "espn_public",
            "score": None,
            "pricing": None,
            "note": f"Match not yet final — status: {stt.get('description', 'in progress')}",
        }

    competitors = target_comp.get("competitors", [])
    c0 = competitors[0] if competitors else {}
    c1 = competitors[1] if len(competitors) > 1 else {}

    def _display_name(c: dict) -> str:
        for key in ("athlete", "team"):
            obj = c.get(key) or {}
            for field in ("displayName", "shortDisplayName", "lastName"):
                v = obj.get(field)
                if v:
                    return v
        return "unknown"

    def _sets_won(c: dict) -> int:
        return sum(1 for ls in (c.get("linescores") or []) if ls.get("winner"))

    def _games_won(c: dict) -> int:
        total = 0
        for ls in c.get("linescores") or []:
            try:
                total += int(ls.get("value") or 0)
            except (TypeError, ValueError):
                pass
        return total

    c0_names = [_display_name(c0).lower()]
    c1_names = [_display_name(c1).lower()]
    for key in ("athlete", "team"):
        for c, lst in ((c0, c0_names), (c1, c1_names)):
            obj = c.get(key) or {}
            for f in ("displayName", "shortDisplayName", "lastName", "abbreviation"):
                v = obj.get(f)
                if v:
                    lst.append(v.lower())

    market = (bet.get("market") or "").lower()
    pick_raw = (bet.get("pick") or "").lower()
    line_val = bet.get("line")

    if _name_matches(pick_raw, c0_names) or pick_raw == "home":
        pick_comp, opp_comp = c0, c1
    elif _name_matches(pick_raw, c1_names) or pick_raw == "away":
        pick_comp, opp_comp = c1, c0
    else:
        pick_comp, opp_comp = c0, c1

    # Tennis uses linescores; soccer and other sports use competitor.score directly
    is_tennis = "tennis" in sport

    def _get_score(c: dict) -> int:
        try:
            return int(c.get("score") or 0)
        except (TypeError, ValueError):
            return 0

    if is_tennis:
        pick_score = _sets_won(pick_comp)
        opp_score = _sets_won(opp_comp)
        c0_score = _sets_won(c0)
        c1_score = _sets_won(c1)
        pick_total = _games_won(pick_comp)
        opp_total = _games_won(opp_comp)
    else:
        pick_score = _get_score(pick_comp)
        opp_score = _get_score(opp_comp)
        c0_score = _get_score(c0)
        c1_score = _get_score(c1)
        pick_total = pick_score
        opp_total = opp_score

    outcome = "pending"
    if market == "moneyline":
        if pick_score > opp_score:
            outcome = "win"
        elif pick_score < opp_score:
            outcome = "loss"
        else:
            outcome = "push"
    elif market in ("spread", "game spread", "puck_line", "run_line"):
        if line_val is not None:
            adj = pick_total + float(line_val)
            if adj > opp_total:
                outcome = "win"
            elif adj < opp_total:
                outcome = "loss"
            else:
                outcome = "push"
    elif market in ("total", "over_under", "total_goals", "total_runs"):
        total = c0_score + c1_score
        if line_val is not None:
            if pick_raw == "over":
                outcome = (
                    "win"
                    if total > float(line_val)
                    else ("push" if total == float(line_val) else "loss")
                )
            else:
                outcome = (
                    "win"
                    if total < float(line_val)
                    else ("push" if total == float(line_val) else "loss")
                )
    elif market == "both_teams_to_score":
        bts = c0_score > 0 and c1_score > 0
        if pick_raw in ("yes", "over"):
            outcome = "win" if bts else "loss"
        elif pick_raw in ("no", "under"):
            outcome = "win" if not bts else "loss"

    pick_name = _display_name(pick_comp)
    opp_name = _display_name(opp_comp)
    return {
        "outcome": outcome,
        "settled": True,
        "source": "espn_public",
        "score": {
            "home": c0_score,
            "away": c1_score,
            "total": c0_score + c1_score,
            "pick_player": pick_name,
            "opp_player": opp_name,
            "pick_score": pick_score,
            "opp_score": opp_score,
            "description": f"{pick_name} {pick_score} - {opp_score} {opp_name}",
        },
        "pricing": None,
    }


async def _build_prop_settlement(bet: dict) -> dict:
    player = bet.get("player")
    if not player:
        return {
            "outcome": "pending",
            "settled": False,
            "source": None,
            "note": "Player name missing.",
        }
    line = bet.get("line")
    if line is None:
        return {
            "outcome": "pending",
            "settled": False,
            "source": None,
            "note": "Line value missing for prop settlement.",
        }
    event_id, home_team, away_team = await _resolve_game_for_bet(bet)
    primary_team = bet.get("team") or home_team
    try:
        result = await sports_bridge.fetch_prop_check(
            player=player,
            market=bet["market"],
            pick=bet["pick"],
            line=float(line),
            event_id=event_id,
            date=bet.get("date"),
            sport=bet.get("sport"),
            team=primary_team,
        )
    except sports_bridge.StatsBridgeHTTPError as exc:
        if exc.status_code == 404:
            recovered = None

            retry_dates: list[str | None] = [bet.get("date")]
            try:
                if bet.get("date"):
                    d = datetime.strptime(str(bet.get("date")), "%Y-%m-%d").date()
                    retry_dates.extend(
                        [
                            (d - timedelta(days=1)).isoformat(),
                            (d + timedelta(days=1)).isoformat(),
                        ]
                    )
            except ValueError:
                pass

            seen_dates: set[str | None] = set()
            retry_dates = [
                d for d in retry_dates if not (d in seen_dates or seen_dates.add(d))
            ]

            team_variants: list[str | None] = [
                primary_team,
                home_team,
                away_team,
                bet.get("team"),
            ]
            seen_teams: set[str | None] = set()
            team_variants = [
                t
                for t in team_variants
                if t and not (t in seen_teams or seen_teams.add(t))
            ]

            sport_variants: list[str | None] = [bet.get("sport"), None]

            for rd in retry_dates:
                if recovered is not None:
                    break
                for rs in sport_variants:
                    if recovered is not None:
                        break
                    for rt in team_variants:
                        try:
                            recovered = await sports_bridge.fetch_prop_check(
                                player=player,
                                market=bet["market"],
                                pick=bet["pick"],
                                line=float(line),
                                event_id=None,
                                date=rd,
                                sport=rs,
                                team=rt,
                            )
                            break
                        except sports_bridge.StatsBridgeHTTPError as retry_exc:
                            if retry_exc.status_code == 404:
                                continue
                            if retry_exc.status_code == 504:
                                return {
                                    "outcome": "pending",
                                    "settled": False,
                                    "source": None,
                                    "note": retry_exc.detail
                                    or "Stats service timed out. Retry settlement.",
                                }
                            return {
                                "outcome": "unknown",
                                "settled": False,
                                "source": None,
                                "error": str(retry_exc),
                            }

            if recovered is None:
                return {
                    "outcome": "pending",
                    "settled": False,
                    "source": None,
                    "note": "Game not found — it may be in the future or not yet tracked.",
                }
            result = recovered
        elif exc.status_code == 504:
            return {
                "outcome": "pending",
                "settled": False,
                "source": None,
                "note": exc.detail or "Stats service timed out. Retry settlement.",
            }
        else:
            return {
                "outcome": "unknown",
                "settled": False,
                "source": None,
                "error": str(exc),
            }
    except Exception as exc:
        return {
            "outcome": "unknown",
            "settled": False,
            "source": None,
            "error": str(exc),
        }
    outcome = result.get("outcome", "pending")
    source = result.get("source", "historical")
    if outcome in {"win", "loss", "push"} and result.get("settled"):
        stat_value = result.get("stat_value")
        stat_name = result.get("market") or bet.get("market")
        home_score = result.get("home_score")
        away_score = result.get("away_score")
        await run_in_threadpool(
            bet_tracking.settle_bet,
            bet["bet_id"],
            outcome,
            source,
            home_score,
            away_score,
            stat_value,
            stat_name,
        )
    return result


async def _build_settlement(bet: dict) -> dict:
    if _is_future_game(bet):
        return {
            "outcome": "pending",
            "settled": False,
            "source": None,
            "score": None,
            "pricing": None,
            "note": "Game has not started yet.",
        }

    shared_bet_id = bet.get("shared_bet_id")
    if shared_bet_id:
        cached = await run_in_threadpool(
            bet_tracking.get_shared_settlement, shared_bet_id
        )
        if cached:
            cached_resp = _shared_settlement_response(cached)
            cached_outcome = cached_resp.get("outcome")
            if cached_outcome in {"win", "loss", "push", "void"}:
                if bet.get("status") == "pending":
                    await run_in_threadpool(
                        bet_tracking.settle_bet,
                        bet["bet_id"],
                        str(cached_outcome),
                        cached_resp.get("source") or "shared_cache",
                        (cached_resp.get("score") or {}).get("home"),
                        (cached_resp.get("score") or {}).get("away"),
                        cached.get("player_stat_value"),
                        cached.get("stat_name"),
                    )
                return cached_resp

    if _is_prop_market(bet.get("market", "")):
        return await _build_prop_settlement(bet)

    market = _normalize_settlement_market(bet.get("market") or "")
    sport = (bet.get("sport") or "").lower()
    use_espn_period = (
        market in _PERIOD_SETTLEABLE
        and sport
        != "soccer"  # soccer period markets route to SofaScore via _AUTO_SETTLEABLE
        and not (sport == "baseball" and market in _MLB_MARKETCHECK_ROUTED)
    )

    if use_espn_period:
        result = await _espn_settle_period_bet(bet)
        if (
            result
            and result.get("outcome") in {"win", "loss", "push"}
            and result.get("settled")
        ):
            await run_in_threadpool(
                bet_tracking.settle_bet,
                bet["bet_id"],
                result["outcome"],
                "espn_public",
                (result.get("score") or {}).get("home"),
                (result.get("score") or {}).get("away"),
                result.get("stat_value"),
                result.get("stat_name") or bet.get("market"),
            )
        if result:
            return result

    if market not in _AUTO_SETTLEABLE or not bet.get("pick"):
        return {
            "outcome": "not_settleable",
            "settled": False,
            "source": None,
            "score": None,
            "pricing": None,
            "espn_limitation": True,
            "note": (
                f"Market '{bet.get('market')}' requires period-specific data. "
                f"Settle manually via POST /bets/{bet.get('bet_id')}/settle."
            ),
        }

    if not sports_bridge.is_available():
        return {
            "outcome": "unknown",
            "settled": False,
            "source": None,
            "score": None,
            "pricing": None,
            "error": "stats service unavailable",
        }

    event_id, home_team, away_team = await _resolve_game_for_bet(bet)
    query_team = home_team or bet.get("team")
    query_opponent = away_team if home_team else None

    if not event_id and not (query_team and bet.get("date")):
        return {
            "outcome": "pending",
            "settled": False,
            "source": None,
            "score": None,
            "pricing": None,
            "note": "Cannot locate game — provide date + team name or event_id.",
        }

    _market = market
    effective_pick, line_value = _spread_pick_and_line_for_settlement(bet)

    # For soccer team-specific markets (team_total, team_total_corners, etc.) the
    # `player` field holds the target team name. Pass it as `team` so that
    # soccer_sofascore_props can pick the right side.
    _soccer_team_markets = {
        "team_total",
        "team_total_corners",
        "1st_half_team_total",
        "1st_half_team_total_corners",
        "2nd_half_team_total",
        "2nd_half_team_total_corners",
    }
    if (
        _market in _soccer_team_markets
        and bet.get("player")
        and bet.get("sport", "").lower() == "soccer"
    ):
        query_team = bet["player"]
        _ht = home_team or bet.get("home_team")
        _at = away_team or bet.get("away_team")
        if _ht and _at:
            _target = query_team.casefold()
            if (
                _target == _ht.casefold()
                or _target in _ht.casefold()
                or _ht.casefold() in _target
            ):
                query_opponent = _at
            elif (
                _target == _at.casefold()
                or _target in _at.casefold()
                or _at.casefold() in _target
            ):
                query_opponent = _ht

    # Normalise pick values that stats_api does not accept directly
    effective_pick = bet["pick"]
    if _market in (
        "both_teams_to_score",
        "1st_half_both_teams_to_score",
        "2nd_half_both_teams_to_score",
        "both_teams_to_score_reg_time",
        "1st_period_both_teams_to_score",
        "2nd_period_both_teams_to_score",
    ):
        if effective_pick.lower() in ("over", "yes"):
            effective_pick = "yes"
        elif effective_pick.lower() in ("under", "no"):
            effective_pick = "no"
    elif _market in ("goal_both_halves",):
        if effective_pick.lower() in ("over", "yes"):
            effective_pick = "yes"
        elif effective_pick.lower() in ("under", "no"):
            effective_pick = "no"

    try:
        result = await sports_bridge.market_check(
            event_id=event_id,
            date=bet.get("date"),
            sport=bet.get("sport"),
            team=query_team,
            opponent=query_opponent,
            player=bet.get("player"),
            market=_market,
            pick=effective_pick,
            line=line_value,
        )
    except sports_bridge.StatsBridgeHTTPError as exc:
        if exc.status_code == 404:
            # Retry with looser matching when event_id is missing or stale.
            # This handles common feed mismatches (team/opponent orientation,
            # sport label differences, and date rollovers around midnight UTC).
            recovered_result = None
            retry_dates: list[str | None] = [bet.get("date")]
            try:
                if bet.get("date"):
                    d = datetime.strptime(str(bet.get("date")), "%Y-%m-%d").date()
                    retry_dates.extend(
                        [
                            (d - timedelta(days=1)).isoformat(),
                            (d + timedelta(days=1)).isoformat(),
                        ]
                    )
            except ValueError:
                pass

            # preserve order while removing duplicates
            seen_dates: set[str | None] = set()
            retry_dates = [
                d for d in retry_dates if not (d in seen_dates or seen_dates.add(d))
            ]

            team_variants: list[tuple[str | None, str | None]] = [
                (query_team, query_opponent),
                (query_team, None),
                (bet.get("team"), None),
                (query_opponent, query_team),
            ]
            sport_variants: list[str | None] = [bet.get("sport"), None]

            for rd in retry_dates:
                if recovered_result is not None:
                    break
                for rs in sport_variants:
                    if recovered_result is not None:
                        break
                    for rt, ro in team_variants:
                        if not rt:
                            continue
                        try:
                            recovered_result = await sports_bridge.market_check(
                                event_id=None,
                                date=rd,
                                sport=rs,
                                team=rt,
                                opponent=ro,
                                player=bet.get("player"),
                                market=_market,
                                pick=effective_pick,
                                line=line_value,
                            )
                            break
                        except sports_bridge.StatsBridgeHTTPError as retry_exc:
                            if retry_exc.status_code == 404:
                                continue
                            if retry_exc.status_code == 504:
                                return {
                                    "outcome": "pending",
                                    "settled": False,
                                    "source": None,
                                    "score": None,
                                    "pricing": None,
                                    "note": retry_exc.detail
                                    or "Stats service timed out. Retry settlement.",
                                }
                            return {
                                "outcome": "unknown",
                                "settled": False,
                                "source": None,
                                "score": None,
                                "pricing": None,
                                "error": str(retry_exc),
                            }

            if recovered_result is not None:
                result = recovered_result
            else:
                fallback = await _espn_settle_bet(bet)
                if fallback is not None:
                    fb_outcome = fallback.get("outcome", "pending")
                    if fb_outcome in {"win", "loss", "push"} and fallback.get(
                        "settled"
                    ):
                        fb_score = fallback.get("score") or {}
                        await run_in_threadpool(
                            bet_tracking.settle_bet,
                            bet["bet_id"],
                            fb_outcome,
                            "espn_public",
                            fb_score.get("home"),
                            fb_score.get("away"),
                        )
                    return fallback
                return {
                    "outcome": "pending",
                    "settled": False,
                    "source": None,
                    "score": None,
                    "pricing": None,
                    "note": "Game not found — may be in the future or not yet tracked.",
                }
        if exc.status_code == 504:
            return {
                "outcome": "pending",
                "settled": False,
                "source": None,
                "score": None,
                "pricing": None,
                "note": exc.detail or "Stats service timed out. Retry settlement.",
            }
        return {
            "outcome": "unknown",
            "settled": False,
            "source": None,
            "score": None,
            "pricing": None,
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "outcome": "unknown",
            "settled": False,
            "source": None,
            "score": None,
            "pricing": None,
            "error": str(exc),
        }

    outcome = result.get("outcome", "pending")
    settled = result.get("settled", False)
    source = result.get("source")

    # stats_api market-check responses may return either:
    # - score: {home, away, total}
    # - home_score / away_score top-level fields
    score = result.get("score")
    if not isinstance(score, dict):
        hs = result.get("home_score")
        as_ = result.get("away_score")
        score = {"home": hs, "away": as_} if hs is not None or as_ is not None else {}

    score = _normalize_settlement_score(score)

    if outcome in {"win", "loss", "push"} and settled and source:
        await run_in_threadpool(
            bet_tracking.settle_bet,
            bet["bet_id"],
            outcome,
            source,
            (score or {}).get("home"),
            (score or {}).get("away"),
            result.get("stat_value"),
            result.get("stat_name") or result.get("market") or bet.get("market"),
        )

    return {
        "outcome": outcome,
        "settled": settled,
        "source": source,
        "score": score if score else None,
        "pricing": result.get("pricing"),
        "note": result.get("note"),
        "event": result.get("event"),
        **_settlement_stat_fields(
            stat_value=result.get("stat_value"),
            stat_name=result.get("stat_name") or result.get("market"),
        ),
    }


def _stored_settlement(bet: dict) -> dict:
    status = bet.get("status", "pending")
    score = _normalize_settlement_score(home=bet.get("home_score"), away=bet.get("away_score"))
    return {
        "outcome": bet.get("outcome") or status,
        "settled": status not in ("pending", "void"),
        "source": bet.get("settlement_source"),
        "settled_at": bet.get("settled_at"),
        "score": score,
        **_settlement_stat_fields(
            player_stat_value=bet.get("player_stat_value"),
            stored_stat_name=bet.get("stat_name"),
        ),
    }


def _is_auto_settleable_bet(bet: dict) -> bool:
    market = _normalize_settlement_market(bet.get("market") or "")
    sport = (bet.get("sport") or "").lower()

    use_espn_period = (
        market in _PERIOD_SETTLEABLE
        and sport != "soccer"
        and not (sport == "baseball" and market in _MLB_MARKETCHECK_ROUTED)
    )
    if use_espn_period:
        return True

    if _is_prop_market(bet.get("market", "")):
        return bool(bet.get("player")) and (bet.get("line") is not None)

    return market in _AUTO_SETTLEABLE and bool(bet.get("pick"))


def _shared_settlement_response(shared: dict) -> dict:
    status = shared.get("status")
    outcome = shared.get("outcome") or status or "pending"
    score = _normalize_settlement_score(
        home=shared.get("home_score"),
        away=shared.get("away_score"),
    )
    return {
        "outcome": outcome,
        "settled": outcome in {"win", "loss", "push", "void"}
        or bool(shared.get("settled")),
        "source": shared.get("source"),
        "settled_at": shared.get("settled_at"),
        "score": score,
        "pricing": None,
        "note": "Loaded from shared settlement cache.",
        **_settlement_stat_fields(
            player_stat_value=shared.get("player_stat_value"),
            stored_stat_name=shared.get("stat_name"),
        ),
    }


def _bet_response(bet: dict, settlement: dict | None = None) -> dict:
    return {
        "bet_id": bet["bet_id"],
        "created_at": bet["created_at"],
        "email": bet.get("email"),
        "status": bet["status"],
        "sport": bet.get("sport"),
        "league": bet.get("league"),
        "date": bet.get("date"),
        "event": bet.get("event"),
        "datetime": bet.get("event_datetime"),
        "event_id": bet.get("event_id"),
        "team": bet.get("team"),
        "home_team": bet.get("home_team"),
        "away_team": bet.get("away_team"),
        "player": bet.get("player"),
        "market": bet["market"],
        "pick": bet["pick"] or None,
        "selection_line": bet.get("selection_line"),
        "line": bet.get("line"),
        "odds": bet.get("odds"),
        "stake": bet.get("stake"),
        "notes": bet.get("notes"),
        "book": bet.get("book"),
        "settled_at": bet.get("settled_at"),
        "settlement_source": bet.get("settlement_source"),
        "nvig_at_placement": bet.get("nvig_at_placement"),
        "nvig_odds_at_placement": bet.get("nvig_odds_at_placement"),
        "book_clv": bet.get("book_clv"),
        "nvig_clv": bet.get("nvig_clv"),
        "book_closing_odds": bet.get("book_closing_odds"),
        "nvig_closing_odds": bet.get("nvig_closing_odds"),
        "clv_calculated_at": bet.get("clv_calculated_at"),
        "settlement": settlement,
    }


def _selection_line_for_storage(body: CreateBetRequest) -> str | None:
    if body._raw_line is not None:
        return body._raw_line
    if isinstance(body.line, str):
        return body.line
    if body.line is None:
        return None
    if body.market == "spread" and body.pick:
        return f"{body.pick} {body.line:+g}"
    if body.market == "total" and body.pick:
        return f"{body.pick} {body.line:g}"
    if body.market == "moneyline" and body.pick:
        return body.pick
    return str(body.line)


# ---------------------------------------------------------------------------
# CLV background calculator (EV feed historics)
# ---------------------------------------------------------------------------


async def _calculate_clv_for_bet(bet_id: str) -> None:
    """
    Fetch closing odds from the EV historics API and write book_clv, nvig_clv,
    book_closing_odds, and nvig_closing_odds. Uses the last recorded odds for
    the bet's book and for nvig fair value.
    Always fire-and-forget (never raises). Called after successful settlement.
    """
    try:
        bet = await run_in_threadpool(bet_tracking.get_bet, bet_id)
        if not bet:
            return

        context = bet.get("historics_context")
        bet_odds = bet.get("odds")
        if not context or bet_odds is None:
            return

        try:
            bet_odds_int = int(bet_odds)
        except (ValueError, TypeError):
            return

        try:
            historics_data = await historics_bridge.fetch_historics(context)
        except Exception:
            return

        books = historics_data.get("books") or {}
        book_series = _find_book_series(books, bet.get("book"))
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
            return

        await run_in_threadpool(
            bet_tracking.update_bet_clv,
            bet_id,
            book_clv,
            nvig_clv,
            book_closing_odds=book_closing,
            nvig_closing_odds=nvig_closing,
        )
    except Exception:
        pass  # CLV is non-critical — must never crash settlement flow


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Bet Tracking API",
        "version": "1.0.0",
        "port": int(os.getenv("API_PORT", "5002")),
        "db": bet_tracking.DB_PATH,
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "bet-tracking-api"}


# ---------------------------------------------------------------------------
# Bets
# ---------------------------------------------------------------------------


async def _resolve_scoped_user_id(
    auth_user: Optional[dict],
    *,
    email: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    Resolve (user_id, email) for bet scoping.

    api_key  → user_id from api_users (auth payload), no users-table lookup
    cookie   → users table via JWT email
    bearer   → users table via caller-supplied email
    """
    if auth_user and auth_user.get("auth_type") == "api_key":
        return auth_user.get("user_id"), auth_user.get("email")

    resolved_email: Optional[str] = None
    if auth_user and auth_user.get("auth_type") == "cookie":
        resolved_email = auth_user.get("email")
    elif email:
        resolved_email = email.strip().lower()

    if not resolved_email:
        return None, None

    db_user = await run_in_threadpool(
        bet_tracking.get_user_by_email, resolved_email
    )
    if db_user:
        return db_user["user_id"], db_user["email"]
    return None, resolved_email


def _is_user_scoped_auth(auth_user: Optional[dict]) -> bool:
    return bool(
        auth_user and auth_user.get("auth_type") in ("cookie", "api_key")
    )


@app.post("/bets", tags=["bets"])
async def create_bet(
    body: CreateBetRequest = Body(...),
    auth_user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    # api_key    → api_users.user_id (stored on user_bets)
    # cookie     → users table via JWT email
    # bearer     → users table via body.email
    user_id: Optional[str] = None
    if auth_user and auth_user.get("auth_type") == "api_key":
        user_id = auth_user.get("user_id")
    else:
        resolved_email = (
            auth_user.get("email")
            if auth_user and auth_user.get("auth_type") == "cookie"
            else body.email
        )
        if resolved_email:
            db_user = await run_in_threadpool(
                bet_tracking.create_or_get_user, resolved_email
            )
            user_id = db_user["user_id"]

    nvig_at_placement = _resolve_nvig_at_placement(
        body.odds, body.nvig_odds_at_placement, body.counterpart_odds
    )

    bet = await run_in_threadpool(
        bet_tracking.create_bet,
        market=body.market,
        pick=body.pick,
        user_id=user_id,
        sport=body.sport,
        league=body.league,
        date=body.date,
        event=body.event,
        event_datetime=body.datetime,
        event_id=body.event_id,
        team=body.team,
        home_team=body.home_team,
        away_team=body.away_team,
        player=body.player,
        selection_line=_selection_line_for_storage(body),
        line=body.line if isinstance(body.line, (int, float)) else None,
        odds=body.odds,
        stake=body.stake,
        notes=body.notes,
        book=body.book,
        counterpart_odds=body.counterpart_odds,
        nvig_at_placement=nvig_at_placement,
        nvig_odds_at_placement=body.nvig_odds_at_placement,
        historics_context=body.historics_context,
    )
    settlement = await _build_settlement(bet)
    refreshed = await run_in_threadpool(bet_tracking.get_bet, bet["bet_id"])
    if refreshed is not None:
        bet = refreshed
    if settlement.get("settled") and settlement.get("outcome") in {
        "win",
        "loss",
        "push",
    }:
        asyncio.ensure_future(_calculate_clv_for_bet(bet["bet_id"]))
    return JSONResponse(status_code=201, content=_bet_response(bet, settlement))


@app.get("/bets", tags=["bets"])
async def list_bets(
    status: Optional[str] = Query(None),
    sport: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    player: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    book: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth_user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    user_id, resolved_email = await _resolve_scoped_user_id(
        auth_user, email=email
    )
    if _is_user_scoped_auth(auth_user) and not user_id:
        return JSONResponse(
            {
                "total": 0,
                "returned": 0,
                "offset": offset,
                "limit": limit,
                "page": (offset // limit) + 1 if limit else 1,
                "total_pages": 0,
                "filters": {"email": resolved_email},
                "bets": [],
            }
        )

    bets, total = await run_in_threadpool(
        bet_tracking.list_bets,
        status=status,
        sport=sport,
        market=market,
        player=player,
        user_id=user_id,
        book=book,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    total_pages = (total + limit - 1) // limit if limit else 0
    page = (offset // limit) + 1 if limit else 1
    return JSONResponse(
        {
            "total": total,
            "returned": len(bets),
            "offset": offset,
            "limit": limit,
            "page": page,
            "total_pages": total_pages,
            "filters": {
                "status": status,
                "sport": sport,
                "market": market,
                "player": player,
                "email": resolved_email,
                "date_from": date_from,
                "date_to": date_to,
            },
            "bets": [_bet_response(b, _stored_settlement(b)) for b in bets],
        }
    )


@app.get("/bets/filter-options", tags=["bets"])
async def bets_filter_options(
    email: Optional[str] = Query(None),
    auth_user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    user_id, _resolved_email = await _resolve_scoped_user_id(
        auth_user, email=email
    )
    if _is_user_scoped_auth(auth_user) and not user_id:
        return JSONResponse({"markets": [], "books": []})
    options = await run_in_threadpool(
        bet_tracking.list_bet_filter_options, user_id
    )
    return JSONResponse(options)


@app.get("/bets/summary", tags=["bets"])
async def bets_summary(
    email: Optional[str] = Query(None),
    auth_user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    user_id, resolved_email = await _resolve_scoped_user_id(
        auth_user, email=email
    )
    if _is_user_scoped_auth(auth_user):
        if not user_id:
            return JSONResponse(
                {
                    "total_bets": 0,
                    "by_status": {},
                    "total_staked": 0.0,
                    "pending": 0,
                    "settled": 0,
                }
            )
        summary = await run_in_threadpool(bet_tracking.get_summary, user_id)
        return JSONResponse(summary)

    if resolved_email and not user_id:
        raise HTTPException(
            status_code=404, detail=f"No user found for email '{resolved_email}'"
        )
    summary = await run_in_threadpool(bet_tracking.get_summary, user_id)
    return JSONResponse(summary)


@app.get("/bets/analytics", tags=["bets"])
async def bets_analytics(
    email: Optional[str] = Query(None),
    date_from: Optional[str] = Query(
        None, description="Event date lower bound (YYYY-MM-DD, inclusive)"
    ),
    date_to: Optional[str] = Query(
        None, description="Event date upper bound (YYYY-MM-DD, inclusive)"
    ),
    period: Optional[str] = Query(
        None, description="Echo-only label: all, daily, weekly, monthly, custom"
    ),
    auth_user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    if date_from and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_from):
        raise HTTPException(status_code=400, detail="date_from must be YYYY-MM-DD")
    if date_to and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_to):
        raise HTTPException(status_code=400, detail="date_to must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=400, detail="date_from must be on or before date_to"
        )

    user_id, resolved_email = await _resolve_scoped_user_id(
        auth_user, email=email
    )

    if _is_user_scoped_auth(auth_user):
        if not user_id:
            analytics = await run_in_threadpool(
                bet_tracking.empty_user_analytics, date_from, date_to
            )
            return JSONResponse(
                {
                    "user": {"user_id": None, "email": resolved_email},
                    "period_label": period,
                    **analytics,
                }
            )
        analytics = await run_in_threadpool(
            bet_tracking.get_user_analytics,
            user_id,
            date_from,
            date_to,
        )
        return JSONResponse(
            {
                "user": {"user_id": user_id, "email": resolved_email},
                "period_label": period,
                **analytics,
            }
        )

    if resolved_email and user_id:
        analytics = await run_in_threadpool(
            bet_tracking.get_user_analytics,
            user_id,
            date_from,
            date_to,
        )
        return JSONResponse(
            {
                "user": {"user_id": user_id, "email": resolved_email},
                "period_label": period,
                **analytics,
            }
        )

    analytics = await run_in_threadpool(
        bet_tracking.get_analytics, date_from, date_to
    )
    return JSONResponse({**analytics, "period_label": period})


# @app.get("/bets/prop-markets", tags=["bets"])
# async def bets_prop_markets(user: Optional[dict] = Depends(require_auth)) -> JSONResponse:
#     return JSONResponse({
#         "auto_settleable": [
#             "player_points", "player_rebounds", "player_assists", "player_threes",
#             "player_steals", "player_blocks", "player_turnovers", "player_minutes",
#             "player_fg_made", "player_ft_made", "player_goals", "player_saves",
#             "player_yellow_cards", "player_goals_hockey", "player_assists_hockey",
#             "player_hits", "player_rbis", "player_runs", "player_home_runs",
#             "player_doubles", "player_triples", "player_bases", "player_singles",
#             "player_hits_runs_rbis",
#             "player_runs_cricket", "player_wickets_cricket",
#             "player_strikeouts", "player_earned_runs", "player_outs",
#         ],
#         "auto_settleable_sources": {
#             "player_runs": "mlb_period_props",
#             "player_home_runs": "mlb_period_props",
#             "player_doubles": "mlb_period_props",
#             "player_triples": "mlb_period_props",
#             "player_bases": "mlb_period_props",
#             "player_singles": "mlb_period_props",
#             "player_hits_runs_rbis": "mlb_period_props",
#             "player_strikeouts":  "mlb_stats_api",
#             "player_earned_runs": "mlb_stats_api",
#             "player_outs":        "mlb_stats_api",
#         },
#         "espn_limitation": [
#             "player_pass_yards", "player_rush_yards", "player_receiving_yards",
#             "player_pass_tds", "player_receptions", "player_tackles", "player_sacks",
#             "player_interceptions", "player_carry_yards", "player_shots_on_target",
#             "player_offsides", "player_cards", "player_aces", "player_double_faults",
#             "player_sets_won",
#             "anytime_scorer", "first_scorer", "last_scorer",
#             "first_td", "last_td", "first_basket",
#             "player_double_double", "player_triple_double", "player_hat_trick",
#         ],
#         "period_auto_settleable": sorted(_PERIOD_SETTLEABLE),
#         "game_specialty": [
#             "will_there_be_a_tiebreak", "set_handicap",
#             "first_team_to_score", "1st_half_total_corners",
#             "1st_quarter_player_points", "team_total",
#         ],
#         "notes": (
#             "Markets in 'espn_limitation' are tracked but will not auto-settle. "
#             "Markets in 'period_auto_settleable' settle automatically from ESPN "
#             "period/linescore data (NBA quarters/halves, MLB innings, NHL periods, "
#             "soccer halftime). MLB batting props like runs, home runs, doubles, "
#             "triples, bases, and singles settle from Baseball Savant /gf via the "
#             "mlb_period_props module. "
#             "Markets in 'game_specialty' must be settled manually via "
#             "POST /bets/{bet_id}/settle. "
#             "player_strikeouts and player_earned_runs settle via the official "
#             "MLB Stats API (statsapi.mlb.com) — see auto_settleable_sources."
#         ),
#     })


@app.get("/bets/prop-markets", tags=["bets"])
async def bets_prop_markets(
    user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    return JSONResponse(
        {
            "auto_settleable": [
                "player_points",
                "player_rebounds",
                "player_assists",
                "player_threes",
                "player_made_threes",
                "player_rebounds_assists",
                "player_points_assists",
                "player_points_rebounds",
                "player_points_rebounds_assists",
                "player_steals",
                "player_blocks",
                "player_turnovers",
                "player_minutes",
                "player_fg_made",
                "player_ft_made",
                "player_goals",
                "player_saves",
                "player_yellow_cards",
                "player_goals_hockey",
                "player_assists_hockey",
                "player_power_play_points",
                "player_hits",
                "player_rbis",
                "player_runs",
                "player_home_runs",
                "player_doubles",
                "player_triples",
                "player_bases",
                "player_singles",
                "player_hits_runs_rbis",
                "player_runs_cricket",
                "player_wickets_cricket",
                "player_strikeouts",
                "player_earned_runs",
                "player_outs",
                "1st_quarter_total_points",
                "2nd_quarter_total_points",
                "3rd_quarter_total_points",
                "4th_quarter_total_points",
                "1st_quarter_total_points_odd_even",
                "2nd_quarter_total_points_odd_even",
                "3rd_quarter_total_points_odd_even",
                "1st_quarter_moneyline",
                "2nd_quarter_moneyline",
                "3rd_quarter_moneyline",
                "4th_quarter_moneyline",
                "1st_quarter_point_spread",
                "2nd_quarter_point_spread",
                "3rd_quarter_point_spread",
                "4th_quarter_point_spread",
                "1st_quarter_team_total",
                "2nd_quarter_team_total",
                "3rd_quarter_team_total",
                "4th_quarter_team_total",
                "1st_half_total_points",
                "1st_half_total_points_odd_even",
                "2nd_half_total_points_odd_even",
                "1st_half_moneyline",
                "1st_half_point_spread",
                "1st_half_team_total",
                "1st_quarter_player_points",
                "2nd_quarter_player_points",
                "3rd_quarter_player_points",
                "4th_quarter_player_points",
                "1st_half_player_points",
                "2nd_half_player_points",
                "total_points_odd_even",
                "will_there_be_overtime",
                "team_first_basket",
                "first_basket",
                "first_basket_including_ft",
                # MMA
                "go_the_distance",
                "total_rounds",
            ],
            "auto_settleable_sources": {
                "player_runs": "mlb_period_props",
                "player_home_runs": "mlb_period_props",
                "player_doubles": "mlb_period_props",
                "player_triples": "mlb_period_props",
                "player_bases": "mlb_period_props",
                "player_singles": "mlb_period_props",
                "player_hits_runs_rbis": "mlb_period_props",
                "player_strikeouts": "mlb_stats_api",
                "player_earned_runs": "mlb_stats_api",
                "player_outs": "mlb_stats_api",
                "1st_quarter_player_points": "nba_period_props",
                "2nd_quarter_player_points": "nba_period_props",
                "3rd_quarter_player_points": "nba_period_props",
                "4th_quarter_player_points": "nba_period_props",
                "1st_half_player_points": "nba_period_props",
                "2nd_half_player_points": "nba_period_props",
                "1st_quarter_total_points": "nba_period_props",
                "2nd_quarter_total_points": "nba_period_props",
                "3rd_quarter_total_points": "nba_period_props",
                "4th_quarter_total_points": "nba_period_props",
                "1st_quarter_total_points_odd_even": "nba_period_props",
                "2nd_quarter_total_points_odd_even": "nba_period_props",
                "3rd_quarter_total_points_odd_even": "nba_period_props",
                "1st_quarter_moneyline": "nba_period_props",
                "2nd_quarter_moneyline": "nba_period_props",
                "3rd_quarter_moneyline": "nba_period_props",
                "4th_quarter_moneyline": "nba_period_props",
                "1st_quarter_point_spread": "nba_period_props",
                "2nd_quarter_point_spread": "nba_period_props",
                "3rd_quarter_point_spread": "nba_period_props",
                "4th_quarter_point_spread": "nba_period_props",
                "1st_quarter_team_total": "nba_period_props",
                "2nd_quarter_team_total": "nba_period_props",
                "3rd_quarter_team_total": "nba_period_props",
                "4th_quarter_team_total": "nba_period_props",
                "1st_half_total_points": "nba_period_props",
                "1st_half_total_points_odd_even": "nba_period_props",
                "2nd_half_total_points_odd_even": "nba_period_props",
                "1st_half_moneyline": "nba_period_props",
                "1st_half_point_spread": "nba_period_props",
                "1st_half_team_total": "nba_period_props",
                "total_points_odd_even": "nba_period_props",
                "will_there_be_overtime": "nba_period_props",
                "team_first_basket": "nba_period_props",
                "first_basket": "nba_period_props",
                "first_basket_including_ft": "nba_period_props",
                # MMA — ESPN scoreboard (with direct API fallback for past events)
                "go_the_distance": "espn_mma",
                "total_rounds": "espn_mma",
            },
            "espn_limitation": [
                "player_pass_yards",
                "player_rush_yards",
                "player_receiving_yards",
                "player_pass_tds",
                "player_receptions",
                "player_tackles",
                "player_sacks",
                "player_interceptions",
                "player_carry_yards",
                "player_shots_on_target",
                "player_offsides",
                "player_cards",
                "player_aces",
                "player_double_faults",
                "player_sets_won",
                "anytime_scorer",
                "first_scorer",
                "last_scorer",
                "first_td",
                "last_td",
                "first_basket",
                "player_double_double",
                "player_triple_double",
                "player_hat_trick",
            ],
            "period_auto_settleable": sorted(_PERIOD_SETTLEABLE),
            "game_specialty": [
                "will_there_be_a_tiebreak",
                "set_handicap",
                "first_team_to_score",
                "1st_half_total_corners",
                "team_total",
            ],
            "notes": (
                "Markets in 'espn_limitation' are tracked but will not auto-settle. "
                "Markets in 'period_auto_settleable' settle automatically from ESPN "
                "period/linescore data (NBA quarters/halves, MLB innings, NHL periods, "
                "soccer halftime). MLB batting props like runs, home runs, doubles, "
                "triples, bases, and singles settle from Baseball Savant /gf via the "
                "mlb_period_props module. "
                "NBA period player-points markets (1Q/2Q/3Q/4Q/1H/2H) settle via "
                "nba_period_props (DataBallr play-by-play and box score). "
                "MMA markets (go_the_distance, total_rounds, moneyline) settle via "
                "ESPN UFC scoreboard with direct API fallback for past bouts. "
                "Pick values for go_the_distance: yes/over (went distance) or no/under (early finish). "
                "Markets in 'game_specialty' must be settled manually via "
                "POST /bets/{bet_id}/settle. "
                "player_strikeouts and player_earned_runs settle via the official "
                "MLB Stats API (statsapi.mlb.com) — see auto_settleable_sources."
            ),
        }
    )


@app.get("/bets/{bet_id}", tags=["bets"])
async def get_bet(
    bet_id: str,
    user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    bet = await run_in_threadpool(bet_tracking.get_bet, bet_id)
    if bet is None:
        raise HTTPException(status_code=404, detail=f"Bet {bet_id!r} not found")
    if bet["status"] == "pending":
        settlement = await _build_settlement(bet)
        bet = await run_in_threadpool(bet_tracking.get_bet, bet_id) or bet
        if settlement.get("settled") and settlement.get("outcome") in {
            "win",
            "loss",
            "push",
        }:
            asyncio.ensure_future(_calculate_clv_for_bet(bet_id))
    else:
        settlement = _stored_settlement(bet)
        if (
            bet.get("clv_calculated_at") is None
            and bet.get("historics_context")
            and bet["status"] in {"win", "loss", "push", "void"}
        ):
            asyncio.ensure_future(_calculate_clv_for_bet(bet_id))
    return JSONResponse(_bet_response(bet, settlement))


@app.post("/bets/{bet_id}/settle", tags=["bets"])
async def settle_bet(
    bet_id: str,
    user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    bet = await run_in_threadpool(bet_tracking.get_bet, bet_id)
    if bet is None:
        raise HTTPException(status_code=404, detail=f"Bet {bet_id!r} not found")
    if bet["status"] != "pending":
        return JSONResponse(
            {
                "message": f"Bet is already settled as '{bet['status']}'. No action taken.",
                "bet": _bet_response(bet),
            }
        )
    settlement = await _build_settlement(bet)
    if settlement.get("settled") and settlement.get("outcome") in {
        "win",
        "loss",
        "push",
    }:
        asyncio.ensure_future(_calculate_clv_for_bet(bet_id))
    fresh_bet = await run_in_threadpool(bet_tracking.get_bet, bet_id)
    if fresh_bet is not None:
        bet = fresh_bet
    elif settlement.get("settled") and settlement.get("outcome") in {
        "win",
        "loss",
        "push",
        "void",
    }:
        # Keep API response consistent even if immediate re-read fails.
        bet = {
            **bet,
            "status": settlement["outcome"],
            "settled_at": datetime.now(timezone.utc).isoformat(),
            "settlement_source": settlement.get("source"),
        }
    return JSONResponse(_bet_response(bet, settlement))


class BulkSettleRequest(BaseModel):
    email: Optional[str] = Field(None, description="User email / Gmail address")
    gmail_id: Optional[str] = Field(
        None, description="Alias for email (backward compat)"
    )

    @model_validator(mode="after")
    def normalize(self) -> "BulkSettleRequest":
        if not self.email and self.gmail_id:
            self.email = self.gmail_id
        if self.email:
            self.email = self.email.strip().lower()
        return self


@app.post("/bets/settle-pending", tags=["bets"])
async def settle_pending_bets_for_user(
    body: BulkSettleRequest = Body(...),
    auth_user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    user_id, resolved_email = await _resolve_scoped_user_id(
        auth_user, email=body.email
    )
    if not user_id:
        if _is_user_scoped_auth(auth_user):
            return JSONResponse(
                {
                    "found": True,
                    "summary": {
                        "email": resolved_email,
                        "user_id": auth_user.get("user_id") if auth_user else None,
                        "total_pending": 0,
                        "processed": 0,
                        "skipped_not_started": 0,
                        "win": 0,
                        "loss": 0,
                        "push": 0,
                        "void": 0,
                        "pending": 0,
                        "manual_settlement_needed": 0,
                        "unknown": 0,
                        "errors": 0,
                    },
                }
            )
        raise HTTPException(
            status_code=400, detail="email is required (provide in body or log in)"
        )

    pending_bets, _ = await run_in_threadpool(
        bet_tracking.list_bets,
        status="pending",
        user_id=user_id,
        limit=1000,
        offset=0,
    )

    summary = {
        "email": body.email,
        "user_id": user_id,
        "total_pending": len(pending_bets),
        "processed": 0,
        "skipped_not_started": 0,
        "win": 0,
        "loss": 0,
        "push": 0,
        "void": 0,
        "pending": 0,
        "manual_settlement_needed": 0,
        "unknown": 0,
        "errors": 0,
    }
    for bet in pending_bets:
        if _is_future_game(bet):
            summary["skipped_not_started"] += 1
            continue

        summary["processed"] += 1
        try:
            settlement = await _build_settlement(bet)
        except Exception as exc:
            summary["errors"] += 1
            continue

        outcome = str(settlement.get("outcome") or "pending")
        settled = bool(settlement.get("settled"))

        if outcome in {"win", "loss", "push", "void"}:
            asyncio.ensure_future(_calculate_clv_for_bet(bet["bet_id"]))

        if outcome in {"win", "loss", "push", "void"}:
            summary[outcome] += 1
        elif outcome == "not_settleable":
            summary["manual_settlement_needed"] += 1
        elif outcome == "unknown":
            summary["unknown"] += 1
            summary["manual_settlement_needed"] += 1
        else:
            summary["pending"] += 1

    return JSONResponse(
        {
            "found": True,
            "summary": summary,
        }
    )


class ManualSettleRequest(BaseModel):
    outcome: str = Field(..., description="win | loss | push | void")
    home_score: Optional[float] = Field(None, description="Optional final home score")
    away_score: Optional[float] = Field(None, description="Optional final away score")
    note: Optional[str] = Field(None, description="Optional reason / context")

    @field_validator("outcome")
    @classmethod
    def validate_outcome(cls, v: str) -> str:
        allowed = {"win", "loss", "push", "void"}
        norm = v.strip().lower()
        if norm not in allowed:
            raise ValueError(f"outcome must be one of: {', '.join(sorted(allowed))}")
        return norm


@app.patch("/bets/{bet_id}", tags=["bets"])
async def manual_settle_bet(
    bet_id: str,
    body: ManualSettleRequest = Body(...),
    user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    """Manually set the outcome of any pending bet."""
    bet = await run_in_threadpool(bet_tracking.get_bet, bet_id)
    if bet is None:
        raise HTTPException(status_code=404, detail=f"Bet {bet_id!r} not found")
    if bet["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Bet is already settled as '{bet['status']}'. Cannot override a settled bet.",
        )

    if _is_auto_settleable_bet(bet):
        raise HTTPException(
            status_code=409,
            detail=(
                "Manual settlement is disabled for auto-settleable bets. "
                "Use POST /bets/{bet_id}/settle instead."
            ),
        )

    if body.outcome == "void":
        updated = await run_in_threadpool(bet_tracking.void_bet, bet_id)
    else:
        updated = await run_in_threadpool(
            bet_tracking.settle_bet,
            bet_id,
            body.outcome,
            "manual",
            body.home_score,
            body.away_score,
        )

    if updated:
        asyncio.ensure_future(_calculate_clv_for_bet(bet_id))

    bet = await run_in_threadpool(bet_tracking.get_bet, bet_id) or bet
    settlement = {
        **_stored_settlement(bet),
        "source": "manual",
        "note": body.note or f"Manually settled as '{body.outcome}'",
    }
    return JSONResponse(_bet_response(bet, settlement))


@app.delete("/bets/{bet_id}", tags=["bets"])
async def delete_bet(
    bet_id: str,
    user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    deleted = await run_in_threadpool(bet_tracking.delete_bet, bet_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Bet {bet_id!r} not found")
    return JSONResponse({"deleted": True, "bet_id": bet_id})


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@app.get("/users", tags=["users"])
async def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    users, total = await run_in_threadpool(
        bet_tracking.list_users, limit=limit, offset=offset
    )
    return JSONResponse(
        {
            "total": total,
            "returned": len(users),
            "offset": offset,
            "limit": limit,
            "users": users,
        }
    )


@app.get("/users/me/tracked-bets", tags=["users"])
async def get_my_tracked_bets(
    auth_user: dict = Depends(require_auth),
) -> JSONResponse:
    """Return historics_context JWTs for the user's pending tracked bets.

    The EV feed hides rows whose data-historics matches any returned token
    (compared by decoded event/market/prop/date/league, ignoring sportsbook).
    """
    if auth_user.get("auth_type") not in ("cookie", "api_key"):
        return JSONResponse([])

    user_id, _email = await _resolve_scoped_user_id(auth_user)
    if not user_id:
        return JSONResponse([])

    tracked_bets = await run_in_threadpool(
        bet_tracking.get_tracked_historics_contexts, user_id
    )
    return JSONResponse(tracked_bets)


@app.get("/users/{email}/bets", tags=["users"])
async def user_bets(
    email: str,
    status: Optional[str] = Query(None),
    sport: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth_user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    db_user = await run_in_threadpool(bet_tracking.get_user_by_email, email)
    if not db_user:
        raise HTTPException(
            status_code=404, detail=f"No user found for email '{email}'"
        )
    bets, total = await run_in_threadpool(
        bet_tracking.list_bets,
        status=status,
        sport=sport,
        market=market,
        date_from=date_from,
        date_to=date_to,
        user_id=db_user["user_id"],
        limit=limit,
        offset=offset,
    )
    total_pages = (total + limit - 1) // limit if limit else 0
    page = (offset // limit) + 1 if limit else 1
    return JSONResponse(
        {
            "user": {
                "user_id": db_user["user_id"],
                "email": db_user["email"],
                "created_at": db_user["created_at"],
            },
            "total": total,
            "returned": len(bets),
            "offset": offset,
            "limit": limit,
            "page": page,
            "total_pages": total_pages,
            "bets": [_bet_response(b) for b in bets],
        }
    )


@app.get("/users/{email}/stats", tags=["users"])
async def user_stats(
    email: str,
    auth_user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    db_user = await run_in_threadpool(bet_tracking.get_user_by_email, email)
    if not db_user:
        raise HTTPException(
            status_code=404, detail=f"No user found for email '{email}'"
        )
    summary = await run_in_threadpool(bet_tracking.get_user_summary, db_user["user_id"])
    return JSONResponse(
        {
            "user": {"user_id": db_user["user_id"], "email": db_user["email"]},
            **summary,
        }
    )


@app.get("/users/{email}/analytics", tags=["users"])
async def user_analytics(
    email: str,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    period: Optional[str] = Query(None),
    auth_user: Optional[dict] = Depends(require_auth),
) -> JSONResponse:
    if date_from and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_from):
        raise HTTPException(status_code=400, detail="date_from must be YYYY-MM-DD")
    if date_to and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_to):
        raise HTTPException(status_code=400, detail="date_to must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=400, detail="date_from must be on or before date_to"
        )

    db_user = await run_in_threadpool(bet_tracking.get_user_by_email, email)
    if not db_user:
        raise HTTPException(
            status_code=404, detail=f"No user found for email '{email}'"
        )
    analytics = await run_in_threadpool(
        bet_tracking.get_user_analytics,
        db_user["user_id"],
        date_from,
        date_to,
    )
    return JSONResponse(
        {
            "user": {"user_id": db_user["user_id"], "email": db_user["email"]},
            "period_label": period,
            **analytics,
        }
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("API_PORT", "5002"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
