import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import httpx

# এনভায়রনমেন্ট ভ্যারিয়েবল
GH_TOKEN = os.getenv("GH_TOKEN")
GH_OWNER = os.getenv("GH_OWNER", "shiftcoin0")
GH_REPO = os.getenv("GH_REPO", "dlive-sports")
PORT = int(os.getenv("PORT", 10000))

SCHEDULE_URL = "https://dlive.sx/schedule/schedule-generated.json"
STATE_FILE = "last_state.txt"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("Watcher")


# Render হেলথ চেক সার্ভার
class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        response = {
            "status": "healthy",
            "service": "DLHD Watcher",
            "interval": "5 minutes",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, format, *args):
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthServer)
    server.serve_forever()


# ১৫ মিনিট আগে লাইভ স্ট্যাটাস ক্যালকুলেশন
def get_event_status(start_dt: datetime) -> str:
    now_utc = datetime.now(timezone.utc)
    start_utc = start_dt.astimezone(timezone.utc)
    live_start = start_utc - timedelta(minutes=15)
    end_time = start_utc + timedelta(hours=3)

    if live_start <= now_utc <= end_time:
        return "LIVE"
    elif now_utc < live_start:
        return "UPCOMING"
    return "ENDED"


# GitHub Trigger কল করা
async def trigger_github(client: httpx.AsyncClient):
    if not GH_TOKEN:
        log.error("GH_TOKEN is missing!")
        return False

    url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/dispatches"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Render-Watcher-Bot",
        "Content-Type": "application/json"
    }
    payload = {
        "event_type": "source_updated",
        "client_payload": {
            "triggered_by": "Render Watcher",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    }

    try:
        res = await client.post(url, headers=headers, json=payload, timeout=10.0)
        if res.status_code in [200, 204]:
            log.info(">>> GitHub Action successfully triggered! <<<")
            return True
        else:
            log.error(f"GitHub dispatch failed: {res.status_code} - {res.text}")
            return False
    except Exception as e:
        log.error(f"Error calling GitHub API: {e}")
        return False


# মূল ওয়াচার লুপ (প্রতি ৫ মিনিট পর পর চেক করবে)
async def watch_schedule():
    last_state = ""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                last_state = f.read().strip()
            log.info("Loaded previous state from file.")
        except Exception:
            last_state = ""

    log.info("Schedule Watcher started. Polling every 5 minutes...")

    # follow_redirects=True যুক্ত করা হয়েছে রিডাইরেক্ট হ্যান্ডেল করতে
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Referer": "https://dlive.sx/"},
        follow_redirects=True,
        timeout=15.0
    ) as client:
        while True:
            try:
                resp = await client.get(SCHEDULE_URL)
                if resp.status_code != 200:
                    log.warning(f"Failed to fetch schedule, HTTP: {resp.status_code}")
                    await asyncio.sleep(60)
                    continue

                schedule_data = resp.json()
                now_utc = datetime.now(timezone.utc)
                current_events = []

                for date_key, category_obj in schedule_data.items():
                    items = category_obj if isinstance(category_obj, dict) else {date_key: category_obj}

                    for sport_cat, games in items.items():
                        if not isinstance(games, list):
                            continue

                        for game in games:
                            if not isinstance(game, dict):
                                continue

                            title = game.get("event") or game.get("name")
                            time_str = game.get("time", "").strip()
                            channels = game.get("channels", [])

                            if not title or not channels:
                                continue

                            event_dt = now_utc
                            m = re.search(r"(\d{1,2}):(\d{2})", time_str)
                            if m:
                                h, minute = int(m.group(1)), int(m.group(2))
                                event_dt = now_utc.replace(hour=h, minute=minute, second=0, microsecond=0)

                            if event_dt < now_utc - timedelta(hours=3):
                                continue

                            status = get_event_status(event_dt)
                            current_events.append(f"{title}|{status}|{len(channels)}")

                new_state = "#".join(sorted(current_events))

                if last_state == "":
                    log.info(f"Initialized with {len(current_events)} active events.")
                    last_state = new_state
                    with open(STATE_FILE, "w", encoding="utf-8") as f:
                        f.write(new_state)

                elif new_state != last_state:
                    log.info("Real change detected! Triggering GitHub Action...")
                    success = await trigger_github(client)
                    if success:
                        last_state = new_state
                        with open(STATE_FILE, "w", encoding="utf-8") as f:
                            f.write(new_state)
                else:
                    log.info("No change detected. Everything up-to-date.")

            except Exception as e:
                log.error(f"Watcher loop exception: {e}")

            # ঠিক ৫ মিনিট অপেক্ষা
            await asyncio.sleep(300)


def main():
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()
    asyncio.run(watch_schedule())


if __name__ == "__main__":
    main()
