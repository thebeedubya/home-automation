"""Main control loop: collect data, predict, and trigger recirc pump."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

from collector import RinnaiClient, UsageTracker, init_db
from config import (
    CONFIDENCE_THRESHOLD, DB_PATH, FLOW_THRESHOLD_GPM,
    POLL_INTERVAL_SEC, PREDICTION_HORIZON_MIN, RECIRC_DURATION_MIN,
    REACTIVE_RECIRC_MIN,
)
from predictor import UsagePredictor
from telegram_bot import TelegramPoller, send_message


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
        self.tracker = UsageTracker(self.conn, on_demand=self._on_demand)
        self.predictor = UsagePredictor()
        self.rinnai = RinnaiClient()
        self.last_recirc_trigger: datetime | None = None
        self.recirc_cooldown_min = 15  # Don't re-trigger within 15 min
        self._reactive_triggered = False  # Track if reactive recirc already fired for current event
        self._idle_inlet: float | None = None  # Inlet temp when no flow (baseline)
        self.recirc_reason: str | None = None  # "reactive", "manual", "predicted", or None (= schedule)

    def _on_demand(self, event_type, fixture_type, duration, avg_flow, details):
        """Called when a demand event completes."""
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        fixture = fixture_type or "unknown"
        send_message(
            f"🔥 <b>Demand: {fixture}</b>\n"
            f"Duration: {minutes}m {seconds}s | Flow: {avg_flow:.1f} GPM\n"
            f"{details.strip(' |')}"
        )

    def trigger_recirc(self, confidence: float):
        """Start the recirc pump via Rinnai Cloud API."""
        now = datetime.now(timezone.utc)

        # Cooldown check
        if self.last_recirc_trigger:
            elapsed = (now - self.last_recirc_trigger).total_seconds() / 60
            if elapsed < self.recirc_cooldown_min:
                print(f"[RECIRC] Cooldown active ({elapsed:.0f}/{self.recirc_cooldown_min} min)")
                return

        if self.rinnai.start_recirculation(RECIRC_DURATION_MIN):
            self.last_recirc_trigger = now
            print(f"[RECIRC] TRIGGERED for {RECIRC_DURATION_MIN} min "
                  f"(confidence={confidence:.1%})")

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

    def backfill_predictions(self):
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

    def run(self):
        print(f"[CONTROLLER] Starting in '{self.mode}' mode")
        print(f"[CONTROLLER] Data source: Rinnai Cloud API (direct)")
        print(f"[CONTROLLER] Polling every {POLL_INTERVAL_SEC}s")
        print(f"[CONTROLLER] Prediction horizon: {PREDICTION_HORIZON_MIN} min")
        print(f"[CONTROLLER] Confidence threshold: {CONFIDENCE_THRESHOLD:.0%}")

        # Start Telegram bot
        self.telegram = TelegramPoller(self)
        self.telegram.start()
        send_message("🔥 Smart Recirc controller started\nMode: " + self.mode)

        # Initial training attempt
        self.retrain()

        retrain_counter = 0

        while True:
            now = datetime.now(timezone.utc)

            # Fetch all sensors in one API call (with maintenance retrieval)
            sensors = self.rinnai.fetch_sensors()

            if sensors is None:
                print(f"[WARN] {now.strftime('%H:%M:%S')} Sensor fetch failed, skipping cycle")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            flow = sensors["flow_rate"]
            inlet = sensors["inlet_temp"]
            outlet = sensors["outlet_temp"]
            heating = sensors["heating"]
            recirc = sensors["recirc_on"]

            # Store reading
            self.conn.execute("""
                INSERT INTO sensor_readings
                    (timestamp, flow_rate, inlet_temp, outlet_temp, heating, recirc_on)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (now.isoformat(), flow, inlet, outlet, heating, recirc))
            self.conn.commit()

            # Track idle inlet temp for baseline
            if flow is not None and flow < FLOW_THRESHOLD_GPM:
                if inlet is not None:
                    self._idle_inlet = inlet
                self._reactive_triggered = False  # Reset for next event

            # Clear recirc reason when pump stops
            if recirc == 0 and self.recirc_reason:
                self.recirc_reason = None

            # Reactive recirc: cold inrush detected + pump not running → start pump
            if (flow is not None and flow >= FLOW_THRESHOLD_GPM
                    and recirc == 0
                    and not self._reactive_triggered
                    and inlet is not None and inlet < 80.0
                    and heating == 1):
                self._reactive_triggered = True
                self.recirc_reason = "reactive"
                print(f"[REACTIVE] Cold inrush detected! "
                      f"flow={flow:.1f} inlet={inlet:.0f}F heating=ON recirc=OFF")
                self.rinnai.start_recirculation(REACTIVE_RECIRC_MIN)
                send_message(
                    f"🚿 <b>Cold inrush detected!</b>\n"
                    f"Flow: {flow:.1f} GPM | Inlet: {inlet:.0f}°F\n"
                    f"Recirc started for {REACTIVE_RECIRC_MIN} min"
                )

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
                        self.trigger_recirc(prediction["confidence"])
                    else:
                        print(f"[PREDICT] Would trigger recirc "
                              f"(confidence={prediction['confidence']:.1%}) "
                              f"— dry run mode")

            # Retrain every 100 cycles (~50 min at 30s intervals)
            retrain_counter += 1
            if retrain_counter >= 100:
                retrain_counter = 0
                self.retrain()
                self.backfill_predictions()

            time.sleep(POLL_INTERVAL_SEC)


def main():
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "observe"
    if mode not in ("observe", "predict", "active"):
        print(f"Usage: python controller.py [observe|predict|active]")
        sys.exit(1)

    controller = SmartRecircController(mode=mode)
    controller.run()


if __name__ == "__main__":
    main()
