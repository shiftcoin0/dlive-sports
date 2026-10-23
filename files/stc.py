import asyncio
import base64
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

BASE_URL = "https://sportlight.live"

TAG = "SPORTLIGHT"

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


SPORT_PAGES = [
    ("Football", f"{BASE_URL}/soccer/"),
    ("Cricket", f"{BASE_URL}/cricket/"),
    ("Basketball", f"{BASE_URL}/nba/"),
    ("Motorsport", f"{BASE_URL}/f1/"),
    ("Tennis", f"{BASE_URL}/tennis/"),
    ("Wrestling", f"{BASE_URL}/wwe/"),
    ("Fighting", f"{BASE_URL}/mma-ufc/"),
    ("Fighting", f"{BASE_URL}/boxing/"),
    ("American Football", f"{BASE_URL}/nfl/"),
    ("Baseball", f"{BASE_URL}/mlb/"),
    ("Hockey", f"{BASE_URL}/nhl/"),
]


@dataclass(slots=True)
class RawMatch:
    sport: str
    title: str
    team_a_name: str
    team_a_logo: str
    team_b_name: str
    team_b_logo: str
    status: str
    start_dt: datetime
    match_url: str


def create_slug(text: str) -> str:
    return re.sub(r"[\W_]+", "-", text.lower().strip()).strip("-")


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


def save_playlists(events: list[dict]) -> None:
    now_bst = datetime.now(BST_TZ)
    last_updated_str = now_bst.strftime("%b %d, %Y, %I:%M:%S %p")

    json_payload = {
        "last_updated": last_updated_str,
        "event_count": len(events),
        "events": {"events": events}
    }

    JSON_OUTPUT.write_text(json.dumps(json_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Saved JSON Playlist: {JSON_OUTPUT} (Total: {len(events)} events)")

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
            headers = ch.get("headers", {})
            referer = headers.get("Referer", "https://embed.st/")
            origin = headers.get("Origin", "https://embed.st")
            status_lbl = f" [{status}]" if status else ""
            display_name = f"[{sub_cat}] {ev['title']} | {ch_name}{status_lbl}"

            m3u_lines.append(f'#EXTINF:-1 tvg-id="Live.Event.us" tvg-logo="{logo}" group-title="{sub_cat}",{display_name}')
            if referer:
                m3u_lines.append(f'#EXTVLCOPT:http-referrer={referer}')
                m3u_lines.append(f'#EXTVLCOPT:http-origin={origin}')
            m3u_lines.append(url)

    M3U_OUTPUT.write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")
    log.info(f"Saved M3U Playlist: {M3U_OUTPUT}")


# -----------------------------------------------------------------------------
# strmd.st এবং embed.st থেকে সরাসরি .m3u8 ডিকোডার
# -----------------------------------------------------------------------------
async def resolve_embed_st_m3u8(client: httpx.AsyncClient, embed_url: str) -> tuple[str, dict]:
    """
    আপনার দেওয়া স্ক্রিনশটের lb*.strmd.st এর m3u8 স্ট্রিম এবং সঠিক হেডার এক্সট্রাক্ট করে
    """
    req_headers = {
        "User-Agent": UA,
        "Referer": f"{BASE_URL}/",
    }
    standard_stream_headers = {
        "Referer": "https://embed.st/",
        "Origin": "https://embed.st"
    }

    try:
        resp = await client.get(embed_url, headers=req_headers, timeout=6.0)
        if resp.status_code != 200:
            return embed_url, standard_stream_headers

        html_text = resp.text

        # ১. সরাসরি strmd.st এর m3u8 লিংক খোঁজা (স্ক্রিনশট অনুযায়ী প্যাটার্ন)
        strmd_match = re.search(r'["\'](https?://[a-zA-Z0-9_\-\.]*strmd\.st/[^"\']+\.m3u8[^"\']*)["\']', html_text)
        if strmd_match:
            final_url = strmd_match.group(1).replace("\\/", "/")
            return final_url, standard_stream_headers

        # ২. যেকোনো সাধারণ .m3u8 ইউআরএল খোঁজা
        general_m3u8 = re.search(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', html_text)
        if general_m3u8:
            final_url = general_m3u8.group(1).replace("\\/", "/")
            return final_url, standard_stream_headers

        # ৩. JavaScript ভ্যারিয়েবল এবং কনফিগারেশন থেকে লিঙ্ক তৈরি
        # embed.st প্রায়শই "source", "file", বা টোকেন ভ্যারিয়েবল ব্যবহার করে
        token_match = re.search(r'source\s*:\s*["\']([^"\']+)["\']', html_text) or \
                      re.search(r'file\s*:\s*["\']([^"\']+)["\']', html_text) or \
                      re.search(r'url\s*:\s*["\']([^"\']+)["\']', html_text)

        if token_match:
            raw_src = token_match.group(1).replace("\\/", "/")
            if ".m3u8" in raw_src:
                if not raw_src.startswith("http"):
                    raw_src = urljoin("https://embed.st", raw_src)
                return raw_src, standard_stream_headers

        # ৪. Base64 এনকোড করা লিঙ্ক ডিকোড
        b64_matches = re.findall(r'atob\(["\']([A-Za-z0-9+/=]+)["\']\)', html_text)
        for b in b64_matches:
            try:
                decoded = base64.b64decode(b).decode('utf-8')
                if ".m3u8" in decoded or "strmd.st" in decoded:
                    m = re.search(r'(https?://[^\s"\']+\.m3u8[^\s"\']*)', decoded)
                    if m:
                        return m.group(1).replace("\\/", "/"), standard_stream_headers
            except Exception:
                pass

        # ৫. embed.st এর ভেতরের নেস্টেড আইফ্রেম চেক করা
        soup = HTMLParser(resp.content)
        iframe = soup.css_first("iframe[src]")
        if iframe:
            sub_src = iframe.attributes.get("src")
            if sub_src:
                sub_url = urljoin(embed_url, sub_src)
                sub_resp = await client.get(sub_url, headers={"User-Agent": UA, "Referer": embed_url}, timeout=5.0)
                if sub_resp.status_code == 200:
                    sub_strmd = re.search(r'["\'](https?://[a-zA-Z0-9_\-\.]*strmd\.st/[^"\']+\.m3u8[^"\']*)["\']', sub_resp.text)
                    if sub_strmd:
                        return sub_strmd.group(1).replace("\\/", "/"), standard_stream_headers
                    sub_m3u8 = re.search(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', sub_resp.text)
                    if sub_m3u8:
                        return sub_m3u8.group(1).replace("\\/", "/"), standard_stream_headers

    except Exception as e:
        log.debug(f"Error decoding embed: {e}")

    # ডিকোড না করা গেলে প্লেয়ারের লিঙ্ক ও হেডার
    return embed_url, standard_stream_headers


async def extract_streams_from_page(client: httpx.AsyncClient, page_url: str) -> list[dict]:
    streams = []
    headers = {"User-Agent": UA, "Referer": f"{BASE_URL}/"}

    try:
        resp = await client.get(page_url, headers=headers, timeout=5.0)
        if resp.status_code != 200:
            return streams

        soup = HTMLParser(resp.content)
        embed_links = set()

        for iframe in soup.css("iframe[src]"):
            src = iframe.attributes.get("src")
            if src:
                embed_links.add(urljoin(BASE_URL, src))

        for a in soup.css("a[href*='embed'], button[data-src]"):
            href = a.attributes.get("href") or a.attributes.get("data-src")
            if href:
                embed_links.add(urljoin(BASE_URL, href))

        found_embeds = re.findall(r'["\'](https?://embed\.st/embed/[^"\']+)["\']', resp.text)
        for emb in found_embeds:
            embed_links.add(emb)

        if not embed_links:
            streams.append({
                "channel_name": "Server 1",
                "stream_type": "HLS",
                "url": page_url,
                "raw_link": f"{page_url}|Referer=https://embed.st/",
                "headers": {"Referer": "https://embed.st/", "Origin": "https://embed.st"},
                "drm_key": None
            })
            return streams

        idx = 1
        for emb_url in embed_links:
            # m3u8 এবং সঠিক হেডার সংগ্রহ
            final_m3u8, stream_headers = await resolve_embed_st_m3u8(client, emb_url)
            ref = stream_headers.get("Referer", "https://embed.st/")
            streams.append({
                "channel_name": f"Server {idx}",
                "stream_type": "HLS",
                "url": final_m3u8,
                "raw_link": f"{final_m3u8}|Referer={ref}",
                "headers": stream_headers,
                "drm_key": None
            })
            idx += 1

    except Exception:
        pass

    return streams


async def parse_sport_page(client: httpx.AsyncClient, sport_name: str, page_url: str) -> list[RawMatch]:
    matches = []
    headers = {"User-Agent": UA, "Referer": f"{BASE_URL}/"}

    try:
        resp = await client.get(page_url, headers=headers, timeout=6.0)
        if resp.status_code != 200:
            return matches

        soup = HTMLParser(resp.content)
        now_utc = datetime.now(timezone.utc)

        rows = soup.css("a[href], .match-row, .game-row, div[class*='match'], div[class*='game']")

        for r in rows:
            text = r.text(strip=True)
            if " vs " not in text.lower() and " vs. " not in text.lower():
                continue

            match_href = r.attributes.get("href") if r.tag == "a" else None
            if not match_href:
                a_tag = r.css_first("a[href]")
                if a_tag:
                    match_href = a_tag.attributes.get("href")

            if not match_href or match_href in ["#", "/"]:
                continue

            full_match_url = urljoin(BASE_URL, match_href)

            imgs = r.css("img")
            team_a_logo = ""
            team_b_logo = ""

            if len(imgs) >= 2:
                src_a = imgs[0].attributes.get("src") or imgs[0].attributes.get("data-src") or ""
                src_b = imgs[1].attributes.get("src") or imgs[1].attributes.get("data-src") or ""
                team_a_logo = urljoin(BASE_URL, src_a)
                team_b_logo = urljoin(BASE_URL, src_b)
            elif len(imgs) == 1:
                src_a = imgs[0].attributes.get("src") or ""
                team_a_logo = urljoin(BASE_URL, src_a)

            for d in [" vs. ", " vs ", " - "]:
                if d in text:
                    parts = text.split(d)
                    left = re.sub(r"(SOCCER|CRICKET|NBA|LIVE|\d+:\d+\s*[AP]M|Starts in.*)", "", parts[0], flags=re.I).strip()
                    right = re.sub(r"(SOCCER|CRICKET|NBA|LIVE|\d+:\d+\s*[AP]M|Starts in.*)", "", parts[1], flags=re.I).strip()
                    clean_title = f"{left} vs {right}"
                    team_a_name = left
                    team_b_name = right
                    break
            else:
                continue

            status = "UPCOMING"
            event_dt = now_utc

            if "live" in text.lower():
                status = "LIVE"
            else:
                starts_in_m = re.search(r"starts in\s*(\d{1,2}):(\d{2})", text, re.I)
                if starts_in_m:
                    hrs = int(starts_in_m.group(1))
                    mins = int(starts_in_m.group(2))
                    total_min_left = (hrs * 60) + mins
                    event_dt = now_utc + timedelta(minutes=total_min_left)

                    if total_min_left <= 15:
                        status = "LIVE"

            matches.append(
                RawMatch(
                    sport=sport_name,
                    title=clean_title,
                    team_a_name=team_a_name,
                    team_a_logo=team_a_logo,
                    team_b_name=team_b_name,
                    team_b_logo=team_b_logo,
                    status=status,
                    start_dt=event_dt,
                    match_url=full_match_url
                )
            )

    except Exception:
        pass

    return matches


async def process_single_match(client: httpx.AsyncClient, m: RawMatch):
    async with HTTP_SEMAPHORE:
        streams = await extract_streams_from_page(client, m.match_url)
        if not streams:
            return None

        start_bst = m.start_dt.astimezone(BST_TZ)
        end_bst = start_bst + timedelta(hours=3)
        final_logo = m.team_a_logo or "https://images.fotmob.com/image_resources/logo/teamlogo/9825.png"

        return {
            "title": m.title,
            "sport": m.sport,
            "sub_category": get_sub_category(m.sport, m.title),
            "status": m.status,
            "is_hot": (m.status == "LIVE"),
            "slug": create_slug(m.title),
            "schedule": {
                "timezone": "BST (UTC+6)",
                "start_time_bst": start_bst.strftime("%b %d, %Y, %I:%M:%S %p"),
                "end_time_bst": end_bst.strftime("%b %d, %Y, %I:%M:%S %p"),
            },
            "teams": {
                "team_a": {
                    "name": m.team_a_name,
                    "flag": m.team_a_logo,
                },
                "team_b": {
                    "name": m.team_b_name,
                    "flag": m.team_b_logo,
                }
            },
            "logo": final_logo,
            "channels": streams,
            "_timestamp": m.start_dt.timestamp()
        }


async def scrape():
    log.info(f"Connecting to {BASE_URL}...")
    headers = {"User-Agent": UA, "Referer": f"{BASE_URL}/"}

    async with httpx.AsyncClient(headers=headers, timeout=6.0, follow_redirects=True) as client:
        tasks = [parse_sport_page(client, s_name, p_url) for s_name, p_url in SPORT_PAGES]
        results = await asyncio.gather(*tasks)

        all_matches = [m for sublist in results for m in sublist]
        log.info(f"Discovered {len(all_matches)} matches across all categories.")

        if not all_matches:
            log.info("No matches found.")
            return

        now_utc = datetime.now(timezone.utc)
        active_matches = []
        for m in all_matches:
            if m.status == "LIVE":
                active_matches.append(m)
            elif m.start_dt <= now_utc + timedelta(hours=5):
                active_matches.append(m)

        log.info(f"Filtered to {len(active_matches)} active matches. Extracting m3u8 streams in parallel...")

        match_tasks = [process_single_match(client, m) for m in active_matches]
        processed_matches = await asyncio.gather(*match_tasks)

        final_playlist = [item for item in processed_matches if item is not None]

        final_playlist.sort(key=lambda x: ({"LIVE": 0, "UPCOMING": 1, "ENDED": 2}.get(x["status"], 99), x["_timestamp"]))
        for it in final_playlist:
            it.pop("_timestamp", None)

        save_playlists(final_playlist)
        log.info(f"Playlist successfully generated with {len(final_playlist)} matches!")


if __name__ == "__main__":
    asyncio.run(scrape())
