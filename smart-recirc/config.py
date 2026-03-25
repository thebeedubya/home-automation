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
