import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import httpx

# GitHub ক্রেডেনশিয়াল
GH_TOKEN = os.getenv("GH_TOKEN")
GH_OWNER = os.getenv("GH_OWNER", "shiftcoin0")
GH_REPO = os.getenv("GH_REPO", "dlive-sports")
PORT = int(os.getenv("PORT", 10000))

BASE_URL = "https://topsurge.st"
API_URL = f"{BASE_URL}/api_proxy.php"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/111.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("Watcher")

# Render Web Service সচল রাখতে হেলথ সার্ভার
class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"running","service":"Topsurge Watcher"}')

def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthServer)
    server.serve_forever()

# ১৫ মিনিট আগে লাইভ স্ট্যাটাস চেক
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

# গিটহাবে সিগন্যাল পাঠানো
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
            "triggered_by": "Topsurge Watcher",
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

# মূল ওয়াচার লুপ (প্রতি ৫ মিনিটে টপসার্জ এপিআই চেক করবে)
async def watch_topsurge():
    last_state = ""
    log.info("Topsurge Watcher started. Polling every 5 minutes...")

    headers = {
        "User-Agent": UA,
        "Referer": f"{BASE_URL}/",
        "Accept": "application/json, text/plain, */*",
    }

    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        while True:
            try:
                resp = await client.get(API_URL)
                if resp.status_code != 200:
                    log.warning(f"Failed to fetch Topsurge API: {resp.status_code}")
                    await asyncio.sleep(60)
                    continue

                data = resp.json()
                matches = data.get("matches", [])
                now_utc = datetime.now(timezone.utc)
                current_events = []

                for m in matches:
                    title = m.get("matchstr") or f"{m.get('team1')} vs {m.get('team2')}"
                    start_ts = m.get("startTimestamp")
                    channels = m.get("channels", [])

                    if not title or not start_ts or not channels:
                        continue

                    try:
                        ts = float(start_ts)
                        if ts > 1e11:
                            ts = ts / 1000.0
                        match_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    except Exception:
                        continue

                    # শুধুমাত্র ৩ ঘণ্টা আগের এবং পরবর্তী ১৮ ঘণ্টার ম্যাচগুলো ট্র্যাক করবে
                    if match_dt >= now_utc - timedelta(hours=3) and match_dt <= now_utc + timedelta(hours=18):
                        st = get_event_status(match_dt)
                        current_events.append(f"{title}|{st}|{len(channels)}")

                new_state = "#".join(sorted(current_events))

                # প্রথমবার চললে
                if last_state == "":
                    log.info(f"Initial run: Tracking {len(current_events)} active events. Syncing GitHub...")
                    if await trigger_github(client):
                        last_state = new_state

                # ম্যাচ শুরু হওয়ার ১৫ মিনিট বাকি হলে st স্বয়ংক্রিয়ভাবে LIVE হবে এবং new_state বদলে যাবে!
                elif new_state != last_state:
                    log.info("Status change detected! (Match reached 15-min LIVE window). Triggering GitHub...")
                    if await trigger_github(client):
                        last_state = new_state
                else:
                    log.info("No status change. Waiting 5 minutes...")

            except Exception as e:
                log.error(f"Watcher loop exception: {e}")

            # ৫ মিনিট ঘুমাবে
            await asyncio.sleep(300)

def main():
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()
    asyncio.run(watch_topsurge())

if __name__ == "__main__":
    main()
