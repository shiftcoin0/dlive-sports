import asyncio
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

BASE_URL = "https://topsurge.st"

# আপনার নির্দেশিত নির্দিষ্ট রেফারার ও অরিজিন
STREAM_REFERER = "https://matchli.st/"
STREAM_ORIGIN = "https://matchli.st"
STREAM_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"
STREAM_PIPE_HEADERS = f"User-Agent={STREAM_UA}&Referer={STREAM_REFERER}&Origin={STREAM_ORIGIN}"

TAG = "TOPSURGE"

JSON_OUTPUT = ROOT_DIR / "strmcntr.json"
M3U_OUTPUT = ROOT_DIR / "strmcntr.m3u"
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

HTTP_SEMAPHORE = asyncio.Semaphore(12)
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
# টিম লোগো সিডিএন (কোনো ম্যাচে যেন লোগো null না থাকে)
# -----------------------------------------------------------------------------
KNOWN_TEAM_LOGOS = {
    # Premier League
    "arsenal": "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png",
    "chelsea": "https://images.fotmob.com/image_resources/logo/teamlogo/10260.png",
    "liverpool": "https://images.fotmob.com/image_resources/logo/teamlogo/8650.png",
    "manchester united": "https://images.fotmob.com/image_resources/logo/teamlogo/10267.png",
    "manchester city": "https://images.fotmob.com/image_resources/logo/teamlogo/8456.png",
    "tottenham": "https://images.fotmob.com/image_resources/logo/teamlogo/8586.png",
    "newcastle": "https://images.fotmob.com/image_resources/logo/teamlogo/10261.png",
    "aston villa": "https://images.fotmob.com/image_resources/logo/teamlogo/10252.png",

    # La Liga
    "real madrid": "https://images.fotmob.com/image_resources/logo/teamlogo/8633.png",
    "barcelona": "https://images.fotmob.com/image_resources/logo/teamlogo/8634.png",
    "atletico madrid": "https://images.fotmob.com/image_resources/logo/teamlogo/9906.png",

    # Bundesliga
    "bayern munich": "https://images.fotmob.com/image_resources/logo/teamlogo/9823.png",
    "borussia dortmund": "https://images.fotmob.com/image_resources/logo/teamlogo/9789.png",
    "dortmund": "https://images.fotmob.com/image_resources/logo/teamlogo/9789.png",
    "eintracht frankfurt": "https://images.fotmob.com/image_resources/logo/teamlogo/9810.png",
    "frankfurt": "https://images.fotmob.com/image_resources/logo/teamlogo/9810.png",
    "bayer leverkusen": "https://images.fotmob.com/image_resources/logo/teamlogo/8178.png",

    # Serie A
    "juventus": "https://images.fotmob.com/image_resources/logo/teamlogo/9885.png",
    "inter milan": "https://images.fotmob.com/image_resources/logo/teamlogo/8636.png",
    "ac milan": "https://images.fotmob.com/image_resources/logo/teamlogo/8564.png",

    # Cricket
    "csk": "https://images.fotmob.com/image_resources/logo/teamlogo/6573.png",
    "mi": "https://images.fotmob.com/image_resources/logo/teamlogo/6576.png",
    "rcb": "https://images.fotmob.com/image_resources/logo/teamlogo/6574.png",

    # Flags
    "argentina": "https://flagcdn.com/w320/ar.png",
    "brazil": "https://flagcdn.com/w320/br.png",
    "england": "https://flagcdn.com/w320/gb-eng.png",
    "france": "https://flagcdn.com/w320/fr.png",
    "germany": "https://flagcdn.com/w320/de.png",
    "india": "https://flagcdn.com/w320/in.png",
    "pakistan": "https://flagcdn.com/w320/pk.png",
    "bangladesh": "https://flagcdn.com/w320/bd.png",
}

DEFAULT_SPORT_ICONS = {
    "football": "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png",
    "cricket": "https://cdn-icons-png.flaticon.com/512/1071/1071178.png",
    "basketball": "https://cdn-icons-png.flaticon.com/512/889/889508.png",
    "tennis": "https://cdn-icons-png.flaticon.com/512/709/709605.png",
    "hockey": "https://cdn-icons-png.flaticon.com/512/2622/2622830.png",
    "motorsport": "https://cdn-icons-png.flaticon.com/512/2418/2418779.png",
}


def get_team_badge(team_name: str, sport: str = "Football") -> str:
    t_clean = team_name.lower().strip()
    for k, v in KNOWN_TEAM_LOGOS.items():
        if k in t_clean or t_clean in k:
            return v
    for sp_k, sp_v in DEFAULT_SPORT_ICONS.items():
        if sp_k in sport.lower():
            return sp_v
    return DEFAULT_SPORT_ICONS["football"]


# -----------------------------------------------------------------------------
# সাব-ক্যাটাগরি নির্ধারণ
# -----------------------------------------------------------------------------
LEAGUE_PATTERNS = [
    (r"\bpremier league\b|\bepl\b", "Premier League"),
    (r"\bla liga\b|\blaliga\b", "La Liga"),
    (r"\bserie a\b", "Serie A"),
    (r"\bbundesliga\b", "Bundesliga"),
    (r"\bligue 1\b", "Ligue 1"),
    (r"\bchampions league\b|\bucl\b", "UEFA Champions League"),
    (r"\beuropa league\b|\buel\b", "UEFA Europa League"),
    (r"\bipl\b|\bindian premier league\b", "Indian Premier League (IPL)"),
    (r"\bbpl\b", "Bangladesh Premier League (BPL)"),
    (r"\bnba\b", "NBA"),
    (r"\bnhl\b", "NHL"),
    (r"\bnfl\b", "NFL"),
    (r"\bufc\b", "UFC"),
    (r"\bwwe\b|\bsmackdown\b|\braw\b", "WWE"),
    (r"\bformula 1\b|\bf1\b", "Formula 1"),
]


def clean_sport_name(sport_raw: str) -> str:
    s = re.sub(r"<[^>]+>", "", sport_raw).strip().lower()
    if "american football" in s or "nfl" in s:
        return "American Football"
    if "football" in s or "soccer" in s:
        return "Football"
    if "basketball" in s:
        return "Basketball"
    if "hockey" in s:
        return "Hockey"
    if "cricket" in s:
        return "Cricket"
    if "tennis" in s:
        return "Tennis"
    if "motorsport" in s or "racing" in s:
        return "Motorsport"
    return sport_raw.title()


def get_sub_category(sport: str, league_raw: str, title: str) -> str:
    comb = f"{sport} {league_raw} {title}".lower()
    for pat, league in LEAGUE_PATTERNS:
        if re.search(pat, comb):
            return league
    if league_raw and len(league_raw.strip()) > 2:
        return league_raw.strip()
    return sport.capitalize()


def parse_team_names(title: str) -> dict[str, str]:
    delimiters = [" vs. ", " vs ", " v ", " - ", " @ ", " at "]
    for d in delimiters:
        if d in title.lower():
            parts = re.split(re.escape(d), title, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return {
                    "team_a": parts[0].strip(),
                    "team_b": parts[1].strip()
                }
    return {
        "team_a": title.strip(),
        "team_b": title.strip()
    }


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
        logo = ev.get("logo") or ""
        status = ev.get("status", "")

        for ch in ev.get("channels", []):
            url = ch.get("url")
            raw_link = ch.get("raw_link") or url
            if not url:
                continue

            ch_name = ch.get("channel_name", "Stream")
            status_lbl = f" [{status}]" if status else ""
            display_name = f"[{sub_cat}] {ev['title']} | {ch_name}{status_lbl}"

            m3u_lines.append(f'#EXTINF:-1 tvg-id="Live.Event.us" tvg-logo="{logo}" group-title="{sub_cat}",{display_name}')
            m3u_lines.append(f'#EXTVLCOPT:http-referrer={STREAM_REFERER}')
            m3u_lines.append(f'#EXTVLCOPT:http-origin={STREAM_ORIGIN}')
            m3u_lines.append(f'#EXTVLCOPT:http-user-agent={STREAM_UA}')
            m3u_lines.append(raw_link)

    M3U_OUTPUT.write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")
    log.info(f"Saved M3U: {M3U_OUTPUT}")


# -----------------------------------------------------------------------------
# Topsurge Player থেকে সরাসরি m3u8 স্ট্রিম বের করার ডিকোডার
# -----------------------------------------------------------------------------
async def resolve_topsurge_stream(client: httpx.AsyncClient, player_url: str) -> str | None:
    if not player_url:
        return None

    if ".m3u8" in player_url:
        return player_url

    headers = {
        "User-Agent": STREAM_UA,
        "Referer": f"{BASE_URL}/",
    }

    try:
        resp = await client.get(player_url, headers=headers, timeout=6.0)
        if resp.status_code != 200:
            return None

        content = resp.text

        # ১. সোর্সে সরাসরি m3u8 লিংক আছে কিনা
        m3u8_m = re.search(r'["\'](https?://[^"\'\s]+\.m3u8[^"\'\s]*)["\']', content)
        if m3u8_m:
            return m3u8_m.group(1)

        # ২. <iframe> ট্যাগ খোঁজা
        soup = HTMLParser(resp.content)
        iframe = soup.css_first("iframe")
        iframe_src = iframe.attributes.get("src") if iframe else None

        if not iframe_src:
            src_m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', content, re.IGNORECASE)
            if src_m:
                iframe_src = src_m.group(1)

        if iframe_src:
            if iframe_src.startswith("//"):
                iframe_src = "https:" + iframe_src
            elif iframe_src.startswith("/"):
                iframe_src = urljoin(player_url, iframe_src)

            if ".m3u8" in iframe_src:
                return iframe_src

            # আইফ্রেম পেজে ঢুকে m3u8 বের করা
            sub_headers = {"User-Agent": STREAM_UA, "Referer": STREAM_REFERER}
            if_resp = await client.get(iframe_src, headers=sub_headers, timeout=5.0)
            if if_resp.status_code == 200:
                sub_text = if_resp.text
                sub_m3u8 = re.search(r'["\'](https?://[^"\'\s]+\.m3u8[^"\'\s]*)["\']', sub_text)
                if sub_m3u8:
                    return sub_m3u8.group(1)

        # ৩. JS কনফিগারেশনের ভেতর স্ট্রিম URL থাকলে
        js_m = re.search(r'(?:file|source|src)\s*[:=]\s*["\'](https?://[^"\'\s]+)["\']', content, re.IGNORECASE)
        if js_m:
            return js_m.group(1)

    except Exception as e:
        log.debug(f"Error resolving stream from {player_url}: {e}")

    return None


@dataclass(slots=True)
class TopsurgeEvent:
    title: str
    sport: str
    sub_cat: str
    start_dt: datetime
    channels: list[tuple[str, str]]


# -----------------------------------------------------------------------------
# Topsurge ওয়েবসাইট থেকে ইভেন্ট তালিকা সংগ্রহ
# -----------------------------------------------------------------------------
async def get_topsurge_events(client: httpx.AsyncClient) -> list[TopsurgeEvent]:
    events = []
    headers = {"User-Agent": STREAM_UA, "Referer": f"{BASE_URL}/"}

    try:
        resp = await client.get(f"{BASE_URL}/", headers=headers, timeout=10.0)
        if resp.status_code != 200:
            log.error(f"Failed to fetch topsurge home page: {resp.status_code}")
            return events

        soup = HTMLParser(resp.content)
        now_utc = datetime.now(timezone.utc)

        # কার্ড বা রো খোঁজা
        rows = soup.css(".match, .game, .card, div[class*='match'], div[class*='row']")
        if not rows:
            rows = soup.css("tr, li, .event")

        for row in rows:
            text_content = row.text(strip=True)
            if "vs" not in text_content.lower() and "at" not in text_content.lower():
                continue

            # টাইটেল বের করা
            title_el = row.css_first("h3, h4, .title, strong, a")
            if not title_el:
                continue

            event_title = title_el.text(strip=True)
            if not event_title or ("vs" not in event_title.lower() and "at" not in event_title.lower()):
                continue

            # সময় ও মেটাডাটা (যেমন: 01:00 PM • England League Cup • Football)
            meta_str = ""
            for el in row.css("p, span, .meta, .info"):
                t = el.text(strip=True)
                if "•" in t or ":" in t:
                    meta_str = t
                    break

            sport = "Football"
            league = "Live"
            event_dt = now_utc

            if meta_str:
                parts = [p.strip() for p in meta_str.split("•")]
                if len(parts) >= 3:
                    time_p, league_p, sport_p = parts[0], parts[1], parts[2]
                    sport = clean_sport_name(sport_p)
                    league = league_p
                    time_m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", time_p, re.IGNORECASE)
                    if time_m:
                        h = int(time_m.group(1))
                        m = int(time_m.group(2))
                        ampm = time_m.group(3)
                        if ampm and ampm.upper() == "PM" and h < 12:
                            h += 12
                        elif ampm and ampm.upper() == "AM" and h == 12:
                            h = 0
                        event_dt = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
                elif len(parts) == 2:
                    league = parts[0]
                    sport = clean_sport_name(parts[1])

            # স্ট্রিম লিংক খোঁজা
            channels = []
            for a in row.css("a"):
                href = a.attributes.get("href")
                c_name = a.text(strip=True)
                if href and ("player.php" in href or "stream" in href or "watch" in href):
                    full_link = urljoin(BASE_URL, href)
                    channels.append((c_name or "Stream", full_link))

            if channels:
                events.append(
                    TopsurgeEvent(
                        title=event_title,
                        sport=sport,
                        sub_cat=get_sub_category(sport, league, event_title),
                        start_dt=event_dt,
                        channels=channels
                    )
                )

    except Exception as e:
        log.error(f"Error scraping Topsurge: {e}")

    return events


# -----------------------------------------------------------------------------
# মূল স্ক্র্যাপার প্রসেস
# -----------------------------------------------------------------------------
async def scrape():
    log.info(f"Scraping from {BASE_URL} with Referer: {STREAM_REFERER}...")

    headers = {
        "User-Agent": STREAM_UA,
        "Referer": STREAM_REFERER,
        "Origin": STREAM_ORIGIN,
    }

    async with httpx.AsyncClient(headers=headers, timeout=12.0) as client:
        raw_events = await get_topsurge_events(client)
        if not raw_events:
            log.info("No active events found on Topsurge.")
            return

        log.info(f"Found {len(raw_events)} events. Resolving streams concurrently...")

        # সকল প্লেয়ার লিঙ্ক থেকে m3u8 স্ট্রিম বের করা
        unique_links = set()
        for ev in raw_events:
            for _, link in ev.channels:
                unique_links.add(link)

        stream_cache = {}
        async def fetch_stream(p_url):
            async with HTTP_SEMAPHORE:
                m3u8 = await resolve_topsurge_stream(client, p_url)
                if m3u8:
                    stream_cache[p_url] = m3u8

        await asyncio.gather(*(fetch_stream(u) for u in unique_links))

        final_playlist = []

        for ev in raw_events:
            event_channels = []
            for cname, link in ev.channels:
                if link in stream_cache:
                    m3u8_url = stream_cache[link]
                    raw_link = f"{m3u8_url}|{STREAM_PIPE_HEADERS}"

                    event_channels.append({
                        "channel_name": cname,
                        "stream_type": "HLS",
                        "url": m3u8_url,
                        "raw_link": raw_link,
                        "headers": {
                            "User-Agent": STREAM_UA,
                            "Referer": STREAM_REFERER,
                            "Origin": STREAM_ORIGIN,
                        },
                        "drm_key": None
                    })

            if event_channels:
                start_bst = ev.start_dt.astimezone(BST_TZ)
                end_bst = start_bst + timedelta(hours=3)

                t_names = parse_team_names(ev.title)
                t_a = t_names["team_a"]
                t_b = t_names["team_b"]

                flag_a = get_team_badge(t_a, ev.sport)
                flag_b = get_team_badge(t_b, ev.sport)

                final_playlist.append({
                    "title": ev.title,
                    "sport": ev.sport,
                    "sub_category": ev.sub_cat,
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

        # সর্টিং (LIVE সবার উপরে)
        final_playlist.sort(key=lambda x: ({"LIVE": 0, "UPCOMING": 1, "ENDED": 2}.get(x["status"], 99), x["_timestamp"]))
        for it in final_playlist:
            it.pop("_timestamp", None)

        save_playlists(final_playlist)
        log.info(f"Generated {len(final_playlist)} events from Topsurge!")


if __name__ == "__main__":
    asyncio.run(scrape())
