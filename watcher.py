import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import httpx

# GitHub ক্রেডেনশিয়াল
GH_TOKEN = os.getenv("GH_TOKEN")
GH_OWNER = os.getenv("GH_OWNER", "shiftcoin0")
GH_REPO = os.getenv("GH_REPO", "dlive-sports")
PORT = int(os.getenv("PORT", 10000))

# -----------------------------------------------------------------------------
# Render Environment Variables থেকে রিড করা হচ্ছে (কোডে সোর্স লুকানো থাকবে)
# -----------------------------------------------------------------------------
BASE_URL = os.getenv("SOURCE_BASE_URL", "")
API_URL = os.getenv("SOURCE_API_URL", "")
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


class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        res = {
            "status": "running",
            "service": "Dynamic Event Watcher",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.wfile.write(json.dumps(res).encode("utf-8"))

    def log_message(self, format, *args):
        return


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthServer)
    log.info(f"Health server listening on port {PORT}")
    server.serve_forever()


def get_event_status(m: dict, start_dt: datetime) -> str:
    raw_status = str(m.get("status", "")).lower()
    if raw_status in ["ended", "finished", "ft", "final", "completed", "closed"]:
        return "ENDED"
    if m.get("ended") is True or m.get("finished") is True:
        return "ENDED"

    now_utc = datetime.now(timezone.utc)
    start_utc = start_dt.astimezone(timezone.utc)
    live_start = start_utc - timedelta(minutes=15)

    if now_utc >= live_start:
        return "LIVE"
    return "UPCOMING"


async def trigger_github(client: httpx.AsyncClient, reason: str = ""):
    if not GH_TOKEN:
        log.error("GH_TOKEN is missing! Please configure it in Environment Variables.")
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


async def watch_topsurge():
    if not API_URL:
        log.error("CRITICAL: 'SOURCE_API_URL' environment variable is missing on Render!")
        return

    last_state = ""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                last_state = f.read().strip()
            log.info(f"Loaded existing state from {STATE_FILE}")
        except Exception:
            last_state = ""

    log.info("Watcher started. Polling every 5 minutes...")

    headers = {
        "User-Agent": UA,
        "Referer": f"{BASE_URL}/" if BASE_URL else "",
        "Accept": "application/json, text/plain, */*",
    }

    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        while True:
            try:
                resp = await client.get(API_URL)
                if resp.status_code != 200:
                    log.warning(f"Failed to fetch API, Status: {resp.status_code}")
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

                    if match_dt >= now_utc - timedelta(hours=12):
                        st = get_event_status(m, match_dt)
                        total_links = sum(len(c.get("links") or c.get("oldLinks") or []) for c in channels)
                        current_events.append(f"{title}|{st}|{total_links}")

                new_state = "#".join(sorted(current_events))

                if last_state == "":
                    log.info(f"Initial run: Tracking {len(current_events)} active matches. Syncing with GitHub...")
                    if await trigger_github(client, "Initial Sync"):
                        last_state = new_state
                        with open(STATE_FILE, "w", encoding="utf-8") as f:
                            f.write(new_state)

                elif new_state != last_state:
                    log.info("Change detected! (Event added/removed or match reached 15-minute LIVE window).")
                    if await trigger_github(client, "15-Min LIVE Window / Event Update"):
                        last_state = new_state
                        with open(STATE_FILE, "w", encoding="utf-8") as f:
                            f.write(new_state)
                else:
                    log.info("No status change or event update. Skipping trigger. Sleeping 5 minutes...")

            except Exception as e:
                log.error(f"Watcher loop exception: {e}")

            # ঠিক ৫ মিনিট পর পর চেক করবে
            await asyncio.sleep(300)


def main():
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()
    asyncio.run(watch_topsurge())


if __name__ == "__main__":
    main()
