import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# রিপোজিটরি রুট ডিরেক্টরি
ROOT_DIR = Path(__file__).resolve().parent.parent

# সম্পূর্ণ নতুন ও ভিন্ন সোর্স API
API_BASE = "https://streamed.su/api"
MATCHES_URL = f"{API_BASE}/matches/all"

TAG = "STREAMED"

# প্লেলিস্ট পাথ
JSON_OUTPUT = ROOT_DIR / "strmcntr.json"
M3U_OUTPUT = ROOT_DIR / "strmcntr.m3u"
CACHE_DIR = ROOT_DIR / "caches"

LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
)

HTTP_SEMAPHORE = asyncio.Semaphore(15)
BST_TZ = ZoneInfo("Asia/Dhaka")

LOG_FMT = (
    "[%(asctime)s] "
    "%(levelname)-8s "
    "[%(name)s] "
    "%(message)-70s "
    "(%(filename)s:%(lineno)d)"
)

log = logging.getLogger(TAG)
log.setLevel(logging.INFO)

if not log.handlers:
    formatter = logging.Formatter(LOG_FMT, datefmt="%Y-%m-%d | %H:%M:%S")
    file_handler = logging.FileHandler(LOG_DIR / "fetch.log", encoding="utf-8")
    console_handler = logging.StreamHandler()

    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    log.addHandler(file_handler)
    log.addHandler(console_handler)
    log.propagate = False


# -----------------------------------------------------------------------------
# আপনার দেওয়া কাস্টম স্পোর্টস আইকন ও ডিফল্ট লোগো
# -----------------------------------------------------------------------------
SPORT_ICONS = {
    "football": "https://i.ibb.co.com/cSMrGjnb/Football.png",
    "cricket": "https://i.ibb.co.com/yccjsRfP/cricket.webp",
    "basketball": "https://i.ibb.co.com/393JJ6zq/Basketball.jpg",
    "tennis": "https://i.ibb.co.com/fz9WSxtQ/Tennis.webp",
    "weightlifting": "https://i.ibb.co.com/wrpLVMpz/Weightlifting.jpg",
    "volleyball": "https://i.ibb.co.com/Qv41gDKh/Volleyball.jpg",
    "hockey": "https://i.ibb.co.com/4RrQy0BV/Hockey.jpg",
    "wrestling": "https://i.ibb.co.com/rGgCRXn0/Wrestling.png",
    "winter sports": "https://i.ibb.co.com/Jw9mKdTV/Winter-sports.jpg",
    "ski jumping": "https://i.ibb.co.com/8gr732c3/Ski-jumping.jpg",
    "handball": "https://i.ibb.co.com/yn02tv2n/Handball.webp",
    "snooker": "https://i.ibb.co.com/rGpnJSJj/Snooker.png",
    "golf": "https://i.ibb.co.com/kgfDcDfc/Golf.jpg",
    "motorsport": "https://i.ibb.co.com/B5jKv66s/Motorsport.png",
}

DEFAULT_LOGO = "https://i.ibb.co.com/pjRdKp8g/Live-Streaming.webp"


def get_event_logo(sport: str, title: str = "") -> str:
    comb = f"{sport} {title}".lower()
    for sp_key, icon_url in SPORT_ICONS.items():
        if sp_key in comb:
            return icon_url

    if any(k in comb for k in ["soccer", "fifa", "uefa", "la liga", "premier league", "serie a", "bundesliga"]):
        return SPORT_ICONS["football"]
    if any(k in comb for k in ["f1", "formula 1", "racing", "motogp", "nascar"]):
        return SPORT_ICONS["motorsport"]
    if any(k in comb for k in ["wwe", "smackdown", "raw", "aew", "ufc", "fighting", "mma", "boxing"]):
        return SPORT_ICONS["wrestling"]

    return DEFAULT_LOGO


def clean_and_normalize_sport(raw_sport: str) -> str:
    clean = re.sub(r"<[^>]+>", "", str(raw_sport)).strip().lower()
    if any(k in clean for k in ["soccer", "football"]):
        return "Football"
    if "cricket" in clean:
        return "Cricket"
    if "basketball" in clean:
        return "Basketball"
    if "tennis" in clean:
        return "Tennis"
    if "hockey" in clean:
        return "Hockey"
    if any(k in clean for k in ["motorsport", "motor-sports", "racing", "f1"]):
        return "Motorsport"
    if any(k in clean for k in ["wwe", "wrestling", "fight", "fighting", "ufc", "mma"]):
        return "Fighting"
    if "baseball" in clean:
        return "Baseball"
    if "american-football" in clean:
        return "American Football"
    return clean.capitalize() or "Live Sports"


LEAGUE_PATTERNS = [
    (r"premier league|epl", "Premier League"),
    (r"la liga|laliga", "La Liga"),
    (r"serie a", "Serie A"),
    (r"bundesliga", "Bundesliga"),
    (r"ligue 1", "Ligue 1"),
    (r"champions league|ucl", "UEFA Champions League"),
    (r"europa league|uel", "UEFA Europa League"),
    (r"saudi pro league|spl", "Saudi Pro League"),
    (r"ipl|indian premier league", "Indian Premier League (IPL)"),
    (r"bpl", "Bangladesh Premier League (BPL)"),
    (r"psl", "Pakistan Super League (PSL)"),
    (r"nba", "NBA"),
    (r"smackdown", "WWE SmackDown"),
    (r"raw", "WWE Raw"),
    (r"ufc", "UFC"),
    (r"formula 1|f1", "Formula 1"),
]


def get_sub_category(sport: str, title: str) -> str:
    comb = f"{sport} {title}".lower()
    for pat, league in LEAGUE_PATTERNS:
        if re.search(pat, comb):
            return league
    return sport


def create_slug(text: str) -> str:
    return re.sub(r"[\W_]+", "-", text.lower().strip()).strip("-")


def determine_status(start_dt: datetime, early_minutes: int = 15) -> str:
    now_utc = datetime.now(timezone.utc)
    start_utc = start_dt.astimezone(timezone.utc)
    live_start_utc = start_utc - timedelta(minutes=early_minutes)
    end_utc = start_utc + timedelta(hours=3)

    if live_start_utc <= now_utc <= end_utc:
        return "LIVE"
    elif now_utc < live_start_utc:
        return "UPCOMING"
    return "ENDED"


def save_playlists(events: list[dict]) -> None:
    now_bst = datetime.now(BST_TZ)
    last_updated_str = now_bst.strftime("%b %d, %Y, %I:%M:%S %p")

    json_payload = {
        "last_updated": last_updated_str,
        "event_count": len(events),
        "events": {
            "events": events
        }
    }

    JSON_OUTPUT.write_text(
        json.dumps(json_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info(f"Saved JSON Playlist: {JSON_OUTPUT} (Total: {len(events)} events)")

    m3u_lines = ["#EXTM3U"]
    m3u_count = 0

    for ev in events:
        sub_cat = ev.get("sub_category", "Live")
        logo = ev.get("logo", "")
        status = ev.get("status", "")

        for ch in ev.get("channels", []):
            url = ch.get("url")
            if not url:
                continue

            ch_name = ch.get("channel_name", "Stream")
            referer = ch.get("headers", {}).get("Referer", "")

            status_lbl = f" [{status}]" if status else ""
            display_name = f"[{sub_cat}] {ev['title']} | {ch_name}{status_lbl}"

            m3u_lines.append(
                f'#EXTINF:-1 '
                f'tvg-id="Live.Event.us" '
                f'tvg-logo="{logo}" '
                f'group-title="{sub_cat}",'
                f'{display_name}'
            )

            if referer:
                m3u_lines.append(f'#EXTVLCOPT:http-referrer={referer}')
                m3u_lines.append(f'#EXTVLCOPT:http-user-agent={UA}')

            m3u_lines.append(url)
            m3u_count += 1

    M3U_OUTPUT.write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")
    log.info(f"Saved M3U Playlist: {M3U_OUTPUT} ({m3u_count} streams)")


# -----------------------------------------------------------------------------
# Streamed.su API থেকে স্ট্রিম ডেটা সংগ্রহ
# -----------------------------------------------------------------------------
async def fetch_stream_sources(client: httpx.AsyncClient, source: str, stream_id: str) -> list[dict]:
    url = f"{API_BASE}/stream/{source}/{stream_id}"
    try:
        resp = await client.get(url, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


async def scrape() -> None:
    log.info(f'Scraping from "{MATCHES_URL}"...')

    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Referer": "https://streamed.su/"},
        follow_redirects=True,
        timeout=10.0,
        http2=True,
    ) as client:

        try:
            resp = await client.get(MATCHES_URL)
            if resp.status_code != 200:
                log.error(f"Failed to fetch matches: HTTP {resp.status_code}")
                return
            matches = resp.json()
        except Exception as e:
            log.error(f"Error fetching Streamed API: {e}")
            return

        now_utc = datetime.now(timezone.utc)
        active_matches = []

        # আগামী ১২ ঘণ্টার ম্যাচ ও চলমান ম্যাচ ফিল্টার করা
        for m in matches:
            date_val = m.get("date")
            if not date_val:
                continue

            # মিলিসেকেন্ড থেকে ডেটটাইম কনভার্সন
            start_dt = datetime.fromtimestamp(date_val / 1000.0, tz=timezone.utc)

            if start_dt < now_utc - timedelta(hours=3):
                continue
            if start_dt > now_utc + timedelta(hours=14):
                continue

            active_matches.append((m, start_dt))

        log.info(f"Found {len(active_matches)} active events. Processing streams...")

        final_playlist: list[dict] = []

        for m, start_dt in active_matches:
            title = m.get("title", "").strip()
            raw_sport = m.get("category", "football")
            sport = clean_and_normalize_sport(raw_sport)
            sub_category = get_sub_category(sport, title)
            event_logo = get_event_logo(sport, title)

            # টিম নাম ও ফ্ল্যাগ
            teams_obj = m.get("teams", {})
            team_a_name = teams_obj.get("home", {}).get("name") if isinstance(teams_obj, dict) else ""
            team_b_name = teams_obj.get("away", {}).get("name") if isinstance(teams_obj, dict) else ""

            if not team_a_name or not team_b_name:
                parts = title.split(" vs ") if " vs " in title else title.split(" - ")
                team_a_name = parts[0].strip() if len(parts) == 2 else title
                team_b_name = parts[1].strip() if len(parts) == 2 else title

            # সোর্স থেকে স্ট্রিম চ্যানেল নিয়ে আসা
            event_channels = []
            sources_list = m.get("sources", [])

            for s in sources_list:
                src_name = s.get("source")
                src_id = s.get("id")
                if not src_name or not src_id:
                    continue

                async def get_streams():
                    async with HTTP_SEMAPHORE:
                        return await fetch_stream_sources(client, src_name, src_id)

                stream_results = await get_streams()

                for st in stream_results:
                    # স্ট্রিম লিঙ্ক পাওয়া গেলে
                    stream_url = st.get("embedUrl") or st.get("streamUrl")
                    if not stream_url:
                        continue

                    channel_title = f"{src_name.upper()} - {st.get('streamNo', 1)}"
                    referer = "https://streamed.su/"

                    event_channels.append(
                        {
                            "channel_name": channel_title,
                            "stream_type": "HLS",
                            "url": stream_url,
                            "raw_link": f"{stream_url}|Referer={referer}&User-Agent={quote_plus(UA)}",
                            "headers": {
                                "Referer": referer,
                                "User-Agent": UA,
                            },
                            "drm_key": None,
                        }
                    )

            if event_channels:
                start_bst = start_dt.astimezone(BST_TZ)
                end_bst = start_bst + timedelta(hours=3)

                formatted_event = {
                    "title": title,
                    "sport": sport,
                    "sub_category": sub_category,
                    "status": determine_status(start_dt, early_minutes=15),
                    "is_hot": False,
                    "slug": create_slug(title),
                    "schedule": {
                        "timezone": "BST (UTC+6)",
                        "start_time_bst": start_bst.strftime("%b %d, %Y, %I:%M:%S %p"),
                        "end_time_bst": end_bst.strftime("%b %d, %Y, %I:%M:%S %p"),
                    },
                    "teams": {
                        "team_a": {
                            "name": team_a_name,
                            "flag": event_logo,
                        },
                        "team_b": {
                            "name": team_b_name,
                            "flag": event_logo,
                        },
                    },
                    "logo": event_logo,
                    "channels": event_channels,
                    "_timestamp": start_dt.timestamp(),
                }

                final_playlist.append(formatted_event)

        # LIVE ইভেন্ট সবসময় উপরে রাখার সর্টিং
        status_priority = {
            "LIVE": 0,
            "UPCOMING": 1,
            "ENDED": 2,
        }

        final_playlist.sort(
            key=lambda item: (
                status_priority.get(item["status"], 99),
                item["_timestamp"]
            )
        )

        for item in final_playlist:
            item.pop("_timestamp", None)

        save_playlists(final_playlist)
        log.info("Playlist generation completed from Streamed API!")


if __name__ == "__main__":
    asyncio.run(scrape())
