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

# স্ক্রিনশট ৪ অনুযায়ী নির্দিষ্ট হেডার ও রেফারার
STREAM_REFERER = "https://matchli.st/"
STREAM_ORIGIN = "https://matchli.st"
STREAM_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"
STREAM_PIPE_HEADERS = f"User-Agent={STREAM_UA}&Referer={STREAM_REFERER}&Origin={STREAM_ORIGIN}"

TAG = "TOPSURGE"

JSON_OUTPUT = ROOT_DIR / "strmcntr.json"
M3U_OUTPUT = ROOT_DIR / "strmcntr.m3u"
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

HTTP_SEMAPHORE = asyncio.Semaphore(10)
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
    # Champions League & Top Teams
    "juventus": "https://images.fotmob.com/image_resources/logo/teamlogo/9885.png",
    "austria wien": "https://images.fotmob.com/image_resources/logo/teamlogo/8169.png",
    "utrecht": "https://images.fotmob.com/image_resources/logo/teamlogo/9908.png",
    "getafe": "https://images.fotmob.com/image_resources/logo/teamlogo/8305.png",
    "osasuna": "https://images.fotmob.com/image_resources/logo/teamlogo/8371.png",
    "arsenal": "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png",
    "chelsea": "https://images.fotmob.com/image_resources/logo/teamlogo/10260.png",
    "liverpool": "https://images.fotmob.com/image_resources/logo/teamlogo/8650.png",
    "manchester united": "https://images.fotmob.com/image_resources/logo/teamlogo/10267.png",
    "manchester city": "https://images.fotmob.com/image_resources/logo/teamlogo/8456.png",
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
# স্ক্রিনশট ৪ অনুযায়ী আসল লাইভ HLS স্ট্রিম ডিকোডার
# -----------------------------------------------------------------------------
async def resolve_topsurge_stream(client: httpx.AsyncClient, player_url: str) -> str | None:
    """
    topsurge.st/player.php/... পেজ থেকে secure_hls.php বা সরাসরি m3u8 বের করে
    """
    if not player_url:
        return None

    if ".m3u8" in player_url or "secure_hls.php" in player_url:
        return player_url

    headers = {
        "User-Agent": STREAM_UA,
        "Referer": f"{BASE_URL}/",
    }

    try:
        resp = await client.get(player_url, headers=headers, timeout=8.0)
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

        # ৩. <iframe> চেক করা
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

            if "secure_hls.php" in iframe_src or ".m3u8" in iframe_src:
                return iframe_src

            # আইফ্রেম পেজে রিকোয়েস্ট পাঠানো
            sub_headers = {"User-Agent": STREAM_UA, "Referer": STREAM_REFERER}
            if_resp = await client.get(iframe_src, headers=sub_headers, timeout=6.0)
            if if_resp.status_code == 200:
                sub_text = if_resp.text
                sub_sec = re.search(r'["\'](https?://[^"\'\s]+/secure_hls\.php\?[^"\'\s]+)["\']', sub_text)
                if sub_sec:
                    return sub_sec.group(1).replace("&amp;", "&")

                sub_m3u8 = re.search(r'["\'](https?://[^"\'\s]+\.m3u8[^"\'\s]*)["\']', sub_text)
                if sub_m3u8:
                    return sub_m3u8.group(1)

        # ৪. JS ভ্যারিয়েবল থেকে source বা file খোঁজা
        js_m = re.search(r'(?:file|source|src)\s*[:=]\s*["\'](https?://[^"\'\s]+)["\']', content, re.IGNORECASE)
        if js_m:
            candidate = js_m.group(1)
            if ".m3u8" in candidate or "secure_hls.php" in candidate:
                return candidate

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
# ২ নম্বর স্ক্রিনশট অনুযায়ী Topsurge থেকে সকল ম্যাচ সংগ্রহ
# -----------------------------------------------------------------------------
async def get_topsurge_events(client: httpx.AsyncClient) -> list[TopsurgeEvent]:
    events = []
    headers = {
        "User-Agent": STREAM_UA,
        "Referer": f"{BASE_URL}/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    }

    try:
        resp = await client.get(f"{BASE_URL}/", headers=headers, timeout=12.0)
        if resp.status_code != 200:
            log.error(f"Failed to fetch topsurge home page: {resp.status_code}")
            return events

        soup = HTMLParser(resp.content)
        now_utc = datetime.now(timezone.utc)

        # স্ক্রিনশট ২ অনুযায়ী কার্ড বা কন্টেইনার খোঁজা
        # সাইটে প্রতিটি ম্যাচ একটি ব্লকে থাকে যেখানে টাইটেল এবং সাবটেক্সট থাকে
        cards = soup.css("div[class*='rounded'], div[class*='border'], div[class*='bg-'], .match, .card")
        
        # যদি নির্দিষ্ট কন্টেইনার না মেলে তবে সরাসরি 'vs' থাকা ব্লকগুলো খোঁজা
        valid_containers = []
        for c in cards:
            text = c.text(strip=True)
            if " vs " in text or " at " in text:
                # ডুপ্লিকেট প্যারেন্ট কন্টেইনার এড়াতে
                if len(c.css("div[class*='rounded'], div[class*='border']")) == 0:
                    valid_containers.append(c)

        # যদি কন্টেইনার শূন্য হয়, বডির সব প্যারাগ্রাফ/ডিভ লাইন থেকে পার্স করা
        if not valid_containers:
            valid_containers = [soup.body] if soup.body else []

        seen_titles = set()

        # HTML থেকে ম্যাচগুলো রেজেক্স দিয়েও নিখুঁতভাবে রিড করা (যাতে কোনোভাবেই মিস না হয়)
        # স্ক্রিনশট ২ এর টেক্সট ফরম্যাট: "Austria Wien vs Juventus" এবং "11:45 PM • Europe UEFA Women's Champions League • Football"
        html_raw = resp.text

        # রেজেক্স দিয়ে স্ক্রিনশটের সব ম্যাচ ও মেটাডাটা একবারে তুলে আনা
        matches_found = re.findall(
            r'([A-Za-z0-9\.\s\-\'\&]+(?:vs|at)[A-Za-z0-9\.\s\-\'\&]+)[\s\S]*?(\d{1,2}:\d{2}\s*(?:AM|PM)?)\s*•\s*([^•\n<]+)\s*•\s*([A-Za-z\s]+)',
            html_raw,
            re.IGNORECASE
        )

        for match_item in matches_found:
            event_title = match_item[0].strip()
            time_str = match_item[1].strip()
            league_str = match_item[2].strip()
            sport_str = match_item[3].strip()

            # ক্লিনআপ
            event_title = re.sub(r'\s+', ' ', event_title).strip()
            if len(event_title) > 60 or "\n" in event_title:
                continue

            if event_title in seen_titles:
                continue
            seen_titles.add(event_title)

            # সময় পার্স করা
            event_dt = now_utc
            tm = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", time_str, re.IGNORECASE)
            if tm:
                h = int(tm.group(1))
                m = int(tm.group(2))
                ampm = tm.group(3)
                if ampm and ampm.upper() == "PM" and h < 12:
                    h += 12
                elif ampm and ampm.upper() == "AM" and h == 12:
                    h = 0
                event_dt = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)

            # ৩ নম্বর স্ক্রিনশট অনুযায়ী লিঙ্ক তৈরি:
            # https://topsurge.st/player.php/{title}-{league}-{sport}
            slug_part = create_slug(f"{event_title} {league_str} {sport_str}")
            player_url = f"{BASE_URL}/player.php/{slug_part}"

            # চ্যানেল তৈরি (স্ক্রিনশট ২ এ থাকা চ্যালেন নাম)
            channel_name = f"{league_str} Stream"
            channels = [(channel_name, player_url)]

            sport_clean = clean_sport_name(sport_str)

            events.append(
                TopsurgeEvent(
                    title=event_title,
                    sport=sport_clean,
                    sub_cat=league_str,
                    start_dt=event_dt,
                    channels=channels
                )
            )

    except Exception as e:
        log.error(f"Error scraping Topsurge events: {e}")

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

        # প্লেয়ার লিংকগুলো প্যারালালে ডিকোড করা
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
                # যদি লাইভ m3u8 রেজলভ হয় অথবা সরাসরি প্লেয়ার থাকে
                stream_url = stream_cache.get(link) or link
                raw_link = f"{stream_url}|{STREAM_PIPE_HEADERS}"

                event_channels.append({
                    "channel_name": cname,
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
        log.info(f"Generated {len(final_playlist)} events from Topsurge successfully!")


if __name__ == "__main__":
    asyncio.run(scrape())
