"""Collects hot water usage data from Home Assistant and stores it locally."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone

import aiohttp

from config import (
    DB_PATH, ENTITY_FLOW_RATE, ENTITY_HEATING, ENTITY_INLET_TEMP,
    ENTITY_OUTLET_TEMP, ENTITY_RECIRC, HA_TOKEN, HA_URL, POLL_INTERVAL_SEC,
    FLOW_THRESHOLD_GPM, RECIRC_SCHEDULE,
)


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

    # Classification thresholds
    INLET_COLD_THRESHOLD = 110.0   # Below this during flow = cold water entering
    FLOW_VARIANCE_THRESHOLD = 0.3  # GPM std dev — recirc is rock-steady

    # Inlet drop thresholds for demand detection during schedule windows.
    # Real demand (shower/faucet) pulls cold supply water, crashing inlet
    # 40-60F from a warm loop. Recirc reheating barely moves it.
    INLET_DROP_THRESHOLD = 30.0    # Pre-flow minus min-during-flow
    PRE_FLOW_WARM = 100.0          # Pre-flow inlet must be warm for drop to count

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.in_event = False
        self.event_start: datetime | None = None
        self.peak_flow = 0.0
        self.flow_samples: list[float] = []
        self.inlet_samples: list[float] = []
        self.heating_samples: list[int] = []
        self.pre_flow_inlet: float | None = None
        self._last_idle_inlet: float | None = None  # tracks inlet when no flow

    def update(self, now: datetime, flow_rate: float,
               inlet_temp: float | None = None,
               heating: int | None = None,
               recirc_on: bool = False):
        # Track inlet temp when idle (no flow) for pre-flow baseline
        if flow_rate < FLOW_THRESHOLD_GPM and inlet_temp is not None:
            self._last_idle_inlet = inlet_temp

        if flow_rate >= FLOW_THRESHOLD_GPM and not self.in_event:
            # Flow started — snapshot pre-flow inlet
            self.in_event = True
            self.event_start = now
            self.peak_flow = flow_rate
            self.flow_samples = [flow_rate]
            self.inlet_samples = [inlet_temp] if inlet_temp is not None else []
            self.heating_samples = [heating] if heating is not None else []
            self.pre_flow_inlet = self._last_idle_inlet

        elif flow_rate >= FLOW_THRESHOLD_GPM and self.in_event:
            # Flow continuing — accumulate samples
            self.peak_flow = max(self.peak_flow, flow_rate)
            self.flow_samples.append(flow_rate)
            if inlet_temp is not None:
                self.inlet_samples.append(inlet_temp)
            if heating is not None:
                self.heating_samples.append(heating)

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

        Classification strategy:
        1. If we're in a recirc schedule window, default to 'recirc' UNLESS:
           a. Flow rate is clearly non-pump (< 2.5 GPM avg = shower/faucet), OR
           b. Inlet temp crashed from a warm pre-flow baseline (someone drew
              water, cold supply entering — the pump alone doesn't do this
              when the loop was already warm).
        2. Outside schedule windows, use the multi-signal approach:
           inlet temp, heating state, and flow variance.
        """
        avg_flow = sum(self.flow_samples) / len(self.flow_samples)

        # Schedule-aware classification
        if self.event_start and self._in_recirc_schedule(self.event_start):
            # Signal A: Flow rate clearly below pump rate
            if avg_flow < self.PUMP_FLOW_MIN:
                return "demand"

            # Signal B: Inlet temp crashed from warm baseline
            # Real demand: pre-flow 115F → min 60F (drop 55F)
            # Recirc reheat: pre-flow 110F → min 105F (drop 5F)
            if (self.pre_flow_inlet is not None
                    and self.pre_flow_inlet >= self.PRE_FLOW_WARM
                    and self.inlet_samples):
                min_inlet = min(self.inlet_samples)
                drop = self.pre_flow_inlet - min_inlet
                if drop >= self.INLET_DROP_THRESHOLD:
                    return "demand"

            return "recirc"

        # Outside schedule: use sensor signals (any one = demand)
        # Signal 1: Cold water entering
        if self.inlet_samples:
            min_inlet = min(self.inlet_samples)
            if min_inlet < self.INLET_COLD_THRESHOLD:
                return "demand"

        # Signal 2: Burner fired
        if self.heating_samples and any(h == 1 for h in self.heating_samples):
            return "demand"

        # Signal 3: Flow rate variance (recirc is rock-steady)
        if len(self.flow_samples) > 2:
            import statistics
            flow_sd = statistics.stdev(self.flow_samples)
            if flow_sd > self.FLOW_VARIANCE_THRESHOLD:
                return "demand"

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


async def fetch_state(session: aiohttp.ClientSession, entity_id: str) -> str | None:
    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("state")
    except Exception as e:
        print(f"[WARN] Failed to fetch {entity_id}: {e}")
    return None


def safe_float(val: str | None) -> float | None:
    if val is None or val in ("unavailable", "unknown"):
        return None
    try:
        return float(val)
    except ValueError:
        return None


def safe_bool(val: str | None) -> int | None:
    if val is None or val in ("unavailable", "unknown"):
        return None
    return 1 if val == "on" else 0


async def collect_loop():
    conn = init_db()
    tracker = UsageTracker(conn)

    print(f"[START] Smart Recirc collector polling every {POLL_INTERVAL_SEC}s")
    print(f"[START] Database: {DB_PATH}")

    async with aiohttp.ClientSession() as session:
        while True:
            now = datetime.now(timezone.utc)

            # Fetch all sensors in parallel
            flow_raw, inlet_raw, outlet_raw, heating_raw, recirc_raw = await asyncio.gather(
                fetch_state(session, ENTITY_FLOW_RATE),
                fetch_state(session, ENTITY_INLET_TEMP),
                fetch_state(session, ENTITY_OUTLET_TEMP),
                fetch_state(session, ENTITY_HEATING),
                fetch_state(session, ENTITY_RECIRC),
            )

            flow = safe_float(flow_raw)
            inlet = safe_float(inlet_raw)
            outlet = safe_float(outlet_raw)
            heating = safe_bool(heating_raw)
            recirc = safe_bool(recirc_raw)

            # Store raw reading
            conn.execute("""
                INSERT INTO sensor_readings
                    (timestamp, flow_rate, inlet_temp, outlet_temp, heating, recirc_on)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (now.isoformat(), flow, inlet, outlet, heating, recirc))
            conn.commit()

            # Track usage events
            if flow is not None:
                tracker.update(now, flow, inlet_temp=inlet, heating=heating)

            if flow is not None and flow >= FLOW_THRESHOLD_GPM:
                print(f"[FLOW] {now.strftime('%H:%M:%S')} | "
                      f"{flow:.1f} GPM | inlet={inlet}°F | outlet={outlet}°F")

            await asyncio.sleep(POLL_INTERVAL_SEC)


def main():
    asyncio.run(collect_loop())


if __name__ == "__main__":
    main()
