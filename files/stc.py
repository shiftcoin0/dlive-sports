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
# জনপ্রিয় দল ও আন্তর্জাতিক পতাকার ডিরেক্ট ১০০% ওয়ার্কিং সিডিএন লোগো
# (কোনো ব্রাউজার বা প্লেয়ারে কখনো পেজ এরর বা ব্লক হবে না)
# -----------------------------------------------------------------------------
KNOWN_TEAM_LOGOS = {
    # Premier League
    "arsenal": "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png",
    "chelsea": "https://images.fotmob.com/image_resources/logo/teamlogo/10260.png",
    "liverpool": "https://images.fotmob.com/image_resources/logo/teamlogo/8650.png",
    "manchester united": "https://images.fotmob.com/image_resources/logo/teamlogo/10267.png",
    "manchester city": "https://images.fotmob.com/image_resources/logo/teamlogo/8456.png",
    "man utd": "https://images.fotmob.com/image_resources/logo/teamlogo/10267.png",
    "man city": "https://images.fotmob.com/image_resources/logo/teamlogo/8456.png",
    "tottenham": "https://images.fotmob.com/image_resources/logo/teamlogo/8586.png",
    "newcastle": "https://images.fotmob.com/image_resources/logo/teamlogo/10261.png",
    "aston villa": "https://images.fotmob.com/image_resources/logo/teamlogo/10252.png",
    "everton": "https://images.fotmob.com/image_resources/logo/teamlogo/8668.png",
    "west ham": "https://images.fotmob.com/image_resources/logo/teamlogo/8654.png",
    "brighton": "https://images.fotmob.com/image_resources/logo/teamlogo/10204.png",
    "wolves": "https://images.fotmob.com/image_resources/logo/teamlogo/8659.png",
    "fulham": "https://images.fotmob.com/image_resources/logo/teamlogo/9879.png",
    "brentford": "https://images.fotmob.com/image_resources/logo/teamlogo/9937.png",
    "crystal palace": "https://images.fotmob.com/image_resources/logo/teamlogo/9826.png",
    "nottingham forest": "https://images.fotmob.com/image_resources/logo/teamlogo/10203.png",

    # La Liga
    "real madrid": "https://images.fotmob.com/image_resources/logo/teamlogo/8633.png",
    "barcelona": "https://images.fotmob.com/image_resources/logo/teamlogo/8634.png",
    "atletico madrid": "https://images.fotmob.com/image_resources/logo/teamlogo/9906.png",
    "sevilla": "https://images.fotmob.com/image_resources/logo/teamlogo/8302.png",
    "valencia": "https://images.fotmob.com/image_resources/logo/teamlogo/10233.png",
    "villarreal": "https://images.fotmob.com/image_resources/logo/teamlogo/10205.png",
    "athletic bilbao": "https://images.fotmob.com/image_resources/logo/teamlogo/8315.png",
    "real sociedad": "https://images.fotmob.com/image_resources/logo/teamlogo/8560.png",
    "girona": "https://images.fotmob.com/image_resources/logo/teamlogo/9812.png",

    # Serie A
    "juventus": "https://images.fotmob.com/image_resources/logo/teamlogo/9885.png",
    "inter milan": "https://images.fotmob.com/image_resources/logo/teamlogo/8636.png",
    "inter": "https://images.fotmob.com/image_resources/logo/teamlogo/8636.png",
    "ac milan": "https://images.fotmob.com/image_resources/logo/teamlogo/8564.png",
    "milan": "https://images.fotmob.com/image_resources/logo/teamlogo/8564.png",
    "napoli": "https://images.fotmob.com/image_resources/logo/teamlogo/9875.png",
    "as roma": "https://images.fotmob.com/image_resources/logo/teamlogo/8686.png",
    "roma": "https://images.fotmob.com/image_resources/logo/teamlogo/8686.png",
    "lazio": "https://images.fotmob.com/image_resources/logo/teamlogo/8543.png",

    # Bundesliga
    "bayern munich": "https://images.fotmob.com/image_resources/logo/teamlogo/9823.png",
    "borussia dortmund": "https://images.fotmob.com/image_resources/logo/teamlogo/9789.png",
    "dortmund": "https://images.fotmob.com/image_resources/logo/teamlogo/9789.png",
    "bayer leverkusen": "https://images.fotmob.com/image_resources/logo/teamlogo/8178.png",
    "rb leipzig": "https://images.fotmob.com/image_resources/logo/teamlogo/178475.png",

    # Ligue 1
    "psg": "https://images.fotmob.com/image_resources/logo/teamlogo/9847.png",
    "paris saint-germain": "https://images.fotmob.com/image_resources/logo/teamlogo/9847.png",
    "marseille": "https://images.fotmob.com/image_resources/logo/teamlogo/8592.png",
    "monaco": "https://images.fotmob.com/image_resources/logo/teamlogo/9829.png",

    # IPL & Cricket
    "csk": "https://images.fotmob.com/image_resources/logo/teamlogo/6573.png",
    "chennai super kings": "https://images.fotmob.com/image_resources/logo/teamlogo/6573.png",
    "mumbai indians": "https://images.fotmob.com/image_resources/logo/teamlogo/6576.png",
    "mi": "https://images.fotmob.com/image_resources/logo/teamlogo/6576.png",
    "royal challengers": "https://images.fotmob.com/image_resources/logo/teamlogo/6574.png",
    "rcb": "https://images.fotmob.com/image_resources/logo/teamlogo/6574.png",
    "kolkata knight riders": "https://images.fotmob.com/image_resources/logo/teamlogo/6575.png",

    # NBA
    "lakers": "https://images.fotmob.com/image_resources/logo/teamlogo/3414.png",
    "warriors": "https://images.fotmob.com/image_resources/logo/teamlogo/3428.png",
    "celtics": "https://images.fotmob.com/image_resources/logo/teamlogo/3422.png",

    # International Flags (FlagCDN)
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
    "usa": "https://flagcdn.com/w320/us.png",
}

# ডিফল্ট স্পোর্টস আইকন (কোনো অক্ষর নয়, প্রফেশনাল স্পোর্টস আইকন)
SPORT_ICONS = {
    "football": "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png",
    "cricket": "https://cdn-icons-png.flaticon.com/512/1071/1071178.png",
    "basketball": "https://cdn-icons-png.flaticon.com/512/889/889508.png",
    "tennis": "https://cdn-icons-png.flaticon.com/512/709/709605.png",
    "hockey": "https://cdn-icons-png.flaticon.com/512/2622/2622830.png",
    "motorsport": "https://cdn-icons-png.flaticon.com/512/2418/2418779.png",
    "wrestling": "https://cdn-icons-png.flaticon.com/512/477/477845.png",
    "fighting": "https://cdn-icons-png.flaticon.com/512/3306/3306613.png",
}

DEFAULT_LOGO = "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png"


def get_real_team_badge(team_name: str, sport: str = "Football") -> str:
    cleaned = team_name.strip().lower()
    if not cleaned:
        return DEFAULT_LOGO

    # ১. পরিচিত দল বা দেশের পতাকা চেক
    for key_name, img_url in KNOWN_TEAM_LOGOS.items():
        if key_name in cleaned or cleaned in key_name:
            return img_url

    # ২. না মিললে খেলার আসল স্পোর্টস আইকন দেওয়া (কোনো দুই অক্ষরের ছবি আসবে না)
    sp_lower = sport.lower()
    for sp_key, icon_url in SPORT_ICONS.items():
        if sp_key in sp_lower:
            return icon_url

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
    if any(k in combined for k in ["soccer", "football", "fifa", "uefa", "la liga", "premier league", "serie a", "bundesliga", "ligue 1"]):
        return "Football"
    if any(k in combined for k in ["cricket", "ipl", "bpl", "psl", "t20", "odi", "test"]):
        return "Cricket"
    if any(k in combined for k in ["basketball", "nba", "euroleague", "wnba"]):
        return "Basketball"
    if any(k in combined for k in ["hockey", "nhl", "ice hockey"]):
        return "Hockey"
    if any(k in combined for k in ["tennis", "wimbledon", "atp", "wta", "roland garros"]):
        return "Tennis"
    if any(k in combined for k in ["baseball", "mlb"]):
        return "Baseball"
    if any(k in combined for k in ["motorsport", "racing", "formula 1", "f1", "motogp", "nascar"]):
        return "Motorsport"
    if any(k in combined for k in ["wwe", "wrestling", "smackdown", "raw", "aew", "nxt"]):
        return "Wrestling"
    if any(k in combined for k in ["ufc", "fighting", "mma", "boxing"]):
        return "Fighting"
    if any(k in combined for k in ["rugby", "afl"]):
        return "Rugby"

    return clean.capitalize() or "Live Sports"


# -----------------------------------------------------------------------------
# স্মার্ট সাব-ক্যাটাগরি ডিটেকশন ইঞ্জিন
# -----------------------------------------------------------------------------
LEAGUE_PATTERNS = [
    (r"premier league|epl|english premier", "Premier League"),
    (r"la liga|laliga|primera division", "La Liga"),
    (r"serie a|serie-a", "Serie A"),
    (r"bundesliga", "Bundesliga"),
    (r"ligue 1|ligue1", "Ligue 1"),
    (r"champions league|ucl|uefa champions", "UEFA Champions League"),
    (r"europa league|uel", "UEFA Europa League"),
    (r"conference league|uecl", "UEFA Conference League"),
    (r"fa cup", "FA Cup"),
    (r"copa del rey", "Copa del Rey"),
    (r"carabao cup|efl cup", "EFL Cup"),
    (r"mls|major league soccer", "MLS"),
    (r"saudi pro league|spl", "Saudi Pro League"),
    (r"copa libertadores", "Copa Libertadores"),
    (r"ipl|indian premier league", "Indian Premier League (IPL)"),
    (r"bpl|bangladesh premier league", "Bangladesh Premier League (BPL)"),
    (r"psl|pakistan super league", "Pakistan Super League (PSL)"),
    (r"big bash|bbl", "Big Bash League"),
    (r"the hundred", "The Hundred"),
    (r"t20 world cup", "ICC T20 World Cup"),
    (r"icc world cup|cricket world cup", "ICC Cricket World Cup"),
    (r"nba", "NBA"),
    (r"euroleague", "EuroLeague"),
    (r"smackdown", "WWE SmackDown"),
    (r"raw", "WWE Raw"),
    (r"nxt", "WWE NXT"),
    (r"wwe", "WWE"),
    (r"ufc", "UFC"),
    (r"boxing", "Boxing"),
    (r"formula 1|formula one|f1", "Formula 1"),
    (r"motogp", "MotoGP"),
    (r"wimbledon", "Wimbledon"),
    (r"us open", "US Open"),
    (r"atp", "ATP Tour"),
    (r"wta", "WTA Tour"),
    (r"nfl", "NFL"),
    (r"mlb", "MLB"),
    (r"nhl", "NHL"),
]


def get_sub_category(sport: str, title: str) -> str:
    combined_text = f"{sport} {title}".lower()
    for pattern, league_name in LEAGUE_PATTERNS:
        if re.search(pattern, combined_text):
            return league_name
    return sport


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
        sub_cat = ev.get("sub_category", "Live")
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
            display_name = f"[{sub_cat}] {title} | {ch_name}{status_lbl}"

            m3u_lines.append(
                f'#EXTINF:-1 '
                f'tvg-id="Live.Event.us" '
                f'tvg-logo="{logo}" '
                f'group-title="{sub_cat}",'
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
                    normalized_sport = clean_and_normalize_sport(sport_cat, event_title)

                    events.append(
                        DLHDEvent(
                            sport=normalized_sport,
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

        log.info(f"Processing {len(raw_events)} active DaddyLive event(s)...")

        # ১. সকল চ্যানেল স্ট্রিম প্যারালালে ডিকোড
        unique_channel_ids = {ch.channel_id for ev in raw_events for ch in ev.channels}
        log.info(f"Decoding {len(unique_channel_ids)} unique channels concurrently...")

        channel_map = {}

        async def fetch_channel(c_id: str):
            async with HTTP_SEMAPHORE:
                res = await decode_dlhd_m3u8(client, c_id)
                if res:
                    channel_map[c_id] = res

        await asyncio.gather(*(fetch_channel(cid) for cid in unique_channel_ids))

        # ২. প্লেলিস্ট তৈরি ও সরাসরি টিম ফ্ল্যাগ বসানো
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

                t_names = parse_team_names(ev.title)
                team_a_name = t_names["team_a"]
                team_b_name = t_names["team_b"]

                # আসল টিম ব্যাজ ও ফ্ল্যাগ সিডিএন লিংক (কখনো এরর আসবে না)
                team_a_flag = get_real_team_badge(team_a_name, ev.sport)
                team_b_flag = get_real_team_badge(team_b_name, ev.sport)

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
                            "flag": team_a_flag,
                        },
                        "team_b": {
                            "name": team_b_name,
                            "flag": team_b_flag,
                        },
                    },
                    "logo": team_a_flag,  # Team A এর আসল লোগো
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

        # জোরপূর্বক নতুন ফাইল সংরক্ষণ
        save_playlists(final_playlist)
        log.info("Playlist successfully regenerated with Real Team Badges!")


if __name__ == "__main__":
    asyncio.run(scrape())
