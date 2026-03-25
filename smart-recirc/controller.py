"""Main control loop: collect data, predict, and trigger recirc pump."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import aiohttp

from collector import UsageTracker, fetch_state, init_db, safe_bool, safe_float
from config import (
    CONFIDENCE_THRESHOLD, DB_PATH, ENTITY_FLOW_RATE, ENTITY_HEATING,
    ENTITY_INLET_TEMP, ENTITY_OUTLET_TEMP, ENTITY_RECIRC,
    ENTITY_WATER_HEATER, FLOW_THRESHOLD_GPM, HA_TOKEN, HA_URL,
    POLL_INTERVAL_SEC, PREDICTION_HORIZON_MIN, RECIRC_DURATION_MIN,
)
from predictor import UsagePredictor


class SmartRecircController:
    """
    Runs the collect → predict → actuate loop.

    Modes:
        observe  — collect data only, no recirc triggering
        predict  — collect + predict, log predictions but don't actuate
        active   — full autonomous control
    """

    def __init__(self, mode: str = "observe"):
        self.mode = mode
        self.conn = init_db()
        self.tracker = UsageTracker(self.conn)
        self.predictor = UsagePredictor()
        self.last_recirc_trigger: datetime | None = None
        self.recirc_cooldown_min = 15  # Don't re-trigger within 15 min

    async def trigger_recirc(self, session: aiohttp.ClientSession, confidence: float):
        """Start the recirc pump via HA service call."""
        now = datetime.now(timezone.utc)

        # Cooldown check
        if self.last_recirc_trigger:
            elapsed = (now - self.last_recirc_trigger).total_seconds() / 60
            if elapsed < self.recirc_cooldown_min:
                print(f"[RECIRC] Cooldown active ({elapsed:.0f}/{self.recirc_cooldown_min} min)")
                return

        url = f"{HA_URL}/api/services/rinnai/start_recirculation"
        headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "entity_id": ENTITY_WATER_HEATER,
            "recirculation_minutes": str(RECIRC_DURATION_MIN),
        }

        try:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    self.last_recirc_trigger = now
                    print(f"[RECIRC] TRIGGERED for {RECIRC_DURATION_MIN} min "
                          f"(confidence={confidence:.1%})")

                    # Log prediction
                    self.conn.execute("""
                        INSERT INTO predictions
                            (predicted_at, predicted_for, confidence, recirc_triggered, actual_usage)
                        VALUES (?, ?, ?, 1, NULL)
                    """, (
                        now.isoformat(),
                        (now + timedelta(minutes=PREDICTION_HORIZON_MIN)).isoformat(),
                        confidence,
                    ))
                    self.conn.commit()
                else:
                    print(f"[RECIRC] Failed to trigger: HTTP {resp.status}")
        except Exception as e:
            print(f"[RECIRC] Error: {e}")

    async def backfill_predictions(self):
        """
        Check if past predictions were accurate by looking at whether
        actual usage occurred within the prediction window.
        """
        conn = self.conn
        pending = conn.execute("""
            SELECT id, predicted_for FROM predictions WHERE actual_usage IS NULL
        """).fetchall()

        for pred_id, predicted_for in pending:
            predicted_dt = datetime.fromisoformat(predicted_for)
            if datetime.now(timezone.utc) < predicted_dt:
                continue  # Not yet past the prediction window

            # Check if there was usage in the window
            window_start = predicted_dt - timedelta(minutes=PREDICTION_HORIZON_MIN)
            usage = conn.execute("""
                SELECT COUNT(*) FROM usage_events
                WHERE start_time BETWEEN ? AND ?
            """, (window_start.isoformat(), predicted_for)).fetchone()[0]

            conn.execute("""
                UPDATE predictions SET actual_usage = ? WHERE id = ?
            """, (1 if usage > 0 else 0, pred_id))

        conn.commit()

    def retrain(self):
        """Retrain the prediction model on all collected data."""
        self.predictor.train()

    async def run(self):
        print(f"[CONTROLLER] Starting in '{self.mode}' mode")
        print(f"[CONTROLLER] Polling every {POLL_INTERVAL_SEC}s")
        print(f"[CONTROLLER] Prediction horizon: {PREDICTION_HORIZON_MIN} min")
        print(f"[CONTROLLER] Confidence threshold: {CONFIDENCE_THRESHOLD:.0%}")

        # Initial training attempt
        self.retrain()

        retrain_counter = 0

        async with aiohttp.ClientSession() as session:
            while True:
                now = datetime.now(timezone.utc)

                # Fetch sensors
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

                # Store reading
                self.conn.execute("""
                    INSERT INTO sensor_readings
                        (timestamp, flow_rate, inlet_temp, outlet_temp, heating, recirc_on)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (now.isoformat(), flow, inlet, outlet, heating, recirc))
                self.conn.commit()

                # Track usage events with multi-signal classification
                if flow is not None:
                    self.tracker.update(
                        now, flow,
                        inlet_temp=inlet,
                        heating=heating,
                        recirc_on=(recirc == 1),
                    )

                    if flow >= FLOW_THRESHOLD_GPM and heating == 1:
                        # Burner firing during flow = real demand
                        self.predictor.last_event_time = now

                # Run prediction (if we have data)
                if self.mode in ("predict", "active") and self.predictor.total_events > 0:
                    prediction = self.predictor.predict(now)

                    if prediction["should_trigger"]:
                        if self.mode == "active":
                            await self.trigger_recirc(session, prediction["confidence"])
                        else:
                            print(f"[PREDICT] Would trigger recirc "
                                  f"(confidence={prediction['confidence']:.1%}) "
                                  f"— dry run mode")

                # Retrain every 100 cycles (~50 min at 30s intervals)
                retrain_counter += 1
                if retrain_counter >= 100:
                    retrain_counter = 0
                    self.retrain()
                    await self.backfill_predictions()

                await asyncio.sleep(POLL_INTERVAL_SEC)


def main():
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "observe"
    if mode not in ("observe", "predict", "active"):
        print(f"Usage: python controller.py [observe|predict|active]")
        sys.exit(1)

    controller = SmartRecircController(mode=mode)
    asyncio.run(controller.run())


if __name__ == "__main__":
    main()
