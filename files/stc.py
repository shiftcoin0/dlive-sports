import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

ROOT_DIR = Path(__file__).resolve().parent.parent

BASE_URL = "https://thedaddy.pk"
SCHEDULE_URL = f"{BASE_URL}/schedule/schedule-generated.json"

TAG = "DLIVE"

JSON_OUTPUT = ROOT_DIR / "strmcntr.json"
M3U_OUTPUT = ROOT_DIR / "strmcntr.m3u"

LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

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


# ডিরেক্ট টিম লোগো সিডিএন
KNOWN_TEAM_LOGOS = {
    "arsenal": "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png",
    "chelsea": "https://images.fotmob.com/image_resources/logo/teamlogo/10260.png",
    "liverpool": "https://images.fotmob.com/image_resources/logo/teamlogo/8650.png",
    "manchester united": "https://images.fotmob.com/image_resources/logo/teamlogo/10267.png",
    "manchester city": "https://images.fotmob.com/image_resources/logo/teamlogo/8456.png",
    "tottenham": "https://images.fotmob.com/image_resources/logo/teamlogo/8586.png",
    "real madrid": "https://images.fotmob.com/image_resources/logo/teamlogo/8633.png",
    "barcelona": "https://images.fotmob.com/image_resources/logo/teamlogo/8634.png",
    "atletico madrid": "https://images.fotmob.com/image_resources/logo/teamlogo/9906.png",
    "juventus": "https://images.fotmob.com/image_resources/logo/teamlogo/9885.png",
    "inter milan": "https://images.fotmob.com/image_resources/logo/teamlogo/8636.png",
    "ac milan": "https://images.fotmob.com/image_resources/logo/teamlogo/8564.png",
    "bayern munich": "https://images.fotmob.com/image_resources/logo/teamlogo/9823.png",
    "dortmund": "https://images.fotmob.com/image_resources/logo/teamlogo/9789.png",
    "psg": "https://images.fotmob.com/image_resources/logo/teamlogo/9847.png",
    "csk": "https://images.fotmob.com/image_resources/logo/teamlogo/6573.png",
    "mi": "https://images.fotmob.com/image_resources/logo/teamlogo/6576.png",
    "rcb": "https://images.fotmob.com/image_resources/logo/teamlogo/6574.png",
    "argentina": "https://flagcdn.com/w320/ar.png",
    "brazil": "https://flagcdn.com/w320/br.png",
    "england": "https://flagcdn.com/w320/gb-eng.png",
    "france": "https://flagcdn.com/w320/fr.png",
    "germany": "https://flagcdn.com/w320/de.png",
    "spain": "https://flagcdn.com/w320/es.png",
    "india": "https://flagcdn.com/w320/in.png",
    "pakistan": "https://flagcdn.com/w320/pk.png",
    "bangladesh": "https://flagcdn.com/w320/bd.png",
}

SPORT_ICONS = {
    "football": "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png",
    "cricket": "https://cdn-icons-png.flaticon.com/512/1071/1071178.png",
    "basketball": "https://cdn-icons-png.flaticon.com/512/889/889508.png",
    "tennis": "https://cdn-icons-png.flaticon.com/512/709/709605.png",
    "hockey": "https://cdn-icons-png.flaticon.com/512/2622/2622830.png",
}

DEFAULT_LOGO = "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png"


def get_real_team_badge(team_name: str, sport: str = "Football") -> str:
    cleaned = team_name.strip().lower()
    for k, v in KNOWN_TEAM_LOGOS.items():
        if k in cleaned or cleaned in k:
            return v
    for k, v in SPORT_ICONS.items():
        if k in sport.lower():
            return v
    return DEFAULT_LOGO


def clean_and_normalize_sport(raw_sport: str, title: str = "") -> str:
    clean = re.sub(r"<[^>]+>", "", raw_sport).strip()
    clean = clean.split("—")[0].strip()
    comb = f"{clean} {title}".lower()

    if "american football" in comb or "nfl" in comb:
        return "American Football"
    if any(k in comb for k in ["soccer", "football", "premier league", "la liga", "serie a", "bundesliga"]):
        return "Football"
    if any(k in comb for k in ["cricket", "ipl", "bpl", "psl", "t20", "odi", "test"]):
        return "Cricket"
    if any(k in comb for k in ["basketball", "nba", "euroleague"]):
        return "Basketball"
    if any(k in comb for k in ["hockey", "nhl", "ice hockey"]):
        return "Hockey"
    if any(k in comb for k in ["tennis", "wimbledon", "atp", "wta"]):
        return "Tennis"
    if any(k in comb for k in ["motorsport", "racing", "f1", "motogp"]):
        return "Motorsport"
    if any(k in comb for k in ["wwe", "wrestling", "smackdown", "raw"]):
        return "Wrestling"
    if any(k in comb for k in ["ufc", "fighting", "mma", "boxing"]):
        return "Fighting"

    return clean.capitalize() or "Live Sports"


LEAGUE_PATTERNS = [
    (r"premier league|epl", "Premier League"),
    (r"la liga|laliga", "La Liga"),
    (r"serie a", "Serie A"),
    (r"bundesliga", "Bundesliga"),
    (r"champions league|ucl", "UEFA Champions League"),
    (r"europa league|uel", "UEFA Europa League"),
    (r"ipl|indian premier league", "Indian Premier League (IPL)"),
    (r"bpl", "Bangladesh Premier League (BPL)"),
    (r"nba", "NBA"),
    (r"smackdown", "WWE SmackDown"),
    (r"raw", "WWE Raw"),
    (r"ufc", "UFC"),
    (r"f1|formula 1", "Formula 1"),
]


def get_sub_category(sport: str, title: str) -> str:
    comb = f"{sport} {title}".lower()
    for pat, league in LEAGUE_PATTERNS:
        if re.search(pat, comb):
            return league
    return sport


def parse_team_names(title: str) -> dict[str, str]:
    for d in [" vs. ", " vs ", " - ", " @ "]:
        if d in title.lower():
            parts = re.split(re.escape(d), title, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                return {"team_a": parts[0].strip(), "team_b": parts[1].strip()}
    return {"team_a": title.strip(), "team_b": title.strip()}


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


def determine_status(start_dt: datetime) -> str:
    now_utc = datetime.now(timezone.utc)
    start_utc = start_dt.astimezone(timezone.utc)
    live_start = start_utc - timedelta(minutes=15)
    end_time = start_utc + timedelta(hours=3)

    if live_start <= now_utc <= end_time:
        return "LIVE"
    elif now_utc < live_start:
        return "UPCOMING"
    return "ENDED"


def save_playlists(events: list[dict]) -> None:
    now_bst = datetime.now(BST_TZ)
    last_updated_str = now_bst.strftime("%b %d, %Y, %I:%M:%S %p")

    json_payload = {
        "last_updated": last_updated_str,
        "event_count": len(events),
        "events": {"events": events}
    }

    JSON_OUTPUT.write_text(json.dumps(json_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Saved JSON: {JSON_OUTPUT}")

    m3u_lines = ["#EXTM3U"]
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

            m3u_lines.append(f'#EXTINF:-1 tvg-id="Live.Event.us" tvg-logo="{logo}" group-title="{sub_cat}",{display_name}')
            if referer:
                m3u_lines.append(f'#EXTVLCOPT:http-referrer={referer}')
            m3u_lines.append(url)

    M3U_OUTPUT.write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")
    log.info(f"Saved M3U: {M3U_OUTPUT}")


async def decode_dlhd_m3u8(client: httpx.AsyncClient, channel_id: str) -> tuple[str, str] | None:
    clean_id = re.sub(r"[^\d]", "", str(channel_id))
    if not clean_id:
        return None

    channel_url = f"{BASE_URL}/stream/stream-{clean_id}.php"
    headers = {"User-Agent": UA, "Referer": f"{BASE_URL}/"}

    try:
        resp = await client.get(channel_url, headers=headers, timeout=5.0)
        if resp.status_code != 200:
            return None

        m3u8_match = re.search(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', resp.text)
        if m3u8_match:
            return m3u8_match.group(1), f"{BASE_URL}/"

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

        parsed = urlsplit(iframe_src)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        p_resp = await client.get(iframe_src, headers={"User-Agent": UA, "Referer": f"{BASE_URL}/"}, timeout=5.0)
        if p_resp.status_code != 200:
            return None

        m3u8_match = re.search(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', p_resp.text)
        if m3u8_match:
            return m3u8_match.group(1), f"{origin}/"

        key_m = re.search(r'channel_id=([^"\'&\s]+)', p_resp.text)
        channel_key = key_m.group(1) if key_m else f"premium{clean_id}"

        lookup_url = f"{origin}/server_lookup.php?channel_id={channel_key}"
        server_key = "topembed"
        l_resp = await client.get(lookup_url, headers={"User-Agent": UA, "Referer": f"{origin}/"}, timeout=4.0)
        if l_resp.status_code == 200:
            try:
                data = l_resp.json()
                if "server_key" in data and data["server_key"]:
                    server_key = data["server_key"]
            except Exception:
                pass

        return f"https://{server_key}new.newkso.ru/{server_key}/{channel_key}/mono.m3u8", f"{origin}/"
    except Exception:
        return None


async def get_dlhd_events(client: httpx.AsyncClient) -> list[DLHDEvent]:
    events = []
    headers = {"User-Agent": UA, "Referer": f"{BASE_URL}/"}

    try:
        resp = await client.get(SCHEDULE_URL, headers=headers, timeout=10.0)
        if resp.status_code != 200:
            return events

        schedule_data = resp.json()
        now_utc = datetime.now(timezone.utc)

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
                    raw_channels = game.get("channels", [])

                    if not title or not raw_channels:
                        continue

                    event_dt = now_utc
                    m = re.search(r"(\d{1,2}):(\d{2})", time_str)
                    if m:
                        event_dt = now_utc.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)

                    if event_dt < now_utc - timedelta(hours=3):
                        continue
                    if event_dt > now_utc + timedelta(hours=5):
                        continue

                    channels = []
                    for ch in raw_channels:
                        cid = str(ch.get("channel_id") if isinstance(ch, dict) else ch)
                        cname = (ch.get("channel_name") if isinstance(ch, dict) else f"Stream {ch}") or "Stream"
                        if cid:
                            channels.append(DLHDChannel(channel_name=cname, channel_id=cid))

                    if channels:
                        norm_sport = clean_and_normalize_sport(sport_cat, title)
                        events.append(DLHDEvent(sport=norm_sport, title=title.strip(), start_dt=event_dt, channels=channels))

    except Exception as e:
        log.error(f"Error reading schedule: {e}")

    return events


async def scrape():
    log.info("Scraping DaddyLive...")
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=10.0, http2=True) as client:
        raw_events = await get_dlhd_events(client)
        if not raw_events:
            log.info("No active events found.")
            return

        unique_channel_ids = {ch.channel_id for ev in raw_events for ch in ev.channels}
        channel_map = {}

        async def fetch_channel(cid):
            async with HTTP_SEMAPHORE:
                res = await decode_dlhd_m3u8(client, cid)
                if res:
                    channel_map[cid] = res

        await asyncio.gather(*(fetch_channel(cid) for cid in unique_channel_ids))

        final_playlist = []
        for ev in raw_events:
            event_channels = []
            for ch in ev.channels:
                if ch.channel_id in channel_map:
                    url, ref = channel_map[ch.channel_id]
                    event_channels.append({
                        "channel_name": ch.channel_name,
                        "stream_type": "HLS",
                        "url": url,
                        "raw_link": f"{url}|Referer={ref}",
                        "headers": {"Referer": ref},
                        "drm_key": None
                    })

            if event_channels:
                start_bst = ev.start_dt.astimezone(BST_TZ)
                end_bst = start_bst + timedelta(hours=3)

                t_names = parse_team_names(ev.title)
                t_a = t_names["team_a"]
                t_b = t_names["team_b"]
                flag_a = get_real_team_badge(t_a, ev.sport)
                flag_b = get_real_team_badge(t_b, ev.sport)
                sub_cat = get_sub_category(ev.sport, ev.title)

                final_playlist.append({
                    "title": ev.title,
                    "sport": ev.sport,
                    "sub_category": sub_cat,
                    "status": determine_status(ev.start_dt),
                    "is_hot": False,
                    "slug": create_slug(ev.title),
                    "schedule": {
                        "timezone": "BST (UTC+6)",
                        "start_time_bst": start_bst.strftime("%b %d, %Y, %I:%M:%S %p"),
                        "end_time_bst": end_bst.strftime("%b %d, %Y, %I:%M:%S %p"),
                    },
                    "teams": {
                        "team_a": {"name": t_a, "flag": flag_a},
                        "team_b": {"name": t_b, "flag": flag_b}
                    },
                    "logo": flag_a,
                    "channels": event_channels,
                    "_timestamp": ev.start_dt.timestamp()
                })

        final_playlist.sort(key=lambda x: ({"LIVE": 0, "UPCOMING": 1, "ENDED": 2}.get(x["status"], 99), x["_timestamp"]))
        for it in final_playlist:
            it.pop("_timestamp", None)

        save_playlists(final_playlist)
        log.info("Scraping completed!")


if __name__ == "__main__":
    asyncio.run(scrape())
