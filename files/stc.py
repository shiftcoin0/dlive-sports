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

# আপনার স্ক্রিনশটের মূল ডোমেইন
BASE_URL = "https://thedaddy.pk"
SCHEDULE_URL = f"{BASE_URL}/schedule/schedule-generated.json"

TAG = "DLIVE"

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
# কাস্টম স্পোর্টস আইকন ও ডিফল্ট লোগো
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

    if any(k in comb for k in ["soccer", "fifa", "uefa", "la liga", "premier league", "serie a", "bundesliga", "national league", "league"]):
        return SPORT_ICONS["football"]
    if any(k in comb for k in ["f1", "formula 1", "racing", "motogp", "nascar"]):
        return SPORT_ICONS["motorsport"]
    if any(k in comb for k in ["wwe", "smackdown", "raw", "aew", "ufc", "fighting", "mma", "boxing"]):
        return SPORT_ICONS["wrestling"]

    return DEFAULT_LOGO


# -----------------------------------------------------------------------------
# স্পোর্টস ক্লিন এবং স্ট্যান্ডার্ড নাম রূপান্তরকারী
# -----------------------------------------------------------------------------
def clean_and_normalize_sport(raw_sport: str, title: str = "") -> str:
    clean = re.sub(r"<[^>]+>", "", raw_sport).strip()
    clean = clean.split("—")[0].strip()
    combined = f"{clean} {title}".lower()

    if "american football" in combined or "nfl" in combined:
        return "American Football"
    if any(k in combined for k in ["soccer", "football", "national league", "premier league", "la liga", "serie a", "bundesliga"]):
        return "Football"
    if any(k in combined for k in ["cricket", "ipl", "bpl", "psl", "t20", "odi", "test"]):
        return "Cricket"
    if any(k in combined for k in ["basketball", "nba", "euroleague"]):
        return "Basketball"
    if any(k in combined for k in ["hockey", "nhl", "ice hockey"]):
        return "Hockey"
    if any(k in combined for k in ["tennis", "wimbledon", "atp", "wta"]):
        return "Tennis"
    if any(k in combined for k in ["weightlifting"]):
        return "Weightlifting"
    if any(k in combined for k in ["volleyball"]):
        return "Volleyball"
    if any(k in combined for k in ["ski jumping"]):
        return "Ski Jumping"
    if any(k in combined for k in ["winter sports"]):
        return "Winter Sports"
    if any(k in combined for k in ["handball"]):
        return "Handball"
    if any(k in combined for k in ["snooker"]):
        return "Snooker"
    if any(k in combined for k in ["golf"]):
        return "Golf"
    if any(k in combined for k in ["motorsport", "racing", "f1", "motogp", "formula 1"]):
        return "Motorsport"
    if any(k in combined for k in ["wwe", "wrestling", "smackdown", "raw"]):
        return "Wrestling"
    if any(k in combined for k in ["ufc", "fighting", "mma", "boxing"]):
        return "Fighting"

    return clean.capitalize() or "Live Sports"


# -----------------------------------------------------------------------------
# স্মার্ট সাব-ক্যাটাগরি ডিটেকশন ইঞ্জিন
# -----------------------------------------------------------------------------
LEAGUE_PATTERNS = [
    (r"national league", "England National League"),
    (r"ireland premier", "Ireland Premier League"),
    (r"premier league|epl|english premier", "Premier League"),
    (r"la liga|laliga|primera division", "La Liga"),
    (r"serie a|serie-a", "Serie A"),
    (r"bundesliga", "Bundesliga"),
    (r"ligue 1|ligue1", "Ligue 1"),
    (r"champions league|ucl|uefa champions", "UEFA Champions League"),
    (r"europa league|uel", "UEFA Europa League"),
    (r"saudi|pro league", "Saudi Pro League"),
    (r"a-league|australia", "Australia A-League"),
    (r"indonesia", "Indonesia League"),
    (r"isl|india isl", "Indian Super League (ISL)"),
    (r"ipl|indian premier league", "Indian Premier League (IPL)"),
    (r"bpl|bangladesh premier league", "Bangladesh Premier League (BPL)"),
    (r"psl|pakistan super league", "Pakistan Super League (PSL)"),
    (r"nba", "NBA"),
    (r"smackdown", "WWE SmackDown"),
    (r"raw", "WWE Raw"),
    (r"ufc", "UFC"),
    (r"formula 1|formula one|f1", "Formula 1"),
]


def get_sub_category(sport: str, title: str) -> str:
    combined_text = f"{sport} {title}".lower()
    for pattern, league_name in LEAGUE_PATTERNS:
        if re.search(pattern, combined_text):
            return league_name
    return sport


def parse_team_names(title: str) -> dict[str, str]:
    # টাইটেল থেকে কোলন বা লিগের নাম আলাদা করা
    clean_title = title.split(":")[-1].strip() if ":" in title else title
    for d in [" vs. ", " vs ", " - ", " @ "]:
        if d in clean_title.lower():
            parts = re.split(re.escape(d), clean_title, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                # অপ্রয়োজনীয় কান্ট্রি ট্যাগ (যেমন 🏴 বা AU) পরিষ্কার করা
                t_a = re.sub(r"[^\w\s]", "", parts[0]).strip()
                t_b = re.sub(r"[^\w\s]", "", parts[1]).strip()
                return {"team_a": t_a, "team_b": t_b}
    return {"team_a": clean_title.strip(), "team_b": clean_title.strip()}


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


# -----------------------------------------------------------------------------
# DaddyLive .m3u8 ডিকোডার (হেডার ও অথেনটিকেশন টোকেন বাইন্ডার)
# -----------------------------------------------------------------------------
KNOWN_LOOKUP_HOSTS = [
    "https://newembedplay.xyz",
    "https://forcedtoplay.xyz",
    "https://allupplay.xyz",
    "https://webxzplay.cfd",
]


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

    direct_match = re.search(r'["\'](https?://[^"\']+\.m3u8(?:\?[^"\']*)?)["\']', resp.text)
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
        "Origin": BASE_URL,
    }
    player_resp = await request(client, iframe_src, headers=player_headers)
    if not player_resp:
        return None

    # ডাবল আইফ্রেম হ্যান্ডলিং
    p_soup = HTMLParser(player_resp.content)
    inner_iframe = p_soup.css_first("iframe")
    if inner_iframe and inner_iframe.attributes.get("src"):
        inner_src = inner_iframe.attributes.get("src")
        if inner_src.startswith("//"):
            inner_src = "https:" + inner_src
        elif inner_src.startswith("/"):
            inner_src = urljoin(player_origin, inner_src)

        parsed_inner = urlsplit(inner_src)
        inner_origin = f"{parsed_inner.scheme}://{parsed_inner.netloc}"

        inner_headers = {
            "User-Agent": UA,
            "Referer": f"{player_origin}/",
            "Origin": player_origin,
        }
        inner_resp = await request(client, inner_src, headers=inner_headers)
        if inner_resp and inner_resp.status_code == 200:
            player_resp = inner_resp
            player_origin = inner_origin

    key_match = re.search(r'channel_id=([^"\'&\s]+)', player_resp.text) or re.search(r'channel_key\s*[:=]\s*["\']([^"\']+)["\']', player_resp.text)
    channel_key = key_match.group(1) if key_match else f"premium{clean_id}"

    server_key = None
    lookup_candidates = [player_origin] + [h for h in KNOWN_LOOKUP_HOSTS if h != player_origin]

    for host in lookup_candidates:
        lookup_url = f"{host}/server_lookup.php?channel_id={channel_key}"
        l_headers = {
            "User-Agent": UA,
            "Referer": f"{host}/",
            "Origin": host,
        }
        l_resp = await request(client, lookup_url, headers=l_headers, timeout=4.0)
        if l_resp and l_resp.status_code == 200:
            try:
                data = l_resp.json()
                if isinstance(data, dict):
                    if data.get("server_key"):
                        server_key = data["server_key"]
                    if "url" in data and ".m3u8" in str(data["url"]):
                        return str(data["url"]), f"{host}/"
                    if server_key:
                        player_origin = host
                        break
            except Exception:
                pass

    if not server_key:
        server_key = "wind"

    domain = "newkso.ru"
    if "iosplayer" in str(server_key):
        domain = "iosplayer.ru"

    if "/" in server_key:
        parts = server_key.split("/")
        base_m3u8 = f"https://{parts[0]}.{domain}/{server_key}/{channel_key}/mono.m3u8"
    else:
        base_m3u8 = f"https://{server_key}new.{domain}/{server_key}/{channel_key}/mono.m3u8"

    return base_m3u8, f"{player_origin}/"


# -----------------------------------------------------------------------------
# thedaddy.pk লাইভ শিডিউল পার্সিং
# -----------------------------------------------------------------------------
async def get_dlhd_events(client: httpx.AsyncClient) -> list[DLHDEvent]:
    events: list[DLHDEvent] = []
    headers = {
        "User-Agent": UA,
        "Referer": f"{BASE_URL}/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # প্রথমে schedule-generated.json ট্রাই করা, না হলে সরাসরি শিডিউল পেজ
    resp = await request(client, SCHEDULE_URL, headers={"User-Agent": UA, "Referer": f"{BASE_URL}/"}, timeout=10.0)
    now_utc = datetime.now(timezone.utc)

    # JSON ফরম্যাট পাওয়া গেলে
    if resp:
        try:
            schedule_data = resp.json()
            for date_key, category_obj in schedule_data.items():
                items = category_obj if isinstance(category_obj, dict) else {date_key: category_obj}

                for sport_cat, games in items.items():
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
                        if event_dt > now_utc + timedelta(hours=12):
                            continue

                        channels = []
                        for ch in raw_channels:
                            cid = str(ch.get("channel_id") if isinstance(ch, dict) else ch)
                            cname = (ch.get("channel_name") if isinstance(ch, dict) else f"Stream {ch}") or "Stream"
                            if cid:
                                channels.append(DLHDChannel(channel_name=cname, channel_id=cid))

                        if channels:
                            norm_sport = clean_and_normalize_sport(sport_cat, event_title)
                            events.append(DLHDEvent(sport=norm_sport, title=event_title.strip(), start_dt=event_dt, channels=channels))
        except Exception:
            pass

    # যদি JSON এ না পাওয়া যায়, তবে thedaddy.pk এর হোমপেজ পার্স করা
    if not events:
        home_resp = await request(client, f"{BASE_URL}/schedule/schedule.php", headers=headers, timeout=12.0)
        if not home_resp:
            home_resp = await request(client, BASE_URL, headers=headers, timeout=12.0)

        if home_resp:
            soup = HTMLParser(home_resp.content)
            current_category = "Football"

            for node in soup.css(".card, .panel, .accordion, .row"):
                cat_header = node.css_first("h2, h3, h4, .header, strong")
                if cat_header:
                    current_category = cat_header.text(strip=True)

                for item in node.css("a, .event-item, tr"):
                    text = item.text(strip=True)
                    href = item.attributes.get("href") or ""

                    m_time = re.search(r"(\d{1,2}:\d{2})", text)
                    if m_time and ("vs" in text.lower() or ":" in text):
                        time_str = m_time.group(1)
                        h, m = map(int, time_str.split(":"))
                        event_dt = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)

                        clean_title = re.sub(r"^\d{1,2}:\d{2}", "", text).strip()
                        clean_title = re.sub(r"[🌐🏴]", "", clean_title).strip()

                        cid_match = re.search(r"stream-(\d+)\.php", href)
                        cid = cid_match.group(1) if cid_match else "302"

                        events.append(
                            DLHDEvent(
                                sport=clean_and_normalize_sport(current_category, clean_title),
                                title=clean_title,
                                start_dt=event_dt,
                                channels=[DLHDChannel(channel_name="Live Stream", channel_id=cid)],
                            )
                        )

    return events


async def scrape() -> None:
    log.info(f'Scraping from "{BASE_URL}"')

    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Referer": f"{BASE_URL}/"},
        follow_redirects=True,
        timeout=10.0,
        http2=True,
    ) as client:

        raw_events = await get_dlhd_events(client)
        if not raw_events:
            log.info("No active events found on thedaddy.pk")
            return

        log.info(f"Processing {len(raw_events)} active event(s) from thedaddy.pk...")

        unique_channel_ids = {ch.channel_id for ev in raw_events for ch in ev.channels}
        channel_map = {}

        async def fetch_channel(c_id: str):
            async with HTTP_SEMAPHORE:
                res = await decode_dlhd_m3u8(client, c_id)
                if res:
                    channel_map[c_id] = res

        await asyncio.gather(*(fetch_channel(cid) for cid in unique_channel_ids))

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
                            "raw_link": f"{m3u8_url}|Referer={referer}&User-Agent={quote_plus(UA)}",
                            "headers": {
                                "Referer": referer,
                                "User-Agent": UA,
                                "Origin": referer.rstrip("/"),
                            },
                            "drm_key": None,
                        }
                    )

            if event_channels:
                start_bst = ev.start_dt.astimezone(BST_TZ)
                end_bst = start_bst + timedelta(hours=3)

                start_str = start_bst.strftime("%b %d, %Y, %I:%M:%S %p")
                end_str = end_bst.strftime("%b %d, %Y, %I:%M:%S %p")

                t_names = parse_team_names(ev.title)
                team_a_name = t_names["team_a"]
                team_b_name = t_names["team_b"]

                event_logo = get_event_logo(ev.sport, ev.title)
                detected_sub_category = get_sub_category(ev.sport, ev.title)

                formatted_event = {
                    "title": ev.title,
                    "sport": ev.sport,
                    "sub_category": detected_sub_category,
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
                            "flag": event_logo,
                        },
                        "team_b": {
                            "name": team_b_name,
                            "flag": event_logo,
                        },
                    },
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
        log.info("Playlist generation completed successfully from thedaddy.pk!")


if __name__ == "__main__":
    asyncio.run(scrape())
