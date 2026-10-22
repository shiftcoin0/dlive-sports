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

# Render-এর হেলথ চেকের জন্য ডামি ওয়েব সার্ভার (Render Free Web Service সচল রাখতে)
class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        response = {
            "status": "running",
            "service": "DLHD Schedule Watcher",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.wfile.write(json.dumps(response).encode("utf-8"))

def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthServer)
    log.info(f"Health server running on port {PORT}")
    server.serve_forever()


# ১৫ মিনিট আগে লাইভ ক্যালকুলেশন
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


# GitHub Trigger কল করার ফাংশন
async def trigger_github(client: httpx.AsyncClient):
    if not GH_TOKEN:
        log.error("GH_TOKEN environment variable is missing!")
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


# মূল ওয়াচার লুপ (প্রতি ৫ মিনিট পর পর চলবে)
async def watch_schedule():
    last_state = ""
    log.info("Schedule Watcher started. Polling every 5 minutes...")

    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=15.0) as client:
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

                # শিডিউল বিশ্লেষণ
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

                            # ৩ ঘণ্টার বেশি পুরনো ম্যাচ বাদ
                            if event_dt < now_utc - timedelta(hours=3):
                                continue

                            # স্ট্যাটাস নির্ধারণ (১৫ মিনিট আগে LIVE হবে)
                            status = get_event_status(event_dt)
                            current_events.append(f"{title}|{status}|{len(channels)}")

                # বর্তমান অবস্থার হ্যাশ/স্টেট স্ট্রিং
                new_state = "#".join(sorted(current_events))

                # প্রথমবার রান হলে স্টেট সেভ করে ট্রিগার পাঠানো
                if last_state == "":
                    log.info(f"Initial run: Found {len(current_events)} active events. Syncing GitHub...")
                    success = await trigger_github(client)
                    if success:
                        last_state = new_state

                # যদি কোনো পরিবর্তন বা লাইভ স্ট্যাটাস আপডেট ঘটে
                elif new_state != last_state:
                    log.info("Change detected! (Event added/removed or match reached 15-min LIVE window)")
                    success = await trigger_github(client)
                    if success:
                        last_state = new_state
                else:
                    log.info("No status change or source update. Skipping GitHub trigger.")

            except Exception as e:
                log.error(f"Watcher loop exception: {e}")

            # ৫ মিনিট অপেক্ষা (৩০০ সেকেন্ড)
            await asyncio.sleep(300)


def main():
    # ব্যাকগ্রাউন্ডে হেলথ সার্ভার চালানো
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()

    # মূল লুপ রান করা
    asyncio.run(watch_schedule())

if __name__ == "__main__":
    main()
