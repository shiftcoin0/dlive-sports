import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

# রিপোজিটরি রুট ডিরেক্টরি
ROOT_DIR = Path(__file__).resolve().parent.parent

BASE_URL = "https://dlive.sx"
SCHEDULE_URL = f"{BASE_URL}/schedule/schedule-generated.json"

TAG = "DLIVE"

# প্লেলিস্ট এবং ক্যাশে স্টেট পাথ
JSON_OUTPUT = ROOT_DIR / "strmcntr.json"
M3U_OUTPUT = ROOT_DIR / "strmcntr.m3u"
CACHE_DIR = ROOT_DIR / "caches"
STATE_FILE = CACHE_DIR / "dlive_state.txt"

LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
)

HTTP_SEMAPHORE = asyncio.Semaphore(10)
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
# স্মার্ট লোগো লাইব্রেরি (হাই-কোয়ালিটি স্পোর্টস ও টুর্নামেন্ট লোগো)
# -----------------------------------------------------------------------------
DEFAULT_LIVE_LOGO = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/united-states/sports-live-us.png"

SPORT_LOGOS = {
    "soccer": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d3/Soccerball.svg/500px-Soccerball.svg.png",
    "football": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d3/Soccerball.svg/500px-Soccerball.svg.png",
    "cricket": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7a/Cricketball.svg/500px-Cricketball.svg.png",
    "basketball": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7a/Basketball.png/500px-Basketball.png",
    "tennis": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/41/Tennis_ball.svg/500px-Tennis_ball.svg.png",
    "motorsport": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/33/F1.svg/500px-F1.svg.png",
    "racing": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/33/F1.svg/500px-F1.svg.png",
    "baseball": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1e/Baseball_%28crop%29.jpg/500px-Baseball_%28crop%29.jpg",
    "hockey": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/82/Ice_Hockey_Puck.jpg/500px-Ice_Hockey_Puck.jpg",
    "rugby": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e5/Rugby_ball.svg/500px-Rugby_ball.svg.png",
    "wrestling": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/03/WWE_Logo.svg/500px-WWE_Logo.svg.png",
    "fighting": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0d/UFC_logo.svg/500px-UFC_logo.svg.png",
    "mma": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0d/UFC_logo.svg/500px-UFC_logo.svg.png",
    "boxing": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0d/UFC_logo.svg/500px-UFC_logo.svg.png",
    "american football": "https://upload.wikimedia.org/wikipedia/en/thumb/a/a2/National_Football_League_logo.svg/500px-National_Football_League_logo.svg.png",
}

# বড় বড় টুর্নামেন্টের জন্য কাস্টম লোগো
TOURNAMENT_KEYWORDS = [
    (r"premier league|epl", "https://upload.wikimedia.org/wikipedia/en/thumb/f/f2/Premier_League_Logo.svg/500px-Premier_League_Logo.svg.png"),
    (r"champions league|ucl", "https://upload.wikimedia.org/wikipedia/en/thumb/b/bf/UEFA_Champions_League_logo_2.svg/500px-UEFA_Champions_League_logo_2.svg.png"),
    (r"la liga", "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0f/LaLiga_logo_2023.svg/500px-LaLiga_logo_2023.svg.png"),
    (r"serie a", "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e9/Serie_A_logo_2019.svg/500px-Serie_A_logo_2019.svg.png"),
    (r"bundesliga", "https://upload.wikimedia.org/wikipedia/en/thumb/d/df/Bundesliga_logo_%282017%29.svg/500px-Bundesliga_logo_%282017%29.svg.png"),
    (r"ipl|indian premier league", "https://upload.wikimedia.org/wikipedia/en/thumb/8/84/Indian_Premier_League_Official_Logo.svg/500px-Indian_Premier_League_Official_Logo.svg.png"),
    (r"nba", "https://upload.wikimedia.org/wikipedia/en/thumb/0/03/National_Basketball_Association_logo.svg/500px-National_Basketball_Association_logo.svg.png"),
    (r"ufc", "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0d/UFC_logo.svg/500px-UFC_logo.svg.png"),
    (r"wwe|smackdown|raw|nxt", "https://upload.wikimedia.org/wikipedia/commons/thumb/0/03/WWE_Logo.svg/500px-WWE_Logo.svg.png"),
    (r"f1|formula 1", "https://upload.wikimedia.org/wikipedia/commons/thumb/3/33/F1.svg/500px-F1.svg.png"),
    (r"nfl", "https://upload.wikimedia.org/wikipedia/en/thumb/a/a2/National_Football_League_logo.svg/500px-National_Football_League_logo.svg.png"),
]


def get_smart_logo(sport: str, title: str) -> str:
    title_lower = title.lower()

    # ১. প্রথমে টাইটেলের ভেতরের টুর্নামেন্ট নাম চেক করা
    for pattern, logo_url in TOURNAMENT_KEYWORDS:
        if re.search(pattern, title_lower):
            return logo_url

    # ২. টুর্নামেন্ট না মিললে খেলার ক্যাটাগরি ভিত্তিক লোগো দেওয়া
    sport_lower = sport.lower()
    for sp_key, logo_url in SPORT_LOGOS.items():
        if sp_key in sport_lower:
            return logo_url

    # ৩. কোনোটিই না মিললে ডিফল্ট লাইভ স্পোর্টস লোগো
    return DEFAULT_LIVE_LOGO


@dataclass(slots=True)
class DLHDChannel:
    channel_name: str
    channel_id: str


@dataclass(slots=True)
class DLHDEvent:
    sport: str
    title: str
    start_dt: datetime
    channels: list[DLHDChannel]


def create_slug(text: str) -> str:
    text = text.lower().strip()
    return re.sub(r"[\W_]+", "-", text).strip("-")


def parse_teams(title: str) -> dict:
    delimiters = [" vs. ", " vs ", " - ", " @ "]
    for d in delimiters:
        if d in title.lower():
            parts = re.split(re.escape(d), title, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                return {
                    "team_a": {"name": parts[0].strip()},
                    "team_b": {"name": parts[1].strip()},
                }
    return {
        "team_a": {"name": title.strip()},
        "team_b": {"name": title.strip()},
    }


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
        title = ev.get("title", "Event")
        sport = ev.get("sport", "Live")
        logo = ev.get("logo", "")
        status = ev.get("status", "")

        for ch in ev.get("channels", []):
            url = ch.get("url")
            if not url:
                continue

            ch_name = ch.get("channel_name", "Stream")
            headers = ch.get("headers", {})
            referer = headers.get("Referer", "")

            status_lbl = f" [{status}]" if status else ""
            display_name = f"[{sport}] {title} | {ch_name}{status_lbl}"

            m3u_lines.append(
                f'#EXTINF:-1 '
                f'tvg-id="Live.Event.us" '
                f'tvg-logo="{logo}" '
                f'group-title="{sport}",'
                f'{display_name}'
            )

            if referer:
                m3u_lines.append(f'#EXTVLCOPT:http-referrer={referer}')

            m3u_lines.append(url)
            m3u_count += 1

    M3U_OUTPUT.write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")
    log.info(f"Saved M3U Playlist: {M3U_OUTPUT} ({m3u_count} streams)")


async def request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict | None = None,
) -> httpx.Response | None:
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response
    except Exception as e:
        log.debug(f'Failed to fetch "{url}": {e}')
        return None


async def safe_process(fn, timeout: int | float = 25):
    async with HTTP_SEMAPHORE:
        task = asyncio.create_task(fn())
        try:
            return await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return None
        except Exception:
            return None


async def decode_dlhd_m3u8(
    client: httpx.AsyncClient,
    channel_id: str,
) -> tuple[str, str] | None:
    clean_id = re.sub(r"[^\d]", "", str(channel_id))
    if not clean_id:
        return None

    channel_page_url = f"{BASE_URL}/stream/stream-{clean_id}.php"
    headers = {
        "User-Agent": UA,
        "Referer": f"{BASE_URL}/",
    }

    resp = await request(client, channel_page_url, headers=headers)
    if not resp:
        return None

    direct_match = re.search(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', resp.text)
    if direct_match:
        return direct_match.group(1), f"{BASE_URL}/"

    soup = HTMLParser(resp.content)
    iframe = soup.css_first("iframe")
    if not iframe:
        return None

    iframe_src = iframe.attributes.get("src")
    if not iframe_src:
        return None

    if iframe_src.startswith("//"):
        iframe_src = "https:" + iframe_src
    elif iframe_src.startswith("/"):
        iframe_src = urljoin(BASE_URL, iframe_src)

    parsed_iframe = urlsplit(iframe_src)
    player_origin = f"{parsed_iframe.scheme}://{parsed_iframe.netloc}"

    player_headers = {
        "User-Agent": UA,
        "Referer": f"{BASE_URL}/",
    }
    player_resp = await request(client, iframe_src, headers=player_headers)
    if not player_resp:
        return None

    player_match = re.search(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', player_resp.text)
    if player_match:
        return player_match.group(1), f"{player_origin}/"

    key_match = re.search(r'channel_id=([^"\'&\s]+)', player_resp.text)
    channel_key = key_match.group(1) if key_match else f"premium{clean_id}"

    lookup_urls = [
        f"{player_origin}/server_lookup.php?channel_id={channel_key}",
        f"{player_origin}/server_lookup.js?channel_id={channel_key}",
    ]

    server_key = None
    lookup_headers = {
        "User-Agent": UA,
        "Referer": f"{player_origin}/",
    }

    for l_url in lookup_urls:
        l_resp = await request(client, l_url, headers=lookup_headers)
        if l_resp and l_resp.status_code == 200:
            try:
                data = l_resp.json()
                if "server_key" in data and data["server_key"]:
                    server_key = data["server_key"]
                    break
            except Exception:
                pass

    if not server_key:
        server_key = "topembed"

    m3u8_url = f"https://{server_key}new.newkso.ru/{server_key}/{channel_key}/mono.m3u8"
    return m3u8_url, f"{player_origin}/"


async def get_dlhd_events(client: httpx.AsyncClient) -> list[DLHDEvent]:
    events: list[DLHDEvent] = []
    headers = {
        "User-Agent": UA,
        "Referer": f"{BASE_URL}/",
        "Accept": "application/json, text/plain, */*",
    }

    resp = await request(client, SCHEDULE_URL, headers=headers)
    if not resp:
        log.error("Failed to fetch DaddyLive schedule JSON")
        return events

    try:
        schedule_data = resp.json()
    except Exception as e:
        log.error(f"Failed to parse schedule JSON: {e}")
        return events

    now_utc = datetime.now(timezone.utc)

    for date_key, category_obj in schedule_data.items():
        if isinstance(category_obj, list):
            items_by_cat = {date_key: category_obj}
        elif isinstance(category_obj, dict):
            items_by_cat = category_obj
        else:
            continue

        for sport_cat, games in items_by_cat.items():
            if not isinstance(games, list):
                continue

            for game in games:
                if not isinstance(game, dict):
                    continue

                event_title = game.get("event") or game.get("name")
                time_str = game.get("time", "").strip()
                raw_channels = game.get("channels", [])

                if not event_title or not raw_channels:
                    continue

                event_dt = now_utc
                time_match = re.search(r"(\d{1,2}):(\d{2})", time_str)
                if time_match:
                    h, m = int(time_match.group(1)), int(time_match.group(2))
                    event_dt = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)

                if event_dt < now_utc - timedelta(hours=3):
                    continue

                if event_dt > now_utc + timedelta(hours=24):
                    continue

                channels: list[DLHDChannel] = []
                for ch in raw_channels:
                    if isinstance(ch, dict):
                        c_name = ch.get("channel_name") or ch.get("name") or "Stream"
                        c_id = str(ch.get("channel_id") or ch.get("id") or "")
                    else:
                        c_name = f"Stream {ch}"
                        c_id = str(ch)

                    if c_id:
                        channels.append(DLHDChannel(channel_name=c_name, channel_id=c_id))

                if channels:
                    events.append(
                        DLHDEvent(
                            sport=sport_cat.capitalize(),
                            title=event_title.strip(),
                            start_dt=event_dt,
                            channels=channels,
                        )
                    )

    return events


async def scrape() -> None:
    log.info(f'Scraping from "{BASE_URL}"')

    async with httpx.AsyncClient(
        headers={"User-Agent": UA},
        follow_redirects=True,
        timeout=15.0,
        http2=True,
    ) as client:

        raw_events = await get_dlhd_events(client)
        if not raw_events:
            log.info("No events found in DaddyLive schedule")
            return

        current_state = []
        for ev in raw_events:
            st = determine_status(ev.start_dt, early_minutes=15)
            current_state.append(f"{ev.title}_{st}_{len(ev.channels)}")

        new_state = "|".join(current_state)
        last_state = STATE_FILE.read_text(encoding="utf-8").strip() if STATE_FILE.exists() else ""

        if last_state == new_state and JSON_OUTPUT.exists() and M3U_OUTPUT.exists():
            log.info("No status change or new events. Exiting in 2 seconds.")
            return

        log.info(f"Processing {len(raw_events)} DaddyLive event(s)...")

        final_playlist: list[dict] = []

        for ev in raw_events:
            event_channels = []

            for ch in ev.channels:
                async def handler(c_id=ch.channel_id):
                    return await decode_dlhd_m3u8(client, c_id)

                result = await safe_process(handler, timeout=20)

                if result:
                    m3u8_url, referer = result
                    event_channels.append(
                        {
                            "channel_name": ch.channel_name,
                            "stream_type": "HLS",
                            "url": m3u8_url,
                            "raw_link": f"{m3u8_url}|Referer={referer}",
                            "headers": {"Referer": referer},
                            "drm_key": None,
                        }
                    )

            if event_channels:
                start_bst = ev.start_dt.astimezone(BST_TZ)
                end_bst = start_bst + timedelta(hours=3)

                start_str = start_bst.strftime("%b %d, %Y, %I:%M:%S %p")
                end_str = end_bst.strftime("%b %d, %Y, %I:%M:%S %p")

                # স্মার্ট লোগো সেট করা হচ্ছে
                event_logo = get_smart_logo(ev.sport, ev.title)

                formatted_event = {
                    "title": ev.title,
                    "sport": ev.sport,
                    "sub_category": ev.sport,
                    "status": determine_status(ev.start_dt, early_minutes=15),
                    "is_hot": False,
                    "slug": create_slug(ev.title),
                    "schedule": {
                        "timezone": "BST (UTC+6)",
                        "start_time_bst": start_str,
                        "end_time_bst": end_str,
                    },
                    "teams": parse_teams(ev.title),
                    "logo": event_logo,
                    "channels": event_channels,
                    "_timestamp": ev.start_dt.timestamp(),
                }

                final_playlist.append(formatted_event)

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
        STATE_FILE.write_text(new_state, encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(scrape())
