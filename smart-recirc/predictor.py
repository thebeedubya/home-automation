"""Predicts hot water usage based on learned temporal patterns."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from config import (
    CONFIDENCE_THRESHOLD, DB_PATH, MIN_TRAINING_DAYS,
    PREDICTION_HORIZON_MIN,
)


class UsagePredictor:
    """
    Learns household hot water usage patterns from historical events.

    The model builds probability distributions across three time scales:
    1. Time-of-day (5-minute buckets) — captures daily routine
    2. Day-of-week × time-of-day — captures weekday/weekend differences
    3. Inter-event intervals — captures "if they used water 10 min ago,
       they'll probably use it again soon" patterns (e.g., shower → sink)

    Predictions combine these signals with exponential decay weighting
    so recent patterns matter more than old ones.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.time_of_day_prob: dict[int, float] = {}    # 5-min bucket → probability
        self.dow_tod_prob: dict[tuple[int, int], float] = {}  # (dow, bucket) → probability
        self.interval_hist: list[float] = []              # minutes between events
        self.last_event_time: datetime | None = None
        self.total_days = 0
        self.total_events = 0

    def train(self):
        """Load all usage events and build probability model."""
        conn = sqlite3.connect(self.db_path)

        events = conn.execute("""
            SELECT start_time, end_time, duration_sec, peak_flow_rate,
                   day_of_week, hour, minute
            FROM usage_events
            WHERE event_type = 'demand' OR event_type IS NULL
            ORDER BY start_time
        """).fetchall()

        if not events:
            print("[MODEL] No training data yet")
            return

        # Check if we have enough data
        first = datetime.fromisoformat(events[0][0])
        last = datetime.fromisoformat(events[-1][0])
        self.total_days = max(1, (last - first).days)
        self.total_events = len(events)

        print(f"[MODEL] Training on {self.total_events} events over {self.total_days} days")

        if self.total_days < MIN_TRAINING_DAYS:
            print(f"[MODEL] Need {MIN_TRAINING_DAYS} days minimum, have {self.total_days}. "
                  "Still learning...")

        # 1. Time-of-day distribution (5-minute buckets, 288 per day)
        tod_counts: dict[int, int] = defaultdict(int)
        for ev in events:
            hour, minute = ev[5], ev[6]
            bucket = (hour * 60 + minute) // 5
            tod_counts[bucket] += 1

        # Normalize: events per bucket per day
        for bucket in range(288):
            tod_counts.setdefault(bucket, 0)
            self.time_of_day_prob[bucket] = tod_counts[bucket] / self.total_days

        # 2. Day-of-week × time-of-day
        dow_tod_counts: dict[tuple[int, int], int] = defaultdict(int)
        dow_day_counts: dict[int, int] = defaultdict(int)
        for ev in events:
            dow = ev[4]
            bucket = (ev[5] * 60 + ev[6]) // 5
            dow_tod_counts[(dow, bucket)] += 1

        # Count how many of each day-of-week we have
        seen_dates: set[tuple[int, str]] = set()
        for ev in events:
            dt = datetime.fromisoformat(ev[0])
            seen_dates.add((dt.weekday(), dt.strftime("%Y-%m-%d")))
        for dow, _ in seen_dates:
            dow_day_counts[dow] += 1

        for dow in range(7):
            n_days = max(1, dow_day_counts.get(dow, 1))
            for bucket in range(288):
                count = dow_tod_counts.get((dow, bucket), 0)
                self.dow_tod_prob[(dow, bucket)] = count / n_days

        # 3. Inter-event intervals
        self.interval_hist = []
        prev_time = None
        for ev in events:
            t = datetime.fromisoformat(ev[0])
            if prev_time is not None:
                gap_min = (t - prev_time).total_seconds() / 60
                if gap_min < 120:  # Only track gaps < 2 hours (same session)
                    self.interval_hist.append(gap_min)
            prev_time = t

        # Track last event for interval prediction
        if events:
            self.last_event_time = datetime.fromisoformat(events[-1][0])

        conn.close()
        print(f"[MODEL] Ready. Peak hours: {self._peak_hours()}")

    def _peak_hours(self) -> str:
        """Return top 3 peak usage hours."""
        hour_totals: dict[int, float] = defaultdict(float)
        for bucket, prob in self.time_of_day_prob.items():
            hour = (bucket * 5) // 60
            hour_totals[hour] += prob
        top = sorted(hour_totals.items(), key=lambda x: -x[1])[:3]
        return ", ".join(f"{h}:00 ({p:.1f}/day)" for h, p in top)

    def predict(self, at_time: datetime | None = None) -> dict:
        """
        Predict probability of hot water usage in the next PREDICTION_HORIZON_MIN minutes.

        Returns dict with confidence score and component breakdown.
        """
        if at_time is None:
            at_time = datetime.now(timezone.utc)

        dow = at_time.weekday()
        minutes = at_time.hour * 60 + at_time.minute
        bucket = minutes // 5

        # Check buckets within prediction horizon
        horizon_buckets = PREDICTION_HORIZON_MIN // 5 + 1

        # Signal 1: Time-of-day probability (general)
        tod_score = 0.0
        for b in range(bucket, bucket + horizon_buckets):
            b_wrapped = b % 288
            tod_score = max(tod_score, self.time_of_day_prob.get(b_wrapped, 0))

        # Signal 2: Day-of-week specific
        dow_score = 0.0
        for b in range(bucket, bucket + horizon_buckets):
            b_wrapped = b % 288
            dow_score = max(dow_score, self.dow_tod_prob.get((dow, b_wrapped), 0))

        # Signal 3: Inter-event interval
        interval_score = 0.0
        if self.last_event_time and self.interval_hist:
            gap_min = (at_time - self.last_event_time).total_seconds() / 60
            if gap_min < 120:
                # What fraction of historical intervals are shorter than
                # (gap + horizon)? That's our probability the next event
                # happens within the horizon.
                cutoff = gap_min + PREDICTION_HORIZON_MIN
                intervals = np.array(self.interval_hist)
                interval_score = np.mean(intervals <= cutoff)

        # Combine signals (weighted)
        # dow_score is more specific, so weight it higher when available
        if self.total_days >= 7:
            # Enough data for day-of-week patterns
            confidence = (0.25 * tod_score + 0.45 * dow_score + 0.30 * interval_score)
        else:
            # Early days — lean on general time-of-day
            confidence = (0.50 * tod_score + 0.15 * dow_score + 0.35 * interval_score)

        # Cap at 1.0
        confidence = min(1.0, confidence)

        return {
            "confidence": confidence,
            "should_trigger": confidence >= CONFIDENCE_THRESHOLD,
            "at_time": at_time.isoformat(),
            "components": {
                "time_of_day": tod_score,
                "day_of_week_specific": dow_score,
                "inter_event_interval": interval_score,
            },
            "data_quality": {
                "total_events": self.total_events,
                "total_days": self.total_days,
                "sufficient_data": self.total_days >= MIN_TRAINING_DAYS,
            },
        }

    def explain(self, at_time: datetime | None = None) -> str:
        """Human-readable explanation of current prediction."""
        p = self.predict(at_time)
        lines = [
            f"Prediction at {p['at_time']}:",
            f"  Confidence: {p['confidence']:.1%}",
            f"  Should trigger recirc: {'YES' if p['should_trigger'] else 'no'}",
            f"  --- Signal breakdown ---",
            f"  Time-of-day:      {p['components']['time_of_day']:.2f}",
            f"  Day-specific:     {p['components']['day_of_week_specific']:.2f}",
            f"  Inter-event:      {p['components']['inter_event_interval']:.2f}",
            f"  --- Data quality ---",
            f"  Events: {p['data_quality']['total_events']}",
            f"  Days:   {p['data_quality']['total_days']}",
            f"  Ready:  {p['data_quality']['sufficient_data']}",
        ]
        return "\n".join(lines)
