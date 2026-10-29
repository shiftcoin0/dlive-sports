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

# রিপোজিটরি রুট ডিরেক্টরি
ROOT_DIR = Path(__file__).resolve().parent.parent

BASE_URL = "https://topsurge.st"
API_URL = f"{BASE_URL}/api_proxy.php"

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
# টিম লোগো সিডিএন (কোনো ম্যাচে যেন লোগো খালি না থাকে)
# -----------------------------------------------------------------------------
KNOWN_TEAM_LOGOS = {
    # Champions League & Top Teams
    "manchester city": "https://images.fotmob.com/image_resources/logo/teamlogo/8456.png",
    "man city": "https://images.fotmob.com/image_resources/logo/teamlogo/8456.png",
    "psg": "https://images.fotmob.com/image_resources/logo/teamlogo/9847.png",
    "paris saint-germain": "https://images.fotmob.com/image_resources/logo/teamlogo/9847.png",
    "everton": "https://images.fotmob.com/image_resources/logo/teamlogo/8668.png",
    "newcastle united": "https://images.fotmob.com/image_resources/logo/teamlogo/10261.png",
    "newcastle": "https://images.fotmob.com/image_resources/logo/teamlogo/10261.png",
    "juventus": "https://images.fotmob.com/image_resources/logo/teamlogo/9885.png",
    "austria wien": "https://images.fotmob.com/image_resources/logo/teamlogo/8169.png",
    "utrecht": "https://images.fotmob.com/image_resources/logo/teamlogo/9908.png",
    "getafe": "https://images.fotmob.com/image_resources/logo/teamlogo/8305.png",
    "osasuna": "https://images.fotmob.com/image_resources/logo/teamlogo/8371.png",
    "arsenal": "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png",
    "chelsea": "https://images.fotmob.com/image_resources/logo/teamlogo/10260.png",
    "liverpool": "https://images.fotmob.com/image_resources/logo/teamlogo/8650.png",
    "manchester united": "https://images.fotmob.com/image_resources/logo/teamlogo/10267.png",
    "real madrid": "https://images.fotmob.com/image_resources/logo/teamlogo/8633.png",
    "barcelona": "https://images.fotmob.com/image_resources/logo/teamlogo/8634.png",
    "bayern munich": "https://images.fotmob.com/image_resources/logo/teamlogo/9823.png",
    "al riyadh": "https://images.fotmob.com/image_resources/logo/teamlogo/10243.png",
    "al qadisiya": "https://images.fotmob.com/image_resources/logo/teamlogo/10245.png",
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
    return sport_raw.strip().title() or "Football"


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
# স্ক্রিনশট ৪ অনুযায়ী আসল লাইভ HLS স্ট্রিম ডিকোডার
# -----------------------------------------------------------------------------
async def resolve_channel_stream(client: httpx.AsyncClient, link_url: str) -> str | None:
    """
    vertex.st / playfa.st লিঙ্ক থেকে secure_hls.php বা সরাসরি m3u8 স্ট্রিম বের করে
    """
    if not link_url:
        return None

    if ".m3u8" in link_url or "secure_hls.php" in link_url:
        return link_url

    headers = {
        "User-Agent": STREAM_UA,
        "Referer": STREAM_REFERER,
        "Origin": STREAM_ORIGIN,
    }

    try:
        resp = await client.get(link_url, headers=headers, timeout=6.0)
        if resp.status_code != 200:
            return None

        content = resp.text

        # ১. স্ক্রিনশট ৪ এর মতো secure_hls.php ফাইল খোঁজা
        sec_m3u8 = re.search(r'["\'](https?://[^"\'\s]+/secure_hls\.php\?[^"\'\s]+)["\']', content)
        if sec_m3u8:
            return sec_m3u8.group(1).replace("&amp;", "&")

        # ২. সরাসরি m3u8 লিংক
        m3u8_m = re.search(r'["\'](https?://[^"\'\s]+\.m3u8[^"\'\s]*)["\']', content)
        if m3u8_m:
            return m3u8_m.group(1)

        # ৩. <iframe> ট্যাগ খোঁজা
        iframe_src = None
        iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', content, re.IGNORECASE)
        if iframe_match:
            iframe_src = iframe_match.group(1)

        if iframe_src:
            if iframe_src.startswith("//"):
                iframe_src = "https:" + iframe_src
            elif iframe_src.startswith("/"):
                iframe_src = urljoin(link_url, iframe_src)

            if "secure_hls.php" in iframe_src or ".m3u8" in iframe_src:
                return iframe_src

            if_resp = await client.get(iframe_src, headers=headers, timeout=5.0)
            if if_resp.status_code == 200:
                sub_text = if_resp.text
                sub_sec = re.search(r'["\'](https?://[^"\'\s]+/secure_hls\.php\?[^"\'\s]+)["\']', sub_text)
                if sub_sec:
                    return sub_sec.group(1).replace("&amp;", "&")
                sub_m3u8 = re.search(r'["\'](https?://[^"\'\s]+\.m3u8[^"\'\s]*)["\']', sub_text)
                if sub_m3u8:
                    return sub_m3u8.group(1)

        # ৪. JS ভ্যারিয়েবল (source / file)
        js_m = re.search(r'(?:file|source|src)\s*[:=]\s*["\'](https?://[^"\'\s]+)["\']', content, re.IGNORECASE)
        if js_m:
            candidate = js_m.group(1)
            if ".m3u8" in candidate or "secure_hls.php" in candidate:
                return candidate

    except Exception as e:
        log.debug(f"Error resolving stream from {link_url}: {e}")

    return None


# -----------------------------------------------------------------------------
# মূল স্ক্র্যাপার প্রসেস
# -----------------------------------------------------------------------------
async def scrape():
    log.info(f"Fetching matches from Topsurge API ({API_URL})...")

    headers = {
        "User-Agent": STREAM_UA,
        "Referer": f"{BASE_URL}/",
        "Accept": "application/json, text/plain, */*",
    }

    async with httpx.AsyncClient(headers=headers, timeout=12.0) as client:
        try:
            resp = await client.get(API_URL)
            if resp.status_code != 200:
                log.error(f"Failed to fetch api_proxy.php: {resp.status_code}")
                return

            data = resp.json()
            matches = data.get("matches", [])
            log.info(f"Retrieved {len(matches)} matches directly from Topsurge API!")
        except Exception as e:
            log.error(f"Error fetching API: {e}")
            return

        now_utc = datetime.now(timezone.utc)
        valid_matches = []

        # ম্যাচ ফিল্টারিং
        for m in matches:
            t_a = m.get("team1")
            t_b = m.get("team2")
            matchstr = m.get("matchstr")
            title = matchstr or f"{t_a} vs {t_b}"
            start_ts = m.get("startTimestamp")
            channels = m.get("channels", [])

            if not title or not start_ts or not channels:
                continue

            match_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)

            # ৩ ঘণ্টা আগের ম্যাচ থেকে পরবর্তী ১৮ ঘণ্টার ম্যাচ রাখা হবে
            if match_dt < now_utc - timedelta(hours=3):
                continue
            if match_dt > now_utc + timedelta(hours=18):
                continue

            valid_matches.append((match_dt, m))

        log.info(f"Processing {len(valid_matches)} active matches in window...")

        # সকল চ্যানেল লিঙ্ক থেকে প্যারালালে আসল m3u8 স্ট্রিম বের করা
        unique_links = set()
        for _, m in valid_matches:
            for ch in m.get("channels", []):
                links_list = ch.get("links") or ch.get("oldLinks") or []
                for l in links_list:
                    if l:
                        unique_links.add(l)

        log.info(f"Resolving {len(unique_links)} unique stream links concurrently...")
        stream_cache = {}

        async def fetch_stream(link):
            async with HTTP_SEMAPHORE:
                m3u8 = await resolve_channel_stream(client, link)
                if m3u8:
                    stream_cache[link] = m3u8

        await asyncio.gather(*(fetch_stream(u) for u in unique_links))

        final_playlist = []

        for match_dt, m in valid_matches:
            t_a = m.get("team1") or "Team A"
            t_b = m.get("team2") or "Team B"
            matchstr = m.get("matchstr")
            title = matchstr or f"{t_a} vs {t_b}"

            league = m.get("league") or "Live Sports"
            sport = clean_sport_name(m.get("sport") or "Football")
            slug = m.get("slug") or create_slug(title)

            event_channels = []
            for ch in m.get("channels", []):
                c_name = ch.get("name") or "Stream"
                c_lang = ch.get("language")
                if c_lang:
                    c_name = f"{c_name} ({c_lang})"

                links_list = ch.get("links") or ch.get("oldLinks") or []
                for l in links_list:
                    stream_url = stream_cache.get(l) or l
                    raw_link = f"{stream_url}|{STREAM_PIPE_HEADERS}"

                    event_channels.append({
                        "channel_name": c_name,
                        "stream_type": "HLS",
                        "url": stream_url,
                        "raw_link": raw_link,
                        "headers": {
                            "User-Agent": STREAM_UA,
                            "Referer": STREAM_REFERER,
                            "Origin": STREAM_ORIGIN,
                        },
                        "drm_key": None
                    })
                    break  # প্রতিটি চ্যানেলের জন্য সেরা প্রথম লিঙ্কটি নেওয়া

            if event_channels:
                start_bst = match_dt.astimezone(BST_TZ)
                end_bst = start_bst + timedelta(hours=3)

                flag_a = get_team_badge(t_a, sport)
                flag_b = get_team_badge(t_b, sport)

                final_playlist.append({
                    "title": title,
                    "sport": sport,
                    "sub_category": league,
                    "status": determine_status(match_dt),
                    "is_hot": False,
                    "slug": slug,
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
        log.info(f"Successfully generated {len(final_playlist)} matches from Topsurge API!")


if __name__ == "__main__":
    asyncio.run(scrape())
