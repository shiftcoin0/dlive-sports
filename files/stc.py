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

# নির্ভরযোগ্য ও স্টেবল গ্লোবাল সোর্স
CRICHD_BASE = "https://m.crichd.vip"
STREAMCENTER_BASE = "https://streamcenter.st"

TAG = "SPORTS"

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
    if any(k in comb for k in ["f1", "formula 1", "racing", "motogp"]):
        return SPORT_ICONS["motorsport"]
    if any(k in comb for k in ["wwe", "smackdown", "raw", "aew", "ufc", "fighting", "mma", "boxing"]):
        return SPORT_ICONS["wrestling"]

    return DEFAULT_LOGO


def clean_and_normalize_sport(raw_sport: str) -> str:
    clean = re.sub(r"<[^>]+>", "", str(raw_sport)).strip().lower()
    if any(k in clean for k in ["soccer", "football"]):
        return "Football"
    if "cricket" in clean:
        return "Cricket"
    if "basketball" in clean or "nba" in clean:
        return "Basketball"
    if "tennis" in clean:
        return "Tennis"
    if "hockey" in clean:
        return "Hockey"
    if any(k in clean for k in ["motorsport", "racing", "f1"]):
        return "Motorsport"
    if any(k in clean for k in ["wwe", "wrestling", "fight", "fighting", "ufc", "boxing"]):
        return "Fighting"
    return clean.capitalize() or "Live Sports"


LEAGUE_PATTERNS = [
    (r"premier league|epl", "Premier League"),
    (r"la liga|laliga", "La Liga"),
    (r"serie a", "Serie A"),
    (r"bundesliga", "Bundesliga"),
    (r"ligue 1", "Ligue 1"),
    (r"champions league|ucl", "UEFA Champions League"),
    (r"europa league|uel", "UEFA Europa League"),
    (r"saudi pro league|spl", "Saudi Pro League"),
    (r"ipl|indian premier league", "Indian Premier League (IPL)"),
    (r"bpl", "Bangladesh Premier League (BPL)"),
    (r"psl", "Pakistan Super League (PSL)"),
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
    return sport


def parse_team_names(title: str) -> dict[str, str]:
    clean = title.split(":")[-1].strip() if ":" in title else title
    clean = clean.replace("–", "-").replace("—", "-")
    for d in [" vs. ", " vs ", " - ", " @ "]:
        if d in clean.lower():
            parts = re.split(re.escape(d), clean, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                return {
                    "team_a": parts[0].strip(),
                    "team_b": parts[1].strip(),
                }
    return {"team_a": clean.strip(), "team_b": clean.strip()}


@dataclass(slots=True)
class SportsEvent:
    sport: str
    title: str
    start_dt: datetime
    stream_links: list[dict]


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
        sub_cat = ev.get("sub_category", "Live")
        logo = ev.get("logo", "")
        status = ev.get("status", "")

        for ch in ev.get("channels", []):
            url = ch.get("url")
            if not url:
                continue

            ch_name = ch.get("channel_name", "Stream")
            referer = ch.get("headers", {}).get("Referer", "")

            status_lbl = f" [{status}]" if status else ""
            display_name = f"[{sub_cat}] {ev['title']} | {ch_name}{status_lbl}"

            m3u_lines.append(
                f'#EXTINF:-1 '
                f'tvg-id="Live.Event.us" '
                f'tvg-logo="{logo}" '
                f'group-title="{sub_cat}",'
                f'{display_name}'
            )

            if referer:
                m3u_lines.append(f'#EXTVLCOPT:http-referrer={referer}')
                m3u_lines.append(f'#EXTVLCOPT:http-user-agent={UA}')

            m3u_lines.append(url)
            m3u_count += 1

    M3U_OUTPUT.write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")
    log.info(f"Saved M3U Playlist: {M3U_OUTPUT} ({m3u_count} streams)")


async def request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict | None = None,
    timeout: float = 8.0,
) -> httpx.Response | None:
    try:
        response = await client.get(url, headers=headers, timeout=timeout)
        if response.status_code == 200:
            return response
        return None
    except Exception:
        return None


# -----------------------------------------------------------------------------
# CricHD এবং EdgeStream সরাসরি m3u8 এক্সট্রাক্টর
# -----------------------------------------------------------------------------
async def extract_crichd_m3u8(client: httpx.AsyncClient, page_url: str) -> str | None:
    resp = await request(client, page_url, headers={"User-Agent": UA, "Referer": f"{CRICHD_BASE}/"})
    if not resp:
        return None

    # ১. পেজের ভেতর সরাসরি .m3u8 লিংক থাকলে
    m_direct = re.search(r'["\'](https?://[^"\'\s<>]+\.m3u8(?:\?[^"\'\s<>]*)?)["\']', resp.text)
    if m_direct:
        return m_direct.group(1)

    # ২. আইফ্রেম প্লেয়ার থাকলে
    soup = HTMLParser(resp.content)
    iframe = soup.css_first("iframe")
    if not iframe:
        return None

    src = iframe.attributes.get("src")
    if not src:
        return None

    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = urljoin(page_url, src)

    p_resp = await request(client, src, headers={"User-Agent": UA, "Referer": page_url})
    if not p_resp:
        return None

    m_player = re.search(r'["\'](https?://[^"\'\s<>]+\.m3u8(?:\?[^"\'\s<>]*)?)["\']', p_resp.text)
    if m_player:
        return m_player.group(1)

    return None


# -----------------------------------------------------------------------------
# CricHD লাইভ শিডিউল পার্সিং
# -----------------------------------------------------------------------------
async def get_crichd_events(client: httpx.AsyncClient) -> list[SportsEvent]:
    events = []
    resp = await request(client, CRICHD_BASE, headers={"User-Agent": UA})
    if not resp:
        return events

    soup = HTMLParser(resp.content)
    now_utc = datetime.now(timezone.utc)
    seen_titles = set()

    for table in soup.css("table.table, table"):
        for row in table.css("tr"):
            link_tag = row.css_first("a[href*='/update/'], a[href*='live']")
            if not link_tag:
                continue

            event_title = link_tag.text(strip=True)
            event_href = link_tag.attributes.get("href") or ""

            if not event_title or event_title in seen_titles:
                continue

            # সময় খোঁজা
            row_text = row.text(strip=True)
            time_match = re.search(r"(\d{1,2}:\d{2})", row_text)
            event_dt = now_utc
            if time_match:
                h, m = map(int, time_match.group(1).split(":"))
                event_dt = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)

            # অতীত ৩ ঘণ্টার পুরানো ম্যাচ বাদ
            if event_dt < now_utc - timedelta(hours=3):
                continue
            if event_dt > now_utc + timedelta(hours=24):
                continue

            seen_titles.add(event_title)

            # খেলা অনুমান করা
            sport = "Football"
            if any(k in event_title.lower() for k in ["cricket", "t20", "ipl", "bpl", "odi"]):
                sport = "Cricket"
            elif any(k in event_title.lower() for k in ["nba", "basketball"]):
                sport = "Basketball"
            elif any(k in event_title.lower() for k in ["tennis", "atp", "wta", "open"]):
                sport = "Tennis"

            channel_url = urljoin(CRICHD_BASE, event_href)
            events.append(
                SportsEvent(
                    sport=clean_and_normalize_sport(sport),
                    title=event_title,
                    start_dt=event_dt,
                    stream_links=[{"name": "HD Stream", "url": channel_url}],
                )
            )

    return events


# -----------------------------------------------------------------------------
# EdgeStream ব্যাকআপ সোর্স
# -----------------------------------------------------------------------------
async def get_streamcenter_events(client: httpx.AsyncClient) -> list[SportsEvent]:
    events = []
    target_url = urljoin(STREAMCENTER_BASE, "game-cards/embed")
    resp = await request(client, target_url, headers={"User-Agent": UA, "Referer": "https://streame.center/"})
    if not resp:
        return events

    soup = HTMLParser(resp.content)
    now_utc = datetime.now(timezone.utc)

    for card in soup.css(".game-card-group"):
        sport_elem = card.css_first("h2")
        raw_sport = sport_elem.text(strip=True) if sport_elem else "Football"

        for game in card.css(".game-card-row"):
            name_elem = game.css_first("h3")
            if not name_elem:
                continue

            time_elem = game.css_first(".game-card-when > time")
            dt_str = time_elem.attributes.get("datetime") if time_elem else None
            event_dt = now_utc
            if dt_str:
                try:
                    if dt_str.endswith("Z"):
                        dt_str = dt_str[:-1] + "+00:00"
                    event_dt = datetime.fromisoformat(dt_str).astimezone(timezone.utc)
                except Exception:
                    pass

            event_title = name_elem.text(strip=True)
            links = []
            for a in game.css("a.game-card-open-link"):
                href = a.attributes.get("href")
                if href:
                    links.append({"name": a.text(strip=True) or "Stream", "url": urljoin(STREAMCENTER_BASE, href)})

            if links:
                events.append(
                    SportsEvent(
                        sport=clean_and_normalize_sport(raw_sport),
                        title=event_title,
                        start_dt=event_dt,
                        stream_links=links,
                    )
                )

    return events


async def scrape() -> None:
    log.info("Fetching sports events from CricHD & EdgeStream...")

    async with httpx.AsyncClient(
        headers={"User-Agent": UA},
        follow_redirects=True,
        timeout=10.0,
        http2=True,
    ) as client:

        # CricHD এবং EdgeStream উভয় সোর্স একসাথে কল করা
        crichd_task = get_crichd_events(client)
        streamcenter_task = get_streamcenter_events(client)
        results = await asyncio.gather(crichd_task, streamcenter_task)

        all_events = results[0] + results[1]
        log.info(f"Total {len(all_events)} active sports matches found!")

        if not all_events:
            log.error("No active events found from sources.")
            return

        final_playlist: list[dict] = []
        seen_matches = set()

        for ev in all_events:
            if ev.title in seen_matches:
                continue
            seen_matches.add(ev.title)

            event_channels = []
            for idx, slink in enumerate(ev.stream_links, start=1):
                # লিঙ্ক যদি EdgeStream এর হয়
                if "game-cards" in slink["url"] or "streamcenter" in slink["url"]:
                    p_resp = await request(client, slink["url"])
                    if p_resp:
                        soup = HTMLParser(p_resp.content)
                        iframe = soup.css_first("iframe")
                        if iframe and iframe.attributes.get("src"):
                            splits = urlsplit(iframe.attributes.get("src"))
                            params = dict(parse_qsl(splits.query))
                            sid = params.get("stream")
                            if sid:
                                stream_url = f"https://edgestream3.pro/stream/{sid}.m3u8" if sid.isdigit() else f"https://edgestream2.pro/hls/{sid}.m3u8"
                                event_channels.append({
                                    "channel_name": f"{slink['name']} Stream",
                                    "stream_type": "HLS",
                                    "url": stream_url,
                                    "raw_link": f"{stream_url}|Referer=https://streame.center/",
                                    "headers": {"Referer": "https://streame.center/"},
                                    "drm_key": None
                                })
                else:
                    # CricHD m3u8 এক্সট্রাক্ট করা
                    m3u8 = await extract_crichd_m3u8(client, slink["url"])
                    if m3u8:
                        event_channels.append({
                            "channel_name": f"Server {idx} - HD",
                            "stream_type": "HLS",
                            "url": m3u8,
                            "raw_link": f"{m3u8}|Referer={CRICHD_BASE}/",
                            "headers": {"Referer": f"{CRICHD_BASE}/"},
                            "drm_key": None
                        })

            if event_channels:
                start_bst = ev.start_dt.astimezone(BST_TZ)
                end_bst = start_bst + timedelta(hours=3)

                t_names = parse_team_names(ev.title)
                team_a_name = t_names["team_a"]
                team_b_name = t_names["team_b"]

                event_logo = get_event_logo(ev.sport, ev.title)
                sub_category = get_sub_category(ev.sport, ev.title)

                formatted_event = {
                    "title": ev.title,
                    "sport": ev.sport,
                    "sub_category": sub_category,
                    "status": determine_status(ev.start_dt, early_minutes=15),
                    "is_hot": False,
                    "slug": create_slug(ev.title),
                    "schedule": {
                        "timezone": "BST (UTC+6)",
                        "start_time_bst": start_bst.strftime("%b %d, %Y, %I:%M:%S %p"),
                        "end_time_bst": end_bst.strftime("%b %d, %Y, %I:%M:%S %p"),
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
        log.info(f"Successfully generated playlist with {len(final_playlist)} live matches!")


if __name__ == "__main__":
    asyncio.run(scrape())
