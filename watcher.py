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

# Topsurge API
BASE_URL = "https://topsurge.st"
API_URL = f"{BASE_URL}/api_proxy.php"

# স্টেট মনে রাখার জন্য ফাইল (রেন্ডার রিস্টার্ট হলেও যাতে ভুলে না যায়)
STATE_FILE = "watcher_state.txt"

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


# -----------------------------------------------------------------------------
# Render Web Service সচল রাখার জন্য ডামি হেলথ সার্ভার (24/7 চালু রাখবে)
# -----------------------------------------------------------------------------
class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        res = {
            "status": "running",
            "service": "Topsurge 5-Minute Watcher",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.wfile.write(json.dumps(res).encode("utf-8"))

    def log_message(self, format, *args):
        return  # অতিরিক্ত লগ অফ রাখা হলো


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthServer)
    log.info(f"Health server listening on port {PORT}")
    server.serve_forever()


# -----------------------------------------------------------------------------
# খেলা শুরুর ১৫ মিনিট আগে LIVE স্ট্যাটাস নির্ধারণ করার লজিক
# -----------------------------------------------------------------------------
def get_event_status(start_dt: datetime) -> str:
    now_utc = datetime.now(timezone.utc)
    start_utc = start_dt.astimezone(timezone.utc)
    
    # ম্যাচ শুরুর ১৫ মিনিট আগে থেকে LIVE হবে
    live_start = start_utc - timedelta(minutes=15)
    end_time = start_utc + timedelta(hours=3)

    if live_start <= now_utc <= end_time:
        return "LIVE"
    elif now_utc < live_start:
        return "UPCOMING"
    return "ENDED"


# -----------------------------------------------------------------------------
# GitHub repository_dispatch ট্রিগার পাঠানোর ফাংশন
# -----------------------------------------------------------------------------
async def trigger_github(client: httpx.AsyncClient, reason: str = ""):
    if not GH_TOKEN:
        log.error("GH_TOKEN is missing! Please set it in Render Environment Variables.")
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
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    }

    try:
        res = await client.post(url, headers=headers, json=payload, timeout=10.0)
        if res.status_code in [200, 204]:
            log.info(f">>> GitHub Action Triggered! Reason: {reason} <<<")
            return True
        else:
            log.error(f"GitHub dispatch failed: {res.status_code} - {res.text}")
            return False
    except Exception as e:
        log.error(f"Error calling GitHub API: {e}")
        return False


# -----------------------------------------------------------------------------
# মূল ওয়াচার লুপ (প্রতি ৫ মিনিট পরপর চেক করবে)
# -----------------------------------------------------------------------------
async def watch_topsurge():
    # ফাইল থেকে আগের সেভ করা স্টেট পড়া (যাতে রেন্ডার রিস্টার্ট হলেও অযথা ট্রিগার না পাঠায়)
    last_state = ""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                last_state = f.read().strip()
            log.info(f"Loaded existing state from {STATE_FILE}")
        except Exception:
            last_state = ""

    log.info("Topsurge Watcher started. Polling every 5 minutes (300 seconds)...")

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
                    log.warning(f"Failed to fetch Topsurge API, HTTP Status: {resp.status_code}")
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

                    # মিলিসেকেন্ড ও সেকেন্ড সেফ হ্যান্ডলিং
                    try:
                        ts = float(start_ts)
                        if ts > 1e11:
                            ts = ts / 1000.0
                        match_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    except Exception:
                        continue

                    # শুধুমাত্র চলমান (৩ ঘণ্টা আগের) এবং আগামী ১৮ ঘণ্টার ম্যাচগুলো ট্র্যাক করবে
                    if match_dt >= now_utc - timedelta(hours=3) and match_dt <= now_utc + timedelta(hours=18):
                        st = get_event_status(match_dt)
                        # চ্যানেলের মোট লিংক সংখ্যা
                        total_links = sum(len(c.get("links") or c.get("oldLinks") or []) for c in channels)
                        # টাইটেল + স্ট্যাটাস + লিংক সংখ্যা নিয়ে ইউনিক সিগনেচার তৈরি
                        current_events.append(f"{title}|{st}|{total_links}")

                # সকল ম্যাচের বর্তমান অবস্থা দিয়ে একটি সিগনেচার স্ট্রিং
                new_state = "#".join(sorted(current_events))

                # ১. একদম প্রথমবার রান হলে সিঙ্ক করা
                if last_state == "":
                    log.info(f"Initial run: Tracking {len(current_events)} active matches. Syncing with GitHub...")
                    if await trigger_github(client, "Initial Sync"):
                        last_state = new_state
                        with open(STATE_FILE, "w", encoding="utf-8") as f:
                            f.write(new_state)

                # ২. কোনো ম্যাচ যোগ/রিমুভ হলে অথবা ১৫ মিনিট বাকি থাকায় UPCOMING থেকে LIVE হলে ট্রিগার পাঠানো
                elif new_state != last_state:
                    log.info("Change detected! (Event added/removed or match reached 15-minute LIVE window).")
                    if await trigger_github(client, "15-Min LIVE Window / Event Update"):
                        last_state = new_state
                        with open(STATE_FILE, "w", encoding="utf-8") as f:
                            f.write(new_state)

                # ৩. যদি কোনো পরিবর্তন না থাকে তবে গিটহাবে কোনো কল যাবে না
                else:
                    log.info("No status change or event update. Skipping trigger. Sleeping 5 minutes...")

            except Exception as e:
                log.error(f"Watcher loop exception: {e}")

            # ঠিক ৫ মিনিট (৩০০ সেকেন্ড) পর আবার চেক করবে
            await asyncio.sleep(300)


def main():
    # ব্যাকগ্রাউন্ডে হেলথ সার্ভার চালানো (Render Free Service সচল রাখতে)
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()

    # মূল লুপ চালু করা
    asyncio.run(watch_topsurge())


if __name__ == "__main__":
    main()
