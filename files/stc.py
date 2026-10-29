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

# নির্দিষ্ট রেফারার ও অরিজিন
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
# আপনার দেওয়া কাস্টম স্পোর্টস আইকন ও ডিফল্ট লোগো
# -----------------------------------------------------------------------------
SPORT_ICONS = {
    "football": "https://i.ibb.co.com/JwDc9ckh/Football.png",
    "cricket": "https://i.ibb.co.com/KxYMqyw8/cricket.webp",
    "basketball": "https://i.ibb.co.com/rGQtdmkG/Basketball.jpg",
    "tennis": "https://i.ibb.co.com/99CFfxDK/Tennis.webp",
    "weightlifting": "https://i.ibb.co.com/0jbC4PcX/Weightlifting.jpg",
    "volleyball": "https://i.ibb.co.com/jvJRYYDc/Volleyball.jpg",
    "hockey": "https://i.ibb.co.com/d0TKNFrY/Hockey.jpg",
    "wrestling": "https://i.ibb.co.com/Xf5DFxsB/Wrestling.png",
    "winter sports": "https://i.ibb.co.com/GvT5mQcb/Winter-sports.jpg",
    "ski jumping": "https://i.ibb.co.com/vvxtCZHN/Ski-jumping.jpg",
    "handball": "https://i.ibb.co.com/R4sMyfv9/Handball.webp",
    "snooker": "https://i.ibb.co.com/vvxtCZHN/Ski-jumping.jpg",
    "golf": "https://i.ibb.co.com/21zYsjH3/Golf.jpg",
    "motorsport": "https://i.ibb.co.com/hFVsfnpC/Motorsport.png",
}

DEFAULT_LOGO = "https://i.ibb.co.com/h1gWgKHy/Live-Streaming.webp"

KNOWN_TEAM_LOGOS = {
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


def get_team_badge(team_name: str, sport: str = "Football") -> str:
    t_clean = team_name.lower().strip()
    for k, v in KNOWN_TEAM_LOGOS.items():
        if k in t_clean or t_clean in k:
            return v
    sp_clean = sport.lower().strip()
    for sp_k, sp_v in SPORT_ICONS.items():
        if sp_k in sp_clean:
            return sp_v
    return DEFAULT_LOGO


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
    if "weightlifting" in s:
        return "Weightlifting"
    if "volleyball" in s:
        return "Volleyball"
    if "wrestling" in s:
        return "Wrestling"
    if "winter" in s:
        return "Winter Sports"
    if "ski" in s:
        return "Ski Jumping"
    if "handball" in s:
        return "Handball"
    if "snooker" in s:
        return "Snooker"
    if "golf" in s:
        return "Golf"
    if "motorsport" in s or "racing" in s:
        return "Motorsport"
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
# আসল secure_hls.php ডিকোডার
# -----------------------------------------------------------------------------
async def resolve_channel_stream(client: httpx.AsyncClient, link_url: str) -> str | None:
    if not link_url:
        return None

    if "secure_hls.php" in link_url or (link_url.endswith(".m3u8") and "vertex" not in link_url):
        return link_url

    embed_page_url = None

    # ১. s3.vertex.st/ch?id={number} থেকে আইডি বের করা
    ch_id_match = re.search(r'id=(\d+)', link_url)
    if ch_id_match:
        ch_id = ch_id_match.group(1)
        api_player_url = f"https://s3.vertex.st/api/player.php?id={ch_id}"
        api_headers = {
            "User-Agent": STREAM_UA,
            "Referer": link_url,
            "Accept": "*/*",
        }

        try:
            resp = await client.get(api_player_url, headers=api_headers, timeout=6.0)
            if resp.status_code == 200:
                data = resp.json()
                embed_page_url = data.get("url")
        except Exception:
            pass

    target_embed = embed_page_url or link_url

    # ২. embed.php পেজ থেকে আসল secure_hls.php বের করা
    try:
        headers = {
            "User-Agent": STREAM_UA,
            "Referer": "https://topsurge.st/",
            "Origin": STREAM_ORIGIN,
        }

        res = await client.get(target_embed, headers=headers, timeout=6.0)
        if res.status_code == 200:
            content = res.text

            sec_m3u8 = re.search(r'["\'](https?://[^"\'\s]+/secure_hls\.php\?[^"\'\s]+)["\']', content)
            if sec_m3u8:
                return sec_m3u8.group(1).replace("&amp;", "&")

            sec_esc = re.search(r'(https?:\\/\\/[^"\'\s]+secure_hls\.php\?[^"\'\s]+)', content)
            if sec_esc:
                clean_url = sec_esc.group(1).replace(r"\/", "/").replace("&amp;", "&")
                return clean_url.strip('"\'')

            iframe_m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', content, re.IGNORECASE)
            if iframe_m:
                sub_src = iframe_m.group(1)
                if sub_src.startswith("//"):
                    sub_src = "https:" + sub_src
                elif sub_src.startswith("/"):
                    sub_src = urljoin(target_embed, sub_src)

                sub_res = await client.get(sub_src, headers={"User-Agent": STREAM_UA, "Referer": target_embed}, timeout=5.0)
                if sub_res.status_code == 200:
                    sub_text = sub_res.text
                    sub_sec = re.search(r'["\'](https?://[^"\'\s]+/secure_hls\.php\?[^"\'\s]+)["\']', sub_text)
                    if sub_sec:
                        return sub_sec.group(1).replace("&amp;", "&")

            gen_m3u8 = re.search(r'["\'](https?://[^"\'\s]+\.m3u8[^"\'\s]*)["\']', content)
            if gen_m3u8:
                return gen_m3u8.group(1)

    except Exception:
        pass

    return embed_page_url or link_url


# -----------------------------------------------------------------------------
# মূল স্ক্র্যাপার প্রসেস (মাত্র ১০-১৫ সেকেন্ডে রান হয়ে শেষ হবে)
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

        for m in matches:
            t_a = m.get("team1")
            t_b = m.get("team2")
            matchstr = m.get("matchstr")
            title = matchstr or f"{t_a} vs {t_b}"
            start_ts = m.get("startTimestamp")
            channels = m.get("channels", [])

            if not title or not start_ts or not channels:
                continue

            try:
                ts = float(start_ts)
                if ts > 1e11:
                    ts = ts / 1000.0
                match_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError, OverflowError):
                continue

            # ৩ ঘণ্টা আগের ম্যাচ থেকে পরবর্তী ১৮ ঘণ্টার ম্যাচ রাখা হবে
            if match_dt < now_utc - timedelta(hours=3):
                continue
            if match_dt > now_utc + timedelta(hours=18):
                continue

            valid_matches.append((match_dt, m))

        log.info(f"Processing {len(valid_matches)} active matches in window...")

        unique_links = set()
        for _, m in valid_matches:
            for ch in m.get("channels", []):
                links_list = ch.get("links") or ch.get("oldLinks") or []
                for l in links_list:
                    if l:
                        unique_links.add(l)

        log.info(f"Resolving {len(unique_links)} unique stream links via vertex API...")
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
                    break

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

        final_playlist.sort(key=lambda x: ({"LIVE": 0, "UPCOMING": 1, "ENDED": 2}.get(x["status"], 99), x["_timestamp"]))
        for it in final_playlist:
            it.pop("_timestamp", None)

        # প্লেলিস্ট ফাইল সংরক্ষণ
        save_playlists(final_playlist)
        log.info(f"Done! Successfully generated {len(final_playlist)} matches.")


if __name__ == "__main__":
    asyncio.run(scrape())
