"""Configuration for Smart Recirc system."""

import os

# Rinnai Cloud API (direct, no HA middleman)
RINNAI_EMAIL = os.environ.get("RINNAI_EMAIL", "brad@wonderingwoods.com")
RINNAI_PASSWORD = os.environ.get("RINNAI_PASSWORD", "wodrih-0dordu-fykViw")
RINNAI_API_KEY = os.environ.get("RINNAI_API_KEY", "da2-dm2g4rqvjbaoxcpo4eccs3k5he")
RINNAI_GRAPHQL_URL = "https://s34ox7kri5dsvdr43bfgp6qh6i.appsync-api.us-east-1.amazonaws.com/graphql"
RINNAI_SHADOW_URL = "https://698suy4zs3.execute-api.us-east-1.amazonaws.com/Prod/thing/%s/shadow"
RINNAI_THING_NAME = os.environ.get("RINNAI_THING_NAME", "CR_a1c82cf1-0add-6e96-3d08-980d3c3bb0f4")

# Prediction parameters
FLOW_THRESHOLD_GPM = 0.1          # Minimum flow to count as "using hot water"
PREDICTION_HORIZON_MIN = 5        # How far ahead to predict (minutes)
RECIRC_DURATION_MIN = 5           # How long to run recirc pump when predicted
REACTIVE_RECIRC_MIN = 10          # How long to run recirc on cold inrush detection
CONFIDENCE_THRESHOLD = 0.6        # Minimum confidence to trigger recirc
POLL_INTERVAL_SEC = 30            # How often to poll for sensor data

# Telegram bot
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8625837955:AAE-WXYWGvd5isnDaClA5pcIZpemKprYmh0")
TELEGRAM_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "8367124313"))

# Data collection
DB_PATH = "smart_recirc.db"
MIN_TRAINING_DAYS = 3             # Minimum days of data before enabling predictions

# Raw API flow rate conversion: raw value / 10 = GPM through heater
FLOW_RAW_DIVISOR = 10.0

# Rinnai recirc schedule — windows when the pump cycles automatically.
# During these windows, flow events default to "recirc" unless flow rate
# clearly indicates a fixture (e.g., shower at 1.4 GPM).
# Format: list of (days, start_hour, start_min, end_hour, end_min)
# Days: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
RECIRC_SCHEDULE = [
    # Monday-Friday
    ((0, 1, 2, 3, 4), 4, 45, 9, 0),
    ((0, 1, 2, 3, 4), 12, 0, 13, 0),
    ((0, 1, 2, 3, 4), 16, 30, 21, 30),
    # Saturday
    ((5,), 4, 0, 8, 0),
    ((5,), 14, 0, 16, 0),
    # Sunday
    ((6,), 4, 0, 8, 0),
    ((6,), 8, 30, 16, 0),   # 08:30-15:00 + 14:00-16:00 merged
    ((6,), 17, 0, 19, 0),
]
