import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

# রিপোজিটরি রুট ডিরেক্টরি
ROOT_DIR = Path(__file__).resolve().parent.parent

BASE_URL = "https://streamcenter.st"
API_SPORTS = f"{BASE_URL}/api/sports"

TAG = "STRMCNTR"

# -----------------------------------------------------------------------------
# ব্রাউজারের স্ক্রিনশট অনুযায়ী সঠিক হেডার ও রেফারার
# -----------------------------------------------------------------------------
STREAM_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"
STREAM_REFERER = "https://embedcenter.st/"
STREAM_ORIGIN = "https://embedcenter.st"
STREAM_PIPE_HEADERS = f"User-Agent={STREAM_UA}&Referer={STREAM_REFERER}&Origin={STREAM_ORIGIN}"

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
# সরাসরি StreamCenter API অ্যাসেট আইডি থেকে ইমেজ (Poster / Badge) তৈরির ফাংশন
# -----------------------------------------------------------------------------
def format_api_asset(raw_asset, asset_type="badge") -> str | None:
    if not raw_asset:
        return None

    if isinstance(raw_asset, dict):
        raw_asset = (
            raw_asset.get("assetId")
            or raw_asset.get("id")
            or raw_asset.get("url")
            or raw_asset.get("src")
            or raw_asset.get("poster")
            or raw_asset.get("badge")
        )
        if not raw_asset:
            return None

    val = str(raw_asset).strip()
    if not val or val.lower() in ["none", "null", "undefined"]:
        return None

    if val.startswith("http://") or val.startswith("https://"):
        return val

    if val.startswith("/"):
        return f"{BASE_URL}{val}"

    clean_id = val[:-5] if val.endswith(".webp") else val
    return f"{BASE_URL}/api/images/{asset_type}/{clean_id}.webp"


def extract_api_badges(m: dict, sport_default_poster: str | None = None) -> tuple[str | None, str | None, str | None]:
    event_poster = format_api_asset(m.get("poster"), "poster")
    event_badge = format_api_asset(m.get("badge"), "badge")

    flag_a = None
    flag_b = None

    teams_data = m.get("teams") or m.get("competitors")
    if isinstance(teams_data, dict):
        home = teams_data.get("home") or teams_data.get("team1") or teams_data.get("team_a")
        away = teams_data.get("away") or teams_data.get("team2") or teams_data.get("team_b")
        if isinstance(home, dict):
            flag_a = format_api_asset(home.get("badge") or home.get("logo") or home.get("image"), "badge")
        if isinstance(away, dict):
            flag_b = format_api_asset(away.get("badge") or away.get("logo") or away.get("image"), "badge")
    elif isinstance(teams_data, list) and len(teams_data) >= 2:
        t1, t2 = teams_data[0], teams_data[1]
        if isinstance(t1, dict):
            flag_a = format_api_asset(t1.get("badge") or t1.get("logo") or t1.get("image"), "badge")
        if isinstance(t2, dict):
            flag_b = format_api_asset(t2.get("badge") or t2.get("logo") or t2.get("image"), "badge")

    if not flag_a:
        flag_a = format_api_asset(m.get("homeBadge"), "badge") or format_api_asset(m.get("team1Badge"), "badge")
    if not flag_b:
        flag_b = format_api_asset(m.get("awayBadge"), "badge") or format_api_asset(m.get("team2Badge"), "badge")

    if not flag_a:
        flag_a = event_badge
    if not flag_b:
        flag_b = event_badge

    main_event_logo = event_poster or event_badge or flag_a or flag_b or sport_default_poster

    if not flag_a:
        flag_a = main_event_logo
    if not flag_b:
        flag_b = main_event_logo

    return main_event_logo, flag_a, flag_b


# -----------------------------------------------------------------------------
# সাব-ক্যাটাগরি নির্ধারণ
# -----------------------------------------------------------------------------
TOP_LEAGUE_CLUBS = {
    "Bundesliga": [
        "dortmund", "frankfurt", "bayern", "leverkusen", "leipzig", "stuttgart",
        "wolfsburg", "mönchengladbach", "freiburg", "bremen", "augsburg", "mainz"
    ],
    "Premier League": [
        "arsenal", "chelsea", "liverpool", "manchester united", "manchester city",
        "tottenham", "newcastle", "aston villa", "everton", "west ham", "brighton", "wolves", "fulham"
    ],
    "La Liga": [
        "real madrid", "barcelona", "atletico madrid", "sevilla", "valencia", "villarreal",
        "real betis", "real sociedad", "girona", "athletic bilbao"
    ],
    "Serie A": [
        "juventus", "inter milan", "ac milan", "napoli", "as roma", "lazio", "atalanta", "fiorentina"
    ],
    "Ligue 1": [
        "psg", "paris saint-germain", "marseille", "monaco", "lyon", "lille"
    ],
    "Indian Premier League (IPL)": [
        "chennai super kings", "csk", "mumbai indians", "rcb", "kolkata knight riders", "kkr", "rajasthan royals"
    ],
    "NBA": [
        "lakers", "warriors", "celtics", "bulls", "heat", "bucks", "mavericks", "nuggets"
    ]
}

LEAGUE_PATTERNS = [
    (r"\bpremier league\b|\bepl\b", "Premier League"),
    (r"\bla liga\b|\blaliga\b", "La Liga"),
    (r"\bserie a\b", "Serie A"),
    (r"\bbundesliga\b", "Bundesliga"),
    (r"\bligue 1\b", "Ligue 1"),
    (r"\bchampions league\b|\bucl\b", "UEFA Champions League"),
    (r"\beuropa league\b|\buel\b", "UEFA Europa League"),
    (r"\bipl\b|\bindian premier league\b", "Indian Premier League (IPL)"),
    (r"\bbpl\b|\bbangladesh premier league\b", "Bangladesh Premier League (BPL)"),
    (r"\bnba\b", "NBA"),
    (r"\bhoosiers\b|\bwolverines\b|\bncaa\b|\bcollege football\b", "NCAA Football"),
    (r"\bnfl\b|\bredzone\b", "NFL"),
    (r"\bmlb\b", "MLB"),
    (r"\bnhl\b", "NHL"),
    (r"\bsmackdown\b", "WWE SmackDown"),
    (r"\braw\b", "WWE Raw"),
    (r"\bufc\b", "UFC"),
    (r"\bformula 1\b|\bf1\b", "Formula 1"),
]


def get_sub_category(sport: str, title: str) -> str:
    comb = f"{sport} {title}".lower()

    for pat, league in LEAGUE_PATTERNS:
        if re.search(pat, comb):
            return league

    for league, clubs in TOP_LEAGUE_CLUBS.items():
        for c in clubs:
            if re.search(r'\b' + re.escape(c) + r'\b', comb):
                return league

    return sport.capitalize()


def clean_sport_name(sport_slug: str) -> str:
    slug = sport_slug.lower()
    if "american-football" in slug or "american football" in slug or "nfl" in slug:
        return "American Football"
    if "football" in slug or "soccer" in slug:
        return "Football"
    if "basketball" in slug:
        return "Basketball"
    if "baseball" in slug:
        return "Baseball"
    if "hockey" in slug:
        return "Hockey"
    if "cricket" in slug:
        return "Cricket"
    if "wwe" in slug or "wrestling" in slug:
        return "Wrestling"
    if "fighting" in slug or "ufc" in slug or "mma" in slug:
        return "Fighting"
    return sport_slug.replace("-", " ").title()


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
            m3u_lines.append(f'#EXTVLCOPT:http-user-agent={STREAM_UA}')
            m3u_lines.append(raw_link)

    M3U_OUTPUT.write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")
    log.info(f"Saved M3U: {M3U_OUTPUT}")


# -----------------------------------------------------------------------------
# আসল M3U8 ডিকোডার (ব্রাউজারের মতো /sh/cdn/hls/{channel}.m3u8 বের করার লজিক)
# -----------------------------------------------------------------------------
async def resolve_m3u8_from_embed(client: httpx.AsyncClient, embed_url: str) -> str | None:
    """
    এমবেড প্লেয়ার থেকে edgestream3-এর আসল /sh/cdn/hls/{channel}.m3u8 বের করে
    """
    if not embed_url:
        return None

    # সরাসরি .m3u8 লিংক থাকলে
    if ".m3u8" in embed_url:
        return embed_url

    headers = {
        "User-Agent": STREAM_UA,
        "Referer": "https://streamcenter.st/",
    }

    try:
        # ১. এমবেড পেজ ফেচ করা (যেমন: https://streamec.st/embed/ch42.php)
        resp = await client.get(embed_url, headers=headers, timeout=6.0)
        if resp.status_code != 200:
            return None

        content = resp.text

        # ২. সরাসরি স্ক্রিনশটের মতো আসল m3u8 লিংক আছে কিনা চেক
        m3u8_match = re.search(r'["\'](https?://[^"\'\s]+/sh/cdn/hls/[^"\'\s]+\.m3u8)["\']', content)
        if m3u8_match:
            return m3u8_match.group(1)

        # ৩. সাধারণ যেকোনো m3u8 লিংক থাকলে
        gen_m3u8 = re.search(r'["\'](https?://[^"\'\s]+\.m3u8[^"\'\s]*)["\']', content)
        if gen_m3u8:
            return gen_m3u8.group(1)

        # ৪. প্লেয়ার কনফিগারেশনের ভেতর স্ট্রিম কী বা চ্যানেল নাম খোঁজা (যেমন: wnbcnxbcs42)
        # প্লেয়ার কোডে source, file, বা stream ভ্যারিয়েবল খোঁজা
        key_match = re.search(r'(?:file|source|stream|channel)\s*[:=]\s*["\']([a-zA-Z0-9_\-\.]+)(?:\.m3u8)?["\']', content, re.IGNORECASE)
        if key_match:
            key = key_match.group(1).replace(".m3u8", "").strip()
            # যদি সরাসরি চ্যানেল কী পাওয়া যায় (যেমন: wnbcnxbcs42)
            if re.search(r'[a-zA-Z]+\d+', key):
                return f"https://edgestream3.pro/sh/cdn/hls/{key}.m3u8"

        # ৫. <iframe> থাকলে ভেতরের প্লেয়ার আইফ্রেম চেক করা
        soup = HTMLParser(resp.content)
        iframe = soup.css_first("iframe")
        iframe_src = iframe.attributes.get("src") if iframe else None

        if iframe_src:
            if iframe_src.startswith("//"):
                iframe_src = "https:" + iframe_src
            elif iframe_src.startswith("/"):
                iframe_src = urljoin(embed_url, iframe_src)

            # আইফ্রেম ফেচ করা
            if_resp = await client.get(iframe_src, headers={"User-Agent": STREAM_UA, "Referer": embed_url}, timeout=5.0)
            if if_resp.status_code == 200:
                if_text = if_resp.text
                sub_m3u8 = re.search(r'["\'](https?://[^"\'\s]+/sh/cdn/hls/[^"\'\s]+\.m3u8)["\']', if_text)
                if sub_m3u8:
                    return sub_m3u8.group(1)

                sub_key = re.search(r'(?:file|source|stream|channel)\s*[:=]\s*["\']([a-zA-Z0-9_\-\.]+)(?:\.m3u8)?["\']', if_text, re.IGNORECASE)
                if sub_key:
                    k = sub_key.group(1).replace(".m3u8", "").strip()
                    if re.search(r'[a-zA-Z]+\d+', k):
                        return f"https://edgestream3.pro/sh/cdn/hls/{k}.m3u8"

        # ৬. URL এ ch42 থাকলে edgestream এর ডেডিকেটেড hls পাথে কনভার্ট
        ch_m = re.search(r'ch(\d+)', embed_url)
        if ch_m:
            ch_num = ch_m.group(1)
            # স্ক্রিনশট অনুযায়ী লাইভ HLS ফরম্যাট
            return f"https://edgestream3.pro/sh/cdn/hls/wnbcnxbcs{ch_num}.m3u8"

    except Exception as e:
        log.debug(f"Error resolving embed {embed_url}: {e}")

    return None


async def resolve_stream_group(client: httpx.AsyncClient, source: str, match_id: str) -> list[dict]:
    url = f"{BASE_URL}/api/stream/{quote_plus(source)}/{quote_plus(match_id)}"
    try:
        async with HTTP_SEMAPHORE:
            res = await client.get(url, timeout=8.0)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []


# -----------------------------------------------------------------------------
# মূল স্ক্র্যাপার প্রসেস
# -----------------------------------------------------------------------------
async def scrape():
    log.info(f"Scraping StreamCenter API from {API_SPORTS}...")

    client_headers = {
        "User-Agent": STREAM_UA,
        "Referer": "https://streamcenter.st/",
    }

    async with httpx.AsyncClient(headers=client_headers, timeout=12.0) as client:
        sport_poster_map = {}
        try:
            sports_res = await client.get(API_SPORTS)
            if sports_res.status_code != 200:
                log.error(f"Failed to fetch sports catalog: {sports_res.status_code}")
                return
            sports_list = sports_res.json()
            for sp in sports_list:
                s_id = sp.get("id")
                s_poster = format_api_asset(sp.get("poster"), "poster") or format_api_asset(sp.get("badge"), "badge")
                if s_id:
                    sport_poster_map[s_id] = s_poster
        except Exception as e:
            log.error(f"Error fetching sports: {e}")
            return

        now_utc = datetime.now(timezone.utc)
        all_raw_matches = []

        async def fetch_sport_matches(sp):
            sp_id = sp.get("id")
            if not sp_id:
                return
            try:
                m_res = await client.get(f"{BASE_URL}/api/matches/{sp_id}", timeout=8.0)
                if m_res.status_code == 200:
                    matches = m_res.json()
                    if isinstance(matches, list):
                        for m in matches:
                            m["_sport_slug"] = sp_id
                            m["_sport_name"] = clean_sport_name(sp_id)
                            all_raw_matches.append(m)
            except Exception:
                pass

        await asyncio.gather(*(fetch_sport_matches(sp) for sp in sports_list))
        log.info(f"Retrieved {len(all_raw_matches)} total matches.")

        valid_matches = []
        for m in all_raw_matches:
            title = m.get("title") or m.get("name")
            date_ms = m.get("date")
            sources = m.get("sources", [])

            if not title or not date_ms or not sources:
                continue

            match_dt = datetime.fromtimestamp(date_ms / 1000.0, tz=timezone.utc)

            if match_dt < now_utc - timedelta(hours=3):
                continue
            if match_dt > now_utc + timedelta(hours=18):
                continue

            valid_matches.append((match_dt, m))

        log.info(f"Processing {len(valid_matches)} active matches...")

        unique_groups = set()
        for _, m in valid_matches:
            for s in m.get("sources", []):
                unique_groups.add((s["source"], s["id"]))

        stream_cache = {}
        async def fetch_stream_data(src, sid):
            res = await resolve_stream_group(client, src, sid)
            if res:
                stream_cache[(src, sid)] = res

        await asyncio.gather(*(fetch_stream_data(src, sid) for src, sid in unique_groups))

        unique_embed_urls = set()
        for _, m in valid_matches:
            for s in m.get("sources", []):
                key = (s["source"], s["id"])
                for item in stream_cache.get(key, []):
                    raw_embed = item.get("embedUrl") or item.get("webUrl")
                    if raw_embed:
                        unique_embed_urls.add(raw_embed)

        log.info(f"Resolving {len(unique_embed_urls)} embed URLs into real HLS streams...")
        embed_m3u8_cache = {}

        async def fetch_real_m3u8(emb_url):
            async with HTTP_SEMAPHORE:
                res = await resolve_m3u8_from_embed(client, emb_url)
                if res:
                    embed_m3u8_cache[emb_url] = res

        await asyncio.gather(*(fetch_real_m3u8(u) for u in unique_embed_urls))

        final_playlist = []

        for match_dt, m in valid_matches:
            title = m.get("title") or m.get("name")
            sport_slug = m.get("_sport_slug", "football")
            sport_name = m.get("_sport_name", "Football")

            event_channels = []
            for s in m.get("sources", []):
                key = (s["source"], s["id"])
                stream_items = stream_cache.get(key, [])

                for item in stream_items:
                    raw_embed = item.get("embedUrl") or item.get("webUrl")
                    if raw_embed in embed_m3u8_cache:
                        m3u8_url = embed_m3u8_cache[raw_embed]
                        ch_name = item.get("stream") or item.get("name") or s.get("source", "Stream")

                        # স্ক্রিনশট অনুযায়ী পাইপ হেডার তৈরি
                        raw_link = f"{m3u8_url}|{STREAM_PIPE_HEADERS}"

                        event_channels.append({
                            "channel_name": str(ch_name),
                            "stream_type": "HLS",
                            "url": m3u8_url,  # https://edgestream3.pro/sh/cdn/hls/wnbcnxbcs42.m3u8
                            "raw_link": raw_link,
                            "headers": {
                                "User-Agent": STREAM_UA,
                                "Referer": STREAM_REFERER,
                                "Origin": STREAM_ORIGIN,
                            },
                            "drm_key": None
                        })

            if event_channels:
                start_bst = match_dt.astimezone(BST_TZ)
                end_bst = start_bst + timedelta(hours=3)

                t_names = parse_team_names(title)
                t_a = t_names["team_a"]
                t_b = t_names["team_b"]

                sp_default_poster = sport_poster_map.get(sport_slug)
                event_cover_poster, flag_a, flag_b = extract_api_badges(m, sp_default_poster)

                api_league = m.get("league") or m.get("competition") or m.get("tournament")
                if api_league and isinstance(api_league, str) and api_league.strip():
                    sub_cat = api_league.strip()
                else:
                    sub_cat = get_sub_category(sport_name, title)

                final_playlist.append({
                    "title": title,
                    "sport": sport_name,
                    "sub_category": sub_cat,
                    "status": determine_status(match_dt),
                    "is_hot": False,
                    "slug": create_slug(title),
                    "schedule": {
                        "timezone": "BST (UTC+6)",
                        "start_time_bst": start_bst.strftime("%b %d, %Y, %I:%M:%S %p"),
                        "end_time_bst": end_bst.strftime("%b %d, %Y, %I:%M:%S %p"),
                    },
                    "teams": {
                        "team_a": {"name": t_a, "flag": flag_a},
                        "team_b": {"name": t_b, "flag": flag_b}
                    },
                    "logo": event_cover_poster,
                    "channels": event_channels,
                    "_timestamp": match_dt.timestamp()
                })

        # সর্টিং (LIVE সবার উপরে)
        final_playlist.sort(key=lambda x: ({"LIVE": 0, "UPCOMING": 1, "ENDED": 2}.get(x["status"], 99), x["_timestamp"]))
        for it in final_playlist:
            it.pop("_timestamp", None)

        save_playlists(final_playlist)
        log.info("Playlists generated with real browser HLS stream URLs!")


if __name__ == "__main__":
    asyncio.run(scrape())
