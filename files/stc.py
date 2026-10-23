import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin
from zoneinfo import ZoneInfo

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

# রিপোজিটরি রুট ডিরেক্টরি
ROOT_DIR = Path(__file__).resolve().parent.parent

# সম্পূর্ণ ভিন্ন ও স্বাধীন আন্তর্জাতিক সোর্স
BASE_URL = "https://livetv808.me"
ALT_URL = "https://livetv.sx"
SCHEDULE_PATH = "/enx/allupcoming/"

TAG = "LIVETV"

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

    if any(k in comb for k in ["soccer", "football", "premier league", "la liga", "serie a", "bundesliga"]):
        return SPORT_ICONS["football"]
    if any(k in comb for k in ["f1", "formula 1", "racing", "motogp"]):
        return SPORT_ICONS["motorsport"]
    if any(k in comb for k in ["wwe", "smackdown", "raw", "ufc", "fighting", "boxing"]):
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
    if "hockey" in clean or "ice hockey" in clean:
        return "Hockey"
    if any(k in clean for k in ["motorsport", "racing", "f1"]):
        return "Motorsport"
    if any(k in clean for k in ["wwe", "wrestling", "fight", "fighting", "ufc", "boxing"]):
        return "Fighting"
    if "baseball" in clean:
        return "Baseball"
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
    (r"national league", "National League"),
    (r"ipl|indian premier league", "Indian Premier League (IPL)"),
    (r"bpl", "Bangladesh Premier League (BPL)"),
    (r"psl", "Pakistan Super League (PSL)"),
    (r"nba", "NBA"),
    (r"smackdown", "WWE SmackDown"),
    (r"raw", "WWE Raw"),
    (r"ufc", "UFC"),
]


def get_sub_category(sport: str, title: str) -> str:
    comb = f"{sport} {title}".lower()
    for pat, league in LEAGUE_PATTERNS:
        if re.search(pat, comb):
            return league
    return sport


def parse_team_names(title: str) -> dict[str, str]:
    clean = title.split(":")[-1].strip() if ":" in title else title
    for d in [" vs. ", " vs ", " - ", " @ "]:
        if d in clean.lower():
            parts = re.split(re.escape(d), clean, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                return {
                    "team_a": parts[0].strip(),
                    "team_b": parts[1].strip(),
                }
    return {"team_a": clean.strip(), "team_b": clean.strip()}


@dataclass(slots=True)
class LiveTVEvent:
    sport: str
    title: str
    start_dt: datetime
    page_url: str


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
    timeout: float = 8.0,
) -> httpx.Response | None:
    try:
        response = await client.get(url, headers=headers, timeout=timeout)
        if response.status_code == 200:
            return response
        return None
    except Exception:
        return None


# -----------------------------------------------------------------------------
# LiveTV ইভেন্ট পেজ থেকে প্লে-যোগ্য স্ট্রিম এক্সট্র্যাক্ট করা
# -----------------------------------------------------------------------------
async def fetch_livetv_streams(client: httpx.AsyncClient, event_page_url: str) -> list[dict]:
    streams = []
    resp = await request(client, event_page_url)
    if not resp:
        return streams

    soup = HTMLParser(resp.content)
    # ব্রডকাস্ট টেবিল থেকে লিঙ্ক সংগ্রহ
    for row in soup.css("table.lnktbj tr"):
        link_elem = row.css_first("a[href*='webplayer.php'], a[href*='stream']")
        if not link_elem:
            continue

        raw_href = link_elem.attributes.get("href") or ""
        stream_url = urljoin(BASE_URL, raw_href)
        channel_name = link_elem.text(strip=True) or "Live Broadcast"

        streams.append({
            "channel_name": channel_name,
            "url": stream_url,
            "referer": f"{BASE_URL}/"
        })

    return streams


async def get_livetv_events(client: httpx.AsyncClient) -> list[LiveTVEvent]:
    events = []
    target_url = f"{BASE_URL}{SCHEDULE_PATH}"
    resp = await request(client, target_url)

    if not resp:
        resp = await request(client, f"{ALT_URL}{SCHEDULE_PATH}")

    if not resp:
        log.error("Could not fetch LiveTV upcoming schedule.")
        return events

    soup = HTMLParser(resp.content)
    now_utc = datetime.now(timezone.utc)

    # টেবিলের প্রতিটি খেলার রো পার্স করা
    for row in soup.css("table tr"):
        title_elem = row.css_first("a.live")
        time_elem = row.css_first("span.date")

        if not title_elem or not time_elem:
            continue

        title = title_elem.text(strip=True)
        href = title_elem.attributes.get("href") or ""
        time_text = time_elem.text(strip=True)

        if not title or not href:
            continue

        # সময় পার্সিং (LiveTV সময় CET/UTC অনুযায়ী থাকে)
        event_dt = now_utc
        time_match = re.search(r"(\d{1,2}):(\d{2})", time_text)
        if time_match:
            h, m = int(time_match.group(1)), int(time_match.group(2))
            event_dt = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)

        # অতীত ৩ ঘণ্টার পুরানো ম্যাচ বাদ
        if event_dt < now_utc - timedelta(hours=3):
            continue
        # আগামী ২৪ ঘণ্টার ম্যাচগুলো রাখা হবে
        if event_dt > now_utc + timedelta(hours=24):
            continue

        # ক্যাটাগরি আইকন বা স্পোর্টস অনুমান
        sport = "Football"
        if any(k in title.lower() for k in ["nba", "basket", "lakers", "celtics"]):
            sport = "Basketball"
        elif any(k in title.lower() for k in ["cricket", "ipl", "t20"]):
            sport = "Cricket"
        elif any(k in title.lower() for k in ["tennis", "atp", "wta", "open"]):
            sport = "Tennis"
        elif any(k in title.lower() for k in ["hockey", "nhl"]):
            sport = "Hockey"

        full_event_url = urljoin(BASE_URL, href)
        events.append(LiveTVEvent(sport=sport, title=title, start_dt=event_dt, page_url=full_event_url))

    return events


async def scrape() -> None:
    log.info(f'Scraping from "{BASE_URL}"...')

    async with httpx.AsyncClient(
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        follow_redirects=True,
        timeout=10.0,
        http2=True,
    ) as client:

        raw_events = await get_livetv_events(client)
        if not raw_events:
            log.info("No active events found on LiveTV.")
            return

        log.info(f"Successfully received {len(raw_events)} active matches from LiveTV!")

        final_playlist: list[dict] = []

        # প্রথম ৩০টি অতি গুরুত্বপূর্ণ ম্যাচের স্ট্রিম দ্রুত লোড করা
        for ev in raw_events[:30]:
            async def get_streams():
                async with HTTP_SEMAPHORE:
                    return await fetch_livetv_streams(client, ev.page_url)

            stream_results = await get_streams()
            event_channels = []

            for idx, st in enumerate(stream_results, start=1):
                stream_url = st["url"]
                event_channels.append(
                    {
                        "channel_name": f"Stream {idx} - {st['channel_name']}",
                        "stream_type": "HLS",
                        "url": stream_url,
                        "raw_link": f"{stream_url}|Referer={st['referer']}&User-Agent={quote_plus(UA)}",
                        "headers": {
                            "Referer": st["referer"],
                            "User-Agent": UA,
                        },
                        "drm_key": None,
                    }
                )

            # কোনো ব্রোকেন স্ট্রিম ছাড়া ইভেন্ট যোগ করা
            if not event_channels:
                event_channels.append(
                    {
                        "channel_name": "Live Broadcast Feed",
                        "stream_type": "HLS",
                        "url": ev.page_url,
                        "raw_link": f"{ev.page_url}|Referer={BASE_URL}/",
                        "headers": {"Referer": f"{BASE_URL}/"},
                        "drm_key": None,
                    }
                )

            start_bst = ev.start_dt.astimezone(BST_TZ)
            end_bst = start_bst + timedelta(hours=3)

            t_names = parse_team_names(ev.title)
            team_a_name = t_names["team_a"]
            team_b_name = t_names["team_b"]

            event_logo = get_event_logo(ev.sport, ev.title)
            sub_category = get_sub_category(ev.sport, ev.title)

            formatted_event = {
                "title": ev.title,
                "sport": ev.sport,
                "sub_category": sub_category,
                "status": determine_status(ev.start_dt, early_minutes=15),
                "is_hot": False,
                "slug": create_slug(ev.title),
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
                "_timestamp": ev.start_dt.timestamp(),
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
        log.info(f"Playlist successfully generated with {len(final_playlist)} LiveTV matches!")


if __name__ == "__main__":
    asyncio.run(scrape())
