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

BASE_URL = "https://streamcenter.st"
ALT_REFERER = "https://streame.center"
API_SPORTS = f"{BASE_URL}/api/sports"

TAG = "STRMCNTR"

# আউটপুট পাথ
JSON_OUTPUT = ROOT_DIR / "strmcntr.json"
M3U_OUTPUT = ROOT_DIR / "strmcntr.m3u"
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
)

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
# টিম লোগো সিডিএন
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# সাব-ক্যাটাগরি ও স্পোর্টস ক্লিনার
# -----------------------------------------------------------------------------
LEAGUE_PATTERNS = [
    (r"premier league|epl", "Premier League"),
    (r"la liga|laliga", "La Liga"),
    (r"serie a", "Serie A"),
    (r"bundesliga", "Bundesliga"),
    (r"ligue 1", "Ligue 1"),
    (r"champions league|ucl", "UEFA Champions League"),
    (r"europa league|uel", "UEFA Europa League"),
    (r"ipl|indian premier league", "Indian Premier League (IPL)"),
    (r"bpl", "Bangladesh Premier League (BPL)"),
    (r"nba", "NBA"),
    (r"smackdown", "WWE SmackDown"),
    (r"raw", "WWE Raw"),
    (r"ufc", "UFC"),
    (r"formula 1|f1", "Formula 1"),
]


def get_sub_category(sport: str, title: str) -> str:
    comb = f"{sport} {title}".lower()
    for pat, league in LEAGUE_PATTERNS:
        if re.search(pat, comb):
            return league
    return sport.capitalize()


def clean_sport_name(sport_slug: str) -> str:
    slug = sport_slug.lower()
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
    for d in [" vs. ", " vs ", " - ", " @ "]:
        if d in title.lower():
            parts = re.split(re.escape(d), title, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                return {"team_a": parts[0].strip(), "team_b": parts[1].strip()}
    return {"team_a": title.strip(), "team_b": title.strip()}


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
            referer = ch.get("headers", {}).get("Referer", ALT_REFERER)
            status_lbl = f" [{status}]" if status else ""
            display_name = f"[{sub_cat}] {ev['title']} | {ch_name}{status_lbl}"

            m3u_lines.append(f'#EXTINF:-1 tvg-id="Live.Event.us" tvg-logo="{logo}" group-title="{sub_cat}",{display_name}')
            if referer:
                m3u_lines.append(f'#EXTVLCOPT:http-referrer={referer}')
            m3u_lines.append(url)

    M3U_OUTPUT.write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")
    log.info(f"Saved M3U: {M3U_OUTPUT}")


# -----------------------------------------------------------------------------
# আসল .m3u8 রেজোলিউশন ইঞ্জিন (streamec.st এমবেড পেজ ডিকোডার)
# -----------------------------------------------------------------------------
async def resolve_m3u8_from_embed(client: httpx.AsyncClient, embed_url: str) -> tuple[str, str] | None:
    """
    এমবেড লিঙ্ক (যেমন: https://streamec.st/embed/ch48.php) থেকে আসল .m3u8 বের করা
    """
    if not embed_url:
        return None

    # সরাসরি .m3u8 লিংক হলে
    if ".m3u8" in embed_url:
        return embed_url, ALT_REFERER

    headers = {
        "User-Agent": UA,
        "Referer": f"{BASE_URL}/",
    }

    try:
        # ১. এমবেড পেজ ফেচ করা
        resp = await client.get(embed_url, headers=headers, timeout=6.0)
        if resp.status_code != 200:
            return None

        content = resp.text

        # ২. সোর্সে সরাসরি .m3u8 থাকলে
        m3u8_match = re.search(r'["\'](https?://[^"\'\s]+\.m3u8[^"\'\s]*)["\']', content)
        if m3u8_match:
            return m3u8_match.group(1), ALT_REFERER

        # ৩. <iframe> ট্যাগ থেকে stream_id বের করা
        soup = HTMLParser(resp.content)
        iframe = soup.css_first("iframe")
        iframe_src = iframe.attributes.get("src") if iframe else None

        if not iframe_src:
            src_m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', content, re.IGNORECASE)
            if src_m:
                iframe_src = src_m.group(1)

        if iframe_src:
            parsed = urlsplit(iframe_src)
            params = dict(parse_qsl(parsed.query))
            stream_id = params.get("stream") or params.get("id")

            if stream_id:
                if stream_id.isdigit():
                    return f"https://edgestream3.pro/stream/{stream_id}.m3u8", ALT_REFERER
                return f"https://edgestream2.pro/hls/{stream_id}.m3u8", ALT_REFERER

        # ৪. JS কোড থেকে stream= বা stream: খোঁজা
        js_m = re.search(r'stream(?:_id)?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]+)["\']?', content, re.IGNORECASE)
        if js_m:
            s_id = js_m.group(1)
            if s_id.isdigit():
                return f"https://edgestream3.pro/stream/{s_id}.m3u8", ALT_REFERER
            return f"https://edgestream2.pro/hls/{s_id}.m3u8", ALT_REFERER

        # ৫. URL প্যাটার্ন (ch48.php ইত্যাদি) হলে সরাসরি edgestream নম্বর ব্যবহার করা
        ch_m = re.search(r'ch(\d+)', embed_url)
        if ch_m:
            ch_num = ch_m.group(1)
            return f"https://edgestream3.pro/stream/{ch_num}.m3u8", ALT_REFERER

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

    async with httpx.AsyncClient(headers={"User-Agent": UA, "Referer": f"{BASE_URL}/"}, timeout=12.0) as client:
        # ১. স্পোর্টস ক্যাটালগ ফেচ
        try:
            sports_res = await client.get(API_SPORTS)
            if sports_res.status_code != 200:
                log.error(f"Failed to fetch sports catalog: {sports_res.status_code}")
                return
            sports_list = sports_res.json()
        except Exception as e:
            log.error(f"Error fetching sports: {e}")
            return

        now_utc = datetime.now(timezone.utc)
        all_raw_matches = []

        # ২. প্রতিটি স্পোর্টের ম্যাচ ফেচ করা
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
                            m["_sport_name"] = clean_sport_name(sp_id)
                            all_raw_matches.append(m)
            except Exception:
                pass

        await asyncio.gather(*(fetch_sport_matches(sp) for sp in sports_list))
        log.info(f"Retrieved {len(all_raw_matches)} total matches.")

        # ৩. সময় অনুযায়ী ফিল্টার (৩ ঘণ্টা আগের থেকে পরবর্তী ১৮ ঘণ্টার ম্যাচ)
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

        # ৪. স্ট্রিম গ্রুপ রেজোলিউশন
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

        # ৫. সকল এমবেড লিঙ্ককে প্যারালালে আসল .m3u8 এ রূপান্তর করা
        unique_embed_urls = set()
        for _, m in valid_matches:
            for s in m.get("sources", []):
                key = (s["source"], s["id"])
                for item in stream_cache.get(key, []):
                    raw_embed = item.get("embedUrl") or item.get("webUrl")
                    if raw_embed:
                        unique_embed_urls.add(raw_embed)

        log.info(f"Resolving {len(unique_embed_urls)} embed URLs into actual .m3u8 streams...")
        embed_m3u8_cache = {}

        async def fetch_real_m3u8(emb_url):
            async with HTTP_SEMAPHORE:
                res = await resolve_m3u8_from_embed(client, emb_url)
                if res:
                    embed_m3u8_cache[emb_url] = res

        await asyncio.gather(*(fetch_real_m3u8(u) for u in unique_embed_urls))

        # ৬. চূড়ান্ত প্লেলিস্ট তৈরি
        final_playlist = []

        for match_dt, m in valid_matches:
            title = m.get("title") or m.get("name")
            sport_name = m.get("_sport_name", "Football")

            event_channels = []
            for s in m.get("sources", []):
                key = (s["source"], s["id"])
                stream_items = stream_cache.get(key, [])

                for item in stream_items:
                    raw_embed = item.get("embedUrl") or item.get("webUrl")
                    if raw_embed in embed_m3u8_cache:
                        m3u8_url, ref = embed_m3u8_cache[raw_embed]
                        ch_name = item.get("stream") or item.get("name") or s.get("source", "Stream")

                        event_channels.append({
                            "channel_name": str(ch_name),
                            "stream_type": "HLS",
                            "url": m3u8_url,  # আসল .m3u8 লিংক
                            "raw_link": f"{m3u8_url}|Referer={ref}",
                            "headers": {"Referer": ref},
                            "drm_key": None
                        })

            if event_channels:
                start_bst = match_dt.astimezone(BST_TZ)
                end_bst = start_bst + timedelta(hours=3)

                t_names = parse_team_names(title)
                t_a = t_names["team_a"]
                t_b = t_names["team_b"]
                flag_a = get_real_team_badge(t_a, sport_name)
                flag_b = get_real_team_badge(t_b, sport_name)
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
                    "logo": flag_a,
                    "channels": event_channels,
                    "_timestamp": match_dt.timestamp()
                })

        # সর্টিং (LIVE সবার উপরে)
        final_playlist.sort(key=lambda x: ({"LIVE": 0, "UPCOMING": 1, "ENDED": 2}.get(x["status"], 99), x["_timestamp"]))
        for it in final_playlist:
            it.pop("_timestamp", None)

        save_playlists(final_playlist)
        log.info("Playlists with Real .m3u8 Streams generated successfully!")


if __name__ == "__main__":
    asyncio.run(scrape())
