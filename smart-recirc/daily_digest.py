"""Daily digest: summarizes yesterday's water heater activity via Gesha and sends to Telegram."""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone

from config import DB_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

GESHA_URL = "http://192.168.1.149:9090/v1/chat/completions"
GESHA_MODEL = "qwen35-35b-uncensored"
CDT = timezone(timedelta(hours=-5))


def _send_telegram(text: str):
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"Telegram send failed: {e}")


def _query_gesha(prompt: str) -> str | None:
    payload = json.dumps({
        "model": GESHA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 250,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        GESHA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Gesha query failed: {e}")
        return None


def build_summary() -> str:
    """Build pre-computed stats for yesterday (CDT)."""
    conn = sqlite3.connect(DB_PATH)

    now_cdt = datetime.now(CDT)
    yesterday_cdt = now_cdt - timedelta(days=1)
    day_label = yesterday_cdt.strftime("%A %B %d")

    # Yesterday 00:00 CDT → today 00:00 CDT in UTC
    start_utc = yesterday_cdt.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end_utc = start_utc + timedelta(days=1)

    rows = conn.execute("""
        SELECT start_time, duration_sec, avg_flow_rate, event_type, fixture_type
        FROM usage_events WHERE start_time >= ? AND start_time < ?
        ORDER BY start_time
    """, (start_utc.isoformat(), end_utc.isoformat())).fetchall()
    conn.close()

    demand = [r for r in rows if r[3] == "demand"]
    recirc = [r for r in rows if r[3] == "recirc"]

    if not demand and not recirc:
        return f"No water heater activity recorded for {day_label}."

    demand_min = sum(r[1] for r in demand) / 60 if demand else 0
    recirc_min = sum(r[1] for r in recirc) / 60 if recirc else 0

    fixtures: dict[str, int] = {}
    for r in demand:
        f = r[4] or "unknown"
        fixtures[f] = fixtures.get(f, 0) + 1
    fixture_str = ", ".join(f"{v} {k}" for k, v in sorted(fixtures.items(), key=lambda x: -x[1]))

    event_lines = []
    for r in demand:
        dt = datetime.fromisoformat(r[0]).astimezone(CDT)
        dur_m = int(r[1] // 60)
        dur_s = int(r[1] % 60)
        event_lines.append(f"  {dt.strftime('%I:%M %p')} - {r[4]}, {dur_m}m{dur_s}s, {r[2]:.1f} GPM")

    ratio = f"Pump ran {recirc_min/demand_min:.0f}x more than needed" if demand_min > 0 else "No demand events"

    return f"""DAILY SUMMARY for {day_label} (all times CDT):

Demand events: {len(demand)} ({fixture_str})
Recirc pump cycles: {len(recirc)}
Total demand time: {demand_min:.0f} minutes
Total recirc pump time: {recirc_min:.0f} minutes
{ratio}

Demand events (CDT):
{chr(10).join(event_lines)}"""


def run():
    stats = build_summary()
    print(stats)

    narration = _query_gesha(
        f"You are a friendly smart home water heater assistant. "
        f"The homeowner gets this daily digest on Telegram. "
        f"Using the pre-computed stats below, write a brief 3-4 sentence summary. "
        f"Do NOT do any math or change any numbers — just narrate the pre-computed stats naturally. "
        f"Plain text only, no markdown.\n\n{stats}"
    )

    if narration:
        msg = f"☀️ Yesterday's Hot Water Digest\n\n{narration}"
    else:
        # Fallback: send raw stats if Gesha is down
        msg = f"☀️ Yesterday's Hot Water Digest\n\n{stats}"

    _send_telegram(msg)
    print(f"\nSent to Telegram:\n{msg}")


if __name__ == "__main__":
    run()
