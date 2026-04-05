"""Telegram bot for smart-recirc: notifications and commands."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DB_PATH

GESHA_URL = "http://192.168.1.149:9090/v1/chat/completions"
GESHA_MODEL = "qwen35-35b-uncensored"

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send_message(text: str, chat_id: int = TELEGRAM_CHAT_ID):
    """Send a message to Telegram."""
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"{_API_BASE}/sendMessage", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"[TELEGRAM] Send failed: {e}")


class TelegramPoller:
    """Polls for incoming Telegram commands in a background thread."""

    def __init__(self, controller):
        self.controller = controller
        self._offset = 0
        self._running = False
        self._chat_history: list[dict] = []  # Last N exchanges for Gesha context
        self._MAX_HISTORY = 10  # 5 user + 5 assistant messages

    def start(self):
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        print("[TELEGRAM] Bot polling started")

    def _poll_loop(self):
        while self._running:
            try:
                self._check_updates()
            except Exception as e:
                print(f"[TELEGRAM] Poll error: {e}")
            time.sleep(2)

    def _check_updates(self):
        url = f"{_API_BASE}/getUpdates?offset={self._offset}&timeout=1"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception:
            return

        if not data.get("ok"):
            return

        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = msg.get("chat", {}).get("id")

            if chat_id != TELEGRAM_CHAT_ID:
                continue

            if text.startswith("/"):
                self._handle_command(text, chat_id)
            elif text:
                self._chat_gesha(text, chat_id)

    def _handle_command(self, text: str, chat_id: int):
        cmd = text.split()[0].lower()

        if cmd == "/status":
            self._cmd_status(chat_id)
        elif cmd == "/events":
            self._cmd_events(chat_id)
        elif cmd == "/recirc":
            self._cmd_recirc(text, chat_id)
        elif cmd in ("/start", "/help"):
            self._cmd_help(chat_id)
        else:
            send_message(f"Unknown command: {cmd}\nTry /help", chat_id)

    def _cmd_help(self, chat_id: int):
        send_message(
            "<b>Lindell HW Bot</b>\n\n"
            "/status — current sensor readings\n"
            "/events — recent demand/recirc events\n"
            "/recirc [min] — start recirc pump (default 5 min)\n"
            "/help — this message",
            chat_id,
        )

    def _cmd_status(self, chat_id: int):
        sensors = self.controller.rinnai.fetch_sensors()
        if sensors is None:
            send_message("Failed to fetch sensors", chat_id)
            return

        flow = sensors["flow_rate"] or 0
        inlet = sensors["inlet_temp"] or 0
        outlet = sensors["outlet_temp"] or 0
        heating = "ON" if sensors["heating"] else "OFF"

        if sensors["recirc_on"]:
            reason = self.controller.recirc_reason or "schedule"
            recirc = f"ON ({reason})"
        else:
            recirc = "OFF"

        send_message(
            f"<b>Current Status</b>\n"
            f"Flow: {flow:.1f} GPM\n"
            f"Inlet: {inlet:.0f}°F\n"
            f"Outlet: {outlet:.0f}°F\n"
            f"Heating: {heating}\n"
            f"Recirc: {recirc}",
            chat_id,
        )

    def _cmd_events(self, chat_id: int):
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT start_time, duration_sec, avg_flow_rate, event_type, fixture_type
            FROM usage_events ORDER BY id DESC LIMIT 5
        """).fetchall()
        conn.close()

        if not rows:
            send_message("No events recorded yet", chat_id)
            return

        lines = ["<b>Recent Events</b>"]
        for start, dur, flow, etype, fixture in rows:
            # Convert UTC to CDT for display
            dt = datetime.fromisoformat(start)
            local = dt.astimezone(timezone(timedelta(hours=-5)))
            tag = "🔥" if etype == "demand" else "🔄"
            fixture_str = f" ({fixture})" if fixture else ""
            lines.append(
                f"{tag} {local.strftime('%I:%M %p')} | {dur:.0f}s | "
                f"{flow:.1f} GPM | {etype}{fixture_str}"
            )

        send_message("\n".join(lines), chat_id)

    def _cmd_recirc(self, text: str, chat_id: int):
        parts = text.split()
        duration = 5
        if len(parts) > 1:
            try:
                duration = int(parts[1])
            except ValueError:
                send_message("Usage: /recirc [minutes]", chat_id)
                return

        if duration < 1 or duration > 30:
            send_message("Duration must be 1-30 minutes", chat_id)
            return

        success = self.controller.rinnai.start_recirculation(duration)
        if success:
            self.controller.recirc_reason = "manual"
            send_message(f"✅ Recirc started for {duration} min", chat_id)
        else:
            send_message("❌ Failed to start recirc", chat_id)

    def _get_context(self) -> str:
        """Build current system context for Gesha conversations."""
        conn = sqlite3.connect(DB_PATH)
        cdt = timezone(timedelta(hours=-5))
        now_cdt = datetime.now(cdt)
        start_utc = now_cdt.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

        rows = conn.execute("""
            SELECT start_time, duration_sec, avg_flow_rate, event_type, fixture_type
            FROM usage_events WHERE start_time >= ?
            ORDER BY start_time
        """, (start_utc.isoformat(),)).fetchall()

        # Current sensors
        sensors = self.controller.rinnai.fetch_sensors()
        conn.close()

        demand = [r for r in rows if r[3] == "demand"]
        recirc = [r for r in rows if r[3] == "recirc"]

        events_str = ""
        for r in demand:
            dt = datetime.fromisoformat(r[0]).astimezone(cdt)
            dur_m = int(r[1] // 60)
            events_str += f"  {dt.strftime('%H:%M')} CDT - {r[4]}, {dur_m}m, {r[2]:.1f} GPM\n"

        sensor_str = ""
        if sensors:
            sensor_str = (f"Current: flow={sensors['flow_rate'] or 0:.1f} GPM, "
                         f"inlet={sensors['inlet_temp'] or 0:.0f}F, "
                         f"outlet={sensors['outlet_temp'] or 0:.0f}F, "
                         f"heating={'ON' if sensors['heating'] else 'OFF'}, "
                         f"recirc={'ON' if sensors['recirc_on'] else 'OFF'}")

        return f"""System: Rinnai tankless water heater with smart recirc controller.
Time: {now_cdt.strftime('%H:%M %Z, %A %B %d')}
{sensor_str}
Today: {len(demand)} demand events, {len(recirc)} recirc cycles
{events_str}"""

    def _send_typing(self, chat_id: int):
        """Send typing indicator to Telegram."""
        payload = json.dumps({"chat_id": chat_id, "action": "typing"}).encode()
        req = urllib.request.Request(
            f"{_API_BASE}/sendChatAction", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception:
            pass

    def _chat_gesha(self, text: str, chat_id: int):
        """Forward message to Gesha with smart-recirc context and conversation history."""
        self._send_typing(chat_id)
        context = self._get_context()

        self._chat_history.append({"role": "user", "content": text})
        if len(self._chat_history) > self._MAX_HISTORY:
            self._chat_history = self._chat_history[-self._MAX_HISTORY:]

        messages = [
            {"role": "system", "content": (
                "You are the Lindell HW Bot — Brad's smart water heater assistant "
                "in Nashville. Talk like a knowledgeable friend texting back, not a "
                "manual or a robot.\n\n"
                "Style:\n"
                "- Match the user's energy. Short message = short reply.\n"
                "- 1-3 sentences for most replies. Only go longer if they asked something detailed.\n"
                "- Be warm and casual. Use contractions. Skip formalities.\n"
                "- Don't end every message asking if they need more help.\n\n"
                "Knowledge:\n"
                "- The recirc pump keeps pipes warm so hot water arrives faster. "
                "Without it, you wait for cold water to flush out.\n"
                "- More pump runtime = more energy used but faster hot water. "
                "The '3x' stat means the schedule is inefficient — pump runs during "
                "times nobody uses water. Fix = smarter schedule, not less pumping.\n"
                "- A cold shower means the pump wasn't running before that fixture "
                "was used, OR pipes cooled between cycles.\n"
                "- Typical pump warmup is 3-5 minutes.\n"
                "- You CANNOT control the pump from chat. Direct users to /recirc.\n"
                "- Nashville inlet water is typically 55-75F depending on season.\n"
                "- For off-topic questions, just be natural — no forced water heater tie-ins.\n\n"
                f"Current state:\n{context}"
            )},
        ] + self._chat_history

        payload = json.dumps({
            "model": GESHA_MODEL,
            "messages": messages,
            "temperature": 0.4,
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
                msg = data["choices"][0]["message"]
                reply = msg.get("content") or ""
                # Strip leaked think tags from model reasoning
                reply = re.sub(r'<think>.*?</think>\s*', '', reply, flags=re.DOTALL).strip()
                if not reply:
                    send_message("Sorry, I couldn't generate a response. Try again.", chat_id)
                    return
                self._chat_history.append({"role": "assistant", "content": reply})
                if len(self._chat_history) > self._MAX_HISTORY:
                    self._chat_history = self._chat_history[-self._MAX_HISTORY:]
                send_message(reply, chat_id)
        except Exception as e:
            send_message(f"Gesha offline: {e}", chat_id)
