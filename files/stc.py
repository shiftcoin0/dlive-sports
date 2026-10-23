import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urljoin, urlsplit
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

    if any(k in comb for k in ["soccer", "fifa", "uefa", "la liga", "premier league", "serie a", "bundesliga"]):
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
    if any(k in combined for k in ["soccer", "football", "premier league", "la liga", "serie a", "bundesliga"]):
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


# -----------------------------------------------------------------------------
# DaddyLive অথেনটিকেশন ডাটা এক্সট্রাক্ট করার ফাংশন
# -----------------------------------------------------------------------------
def extract_auth_params(html: str) -> dict:
    params = {}

    # ১. Base64 এনকোডেড JSON বান্ডেল থেকে ডিকোড করা
    b64_matches = re.findall(r'["\']([A-Za-z0-9+/=]{40,})["\']', html)
    for b64 in b64_matches:
        try:
            decoded = base64.b64decode(b64).decode("utf-8", errors="ignore")
            if "b_host" in decoded or "b_ts" in decoded or "b_sig" in decoded:
                obj = json.loads(decoded)
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if isinstance(v, str):
                            try:
                                params[k] = base64.b64decode(v).decode("utf-8", errors="ignore")
                            except Exception:
                                params[k] = v
                        else:
                            params[k] = v
                    break
        except Exception:
            continue

    # ২. সরাসরি স্ক্রিপ্ট ভ্যারিয়েবল থেকে খোঁজা (var c, var d, var e)
    if not params.get("b_ts"):
        m = re.search(r'var\s+[a-zA-Z0-9_]*c\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'["\']?b_ts["\']?\s*[:=]\s*["\']([^"\']+)["\']', html)
        if m:
            params["b_ts"] = m.group(1)

    if not params.get("b_rnd"):
        m = re.search(r'var\s+[a-zA-Z0-9_]*d\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'["\']?b_rnd["\']?\s*[:=]\s*["\']([^"\']+)["\']', html)
        if m:
            params["b_rnd"] = m.group(1)

    if not params.get("b_sig"):
        m = re.search(r'var\s+[a-zA-Z0-9_]*e\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'["\']?b_sig["\']?\s*[:=]\s*["\']([^"\']+)["\']', html)
        if m:
            params["b_sig"] = m.group(1)

    if not params.get("b_host"):
        m = re.search(r'var\s+[a-zA-Z0-9_]*a\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'["\']?b_host["\']?\s*[:=]\s*["\']([^"\']+)["\']', html)
        if m:
            params["b_host"] = m.group(1)

    if not params.get("b_script"):
        m = re.search(r'var\s+[a-zA-Z0-9_]*b\s*=\s*["\']([^"\']+)["\']', html) or re.search(r'["\']?b_script["\']?\s*[:=]\s*["\']([^"\']+)["\']', html)
        if m:
            params["b_script"] = m.group(1)

    return params


# -----------------------------------------------------------------------------
# ডাইনামিক DaddyLive .m3u8 ডিকোডার (auth.php হ্যান্ডশেক ও টোকেনসহ)
# -----------------------------------------------------------------------------
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

    player_match = re.search(r'["\'](https?://[^"\']+\.m3u8(?:\?[^"\']*)?)["\']', player_resp.text)
    if player_match:
        return player_match.group(1), f"{player_origin}/"

    # চ্যানেল কী নির্ধারণ
    key_match = re.search(r'channel_id=([^"\'&\s]+)', player_resp.text) or re.search(r'channel_key\s*[:=]\s*["\']([^"\']+)["\']', player_resp.text)
    channel_key = key_match.group(1) if key_match else f"premium{clean_id}"

    # ধাপ ১: প্লেয়ার থেকে অথেনটিকেশন ডাটা বের করা
    auth_params = extract_auth_params(player_resp.text)

    token = None
    expires = None

    auth_ts = auth_params.get("b_ts")
    auth_rnd = auth_params.get("b_rnd")
    auth_sig = auth_params.get("b_sig")
    auth_host = (auth_params.get("b_host") or player_origin).rstrip("/")
    auth_script = auth_params.get("b_script") or "/auth.php"

    if auth_script.strip().lstrip("/") == "a.php":
        auth_script = "/auth.php"
    if not auth_script.startswith("/"):
        auth_script = "/" + auth_script

    # ধাপ ২: auth.php তে হ্যান্ডশেক রিকোয়েস্ট পাঠিয়ে টোকেন জেনারেট করানো
    if auth_ts and auth_rnd and auth_sig:
        sig_encoded = quote_plus(auth_sig)
        auth_url = f"{auth_host}{auth_script}?channel_id={channel_key}&ts={auth_ts}&rnd={auth_rnd}&sig={sig_encoded}"
        h_auth = {
            "User-Agent": UA,
            "Referer": f"{player_origin}/",
            "Origin": player_origin
        }
        auth_resp = await request(client, auth_url, headers=h_auth, timeout=5.0)
        if auth_resp and auth_resp.status_code == 200:
            try:
                a_data = auth_resp.json()
                if isinstance(a_data, dict):
                    token = a_data.get("token") or a_data.get("auth_token") or a_data.get("key") or a_data.get("auth") or a_data.get("sig")
                    expires = a_data.get("expires") or a_data.get("expiry") or a_data.get("ts")
            except Exception:
                pass

    # ধাপ ৩: server_lookup.php কল করে সার্ভার কী ও টোকেন গ্রহণ করা
    lookup_url = f"{player_origin}/server_lookup.php?channel_id={channel_key}"
    lookup_headers = {
        "User-Agent": UA,
        "Referer": f"{player_origin}/",
        "Origin": player_origin,
    }

    server_key = "topembed"
    l_resp = await request(client, lookup_url, headers=lookup_headers, timeout=5.0)

    if l_resp and l_resp.status_code == 200:
        try:
            data = l_resp.json()
            if isinstance(data, dict):
                if "url" in data and ".m3u8" in str(data["url"]):
                    return str(data["url"]), f"{player_origin}/"
                if "stream" in data and ".m3u8" in str(data["stream"]):
                    return str(data["stream"]), f"{player_origin}/"

                if data.get("server_key"):
                    server_key = data["server_key"]

                if not token:
                    token = data.get("token") or data.get("auth_token") or data.get("key") or data.get("auth") or data.get("sig")
                if not expires:
                    expires = data.get("expires") or data.get("expiry")
        except Exception:
            pass

    # ধাপ ৪: m3u8 লিঙ্কের সাথে টোকেন যুক্ত করা
    if "/" in server_key:
        parts = server_key.split("/")
        base_m3u8 = f"https://{parts[0]}.newkso.ru/{server_key}/{channel_key}/mono.m3u8"
    else:
        base_m3u8 = f"https://{server_key}new.newkso.ru/{server_key}/{channel_key}/mono.m3u8"

    query_parts = []
    if token:
        query_parts.append(f"token={token}")
    if expires:
        query_parts.append(f"expires={expires}")

    if query_parts:
        m3u8_url = f"{base_m3u8}?{'&'.join(query_parts)}"
    else:
        m3u8_url = base_m3u8

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
                if event_dt > now_utc + timedelta(hours=6):
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
        headers={"User-Agent": UA, "Referer": f"{BASE_URL}/"},
        follow_redirects=True,
        timeout=10.0,
        http2=True,
    ) as client:

        raw_events = await get_dlhd_events(client)
        if not raw_events:
            log.info("No active events found in window")
            return

        log.info(f"Processing {len(raw_events)} active DaddyLive event(s)...")

        # ১. সকল চ্যানেল স্ট্রিম প্যারালালে ডিকোড (auth হ্যান্ডশেক ও টোকেনসহ)
        unique_channel_ids = {ch.channel_id for ev in raw_events for ch in ev.channels}
        log.info(f"Decoding {len(unique_channel_ids)} unique channels with Auth Handshake...")

        channel_map = {}

        async def fetch_channel(c_id: str):
            async with HTTP_SEMAPHORE:
                res = await decode_dlhd_m3u8(client, c_id)
                if res:
                    channel_map[c_id] = res

        await asyncio.gather(*(fetch_channel(cid) for cid in unique_channel_ids))

        # ২. প্লেলিস্ট তৈরি
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
        log.info("Playlist generation completed with valid Tokens!")


if __name__ == "__main__":
    asyncio.run(scrape())
