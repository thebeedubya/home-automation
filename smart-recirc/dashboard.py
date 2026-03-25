"""Quick CLI dashboard to view collected data and predictions."""

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from config import DB_PATH
from predictor import UsagePredictor


def show_status():
    conn = sqlite3.connect(DB_PATH)

    # Reading count
    readings = conn.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    predictions = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]

    print("=" * 60)
    print("  SMART RECIRC — STATUS")
    print("=" * 60)
    print(f"  Sensor readings:  {readings:,}")
    print(f"  Usage events:     {events:,}")
    print(f"  Predictions made: {predictions:,}")

    # Prediction accuracy
    if predictions > 0:
        correct = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE actual_usage = recirc_triggered"
        ).fetchone()[0]
        evaluated = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE actual_usage IS NOT NULL"
        ).fetchone()[0]
        if evaluated > 0:
            print(f"  Prediction accuracy: {correct/evaluated:.1%} ({correct}/{evaluated})")
        else:
            print("  Prediction accuracy: (not yet evaluated)")

    print()

    # Recent events
    recent = conn.execute("""
        SELECT start_time, duration_sec, peak_flow_rate, avg_flow_rate,
               event_type, fixture_type
        FROM usage_events ORDER BY start_time DESC LIMIT 15
    """).fetchall()

    if recent:
        print("  RECENT HOT WATER EVENTS")
        print("  " + "-" * 72)
        print(f"  {'When':<16} {'Type':<14} {'Duration':>8} {'Peak GPM':>9} {'Avg GPM':>9}")
        print("  " + "-" * 72)
        for row in recent:
            dt = datetime.fromisoformat(row[0])
            local_tz = timezone(timedelta(hours=-5))  # CDT
            local = dt.astimezone(local_tz).strftime("%a %I:%M%p")
            dur = f"{row[1]:.0f}s"
            fixture = row[5] or row[4] or "?"
            print(f"  {local:<16} {fixture:<14} {dur:>8} {row[2]:>9.1f} {row[3]:>9.1f}")

        # Fixture summary
        print()
        print("  FIXTURE SUMMARY")
        print("  " + "-" * 72)
        fixtures = conn.execute("""
            SELECT fixture_type, COUNT(*),
                   ROUND(SUM(duration_sec)), ROUND(AVG(avg_flow_rate), 1)
            FROM usage_events
            WHERE event_type = 'demand' AND fixture_type IS NOT NULL
            GROUP BY fixture_type ORDER BY COUNT(*) DESC
        """).fetchall()
        for f in fixtures:
            total_min = f[2] / 60
            print(f"  {f[0]:<14} {f[1]:>3} events | {total_min:>5.0f} min total | avg {f[3]} GPM")
    else:
        print("  No usage events recorded yet.")

    print()

    # Daily pattern (convert UTC stored hours to local time)
    if events > 0:
        print("  DAILY USAGE PATTERN — demand only (events per hour, CDT)")
        print("  " + "-" * 56)
        demand_events = conn.execute("""
            SELECT start_time FROM usage_events WHERE event_type = 'demand'
        """).fetchall()
        local_tz = timezone(timedelta(hours=-5))
        hour_map = defaultdict(int)
        for (ts,) in demand_events:
            dt = datetime.fromisoformat(ts).astimezone(local_tz)
            hour_map[dt.hour] += 1
        max_count = max(hour_map.values()) if hour_map else 1

        for h in range(24):
            count = hour_map.get(h, 0)
            bar_len = int(40 * count / max_count) if max_count > 0 else 0
            bar = "█" * bar_len
            label = f"  {h:02d}:00"
            print(f"{label} {'▏' if count == 0 else bar} {count}")

    print()

    # Current prediction
    predictor = UsagePredictor()
    predictor.train()
    if predictor.total_events > 0:
        print(predictor.explain())

    conn.close()


if __name__ == "__main__":
    show_status()
