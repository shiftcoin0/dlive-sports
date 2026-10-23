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
# প্রতিটি সাব-ক্যাটাগরির নিজস্ব অফিশিয়াল এইচডি লোগো ডিকশনারি (ESPN Open CDN)
# ব্রাউজারে লিংক দিলে কোনোদিন পেজ এরর আসবে না
# -----------------------------------------------------------------------------
SUBCATEGORY_LOGOS = {
    # Football / Soccer Leagues
    "Premier League": "https://a.espncdn.com/i/leaguelogos/soccer/500/23.png",
    "La Liga": "https://a.espncdn.com/i/leaguelogos/soccer/500/15.png",
    "Serie A": "https://a.espncdn.com/i/leaguelogos/soccer/500/12.png",
    "Bundesliga": "https://a.espncdn.com/i/leaguelogos/soccer/500/10.png",
    "Ligue 1": "https://a.espncdn.com/i/leaguelogos/soccer/500/9.png",
    "UEFA Champions League": "https://a.espncdn.com/i/leaguelogos/soccer/500/2.png",
    "UEFA Europa League": "https://a.espncdn.com/i/leaguelogos/soccer/500/2035.png",
    "UEFA Conference League": "https://a.espncdn.com/i/leaguelogos/soccer/500/20268.png",
    "FA Cup": "https://a.espncdn.com/i/leaguelogos/soccer/500/20.png",
    "Copa del Rey": "https://a.espncdn.com/i/leaguelogos/soccer/500/83.png",
    "EFL Cup": "https://a.espncdn.com/i/leaguelogos/soccer/500/22.png",
    "MLS": "https://a.espncdn.com/i/leaguelogos/soccer/500/19.png",
    "Saudi Pro League": "https://a.espncdn.com/i/leaguelogos/soccer/500/2844.png",
    "Copa Libertadores": "https://a.espncdn.com/i/leaguelogos/soccer/500/2038.png",
    "Football": "https://a.espncdn.com/i/leaguelogos/soccer/500/23.png",

    # Cricket
    "Indian Premier League (IPL)": "https://a.espncdn.com/i/leaguelogos/cricket/500/ipl.png",
    "Bangladesh Premier League (BPL)": "https://a.espncdn.com/i/leaguelogos/cricket/500/bpl.png",
    "Pakistan Super League (PSL)": "https://a.espncdn.com/i/leaguelogos/cricket/500/psl.png",
    "Big Bash League": "https://a.espncdn.com/i/leaguelogos/cricket/500/bbl.png",
    "The Hundred": "https://a.espncdn.com/i/leaguelogos/cricket/500/hundred.png",
    "ICC T20 World Cup": "https://a.espncdn.com/i/leaguelogos/cricket/500/t20wc.png",
    "ICC Cricket World Cup": "https://a.espncdn.com/i/leaguelogos/cricket/500/cwc.png",
    "Cricket": "https://a.espncdn.com/i/leaguelogos/cricket/500/ipl.png",

    # Basketball
    "NBA": "https://a.espncdn.com/i/teamlogos/leagues/500/nba.png",
    "WNBA": "https://a.espncdn.com/i/teamlogos/leagues/500/wnba.png",
    "EuroLeague": "https://a.espncdn.com/i/teamlogos/leagues/500/euroleague.png",
    "NCAA Basketball": "https://a.espncdn.com/i/teamlogos/ncaa/500/ncaa.png",
    "Basketball": "https://a.espncdn.com/i/teamlogos/leagues/500/nba.png",

    # Hockey
    "NHL": "https://a.espncdn.com/i/teamlogos/leagues/500/nhl.png",
    "Hockey": "https://a.espncdn.com/i/teamlogos/leagues/500/nhl.png",

    # Baseball
    "MLB": "https://a.espncdn.com/i/teamlogos/leagues/500/mlb.png",
    "Baseball": "https://a.espncdn.com/i/teamlogos/leagues/500/mlb.png",

    # American Football
    "NFL": "https://a.espncdn.com/i/teamlogos/leagues/500/nfl.png",
    "American Football": "https://a.espncdn.com/i/teamlogos/leagues/500/nfl.png",

    # Motorsport
    "Formula 1": "https://a.espncdn.com/i/teamlogos/racing/500/f1.png",
    "MotoGP": "https://a.espncdn.com/i/teamlogos/racing/500/motogp.png",
    "NASCAR": "https://a.espncdn.com/i/teamlogos/racing/500/nascar.png",
    "IndyCar": "https://a.espncdn.com/i/teamlogos/racing/500/indycar.png",
    "Motorsport": "https://a.espncdn.com/i/teamlogos/racing/500/f1.png",

    # Tennis
    "Wimbledon": "https://a.espncdn.com/i/teamlogos/tennis/500/wimbledon.png",
    "US Open": "https://a.espncdn.com/i/teamlogos/tennis/500/usopen.png",
    "French Open": "https://a.espncdn.com/i/teamlogos/tennis/500/rolandgarros.png",
    "Australian Open": "https://a.espncdn.com/i/teamlogos/tennis/500/ausopen.png",
    "ATP Tour": "https://a.espncdn.com/i/teamlogos/tennis/500/atp.png",
    "WTA Tour": "https://a.espncdn.com/i/teamlogos/tennis/500/wta.png",
    "Tennis": "https://a.espncdn.com/i/teamlogos/tennis/500/atp.png",

    # Combat / Wrestling
    "WWE SmackDown": "https://a.espncdn.com/i/teamlogos/mma/500/wwe.png",
    "WWE Raw": "https://a.espncdn.com/i/teamlogos/mma/500/wwe.png",
    "WWE NXT": "https://a.espncdn.com/i/teamlogos/mma/500/wwe.png",
    "WWE": "https://a.espncdn.com/i/teamlogos/mma/500/wwe.png",
    "AEW": "https://a.espncdn.com/i/teamlogos/mma/500/aew.png",
    "UFC": "https://a.espncdn.com/i/teamlogos/mma/500/ufc.png",
    "Boxing": "https://a.espncdn.com/i/teamlogos/mma/500/ufc.png",
    "Fighting": "https://a.espncdn.com/i/teamlogos/mma/500/ufc.png",
    "Wrestling": "https://a.espncdn.com/i/teamlogos/mma/500/wwe.png",

    # Rugby
    "Rugby": "https://a.espncdn.com/i/teamlogos/rugby/500/rugby.png",
}

# আন্তর্জাতিক দেশের সরাসরি জাতীয় পতাকা (FlagCDN)
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


# -----------------------------------------------------------------------------
# সাব-ক্যাটাগরি অনুযায়ী সরাসরি নিজস্ব লোগো নির্ধারণের ফাংশন
# -----------------------------------------------------------------------------
def get_category_logo(sub_category: str, sport: str) -> str:
    # ১. সাব-ক্যাটাগরির হুবহু নাম চেক
    if sub_category in SUBCATEGORY_LOGOS:
        return SUBCATEGORY_LOGOS[sub_category]

    sub_low = sub_category.lower()
    for cat_name, img_url in SUBCATEGORY_LOGOS.items():
        if cat_name.lower() in sub_low or sub_low in cat_name.lower():
            return img_url

    # ২. খেলার নাম অনুযায়ী নিজস্ব লোগো
    sp_low = sport.lower()
    for cat_name, img_url in SUBCATEGORY_LOGOS.items():
        if cat_name.lower() in sp_low:
            return img_url

    # ৩. স্পোর্ট টাইপ অনুযায়ী নির্দিষ্ট লোগো
    if "crick" in sp_low:
        return SUBCATEGORY_LOGOS["Cricket"]
    if "basket" in sp_low:
        return SUBCATEGORY_LOGOS["Basketball"]
    if "hock" in sp_low:
        return SUBCATEGORY_LOGOS["Hockey"]
    if "tenn" in sp_low:
        return SUBCATEGORY_LOGOS["Tennis"]
    if "rac" in sp_low or "motor" in sp_low:
        return SUBCATEGORY_LOGOS["Formula 1"]
    if "fight" in sp_low or "mma" in sp_low or "box" in sp_low:
        return SUBCATEGORY_LOGOS["UFC"]
    if "wrest" in sp_low:
        return SUBCATEGORY_LOGOS["WWE"]

    return SUBCATEGORY_LOGOS["Football"]


def get_team_flag_or_badge(team_name: str, category_logo: str) -> str:
    c_low = team_name.strip().lower()
    for country, flag_url in COUNTRY_FLAGS.items():
        if country in c_low:
            return flag_url
    return category_logo


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

    return clean.capitalize() or "Football"


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

TOP_CLUBS = {
    "Premier League": ["arsenal", "chelsea", "liverpool", "manchester united", "manchester city", "tottenham", "newcastle", "aston villa", "everton", "west ham", "brighton", "wolves", "fulham", "crystal palace", "brentford"],
    "La Liga": ["real madrid", "barcelona", "atletico madrid", "sevilla", "valencia", "athletic bilbao", "villarreal", "real betis", "real不断", "girona", "celta vigo", "mallorca", "osasuna", "getafe", "salzburg", "celje"],
    "Serie A": ["juventus", "inter milan", "ac milan", "napoli", "as roma", "lazio", "atalanta", "fiorentina"],
    "Bundesliga": ["bayern munich", "borussia dortmund", "dortmund", "bayer leverkusen", "leverkusen", "rb leipzig", "eintracht frankfurt"],
    "Ligue 1": ["psg", "paris saint-germain", "marseille", "monaco", "lyon", "lille"],
    "Indian Premier League (IPL)": ["chennai super kings", "csk", "mumbai indians", "mi", "royal challengers", "rcb", "kolkata knight riders", "kkr"],
    "NBA": ["lakers", "warriors", "celtics", "bulls", "heat", "bucks", "mavericks", "suns", "clippers"]
}


def get_sub_category(sport: str, title: str) -> str:
    combined_text = f"{sport} {title}".lower()

    for pattern, league_name in LEAGUE_PATTERNS:
        if re.search(pattern, combined_text):
            return league_name

    for league, teams in TOP_CLUBS.items():
        for t in teams:
            if t in combined_text:
                return league

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

        # ১. সকল চ্যানেল স্ট্রিম প্যারালালে ডিকোড করা (দ্রুতগতির জন্য)
        unique_channel_ids = {ch.channel_id for ev in raw_events for ch in ev.channels}
        log.info(f"Decoding {len(unique_channel_ids)} unique channels concurrently...")

        channel_map = {}

        async def fetch_channel(c_id: str):
            async with HTTP_SEMAPHORE:
                res = await decode_dlhd_m3u8(client, c_id)
                if res:
                    channel_map[c_id] = res

        await asyncio.gather(*(fetch_channel(cid) for cid in unique_channel_ids))

        # ২. চূড়ান্ত প্লেলিস্ট তৈরি (সাব-ক্যাটাগরি অনুযায়ী আসল লোগো সেট করা)
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

                # নির্ধারিত সাব-ক্যাটাগরি
                detected_sub_category = get_sub_category(ev.sport, ev.title)

                # ওই সাব-ক্যাটাগরির নিজস্ব অফিশিয়াল এইচডি লোগো
                category_official_logo = get_category_logo(detected_sub_category, ev.sport)

                # টিম এ এবং টিম বি এর লোগো নির্ধারণ
                team_a_flag = get_team_flag_or_badge(team_a_name, category_official_logo)
                team_b_flag = get_team_flag_or_badge(team_b_name, category_official_logo)

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
                    "logo": category_official_logo,  # ক্যাটাগরির নিজস্ব আসল লোগো
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
        log.info("Playlist generation completed with Official Sub-Category Logos!")


if __name__ == "__main__":
    asyncio.run(scrape())
