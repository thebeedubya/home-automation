"""Collects hot water usage data directly from Rinnai Cloud API."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone

import boto3

from config import (
    DB_PATH, FLOW_RAW_DIVISOR, FLOW_THRESHOLD_GPM, POLL_INTERVAL_SEC,
    RECIRC_SCHEDULE, RINNAI_API_KEY, RINNAI_EMAIL, RINNAI_GRAPHQL_URL,
    RINNAI_PASSWORD, RINNAI_SHADOW_URL, RINNAI_THING_NAME,
)

# Cognito config
_COGNITO_CLIENT_ID = "5ghq3i6k4p9s7dfu34ckmec91"
_COGNITO_REGION = "us-east-1"

# GraphQL query — pulls sensor data and shadow state in one call
_SENSOR_QUERY = """
query GetUserByEmail($email: String) {
  getUserByEmail(email: $email) {
    items {
      devices {
        items {
          info {
            m01_water_flow_rate_raw
            m08_inlet_temperature
            m02_outlet_temperature
            domestic_combustion
          }
          shadow {
            recirculation_enabled
          }
        }
      }
    }
  }
}
"""

_GQL_HEADERS = {
    "x-amz-user-agent": "aws-amplify/3.4.3 react-native",
    "x-api-key": RINNAI_API_KEY,
    "Content-Type": "application/json",
}


class RinnaiClient:
    """Handles auth, maintenance retrieval, and sensor reads against Rinnai Cloud API."""

    def __init__(self):
        self._id_token: str | None = None
        self._token_expires: float = 0.0
        self._last_maintenance: float = 0.0
        self._maint_interval: float = 60.0  # seconds between maintenance retrievals

    def _authenticate(self):
        """Get a fresh Cognito ID token via USER_PASSWORD_AUTH."""
        client = boto3.client(
            "cognito-idp", region_name=_COGNITO_REGION,
            aws_access_key_id="dummy", aws_secret_access_key="dummy",
        )
        resp = client.initiate_auth(
            ClientId=_COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": RINNAI_EMAIL,
                "PASSWORD": RINNAI_PASSWORD,
            },
        )
        result = resp["AuthenticationResult"]
        self._id_token = result["IdToken"]
        self._token_expires = time.time() + result["ExpiresIn"] - 300  # refresh 5 min early

    def _ensure_auth(self):
        if self._id_token is None or time.time() >= self._token_expires:
            self._authenticate()
            print("[AUTH] Rinnai token refreshed")

    def _do_maintenance_retrieval(self):
        """Tell the device to push fresh sensor data to the cloud."""
        now = time.time()
        if now - self._last_maintenance < self._maint_interval:
            return

        self._ensure_auth()
        url = RINNAI_SHADOW_URL % RINNAI_THING_NAME
        data = json.dumps({"do_maintenance_retrieval": True}).encode()
        req = urllib.request.Request(url, data=data, method="PATCH", headers={
            "User-Agent": "okhttp/3.12.1",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {self._id_token}",
        })
        try:
            with urllib.request.urlopen(req, timeout=15):
                self._last_maintenance = now
        except Exception as e:
            print(f"[WARN] Maintenance retrieval failed: {e}")

    def start_recirculation(self, duration: int = 5):
        """Start the recirc pump for the given duration in minutes."""
        self._ensure_auth()
        url = RINNAI_SHADOW_URL % RINNAI_THING_NAME
        data = json.dumps({
            "recirculation_duration": str(duration),
            "set_recirculation_enabled": True,
        }).encode()
        req = urllib.request.Request(url, data=data, method="PATCH", headers={
            "User-Agent": "okhttp/3.12.1",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {self._id_token}",
        })
        try:
            with urllib.request.urlopen(req, timeout=15):
                print(f"[RECIRC] Started for {duration} min via Rinnai API")
                return True
        except Exception as e:
            print(f"[RECIRC] Failed to start: {e}")
            return False

    def fetch_sensors(self) -> dict | None:
        """Trigger maintenance retrieval then fetch fresh sensor values.

        Returns dict with keys: flow_rate, inlet_temp, outlet_temp, heating, recirc_on
        Returns None on error.
        """
        self._do_maintenance_retrieval()

        payload = json.dumps({
            "query": _SENSOR_QUERY,
            "variables": {"email": RINNAI_EMAIL},
        }).encode()

        req = urllib.request.Request(
            RINNAI_GRAPHQL_URL, data=payload, headers=_GQL_HEADERS, method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"[WARN] Rinnai API fetch failed: {e}")
            return None

        try:
            device = data["data"]["getUserByEmail"]["items"][0]["devices"]["items"][0]
            info = device["info"]
            shadow = device["shadow"]
        except (KeyError, IndexError) as e:
            print(f"[WARN] Rinnai API response parse error: {e}")
            return None

        flow_raw = _safe_float(info.get("m01_water_flow_rate_raw"))
        inlet = _safe_float(info.get("m08_inlet_temperature"))
        outlet = _safe_float(info.get("m02_outlet_temperature"))
        combustion = info.get("domestic_combustion")
        recirc = shadow.get("recirculation_enabled")

        return {
            "flow_rate": flow_raw / FLOW_RAW_DIVISOR if flow_raw is not None else None,
            "inlet_temp": inlet,
            "outlet_temp": outlet,
            "heating": 1 if combustion in (True, "true") else 0,
            "recirc_on": 1 if recirc in (True, "true") else 0,
        }


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sensor_readings (
            timestamp TEXT NOT NULL,
            flow_rate REAL,
            inlet_temp REAL,
            outlet_temp REAL,
            heating INTEGER,
            recirc_on INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            duration_sec REAL,
            peak_flow_rate REAL,
            avg_flow_rate REAL,
            day_of_week INTEGER,
            hour INTEGER,
            minute INTEGER,
            event_type TEXT DEFAULT 'demand',
            fixture_type TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            predicted_at TEXT NOT NULL,
            predicted_for TEXT NOT NULL,
            confidence REAL,
            recirc_triggered INTEGER,
            actual_usage INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_readings_ts ON sensor_readings(timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_start ON usage_events(start_time)
    """)
    # Migrate: add fixture_type column if missing
    cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_events)")}
    if "fixture_type" not in cols:
        conn.execute("ALTER TABLE usage_events ADD COLUMN fixture_type TEXT")
    conn.commit()
    return conn


class UsageTracker:
    """Tracks flow state transitions to detect discrete usage events.

    Distinguishes real hot water demand from recirculation pump flow using
    multi-signal classification instead of relying on the recirc binary sensor
    (which stays "on" during both recirc and demand on Control-R units).

    Recirc signature: warm inlet (>110F), no heating, steady flow, small delta.
    Demand signature: inlet drops (cold water entering), heating fires, flow varies.
    """

    # Classification thresholds — tuned from 2 weeks of data (513 events)
    #
    # Pump flow: 2.5-3.3 GPM (floor 2.5, 99% at 2.6+). Runs slow at 2.0-2.5
    # sometimes but always with small drop and short gap from prior recirc.
    # Real fixtures: 0.1-2.0 GPM (faucet/shower). Baths overlap at 2.2-2.5.
    #
    # Key discriminator: inlet temp drop from pre-flow baseline.
    # - Recirc between-cycle cooldown: 0-15F drop
    # - Pump cold-start after long gap: big drop but pre-flow already cold (<80F)
    # - Real demand: >20F drop from warm pre-flow (>=100F)
    PUMP_FLOW_MIN = 2.0            # Below this = definitely a fixture
    PUMP_FLOW_AMBIG = 2.5          # 2.0-2.5 GPM = ambiguous zone, needs drop signal
    INLET_DROP_THRESHOLD = 20.0    # Pre-flow minus min-during-flow
    PRE_FLOW_WARM = 100.0          # Pre-flow inlet must be warm for drop to count
    PRE_FLOW_COLD = 80.0           # Below this = pipes fully cooled (ambient)
    LONG_GAP_MIN = 60              # Minutes — gap > this = schedule restart

    def __init__(self, conn: sqlite3.Connection, on_demand=None):
        self.conn = conn
        self.on_demand = on_demand  # callback(event_type, fixture_type, duration, avg_flow, details)
        self.in_event = False
        self.event_start: datetime | None = None
        self.peak_flow = 0.0
        self.flow_samples: list[float] = []
        self.inlet_samples: list[float] = []
        self.heating_samples: list[int] = []
        self.pre_flow_inlet: float | None = None
        self.recirc_samples: list[int] = []
        self._idle_inlet_history: list[float] = []  # recent idle inlet readings
        self._IDLE_HISTORY_MAX = 10  # keep last ~5 min at 30s polling
        self._last_event_end: datetime | None = None  # for gap calculation

    def update(self, now: datetime, flow_rate: float,
               inlet_temp: float | None = None,
               heating: int | None = None,
               recirc_on: bool = False):
        # Track inlet temp when idle (no flow) for pre-flow baseline
        if flow_rate < FLOW_THRESHOLD_GPM and inlet_temp is not None:
            self._idle_inlet_history.append(inlet_temp)
            if len(self._idle_inlet_history) > self._IDLE_HISTORY_MAX:
                self._idle_inlet_history.pop(0)

        if flow_rate >= FLOW_THRESHOLD_GPM and not self.in_event:
            # Flow started — snapshot pre-flow inlet
            self.in_event = True
            self.event_start = now
            self.peak_flow = flow_rate
            self.flow_samples = [flow_rate]
            self.inlet_samples = [inlet_temp] if inlet_temp is not None else []
            self.heating_samples = [heating] if heating is not None else []
            self.recirc_samples = [1 if recirc_on else 0]
            self.pre_flow_inlet = max(self._idle_inlet_history) if self._idle_inlet_history else None

        elif flow_rate >= FLOW_THRESHOLD_GPM and self.in_event:
            # Flow continuing — accumulate samples
            self.peak_flow = max(self.peak_flow, flow_rate)
            self.flow_samples.append(flow_rate)
            if inlet_temp is not None:
                self.inlet_samples.append(inlet_temp)
            if heating is not None:
                self.heating_samples.append(heating)
            self.recirc_samples.append(1 if recirc_on else 0)

        elif flow_rate < FLOW_THRESHOLD_GPM and self.in_event:
            # Flow ended — classify and record
            self.in_event = False
            duration = (now - self.event_start).total_seconds()
            avg_flow = sum(self.flow_samples) / len(self.flow_samples)

            event_type = self._classify()
            fixture_type = self._identify_fixture(event_type, duration, avg_flow)

            self.conn.execute("""
                INSERT INTO usage_events
                    (start_time, end_time, duration_sec, peak_flow_rate,
                     avg_flow_rate, day_of_week, hour, minute, event_type,
                     fixture_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.event_start.isoformat(),
                now.isoformat(),
                duration,
                self.peak_flow,
                avg_flow,
                self.event_start.weekday(),
                self.event_start.hour,
                self.event_start.minute,
                event_type,
                fixture_type,
            ))
            self.conn.commit()

            # Build detail string for logging
            details = []
            if fixture_type:
                details.append(fixture_type)
            if self.pre_flow_inlet is not None:
                details.append(f"pre={self.pre_flow_inlet:.0f}F")
            if self.inlet_samples:
                details.append(f"inlet={min(self.inlet_samples):.0f}-{max(self.inlet_samples):.0f}F")
            if self.heating_samples:
                heat_pct = sum(self.heating_samples) / len(self.heating_samples)
                details.append(f"heat={heat_pct:.0%}")
            if len(self.flow_samples) > 1:
                import statistics
                details.append(f"flow_sd={statistics.stdev(self.flow_samples):.2f}")
            detail_str = " | " + " | ".join(details) if details else ""

            tag = "[RECIRC]" if event_type == "recirc" else "[DEMAND]"
            print(f"{tag} {self.event_start.strftime('%H:%M')} → "
                  f"{now.strftime('%H:%M')} | "
                  f"{duration:.0f}s | peak {self.peak_flow:.1f} GPM | "
                  f"avg {avg_flow:.1f} GPM{detail_str}")

            self._last_event_end = now

            if event_type == "demand" and self.on_demand:
                self.on_demand(event_type, fixture_type, duration, avg_flow, detail_str)

    # Flow rates that clearly indicate a fixture, not the pump.
    # The recirc pump runs at 2.7-3.2 GPM. Anything well below that
    # during a schedule window is a real fixture (shower ~1.4, faucet varies).
    PUMP_FLOW_MIN = 2.5  # Below this = not the pump

    def _in_recirc_schedule(self, dt: datetime) -> bool:
        """Check if a timestamp falls within a configured recirc schedule window."""
        from datetime import timedelta, timezone
        local = dt.astimezone(timezone(timedelta(hours=-5)))  # CDT
        dow = local.weekday()  # 0=Mon
        t = local.hour * 60 + local.minute

        for days, sh, sm, eh, em in RECIRC_SCHEDULE:
            if dow in days:
                start = sh * 60 + sm
                end = eh * 60 + em
                if start <= t < end:
                    return True
        return False

    def _classify(self) -> str:
        """Classify a completed flow event as 'demand' or 'recirc'.

        Rules derived from 2 weeks of sensor data (513 events), cross-
        referenced with ecobee occupancy:

        1. Drop > 20F AND pre-flow >= 100F -> DEMAND
           Pipes were warm, cold water rushed in = someone opened a tap.

        2. Flow < 2.0 GPM -> DEMAND
           Pump never runs this slow. Definitely a fixture.

        3. Flow 2.0-2.5 GPM -> AMBIGUOUS (pump runs slow sometimes):
           a. Drop > 20F -> DEMAND (cold inrush overrides flow ambiguity)
           b. Drop <= 20F AND gap < 30 min -> RECIRC (pump running slow)
           c. Drop <= 20F AND pre-flow < 80F -> RECIRC (cold-start, slow)
           d. Drop <= 10F -> RECIRC (tiny drop = normal cooldown)
           e. Otherwise -> DEMAND (can't rule it out)

        4. Flow >= 2.5 GPM AND drop <= 20F -> RECIRC (pump rate, normal)

        5. Flow >= 2.5 GPM AND drop > 20F AND pre >= 100F -> DEMAND
           Someone drew water at high flow during pump cycle.

        6. Flow >= 2.5 GPM AND drop > 20F AND pre < 80F AND gap > 60m
           -> RECIRC (pump cold-start after schedule gap)
        """
        avg_flow = sum(self.flow_samples) / len(self.flow_samples)

        # Calculate inlet drop
        drop = None
        if self.pre_flow_inlet is not None and self.inlet_samples:
            min_inlet = min(self.inlet_samples)
            drop = self.pre_flow_inlet - min_inlet

        has_big_drop = drop is not None and drop > self.INLET_DROP_THRESHOLD
        pre_warm = (self.pre_flow_inlet is not None
                    and self.pre_flow_inlet >= self.PRE_FLOW_WARM)
        pre_cold = (self.pre_flow_inlet is not None
                    and self.pre_flow_inlet < self.PRE_FLOW_COLD)

        # Gap since last event
        gap_min = 9999
        if self._last_event_end is not None and self.event_start is not None:
            gap_min = (self.event_start - self._last_event_end).total_seconds() / 60

        # Rule 1: Warm pipes + big drop = someone drew water
        if has_big_drop and pre_warm:
            return "demand"

        # Rule 2: Definitely not the pump
        if avg_flow < self.PUMP_FLOW_MIN:
            return "demand"

        # Rule 3: Ambiguous zone (2.0-2.5 GPM)
        if avg_flow < self.PUMP_FLOW_AMBIG:
            if has_big_drop:
                return "demand"   # 3a: cold inrush overrides
            if gap_min < 30:
                return "recirc"   # 3b: short gap = pump slow
            if pre_cold:
                return "recirc"   # 3c: cold-start, slow
            if drop is not None and drop <= 10:
                return "recirc"   # 3d: tiny drop = cooldown
            return "demand"       # 3e: can't rule out

        # Rule 5: Pump-rate flow + big drop from warm pipes
        if has_big_drop and pre_warm:
            return "demand"

        # Rule 6: Pump cold-start after long gap
        if has_big_drop and pre_cold and gap_min > self.LONG_GAP_MIN:
            return "recirc"

        # Rule 4: Pump-rate flow, small/no drop
        return "recirc"

    def _identify_fixture(self, event_type: str, duration: float,
                          avg_flow: float) -> str | None:
        """Fingerprint the fixture based on flow rate and duration.

        Learned from observed Rinnai sensor data:
          Bath/tub fill:  2.3-3.2 GPM, 10+ min, high volume (50 gal tub)
          Shower:         1.0-2.2 GPM, 5-20 min, sustained cold inlet
          Faucet/sink:    any flow, < 3 min (quick hand wash, dish rinse)
          Recirc pump:    2.7-3.2 GPM steady, warm inlet, no heating
        """
        if event_type == "recirc":
            return "recirc_pump"

        # Demand events — classify by fixture
        if duration < 180:  # < 3 minutes
            return "faucet"

        if avg_flow < 2.2:
            # Below the recirc pump's natural rate — not tub fill
            if duration >= 300:  # 5+ min sustained
                return "shower"
            return "faucet"

        # High flow (>= 2.2 GPM) demand events
        if duration >= 600:  # 10+ min at high flow = tub fill
            return "bath"
        if duration >= 300:  # 5-10 min at high flow
            # Could be bath starting or a long faucet run.
            # Check if inlet stayed cold the whole time (sustained draw)
            if self.inlet_samples:
                cold_ratio = sum(1 for t in self.inlet_samples
                                 if t < self.INLET_COLD_THRESHOLD) / len(self.inlet_samples)
                if cold_ratio > 0.6:
                    return "bath"
            return "faucet"

        return "faucet"
