import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx

# রিপোজিটরি রুট ডিরেক্টরি
ROOT_DIR = Path(__file__).resolve().parent.parent

# নির্ভরযোগ্য লাইভ স্পোর্টস API সোর্স (সরাসরি HLS .m3u8 প্রদান করে)
VEBO_API = "https://api.vebotv.org/api/match/featured"

TAG = "SPORTS"

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
    if any(k in comb for k in ["basketball", "nba"]):
        return SPORT_ICONS["basketball"]

    return DEFAULT_LOGO


def clean_and_normalize_sport(raw_sport: str) -> str:
    clean = re.sub(r"<[^>]+>", "", str(raw_sport)).strip().lower()
    if any(k in clean for k in ["soccer", "football"]):
        return "Football"
    if "basketball" in clean:
        return "Basketball"
    if "cricket" in clean:
        return "Cricket"
    if "tennis" in clean:
        return "Tennis"
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
    (r"nba", "NBA"),
]


def get_sub_category(sport: str, title: str, tournament_name: str = "") -> str:
    comb = f"{sport} {title} {tournament_name}".lower()
    for pat, league in LEAGUE_PATTERNS:
        if re.search(pat, comb):
            return league
    return tournament_name or sport


# En-dash (–), Em-dash (—) এবং সব ধরণের ড্যাশ সাপোর্টসহ পার্সিং
def parse_team_names(title: str) -> dict[str, str]:
    clean = title.split(":")[-1].strip() if ":" in title else title
    # সব রকমের ড্যাশকে সাধারণ হাইফেনে রূপান্তর
    clean = clean.replace("–", "-").replace("—", "-")
    for d in [" vs. ", " vs ", " - ", " @ "]:
        if d in clean.lower():
            parts = re.split(re.escape(d), clean, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                return {
                    "team_a": parts[0].strip(),
                    "team_b": parts[1].strip(),
                }
    return {"team_a": clean.strip(), "team_b": clean.strip()}


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


async def scrape() -> None:
    log.info(f'Fetching live events from API: "{VEBO_API}"...')

    async with httpx.AsyncClient(
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://vebo.live",
            "Referer": "https://vebo.live/"
        },
        follow_redirects=True,
        timeout=12.0,
        http2=True,
    ) as client:

        resp = await request(client, VEBO_API)
        if not resp:
            log.error("Could not fetch API data.")
            return

        try:
            api_data = resp.json()
            matches = api_data.get("data", []) if isinstance(api_data, dict) else api_data
        except Exception as e:
            log.error(f"Error parsing API JSON: {e}")
            return

        now_utc = datetime.now(timezone.utc)
        final_playlist: list[dict] = []
        seen_matches = set()

        for m in matches:
            if not isinstance(m, dict):
                continue

            # ডুপ্লিকেট ম্যাচ আইডি রোধ
            match_id = str(m.get("id") or "")
            name_str = m.get("name") or f"{m.get('home', {}).get('name', '')} vs {m.get('away', {}).get('name', '')}"
            if not name_str or name_str in seen_matches:
                continue

            # সময় পার্সিং (ম্যাচের টাইমস্ট্যাম্প)
            date_val = m.get("date") or m.get("timestamp") or m.get("match_time")
            if not date_val:
                continue

            try:
                # টাইমস্ট্যাম্প যদি সেকেন্ড বা মিলিসেকেন্ডে থাকে
                ts = float(date_val)
                if ts > 1e11:
                    ts = ts / 1000.0
                start_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                continue

            # অতীত ৩ ঘণ্টার পুরানো এবং আগামী ২৪ ঘণ্টার দূরের ম্যাচ ফিল্টার
            if start_dt < now_utc - timedelta(hours=3):
                continue
            if start_dt > now_utc + timedelta(hours=24):
                continue

            seen_matches.add(name_str)

            sport = clean_and_normalize_sport(m.get("sport_type") or "Football")
            tournament = m.get("tournament", {}).get("name") if isinstance(m.get("tournament"), dict) else ""
            sub_category = get_sub_category(sport, name_str, tournament)
            event_logo = get_event_logo(sport, name_str)

            # টিম নেম পার্সিং
            team_a_name = m.get("home", {}).get("name") if isinstance(m.get("home"), dict) else ""
            team_b_name = m.get("away", {}).get("name") if isinstance(m.get("away"), dict) else ""

            if not team_a_name or not team_b_name:
                t_names = parse_team_names(name_str)
                team_a_name = t_names["team_a"]
                team_b_name = t_names["team_b"]

            # সরাসরি প্লে-যোগ্য HLS স্ট্রিম সংগ্রহ
            event_channels = []
            sources = m.get("streaming_sources") or m.get("sources") or []

            # যদি সরাসরি প্লে-URL থাকে
            direct_hls = m.get("live_url") or m.get("hls_url") or m.get("play_url")
            if direct_hls and ".m3u8" in str(direct_hls):
                event_channels.append({
                    "channel_name": "Main HD Stream",
                    "stream_type": "HLS",
                    "url": direct_hls,
                    "raw_link": f"{direct_hls}|Referer=https://vebo.live/",
                    "headers": {"Referer": "https://vebo.live/"},
                    "drm_key": None
                })

            for idx, src in enumerate(sources, start=1):
                s_url = src.get("url") or src.get("stream_url") if isinstance(src, dict) else str(src)
                if s_url and ".m3u8" in s_url:
                    event_channels.append({
                        "channel_name": f"Server {idx}",
                        "stream_type": "HLS",
                        "url": s_url,
                        "raw_link": f"{s_url}|Referer=https://vebo.live/",
                        "headers": {"Referer": "https://vebo.live/"},
                        "drm_key": None
                    })

            # যদি কোনো স্ট্রিম না পাওয়া যায়, ব্যাকআপ প্লেয়ার লিংক
            if not event_channels and match_id:
                fallback_stream = f"https://live.vebo.live/stream/{match_id}/playlist.m3u8"
                event_channels.append({
                    "channel_name": "Live HD Stream",
                    "stream_type": "HLS",
                    "url": fallback_stream,
                    "raw_link": f"{fallback_stream}|Referer=https://vebo.live/",
                    "headers": {"Referer": "https://vebo.live/"},
                    "drm_key": None
                })

            start_bst = start_dt.astimezone(BST_TZ)
            end_bst = start_bst + timedelta(hours=3)

            formatted_event = {
                "title": name_str,
                "sport": sport,
                "sub_category": sub_category,
                "status": determine_status(start_dt, early_minutes=15),
                "is_hot": False,
                "slug": create_slug(name_str),
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
        log.info(f"Successfully generated playlist with {len(final_playlist)} unique matches!")


if __name__ == "__main__":
    asyncio.run(scrape())
