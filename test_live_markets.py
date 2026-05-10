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
    1. Scans ESPN API, 365scores, 1xbet, and DuckDuckGo for games currently IN PROGRESS
    2. For each live game, places bets on all relevant period markets
    3. Shows current settlement status (pending while live, auto-settles when final)
    4. Re-run with --recheck after the game ends to confirm auto-settlement kicked in

Run this while a game is live. Then re-run after the final whistle.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

import httpx
try:
    from ddgs import DDGS
    _DDGS_AVAILABLE = True
except ImportError:
    _DDGS_AVAILABLE = False

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


# ── DuckDuckGo live-game detection ───────────────────────────────────────────

# Maps search query → (sport, league) so we can classify results
_DDGS_QUERIES = [
    ("NBA live score today",           "basketball", "nba"),
    ("EPL Premier League live score",  "soccer",     "epl"),
    ("La Liga live score today",       "soccer",     "laliga"),
    ("MLS live score today",           "soccer",     "mls"),
    ("Champions League live score",    "soccer",     "ucl"),
    ("MLB live score today",           "baseball",   "mlb"),
    ("NHL live score today",           "hockey",     "nhl"),
    ("WNBA live score today",          "basketball", "wnba"),
]

# Regex to parse "Team A vs Team B" — each team is 1-4 capitalized words
_VS_RE = re.compile(
    r"([A-Z][a-zA-Z'.\-]+(?:\s+[A-Z][a-zA-Z'.\-]+){0,3})"
    r"\s+(?:vs\.?|v\.?)\s+"
    r"([A-Z][a-zA-Z'.\-]+(?:\s+[A-Z][a-zA-Z'.\-]+){0,3})",
)

# Strip common non-team trailer words from regex captures
_TEAM_TRAILER = re.compile(
    r"\s+(?:Results?|Today|Lineups?|Highlights?|Updates?|Streams?|Scores?|"
    r"Live|Stats?|Tips?|Preview|Analysis|Watch|Odds|Picks?|Prediction|"
    r"Final|Game\d?|Match|At|After)$",
    re.IGNORECASE,
)


def _clean_team(name: str) -> str:
    for _ in range(3):  # iteratively strip multiple trailers
        cleaned = _TEAM_TRAILER.sub("", name).strip()
        if cleaned == name:
            break
        name = cleaned
    return name

# Keywords that signal the article is about a match that is actually live right now
_LIVE_KEYWORDS = re.compile(
    r"\blive\b.*\bscore|\blive\b.*\bupdate|\blive\b.*\bstream|"
    r"\bin progress\b|\bhalftime\b|\bhalf.?time\b|\blive match\b|"
    r"\bright now\b|\bcurrent score\b|\bscore update\b",
    re.IGNORECASE,
)

# Threshold: articles older than this many hours are not "live"
_MAX_AGE_HOURS = 6


def _is_recent(date_str: str) -> bool:
    """Return True if the article date is within _MAX_AGE_HOURS of now."""
    try:
        # ddgs dates are ISO-8601 with offset e.g. "2026-05-10T14:00:00+00:00"
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return age <= _MAX_AGE_HOURS
    except Exception:
        return True  # assume recent if we can't parse


def _ddgs_live_games() -> list[dict]:
    """Search DuckDuckGo news for currently live matches across all sports."""
    if not _DDGS_AVAILABLE:
        print("  [WARN] ddgs not installed — skipping DuckDuckGo source")
        return []

    found = []
    seen_pairs: set[str] = set()

    for query, sport, league in _DDGS_QUERIES:
        try:
            results = DDGS().news(query, max_results=8)
        except Exception as exc:
            print(f"  [WARN] DuckDuckGo failed for '{query}': {exc}")
            time.sleep(1)
            continue

        for r in results:
            if not _is_recent(r.get("date", "")):
                continue

            text = f"{r.get('title', '')} {r.get('body', '')}"
            if not _LIVE_KEYWORDS.search(text):
                continue

            m = _VS_RE.search(text)
            if not m:
                continue

            away_team = _clean_team(m.group(1).strip())
            home_team = _clean_team(m.group(2).strip())

            # Deduplicate: same pair already added for this league
            pair_key = f"{league}:{away_team.lower()}:{home_team.lower()}"
            rev_key  = f"{league}:{home_team.lower()}:{away_team.lower()}"
            if pair_key in seen_pairs or rev_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            found.append({
                "event_id":   "",
                "home_team":  home_team,
                "away_team":  away_team,
                "home_score": "?",
                "away_score": "?",
                "status":     "In Progress (DDG)",
                "sport":      sport,
                "league":     league,
                "date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "_source":    "ddgs",
                "_headline":  r.get("title", "")[:80],
            })

        time.sleep(0.5)  # be polite between queries

    return found


# ── 365scores source ─────────────────────────────────────────────────────────

_365_SPORT_MAP = {
    1:  "soccer",
    2:  "basketball",
    4:  "hockey",
    13: "baseball",
}

_365_LEAGUE_FRAGMENTS = [
    ("Premier League",    "epl"),
    ("La Liga",           "laliga"),
    ("MLS",               "mls"),
    ("Champions League",  "ucl"),
    ("Bundesliga",        "bundesliga"),
    ("Serie A",           "seriea"),
    ("NBA",               "nba"),
    ("WNBA",              "wnba"),
    ("NHL",               "nhl"),
    ("MLB",               "mlb"),
]


def _365_league_code(comp_name: str) -> str:
    lower = comp_name.lower()
    for fragment, code in _365_LEAGUE_FRAGMENTS:
        if fragment.lower() in lower:
            return code
    return comp_name[:20].lower().replace(" ", "_")


def _365scores_live_games() -> list[dict]:
    """Fetch live games from the 365scores public REST API."""
    url = "https://webws.365scores.com/web/games/allscores/"
    params = {
        "langId": "1",
        "sports": "1,2,4,13",
        "statusGroup": "1",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.365scores.com/",
    }
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"  [WARN] 365scores HTTP {r.status_code}")
            return []
        data = r.json()
    except Exception as exc:
        print(f"  [WARN] 365scores fetch failed: {exc}")
        return []

    result = []
    for g in data.get("games", []):
        if g.get("statusGroup") != 1:
            continue
        sport = _365_SPORT_MAP.get(g.get("sportId", 0))
        if not sport:
            continue

        comp   = g.get("competitionDisplayName", "")
        home_c = g.get("homeCompetitor", {})
        away_c = g.get("awayCompetitor", {})
        status = g.get("statusText", "Live")
        gt     = g.get("gameTimeDisplay", "")
        result.append({
            "event_id":   str(g.get("id", "")),
            "home_team":  home_c.get("name", "Unknown"),
            "away_team":  away_c.get("name", "Unknown"),
            "home_score": str(home_c.get("score", "?")),
            "away_score": str(away_c.get("score", "?")),
            "status":     f"{status} {gt}".strip(),
            "sport":      sport,
            "league":     _365_league_code(comp),
            "date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "_source":    "365scores",
            "_headline":  comp,
        })
    return result


# ── 1xbet source (via 1xlite-08668.world) ────────────────────────────────────

# 1xbet sport name → our sport label
_1XBET_SPORT_NAME_MAP: dict[str, str] = {
    "Football":    "soccer",
    "Basketball":  "basketball",
    "Ice Hockey":  "hockey",
    "Baseball":    "baseball",
    "Cricket":     "cricket",
    "Tennis":      "tennis",
}

_1XBET_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",   # no br — httpx auto-decompresses gzip
    "Referer":         "https://1xlite-08668.world/en/live",
    "Origin":          "https://1xlite-08668.world",
}


def _1xbet_live_games() -> list[dict]:
    """
    Fetch all live matches from 1xlite-08668.world in a single call.
    FanCode (fancode.com) is a streaming platform — its GraphQL only exposes
    content category pills, not individual match scores, so it is not included.
    """
    url = "https://1xlite-08668.world/service-api/LiveFeed/Get1x2_VZip"
    params = {
        "count":             "200",
        "lng":               "en",
        "gr":                "1258",
        "mode":              "4",
        "country":           "19",
        "top":               "true",
        "virtualSports":     "true",
        "noFilterBlockEvent":"true",
    }
    try:
        r = httpx.get(url, params=params, headers=_1XBET_HEADERS, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            print(f"  [WARN] 1xbet HTTP {r.status_code}")
            return []
        data = r.json()
    except Exception as exc:
        print(f"  [WARN] 1xbet fetch failed: {exc}")
        return []

    if not data.get("Success") or not data.get("Value"):
        return []

    result = []
    for m in data["Value"]:
        home = m.get("O1E") or m.get("O1", "")
        away = m.get("O2E") or m.get("O2", "")
        if not home or not away:
            continue

        sport_name = m.get("SN", "")
        sport = _1XBET_SPORT_NAME_MAP.get(sport_name)
        if not sport:
            continue  # skip table tennis, volleyball, etc.

        sc  = m.get("SC", {}) or {}
        fs  = sc.get("FS", {})
        sls = sc.get("SLS", "In Progress")

        raw_league = m.get("L", "") or ""
        league = raw_league[:30].lower().replace(" ", "_") or sport

        result.append({
            "event_id":   str(m.get("I", "")),
            "home_team":  home,
            "away_team":  away,
            "home_score": str(fs.get("S1", "?")),
            "away_score": str(fs.get("S2", "?")),
            "status":     sls,
            "sport":      sport,
            "league":     league,
            "date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "_source":    "1xbet",
            "_headline":  raw_league,
        })

    return result


# ── Virtual / cyber game filter ───────────────────────────────────────────────

_VIRTUAL_TEAM_RE = re.compile(
    r"\(cyber\)|\bvirtual\b|"
    r"[\+＋]\s*$|^\s*[\+＋]|"        # trailing/leading + (1xbet cyber mark)
    r"\(esport\)|\(sim\)|\(amateur\)\b",
    re.IGNORECASE,
)

_VIRTUAL_LEAGUE_RE = re.compile(
    r"\bshort\s+football\b|\blfl\b|\bcyber\b|\besport\b|"
    r"\bvirtual\b|\b2x2\b|\b3x3\b|\b4x4\b|\b5x5\b|\b6x6\b|"
    r"\bsimulated\b|\bsim\b",
    re.IGNORECASE,
)


def _is_virtual(game: dict) -> bool:
    """Return True if the game looks like a virtual/cyber/simulated match."""
    for name in (game["home_team"], game["away_team"]):
        if _VIRTUAL_TEAM_RE.search(name):
            return True
    league_raw = game.get("_headline", "") or game.get("league", "")
    if _VIRTUAL_LEAGUE_RE.search(league_raw):
        return True
    return False


# ── Merge all sources ─────────────────────────────────────────────────────────

def _merge_games(*source_lists: list[dict]) -> list[dict]:
    """
    Merge game lists from all sources, deduplicating by last-word of team name.
    Tracks all confirming sources per game in _sources list.
    First source wins for field values (ESPN takes priority as the first arg).
    Virtual/cyber games are filtered out before returning.
    """
    # First pass: index by token → primary record + source set
    token_map: dict[str, dict] = {}   # token → game dict (primary record)
    source_map: dict[str, set] = {}   # token → set of sources

    for games in source_lists:
        for g in games:
            if _is_virtual(g):
                continue
            home_key  = g["home_team"].lower().split()[-1]
            away_key  = g["away_team"].lower().split()[-1]
            sport_key = g["sport"]
            token = f"{sport_key}:{min(home_key, away_key)}:{max(home_key, away_key)}"

            if token not in token_map:
                token_map[token] = g
                source_map[token] = {g.get("_source", "unknown")}
            else:
                source_map[token].add(g.get("_source", "unknown"))

    # Annotate each game with all confirming sources
    merged = []
    for token, game in token_map.items():
        game = dict(game)
        game["_sources"] = sorted(source_map[token])
        merged.append(game)

    return merged


# ── DuckDuckGo game verification ─────────────────────────────────────────────

# Leagues matching "Country. League Name" or "Country vs Country" → real by structure
_STRUCTURED_LEAGUE_RE = re.compile(
    r"^[A-Z][a-zA-Z\s]+\.\s+\w|"     # "Bangladesh. Dhaka Premier..."
    r"^[A-Z][a-zA-Z]+\s+vs\s+[A-Z]", # "Bangladesh vs Pakistan"
    re.IGNORECASE,
)

# Suspicious generic names that don't follow a country/league pattern
_SUSPICIOUS_LEAGUE_RE = re.compile(
    r"^\s*(dream|student|table\s+\w+|indoor|amateur|test\s+\w+|"
    r"cyber\s+\w+|esport\s+\w+|fantasy\s+\w+)\s*(league|cup|series)?\s*$",
    re.IGNORECASE,
)


def _league_needs_ddgs_check(league_raw: str) -> bool:
    """Return True only for leagues that look suspicious and need DDG confirmation."""
    if not league_raw:
        return True
    if _SUSPICIOUS_LEAGUE_RE.match(league_raw.strip()):
        return True
    # Well-known country-prefixed leagues are real by structure
    if _STRUCTURED_LEAGUE_RE.match(league_raw.strip()):
        return False
    # Short single-word or two-word league names without country context → suspicious
    words = [w for w in league_raw.split() if len(w) > 2]
    if len(words) <= 2:
        return True
    return False


def _ddgs_verify_suspicious(league_raw: str, sport: str, sample_game: dict) -> bool:
    """
    DDG check for genuinely suspicious league names.
    Tries: league query, then country+sport query, then team pair query.
    Returns True if any check finds a real recent match.
    """
    if not _DDGS_AVAILABLE:
        return True

    queries = [
        f"{league_raw} {sport} 2026",
        f"{sample_game['away_team']} {sample_game['home_team']} {sport} live",
    ]
    for query in queries:
        try:
            results = DDGS().news(query, max_results=3)
        except Exception:
            return True
        for r in results:
            if _is_recent(r.get("date", "")):
                text = f"{r.get('title','')} {r.get('body','')}".lower()
                team_last = [
                    sample_game["home_team"].lower().split()[-1],
                    sample_game["away_team"].lower().split()[-1],
                ]
                if any(t in text for t in team_last):
                    return True
        time.sleep(0.3)

    return False


def _verify_games(games: list[dict]) -> list[dict]:
    """
    Authenticate all live games via DDG.

    Trust tiers (fastest → slowest, DDG only called for suspicious cases):
      1. Multi-source confirmation → trusted immediately
      2. ESPN or 365scores sourced → trusted (authoritative live APIs)
      3. 1xbet with structured "Country. League" name → trusted by structure
      4. 1xbet with suspicious generic name → DDG spot-check (one per suspicious league)
      5. DDG-only games → DDG verification of team pair
    """
    verified: list[dict] = []
    suspicious_groups: dict[tuple, list[dict]] = {}

    for g in games:
        sources = g.get("_sources", [g.get("_source", "unknown")])
        league_raw = g.get("_headline", "") or g.get("league", "")

        if len(sources) > 1:
            g["_verified"] = "multi-source"
            verified.append(g)
        elif "espn" in sources:
            g["_verified"] = "espn"
            verified.append(g)
        elif "365scores" in sources:
            g["_verified"] = "365scores"
            verified.append(g)
        elif not _league_needs_ddgs_check(league_raw):
            # Structured "Country. League" name → trusted by form
            g["_verified"] = "structured-league"
            verified.append(g)
        else:
            # Suspicious name → group for DDG spot-check
            key = (league_raw, g["sport"])
            suspicious_groups.setdefault(key, []).append(g)

    if not suspicious_groups:
        return verified

    total_suspicious = sum(len(v) for v in suspicious_groups.values())
    print(f"  [DDG verify] Spot-checking {total_suspicious} suspicious game(s) "
          f"across {len(suspicious_groups)} league(s)...")

    for (league_raw, sport), group in suspicious_groups.items():
        time.sleep(0.4)
        label = (league_raw or sport)[:40] or "(unnamed)"
        sample = group[0]
        if _ddgs_verify_suspicious(league_raw, sport, sample):
            for g in group:
                g["_verified"] = "ddgs-confirmed"
            verified.extend(group)
            print(f"    ✅ {label} ({len(group)} game(s))")
        else:
            print(f"    ❌ {label} ({len(group)} game(s)) — rejected as unverifiable")

    return verified


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

    # ── Collect games from all sources ───────────────────────────────────────
    def _sport_ok(g: dict) -> bool:
        if not sport_filter:
            return True
        return g["sport"] == sport_filter or g["league"] == sport_filter

    all_espn_games: list[dict] = []
    for league_info in ESPN_LEAGUES:
        if sport_filter and league_info["sport"] != sport_filter and league_info["league"] != sport_filter:
            continue
        all_espn_games.extend(_espn_live_games(league_info))
    print(f"\n  [ESPN]       {len(all_espn_games)} live game(s) found")

    scores365_games = [g for g in _365scores_live_games() if _sport_ok(g)]
    print(f"  [365scores]  {len(scores365_games)} live game(s) found")

    xbet_games = [g for g in _1xbet_live_games() if _sport_ok(g)]
    print(f"  [1xbet]      {len(xbet_games)} live game(s) found")

    ddgs_games_raw = _ddgs_live_games()
    ddgs_games = [g for g in ddgs_games_raw if _sport_ok(g)]
    print(f"  [DuckDuckGo] {len(ddgs_games)} live game(s) found")

    # ESPN first so its event_id/score data takes priority in dedup
    live_games_all = _merge_games(all_espn_games, scores365_games, xbet_games, ddgs_games)
    print(f"  [Merged]     {len(live_games_all)} unique (virtual/cyber stripped)")

    live_games_all = _verify_games(live_games_all)
    print(f"  [Verified]   {len(live_games_all)} confirmed real event(s)\n")

    for game in live_games_all:
        total_games += 1
        sources_str = ",".join(game.get("_sources", [game.get("_source", "?")]))
        verified_str = game.get("_verified", "?")
        print(f"\n🟢 LIVE [{game['league'].upper()}]  "
              f"{game['away_team']} @ {game['home_team']}")
        print(f"   Score:    {game['away_score']} - {game['home_score']}  |  {game['status']}")
        print(f"   Date:     {game['date']}  |  event_id: {game['event_id'] or 'N/A'}")
        print(f"   Sources:  {sources_str}  |  verified: {verified_str}")
        if game.get("_headline"):
            print(f"   Headline: {game['_headline']}")

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
        print("\n⚠️  No verified live games found right now.")
        print("   Supported sports: NBA, WNBA, MLB, NHL, EPL, MLS, La Liga, UCL")
        print("   Sources: ESPN, 365scores, 1xbet, DuckDuckGo")
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
