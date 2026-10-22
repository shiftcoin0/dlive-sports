import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlsplit
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
# আন্তর্জাতিক পতাকার ওপেন সিডিএন (ব্রাউজারে সরাসরি ১০০% ওপেন হবে)
# -----------------------------------------------------------------------------
COUNTRY_FLAGS = {
    "argentina": "https://flagcdn.com/w320/ar.png",
    "brazil": "https://flagcdn.com/w320/br.png",
    "england": "https://flagcdn.com/w320/gb-eng.png",
    "france": "https://flagcdn.com/w320/fr.png",
    "germany": "https://flagcdn.com/w320/de.png",
    "spain": "https://flagcdn.com/w320/es.png",
    "italy": "https://flagcdn.com/w320/it.png",
    "portugal": "https://flagcdn.com/w320/pt.png",
    "netherlands": "https://flagcdn.com/w320/nl.png",
    "india": "https://flagcdn.com/w320/in.png",
    "pakistan": "https://flagcdn.com/w320/pk.png",
    "bangladesh": "https://flagcdn.com/w320/bd.png",
    "australia": "https://flagcdn.com/w320/au.png",
    "south africa": "https://flagcdn.com/w320/za.png",
    "new zealand": "https://flagcdn.com/w320/nz.png",
    "sri lanka": "https://flagcdn.com/w320/lk.png",
    "afghanistan": "https://flagcdn.com/w320/af.png",
    "west indies": "https://flagcdn.com/w320/jm.png",
    "usa": "https://flagcdn.com/w320/us.png",
    "canada": "https://flagcdn.com/w320/ca.png",
}

# মেমোরি ক্যাশ যাতে একই দলের জন্য বারবার রিকোয়েস্ট না যায়
TEAM_BADGE_CACHE: dict[str, str] = {}


async def get_team_badge(client: httpx.AsyncClient, team_name: str) -> str:
    """
    টিমের আসল লোগো/ফ্ল্যাগ খোঁজার ফাংশন (TheSportsDB API + FlagCDN)
    """
    cleaned = team_name.strip()
    if not cleaned:
        return "https://ui-avatars.com/api/?name=Live+Match&background=0D8ABC&color=fff&size=200"

    # ক্যাশে থাকলে সেখান থেকে নেওয়া
    if cleaned in TEAM_BADGE_CACHE:
        return TEAM_BADGE_CACHE[cleaned]

    # ১. আন্তর্জাতিক দল কিনা চেক
    name_lower = cleaned.lower()
    for country, flag_url in COUNTRY_FLAGS.items():
        if country in name_lower:
            TEAM_BADGE_CACHE[cleaned] = flag_url
            return flag_url

    # ২. TheSportsDB ফ্রি API দিয়ে ক্লাব ব্যাজ সার্চ করা
    api_url = f"https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t={quote_plus(cleaned)}"
    try:
        resp = await client.get(api_url, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            if data and data.get("teams"):
                badge = data["teams"][0].get("strBadge")
                if badge:
                    TEAM_BADGE_CACHE[cleaned] = badge
                    return badge
    except Exception:
        pass

    # ৩. কোনোটিই না পাওয়া গেলে সুন্দর এইচডি ক্রিয়েটিভ স্পোর্টস ব্যাজ (কোনোদিন এরর দিবে না)
    fallback_badge = f"https://ui-avatars.com/api/?name={quote_plus(cleaned)}&background=0D8ABC&color=fff&size=200&bold=true&format=png"
    TEAM_BADGE_CACHE[cleaned] = fallback_badge
    return fallback_badge


def parse_team_names(title: str) -> dict[str, str]:
    delimiters = [" vs. ", " vs ", " - ", " @ "]
    for d in delimiters:
        if d in title.lower():
            parts = re.split(re.escape(d), title, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                return {
                    "team_a": parts[0].strip(),
                    "team_b": parts[1].strip(),
                }
    return {
        "team_a": title.strip(),
        "team_b": title.strip(),
    }


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
    timeout: float = 6.0,
) -> httpx.Response | None:
    try:
        response = await client.get(url, headers=headers, timeout=timeout)
        if response.status_code == 200:
            return response
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

    lookup_url = f"{player_origin}/server_lookup.php?channel_id={channel_key}"
    lookup_headers = {
        "User-Agent": UA,
        "Referer": f"{player_origin}/",
    }

    server_key = "topembed"
    l_resp = await request(client, lookup_url, headers=lookup_headers, timeout=4.0)
    if l_resp:
        try:
            data = l_resp.json()
            if "server_key" in data and data["server_key"]:
                server_key = data["server_key"]
        except Exception:
            pass

    m3u8_url = f"https://{server_key}new.newkso.ru/{server_key}/{channel_key}/mono.m3u8"
    return m3u8_url, f"{player_origin}/"


async def get_dlhd_events(client: httpx.AsyncClient) -> list[DLHDEvent]:
    events: list[DLHDEvent] = []
    headers = {
        "User-Agent": UA,
        "Referer": f"{BASE_URL}/",
        "Accept": "application/json, text/plain, */*",
    }

    resp = await request(client, SCHEDULE_URL, headers=headers, timeout=10.0)
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

                if event_dt > now_utc + timedelta(hours=5):
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
        timeout=10.0,
        http2=True,
    ) as client:

        raw_events = await get_dlhd_events(client)
        if not raw_events:
            log.info("No active events found in window")
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

        log.info(f"Processing {len(raw_events)} active DaddyLive event(s)...")

        # ---------------------------------------------------------------------
        # ১. সকল টিমের ফ্ল্যাগ/ব্যাজ একসাথে প্যারালালে সংগ্রহ করা
        # ---------------------------------------------------------------------
        unique_team_names = set()
        for ev in raw_events:
            t = parse_team_names(ev.title)
            unique_team_names.add(t["team_a"])
            unique_team_names.add(t["team_b"])

        log.info(f"Fetching badges for {len(unique_team_names)} unique teams...")
        team_badge_map = {}

        async def fetch_team(team_name: str):
            async with HTTP_SEMAPHORE:
                team_badge_map[team_name] = await get_team_badge(client, team_name)

        await asyncio.gather(*(fetch_team(name) for name in unique_team_names))

        # ---------------------------------------------------------------------
        # ২. সকল চ্যানেল স্ট্রিম একসাথে প্যারালালে ডিকোড করা
        # ---------------------------------------------------------------------
        unique_channel_ids = {ch.channel_id for ev in raw_events for ch in ev.channels}
        log.info(f"Decoding {len(unique_channel_ids)} unique channels concurrently...")

        channel_map = {}

        async def fetch_channel(c_id: str):
            async with HTTP_SEMAPHORE:
                res = await decode_dlhd_m3u8(client, c_id)
                if res:
                    channel_map[c_id] = res

        await asyncio.gather(*(fetch_channel(cid) for cid in unique_channel_ids))

        # ---------------------------------------------------------------------
        # ৩. চূড়ান্ত প্লেলিস্ট তৈরি
        # ---------------------------------------------------------------------
        final_playlist: list[dict] = []

        for ev in raw_events:
            event_channels = []

            for ch in ev.channels:
                if ch.channel_id in channel_map:
                    m3u8_url, referer = channel_map[ch.channel_id]
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

                # টিম নেম এবং সংশ্লিষ্ট ফ্ল্যাগ নির্ধারণ
                t_names = parse_team_names(ev.title)
                team_a_name = t_names["team_a"]
                team_b_name = t_names["team_b"]

                team_a_flag = team_badge_map.get(team_a_name, "")
                team_b_flag = team_badge_map.get(team_b_name, "")

                # টিম A এর ফ্ল্যাগ/লোগোটিকে মূল লোগো হিসেবে সেট করা
                event_logo = team_a_flag

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
                    "teams": {
                        "team_a": {
                            "name": team_a_name,
                            "flag": team_a_flag,
                        },
                        "team_b": {
                            "name": team_b_name,
                            "flag": team_b_flag,
                        },
                    },
                    "logo": event_logo,  # Team A এর ইমেজ মূল লোগোতে সেট হলো
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
        log.info("Playlist generation with Real Team Logos completed successfully!")


if __name__ == "__main__":
    asyncio.run(scrape())
