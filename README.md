# Bet Tracking API

Standalone FastAPI service for persistent bet and user tracking. Source of truth for all bets — the Cache API proxies bet endpoints to this service.

## What this service does

- Records bets with full game context: sport, league, date, event, teams, market, pick, line, odds, and stake
- **Auto-settles** game markets (moneyline, spread, total, puck\_line, run\_line, total goals/runs/corners, both teams to score) by querying the internal stats service, which checks live and historical data
- **Period markets** (NBA quarters/halves, MLB innings, NHL periods, soccer halftime) settle automatically from ESPN linescore data — no external odds service required
- **Player prop markets** settle automatically for common stats (points, rebounds, assists, goals, etc.) via the stats service
- Falls back to direct ESPN scoreboard queries for any game not in the stats database (useful for tennis, older completed games)
- Tracks users by email — profiles are created automatically on first bet; no separate registration step
- Provides per-user and global analytics: win rate, ROI, average odds, net profit, market and sport breakdowns

## Architecture

```
bet-tracking-api/
├── main.py           # FastAPI app, all routes, settlement logic
├── bet_tracking.py   # SQLite query layer (bets.db, users) with WAL mode
├── sports_bridge.py  # Async bridge to the internal stats_api (port 8001)
├── test_live_markets.py  # Live integration test script
└── requirements.txt
```

**Data flow:**

```
POST /bets
  └── bet_tracking.create_bet()  →  bets.db (SQLite)
  └── _build_settlement()
        ├── sports_bridge.market_check()  →  stats_api :8001  →  live_state.json / DuckDB
        ├── _espn_settle_period_bet()     →  ESPN public scoreboard (period markets)
        └── _espn_settle_bet()            →  ESPN public scoreboard (fallback)
```

## Environment variables

| Variable        | Default               | Description                              |
| --------------- | --------------------- | ---------------------------------------- |
| `BET_API_TOKEN` | *(none — auth off)*   | Bearer token required on all requests    |
| `BET_DB_PATH`   | `bets.db`             | Path to SQLite database file             |
| `API_PORT`      | `5002`                | Port to listen on                        |
| `STATS_API_URL` | `http://localhost:8001` | Internal stats service base URL        |
| `STATS_API_TOKEN` | *(none)*            | Bearer token for the stats service (if set) |

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env   # set BET_API_TOKEN, STATS_API_URL, etc.
python main.py
```

The service listens on port 5002. If `BET_API_TOKEN` is not set, all endpoints are open (useful for local development).

## Authentication

All endpoints require `Authorization: Bearer <token>` when `BET_API_TOKEN` is configured.

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:5002/bets
```

## Endpoints

### Status

| Method | Path      | Description                                      |
| ------ | --------- | ------------------------------------------------ |
| GET    | `/`       | Service info (version, port, DB path)            |
| GET    | `/health` | Liveness probe — returns `{"status": "healthy"}` |

### Bets

| Method | Path                    | Description                                                                 |
| ------ | ----------------------- | --------------------------------------------------------------------------- |
| POST   | `/bets`                 | Create a bet; attempts auto-settlement immediately                          |
| GET    | `/bets`                 | List bets with filtering and pagination                                     |
| GET    | `/bets/summary`         | Aggregate summary (total bets, pending, wins, losses, ROI)                 |
| GET    | `/bets/analytics`       | Global analytics: win rate, net profit, per-market/per-sport breakdown      |
| GET    | `/bets/prop-markets`    | Lists all supported prop markets grouped by settlement method               |
| GET    | `/bets/{bet_id}`        | Get one bet; re-runs settlement if still pending                            |
| POST   | `/bets/{bet_id}/settle` | Force re-attempt settlement on a pending bet                                |
| DELETE | `/bets/{bet_id}`        | Delete a bet record                                                         |

### Users

| Method | Path                          | Description                                            |
| ------ | ----------------------------- | ------------------------------------------------------ |
| GET    | `/users`                      | List all users (paginated)                             |
| GET    | `/users/{email}/bets`         | All bets for one user (filterable, paginated)          |
| GET    | `/users/{email}/stats`        | Per-user summary (total, pending, wins, losses, ROI)   |
| GET    | `/users/{email}/analytics`    | Per-user analytics with market and sport breakdown     |

### `GET /bets` query parameters

| Parameter   | Type   | Description                            |
| ----------- | ------ | -------------------------------------- |
| `status`    | string | `pending`, `win`, `loss`, `push`       |
| `sport`     | string | Filter by sport                        |
| `market`    | string | Filter by market type                  |
| `player`    | string | Filter by player name                  |
| `email`     | string | Filter by user email                   |
| `book`      | string | Filter by sportsbook                   |
| `date_from` | string | `YYYY-MM-DD` — earliest game date      |
| `date_to`   | string | `YYYY-MM-DD` — latest game date        |
| `limit`     | int    | Max results (1–200, default 50)        |
| `offset`    | int    | Pagination offset (default 0)          |

## Creating a bet

```bash
curl -X POST http://localhost:5002/bets \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "market": "moneyline",
    "pick": "Lakers",
    "sport": "basketball",
    "league": "nba",
    "date": "2026-05-10",
    "home_team": "Lakers",
    "away_team": "Celtics",
    "odds": -115,
    "stake": 50,
    "email": "user@example.com"
  }'
```

### Required fields

| Field    | Type   | Notes                                     |
| -------- | ------ | ----------------------------------------- |
| `market` | string | Market type (see below)                   |
| `pick`   | string | Team, player, or `over`/`under`. Required for game and prop markets. |

### Optional fields

| Field       | Type         | Notes                                                                |
| ----------- | ------------ | -------------------------------------------------------------------- |
| `sport`     | string       | `basketball`, `soccer`, `hockey`, `baseball`, `tennis`, etc.        |
| `league`    | string       | `nba`, `nhl`, `mlb`, `epl`, `laliga`, `ucl`, etc.                  |
| `date`      | string       | `YYYY-MM-DD` — game date (inferred from `datetime` if omitted)      |
| `datetime`  | string       | Timezone-aware ISO-8601 game start time                              |
| `event`     | string       | `"Home Team vs Away Team"` — auto-splits into home/away             |
| `home_team` | string       | Home team name                                                       |
| `away_team` | string       | Away team name                                                       |
| `event_id`  | string       | ESPN event ID for precise game lookup (overrides team/date search)  |
| `player`    | string       | Player name — required for `player_*` prop markets                  |
| `team`      | string       | Player's team (improves prop lookup accuracy)                        |
| `line`      | string/float | Spread value, total line, or prop line                               |
| `odds`      | int          | American moneyline odds (e.g. `-115`, `+200`)                        |
| `stake`     | float        | Wager amount                                                         |
| `notes`     | string       | Free-form notes                                                      |
| `book`      | string       | Sportsbook name (e.g. `"FanDuel"`)                                  |
| `counterpart_odds` | int    | Other side's placement odds; enables `nvig_at_placement`             |
| `historics_context` | string | Signed KeepBetting historics context; enables same-book closing CLV |
| `email`     | string       | Associates bet with a user profile (created automatically)           |

### Market aliases

`ml` → `moneyline`, `ats` → `spread`, `ou` → `total`, `game_spread` → `spread`, `o_u` → `total`

## Settlement

Settlement runs automatically on `POST /bets` and on `GET /bets/{bet_id}` when the bet is still pending. It can also be forced via `POST /bets/{bet_id}/settle`.

### Auto-settleable game markets

| Market              | Description                                 |
| ------------------- | ------------------------------------------- |
| `moneyline`         | Straight win/loss                           |
| `spread`            | Against the spread (with `line`)            |
| `total`             | Over/under total score (with `line`)        |
| `puck_line`         | Hockey spread (±1.5)                        |
| `run_line`          | Baseball spread (±1.5)                      |
| `total_goals`       | Soccer total goals                          |
| `total_runs`        | Baseball total runs                         |
| `total_corners`     | Soccer total corners                        |
| `both_teams_to_score` | Soccer BTTS                               |

### Period markets (settle from ESPN linescore data)

**Basketball:** `1st_quarter_moneyline`, `1st_quarter_point_spread`, `1st_quarter_total_points`, `1st_quarter_team_total`, `1st_half_moneyline`, `1st_half_total_points`, `1st_half_team_total`, `1st_half_home_team_total`, `1st_half_away_team_total`, `2nd_half_moneyline`, `2nd_half_total_points`, `2nd_half_team_total`, `2nd_half_both_teams_to_score`

**Baseball:** `1st_inning_total_runs`, `1st_3_innings_run_line`, `1st_3_innings_total_runs`, `1st_5_innings_moneyline`, `1st_5_innings_total`, `1st_5_innings_team_total`

**Hockey:** `1st_period_moneyline`, `1st_period_total_goals`, `2nd_period_moneyline`, `2nd_period_total_goals`

**Soccer (halftime):** `1st_half_both_teams_to_score`, `1st_half_total_goals`, `1st_half_asian_total_goals`, `1st_half_draw_bet`

### Auto-settleable player prop markets

`player_points`, `player_rebounds`, `player_assists`, `player_threes`, `player_steals`, `player_blocks`, `player_turnovers`, `player_minutes`, `player_fg_made`, `player_ft_made`, `player_goals`, `player_saves`, `player_yellow_cards`, `player_goals_hockey`, `player_assists_hockey`, `player_hits`, `player_rbis`, `player_runs_cricket`, `player_wickets_cricket`

Pick must be `"over"` or `"under"`. `player` and `line` fields are required.

### Prop markets that require manual settlement

NFL player stats (`player_pass_yards`, `player_rush_yards`, `player_receiving_yards`, etc.), scorer markets (`anytime_scorer`, `first_scorer`, `first_td`), and advanced baseball/tennis stats are tracked but not auto-settled. Use `POST /bets/{bet_id}/settle` with a manual override after the game completes.

### Settlement response

```json
{
  "outcome": "win",
  "settled": true,
  "source": "historical",
  "score": { "home": 112, "away": 98 },
  "pricing": null
}
```

| Field      | Values                                  |
| ---------- | --------------------------------------- |
| `outcome`  | `win`, `loss`, `push`, `pending`, `not_settleable`, `unknown` |
| `settled`  | `true` when outcome is final            |
| `source`   | `historical`, `espn_public`, or `null`  |

## Settlement priority

1. **Period market** → ESPN linescore data (`_espn_settle_period_bet`)
2. **Stats service** → `stats_api :8001` via `sports_bridge.market_check()` (checks `live_state.json` first, then DuckDB)
3. **ESPN fallback** → direct ESPN scoreboard query (for games not yet in the stats DB — tennis, recently completed games)

If none resolves to a final result, `outcome` is `"pending"` and the bet stays in `pending` status.

## Supported leagues (ESPN fallback)

Tennis (ATP, WTA, ITF, Challenger), NBA, WNBA, NCAAB, NBA G League, MLB, NHL, EPL, La Liga, Bundesliga, Serie A, Ligue 1, MLS, UCL, UEL.

## Response format

Every bet response includes:

```json
{
  "bet_id": "uuid",
  "created_at": "ISO timestamp",
  "email": "user@example.com",
  "status": "pending",
  "sport": "basketball",
  "league": "nba",
  "date": "2026-05-10",
  "event": "Lakers vs Celtics",
  "event_id": "401234567",
  "home_team": "Lakers",
  "away_team": "Celtics",
  "market": "moneyline",
  "pick": "Lakers",
  "line": null,
  "odds": -115,
  "stake": 50.0,
  "book": "FanDuel",
  "book_clv": 0.0421,
  "nvig_clv": 0.0618,
  "clv_source": "keepbetting_historics",
  "clv_book": "FanDuel",
  "book_closing_odds": -125,
  "nvig_closing_odds": -120,
  "clv_closing_at": "2026-05-10T18:59:30+00:00",
  "settlement": { "outcome": "...", "settled": false, ... }
}
```

When `historics_context` is present, settlement uses the last pre-start price
for the tracked `book` and the endpoint's no-vig history. Without a context,
CLV remains unavailable. Missing tracked-book history leaves `book_clv` as
`null` while `nvig_clv` can still be calculated when no-vig history exists.

## Live testing

`test_live_markets.py` runs a multi-source live game integration test — queries ESPN, 365scores, and 1xbet, then confirms live results via DuckDuckGo:

```bash
python test_live_markets.py
```

## Requirements

```
fastapi
uvicorn
httpx
python-dotenv
pydantic
```

```bash
pip install -r requirements.txt
```

## VPS deployment

The service runs on port 5002 (VPS-internal). Access via the Cache API (which proxies `/bets`, `/users`, and related endpoints) or directly with the bearer token.

```bash
# Check service health
curl http://localhost:5002/health

# Watch logs (if running under systemd or pm2)
journalctl -u bet-tracking-api -f
```
