# Home Automation

Smart home automation projects for a residential setup in Nashville, TN.

## smart-recirc

AI-powered hot water recirculation controller for a Rinnai tankless water heater with Control-R module. Replaces the dumb recirc schedule with demand classification, reactive triggering, and predictive pre-heating.

### The Problem

The Rinnai's built-in recirc schedule runs the pump ~10 hours/day to keep pipes warm. Actual hot water demand is ~1 hour/day. That's 9 hours of wasted gas and pump wear.

### The Approach

1. **Collect** sensor data from the Rinnai Cloud API every 30 seconds (flow rate, inlet/outlet temp, heating state, pump state)
2. **Classify** every flow event as real demand or recirc pump cycle using inlet temperature drop, flow rate, and gap timing — tuned from 2+ weeks of sensor data cross-referenced with ecobee occupancy sensors
3. **Predict** when demand will occur and run short 5-minute pump bursts instead of hour-long windows
4. **React** to missed predictions with a cold-inrush trigger that fires the pump within 30 seconds

### Classification Logic

The classifier distinguishes demand from recirc using three signals:

- **Inlet temperature drop**: Real demand pulls cold municipal water (55-68F) into the system, crashing the inlet temp 20-60F from a warm baseline. Recirc cooldown between pump cycles is 0-15F.
- **Flow rate**: Pump runs at 2.5-3.3 GPM. Fixtures run at 0.1-2.0 GPM. The 2.0-2.5 GPM zone is ambiguous and resolved by drop + gap timing.
- **Gap since last event**: A short gap (<30 min) with small drop and ambiguous flow = pump running slow. A long gap (>60 min) with cold pre-flow = pump cold-start after schedule restart.

### Components

| File | What it does |
|------|-------------|
| `collector.py` | Rinnai Cloud API client, sensor polling, event detection, demand/recirc classifier |
| `controller.py` | Main loop, reactive cold-inrush trigger, prediction integration |
| `predictor.py` | Time-of-day demand model (will be replaced with ML predictor) |
| `telegram_bot.py` | Two-way Telegram bot: commands (/status, /events, /recirc) + AI chat via local LLM |
| `daily_digest.py` | Morning Telegram digest summarizing yesterday's usage |
| `config.py` | Rinnai creds, thresholds, recirc schedule, Telegram config |

### Telegram Bot

The bot runs a conversational AI powered by Qwen 3.5 35B on a local inference server. It has real-time access to sensor data and usage history. Ask it anything about the water heater in natural language.

### Infrastructure

- **Runtime**: Python daemon on macOS, managed by launchd
- **Data**: SQLite (sensor_readings, usage_events, predictions)
- **API**: Rinnai Cloud GraphQL + Shadow REST (boto3 Cognito auth)
- **Inference**: Qwen 3.5 35B (MoE, 3B active) via llama.cpp Vulkan on local AMD GPU
- **Notifications**: Telegram Bot API

### Current Results

- 2+ weeks of data: 500+ events classified
- Dumb schedule: ~615 min/day pump time
- Target: <100 min/day with 95%+ demand coverage
- Reactive trigger catches missed predictions within 30 seconds
