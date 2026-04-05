# Smart Recirc — Pickup File (2026-03-30)

## What This Is
AI-powered hot water recirculation controller for a Rinnai tankless water heater with Control-R module. Replaces the dumb recirc schedule with demand classification, reactive triggering, and (soon) predictive pre-heating.

## Architecture
- **Runtime**: Python 3.9 daemon on Kush (MBP 16" M3 Pro), launchd `com.smartrecirc.collector`
- **Data source**: Rinnai Cloud API direct (GraphQL reads + Shadow PATCH commands). No Home Assistant dependency.
- **Auth**: boto3 Cognito USER_PASSWORD_AUTH → IdToken for shadow commands. API key for GraphQL reads (no auth needed).
- **DB**: SQLite `smart_recirc.db` — sensor_readings, usage_events, predictions tables
- **Telegram bot**: t.me/LindellHW_bot — commands (/status, /events, /recirc, /help) + interactive AI chat via Gesha
- **AI chat**: Qwen3.5 35B uncensored on Gesha (AMD Strix Halo, 192.168.1.149:9090) with 5-turn conversation history
- **Daily digest**: 7 AM CDT cron via launchd `com.smartrecirc.digest`, stats computed in Python, narrated by Gesha, sent to Telegram

## Key Files
- `config.py` — Rinnai creds, Telegram bot token/chat ID, thresholds, recirc schedule
- `collector.py` — RinnaiClient (auth, maintenance retrieval, sensor fetch, recirc commands), UsageTracker (event detection + classification), init_db
- `controller.py` — SmartRecircController (main loop, reactive trigger, prediction, Telegram integration)
- `telegram_bot.py` — TelegramPoller (commands, Gesha AI chat with history, typing indicator)
- `predictor.py` — UsagePredictor (basic time-of-day model, will be replaced with real predictor)
- `daily_digest.py` — Yesterday's summary via Gesha to Telegram
- `run.sh` — Launch script for launchd

## How the Classifier Works
Multi-signal demand vs recirc classification in `UsageTracker._classify()`:
1. **Signal 0**: If `recirc_on=1` entire event → recirc, UNLESS:
   - 0a: Any flow sample < 2.5 GPM (fixture pulling water during pump run)
   - 0b: Cold inrush from warm baseline (pre-flow max of last 10 idle readings > 100F, drop > 30F)
2. **Schedule-aware**: During recirc schedule windows, default recirc unless low flow or inlet crash
3. **Outside schedule**: Cold inlet < 110F, burner firing, or flow variance → demand

### Fixture Fingerprinting
- Bath: > 2.2 GPM, > 10 min, sustained cold inlet
- Shower: < 2.2 GPM, > 5 min
- Faucet: < 3 min, or anything else short
- Recirc pump: tagged as recirc_pump

## Reactive Trigger (LIVE)
When controller detects: flow > threshold + inlet < 80F + heating ON + recirc OFF → starts recirc pump for 10 min. Floods loop at 28 GPM. Fires once per event (resets when flow drops to zero). Sends Telegram notification.

## Rinnai API Details
- **GraphQL endpoint**: `https://s34ox7kri5dsvdr43bfgp6qh6i.appsync-api.us-east-1.amazonaws.com/graphql`
- **Shadow PATCH**: `https://698suy4zs3.execute-api.us-east-1.amazonaws.com/Prod/thing/{thing_name}/shadow`
- **Thing name**: CR_a1c82cf1-0add-6e96-3d08-980d3c3bb0f4
- **Device IP**: 192.168.1.173 (SONOFF-based, port 9798 filtered, no local access)
- **Critical**: Must call `do_maintenance_retrieval` via shadow PATCH every ~60s to force fresh sensor data. Without this, `info` block is stale.
- **Flow rate**: Raw API value / 10 = GPM through heater. App shows raw (28), HA showed /10 (2.8).

## Classifier Bugs Found & Fixed
1. **recirc_on signal** (3/29): False demand from recirc reheating. Added Signal 0.
2. **Washer detection** (3/29): Sub-pump flow dips during recirc. Added Signal 0a.
3. **Pre-flow baseline race** (3/30): Cold water beats flow sensor. Changed to max of last 10 idle readings.

## Data Collection Status
- 5 days collected (3/25-3/30), ~40 demand events, ~200+ recirc cycles
- Model training on peak hours: 7-9 PM CDT
- Collecting through ~2026-04-04 before building real predictor
- Dumb schedule wastes 3x pump runtime vs actual demand (~70-75% reduction possible)
- Estimated $20-25/month gas savings on $70 bill

## Occupancy Signals (Not Yet Wired)
Available in HA for future predictor input:
- Ecobee occupancy: main_floor, living_room, bedroom, upstairs, office
- Door contacts: front_breezeway, back_breezeway, front_door, backyard_door, basement_door
- Validated: zero demand when house empty, door opens lead demand by 15-30 min

## Next Steps
1. **Blog post** — Brad writing about the project on dbradwood.com
2. **Continue data collection** through April 4
3. **Wire occupancy/door sensors** into DB for predictor input
4. **Build real predictor** — replace dumb schedule with ML model using time-of-day + day-of-week + occupancy + door signals
5. **Package for HACS** — potential PR to explosivo22/rinnaicontrolr-ha or standalone
