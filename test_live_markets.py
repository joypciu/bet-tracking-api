"""
test_live_markets.py
====================
Live match periodic market test runner.

Usage:
    python test_live_markets.py               # place bets on all live games + check status
    python test_live_markets.py --recheck     # recheck previously placed live bets
    python test_live_markets.py --cleanup     # delete all test bets placed by this script
    python test_live_markets.py --sport nba   # only test a specific sport

How it works:
    1. Scans ESPN for games that are currently IN PROGRESS across NBA, MLB, NHL, EPL, La Liga, MLS
    2. For each live game, places bets on all relevant period markets
    3. Shows current settlement status (pending while live, auto-settles when final)
    4. Re-run with --recheck after the game ends to confirm auto-settlement kicked in

Run this while a game is live. Then re-run after the final whistle.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import httpx

# ── Config ────────────────────────────────────────────────────────────────────
BET_API_BASE  = "http://142.44.160.36:5002"
BET_API_TOKEN = "J6zbakG6vi41QN7YFPdwzbzWqBeFVAsLZLKXHgiUnYE"
TEST_EMAIL    = "live-market-test@eternity.dev"
DEFAULT_STAKE = 10.0
DEFAULT_ODDS  = -110

ESPN_HEADERS  = {"User-Agent": "Mozilla/5.0"}

# ── ESPN endpoints to scan ───────────────────────────────────────────────────
ESPN_LEAGUES = [
    {"sport": "basketball", "league": "nba",  "league_path": "basketball/nba",  "espn_league": "nba"},
    {"sport": "basketball", "league": "wnba", "league_path": "basketball/wnba", "espn_league": "wnba"},
    {"sport": "baseball",   "league": "mlb",  "league_path": "baseball/mlb",    "espn_league": "mlb"},
    {"sport": "hockey",     "league": "nhl",  "league_path": "hockey/nhl",      "espn_league": "nhl"},
    {"sport": "soccer",     "league": "epl",  "league_path": "soccer/eng.1",    "espn_league": "eng.1"},
    {"sport": "soccer",     "league": "mls",  "league_path": "soccer/usa.1",    "espn_league": "usa.1"},
    {"sport": "soccer",     "league": "laliga","league_path":"soccer/esp.1",    "espn_league": "esp.1"},
    {"sport": "soccer",     "league": "ucl",  "league_path": "soccer/uefa.champions", "espn_league": "uefa.champions"},
]

# ── Period markets to test per sport ─────────────────────────────────────────
SPORT_MARKETS = {
    "basketball": [
        {"market": "1st_quarter_moneyline",    "pick": "home",  "line": None},
        {"market": "1st_quarter_total_points", "pick": "over",  "line": 55.5},
        {"market": "1st_half_moneyline",       "pick": "home",  "line": None},
        {"market": "1st_half_total_points",    "pick": "over",  "line": 110.5},
        {"market": "2nd_half_moneyline",       "pick": "home",  "line": None},
    ],
    "baseball": [
        {"market": "1st_5_innings_moneyline",  "pick": "home",  "line": None},
        {"market": "1st_5_innings_total",      "pick": "over",  "line": 4.5},
        {"market": "1st_3_innings_run_line",   "pick": "home",  "line": -0.5},
        {"market": "1st_3_innings_total_runs", "pick": "over",  "line": 1.5},
        {"market": "1st_inning_total_runs",    "pick": "over",  "line": 0.5},
    ],
    "hockey": [
        {"market": "1st_period_moneyline",     "pick": "home",  "line": None},
        {"market": "1st_period_total_goals",   "pick": "over",  "line": 0.5},
        {"market": "2nd_period_moneyline",     "pick": "home",  "line": None},
        {"market": "2nd_period_total_goals",   "pick": "over",  "line": 0.5},
    ],
    "soccer": [
        {"market": "1st_half_both_teams_to_score", "pick": "yes",  "line": None},
        {"market": "1st_half_total_goals",         "pick": "over",  "line": 0.5},
        {"market": "1st_half_moneyline",           "pick": "home",  "line": None},
        {"market": "1st_half_draw_bet",            "pick": "yes",   "line": None},
    ],
}

# State file to persist bet IDs between runs
STATE_FILE = "live_test_state.json"


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"bets": [], "run_at": None}


def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _bet_headers() -> dict:
    return {
        "Authorization": f"Bearer {BET_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _espn_live_games(league_info: dict) -> list[dict]:
    """Return list of currently IN-PROGRESS competitions for a league."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{league_info['league_path']}/scoreboard"
    try:
        r = httpx.get(url, headers=ESPN_HEADERS, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as exc:
        print(f"  [WARN] ESPN fetch failed for {league_info['league']}: {exc}")
        return []

    live = []
    for event in data.get("events", []):
        comp  = event.get("competitions", [{}])[0]
        state = comp.get("status", {}).get("type", {}).get("state", "")
        if state != "in":
            continue

        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})

        def _name(c):
            return (c.get("team") or {}).get("displayName", "Unknown")

        detail = comp.get("status", {}).get("type", {}).get("detail", "In Progress")
        live.append({
            "event_id":   event.get("id", ""),
            "home_team":  _name(home),
            "away_team":  _name(away),
            "home_score": home.get("score", "0"),
            "away_score": away.get("score", "0"),
            "status":     detail,
            "sport":      league_info["sport"],
            "league":     league_info["league"],
            "date":       (comp.get("startDate") or event.get("date", ""))[:10],
        })
    return live


def _place_bet(game: dict, market_cfg: dict) -> dict | None:
    payload = {
        "market":     market_cfg["market"],
        "pick":       market_cfg["pick"],
        "odds":       DEFAULT_ODDS,
        "stake":      DEFAULT_STAKE,
        "email":      TEST_EMAIL,
        "sport":      game["sport"],
        "league":     game["league"],
        "date":       game["date"],
        "home_team":  game["home_team"],
        "away_team":  game["away_team"],
        "team":       game["home_team"],
        "event_id":   game["event_id"] or None,
    }
    if market_cfg.get("line") is not None:
        payload["line"] = market_cfg["line"]

    try:
        r = httpx.post(
            f"{BET_API_BASE}/bets",
            headers=_bet_headers(),
            json=payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            return r.json()
        print(f"    [ERROR] {r.status_code}: {r.text[:120]}")
    except Exception as exc:
        print(f"    [ERROR] {exc}")
    return None


def _get_bet(bet_id: str) -> dict | None:
    try:
        r = httpx.get(f"{BET_API_BASE}/bets/{bet_id}", headers=_bet_headers(), timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _delete_bet(bet_id: str) -> bool:
    try:
        r = httpx.delete(f"{BET_API_BASE}/bets/{bet_id}", headers=_bet_headers(), timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def _settle_icon(outcome: str, settled: bool) -> str:
    if not settled:
        return "⏳ pending"
    icons = {"win": "✅ WIN", "loss": "❌ LOSS", "push": "🔁 PUSH"}
    return icons.get(outcome, f"? {outcome}")


def cmd_place(sport_filter: str | None):
    """Scan for live games and place period market bets."""
    print(f"\n{'='*60}")
    print(f"  LIVE MARKET TEST — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    state = _load_state()
    new_bets = []
    total_games = 0

    for league_info in ESPN_LEAGUES:
        if sport_filter and league_info["sport"] != sport_filter and league_info["league"] != sport_filter:
            continue

        live_games = _espn_live_games(league_info)
        if not live_games:
            continue

        for game in live_games:
            total_games += 1
            print(f"\n🟢 LIVE [{league_info['league'].upper()}]  "
                  f"{game['away_team']} @ {game['home_team']}")
            print(f"   Score: {game['away_score']} - {game['home_score']}  |  {game['status']}")
            print(f"   Date:  {game['date']}  |  event_id: {game['event_id']}")

            markets = SPORT_MARKETS.get(game["sport"], [])
            print(f"   Placing {len(markets)} period market bets...")

            for m in markets:
                resp = _place_bet(game, m)
                if not resp:
                    continue
                bet_id  = resp.get("bet_id", "")
                s       = resp.get("settlement", {}) or {}
                outcome = s.get("outcome", "?")
                settled = s.get("settled", False)
                note    = s.get("note", "")
                period  = (s.get("score") or {}).get("period_detail")

                status_str = _settle_icon(outcome, settled)
                print(f"   [{status_str}] {m['market']} (pick={m['pick']}"
                      + (f", line={m['line']}" if m.get("line") else "") + ")")
                if note:
                    print(f"            note: {note}")
                if period:
                    print(f"            period_data: {period}")

                new_bets.append({
                    "bet_id":    bet_id,
                    "market":    m["market"],
                    "pick":      m["pick"],
                    "line":      m.get("line"),
                    "game":      f"{game['away_team']} @ {game['home_team']}",
                    "sport":     game["sport"],
                    "league":    game["league"],
                    "date":      game["date"],
                    "placed_at": datetime.now(timezone.utc).isoformat(),
                })

    if total_games == 0:
        print("\n⚠️  No live games found right now.")
        print("   Supported sports: NBA, WNBA, MLB, NHL, EPL, MLS, La Liga, UCL")
        print("   Re-run this script when a game is in progress.")
    else:
        state["bets"].extend(new_bets)
        state["run_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        print(f"\n{'='*60}")
        print(f"  Placed {len(new_bets)} bets across {total_games} live game(s).")
        print(f"  Run with --recheck after games finish to confirm auto-settlement.")
        print(f"{'='*60}\n")


def cmd_recheck():
    """Re-fetch all previously placed test bets and show updated settlement status."""
    state = _load_state()
    bets  = state.get("bets", [])

    if not bets:
        print("\n⚠️  No previous test bets found. Run without --recheck first.")
        return

    print(f"\n{'='*60}")
    print(f"  RECHECK — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Checking {len(bets)} previously placed bets")
    print(f"{'='*60}")

    settled_count = 0
    pending_count = 0
    current_game  = None

    for entry in bets:
        if entry["game"] != current_game:
            current_game = entry["game"]
            print(f"\n📋 {entry['league'].upper()} — {entry['game']}  ({entry['date']})")

        bet = _get_bet(entry["bet_id"])
        if not bet:
            print(f"   [MISSING] {entry['market']} — bet not found")
            continue

        s       = bet.get("settlement") or {}
        outcome = s.get("outcome", "?")
        settled = s.get("settled", False)
        period  = (s.get("score") or {}).get("period_detail")
        source  = s.get("source", "")
        note    = s.get("note", "")

        status_str = _settle_icon(outcome, settled)
        line_str   = f", line={entry['line']}" if entry.get("line") else ""
        print(f"   [{status_str}] {entry['market']} (pick={entry['pick']}{line_str})")

        if settled:
            settled_count += 1
            if period:
                print(f"            period_data: {period}")
            if source:
                print(f"            source: {source}")
        else:
            pending_count += 1
            if note:
                print(f"            note: {note}")

    print(f"\n{'='*60}")
    print(f"  Results: {settled_count} settled  |  {pending_count} still pending")
    if pending_count > 0:
        print("  Re-run --recheck after the game ends for final results.")
    print(f"{'='*60}\n")


def cmd_cleanup():
    """Delete all test bets placed by this script."""
    state = _load_state()
    bets  = state.get("bets", [])

    if not bets:
        print("\n⚠️  No test bets to clean up.")
        return

    print(f"\nCleaning up {len(bets)} test bets...")
    deleted = 0
    for entry in bets:
        if _delete_bet(entry["bet_id"]):
            print(f"  ✅ Deleted {entry['bet_id'][:8]}... ({entry['market']})")
            deleted += 1
        else:
            print(f"  ⚠️  Could not delete {entry['bet_id'][:8]}... (may already be gone)")

    state["bets"] = []
    _save_state(state)
    print(f"\nDeleted {deleted}/{len(bets)} bets. State file cleared.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live periodic market test runner")
    parser.add_argument("--recheck", action="store_true", help="Recheck previously placed bets")
    parser.add_argument("--cleanup", action="store_true", help="Delete all test bets")
    parser.add_argument("--sport",   type=str, default=None,
                        help="Filter by sport: basketball, baseball, hockey, soccer, nba, mlb, nhl, epl")
    args = parser.parse_args()

    if args.cleanup:
        cmd_cleanup()
    elif args.recheck:
        cmd_recheck()
    else:
        cmd_place(args.sport)
