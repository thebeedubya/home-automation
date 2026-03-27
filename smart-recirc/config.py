"""Configuration for Smart Recirc system."""

import os

HA_URL = os.environ.get("HA_URL", "http://192.168.1.98:8123")
HA_TOKEN = os.environ["HA_TOKEN"]  # Long-lived access token from Home Assistant

# Rinnai entity IDs
ENTITY_FLOW_RATE = "sensor.main_house_water_flow_rate"
ENTITY_INLET_TEMP = "sensor.main_house_inlet_temperature"
ENTITY_OUTLET_TEMP = "sensor.main_house_outlet_temperature"
ENTITY_HEATING = "binary_sensor.main_house_heating"
ENTITY_RECIRC = "binary_sensor.main_house_recirculation"
ENTITY_RECIRC_SWITCH = "switch.main_house_recirculation"
ENTITY_WATER_HEATER = "water_heater.main_house_water_heater"

# Prediction parameters
FLOW_THRESHOLD_GPM = 0.1          # Minimum flow to count as "using hot water"
PREDICTION_HORIZON_MIN = 5        # How far ahead to predict (minutes)
RECIRC_DURATION_MIN = 5           # How long to run recirc pump when triggered
CONFIDENCE_THRESHOLD = 0.6        # Minimum confidence to trigger recirc
POLL_INTERVAL_SEC = 30            # How often to poll HA for sensor data

# Data collection
DB_PATH = "smart_recirc.db"
MIN_TRAINING_DAYS = 3             # Minimum days of data before enabling predictions

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
