#!/usr/bin/env python3
"""
SELF-DRIVING STEAM SCRAPER (LOCAL MODE)
---------------------------------------
- Strict mirror (jsnli)
- Fetch 1 appid per cycle (1 detik)
- Auto-backoff: 10m → 30m → 1h
- Auto-resume progress
- Strict fields:
    appid, title, header, header_candidates,
    genre, short_description,
    developers, publishers,
    release_date,
    price_display, price_normalized,
    protection,
    last_update
"""

import asyncio
import aiohttp
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from typing import Optional, Dict, Any, List

# ------------------------------
# CONFIG
# ------------------------------
OUTPUT = Path("steam_data.json")
PROGRESS_FILE = Path("progress.json")

APPID_MIRROR = (
    "https://raw.githubusercontent.com/jsnli/steamappidlist/"
    "refs/heads/master/data/games_appid.json"
)

DETAIL_URL = "https://store.steampowered.com/api/appdetails?appids={appid}&cc=id&l=en"
HTML_URL = "https://store.steampowered.com/app/{appid}/"

FETCH_DELAY = 1            # 1 detik
BACKOFF_STEPS = [600, 1800, 3600]  # 10m → 30m → 1h
MAX_ATTEMPTS = 3

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (X11; Linux x86_64)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5)",
    "Mozilla/5.0 (iPad; CPU OS 16_2 like Mac OS X)",
]


def now():
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------
# PARSE STORE API
# ------------------------------
def parse_store_api(appid: int, js: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d = js.get(str(appid), {})
    if not d.get("success"):
        return None
    info = d.get("data")
    if not info:
        return None

    header = info.get("header_image")
    genres = ", ".join([g.get("description", "") for g in info.get("genres", [])])

    # price
    price = info.get("price_overview")
    if price:
        init = price.get("initial", 0)
        fin = price.get("final", 0)
        chosen = init if init > 0 else fin
        if chosen > 0:
            norm = chosen // 100
            disp = f"Rp {norm:,.0f}".replace(",", ".")
        else:
            disp, norm = "Free", 0
    else:
        disp, norm = "Free", 0

    return {
        "appid": appid,
        "title": info.get("name"),
        "header": header,
        "header_candidates": build_header_candidates(appid, header),
        "genre": genres or None,
        "short_description": info.get("short_description"),
        "developers": info.get("developers", []),
        "publishers": info.get("publishers", []),
        "release_date": (info.get("release_date") or {}).get("date"),
        "price_display": disp,
        "price_normalized": norm,
    }


def build_header_candidates(appid: int, header_api: Optional[str]):
    base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}"
    c = []
    if header_api:
        c.append(header_api)
    c.extend([
        f"{base}/header.jpg",
        f"{base}/header_alt_assets_0.jpg",
        f"{base}/header_alt_assets_1.jpg",
        f"{base}/header_alt_assets_2.jpg",
    ])
    out, seen = [], set()
    for x in c:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ------------------------------
# PROTECTION ENGINE
# ------------------------------
def detect_protection(base: dict, txt: str) -> Optional[bool]:
    t = txt.lower()

    if "denuvo" in t:
        return True

    launch_kw = [
        "electronic arts", "ea", "ea games", "ea sports",
        "ubisoft", "uplay", "ubisoft connect",
        "rockstar games", "social club",
        "activision", "battle.net", "battlenet",
        "epic games", "epic online services",
    ]

    pubs = [p.lower() for p in base.get("publishers", [])]
    devs = [d.lower() for d in base.get("developers", [])]

    if any(l in p for p in pubs for l in launch_kw):
        return True
    if any(l in d for d in devs for l in launch_kw):
        return True

    html_kw = [
        "ea app", "origin",
        "requires ea account",
        "requires ubisoft account",
        "requires rockstar account",
        "requires activision",
        "requires epic account",
        "login with epic",
    ]

    if any(k in t for k in html_kw):
        return True

    anti = ["easy anti-cheat", "eac", "battleye", "vac"]
    if any(a in t for a in anti):
        return None

    return None


# ------------------------------
# NETWORK HELPERS
# ------------------------------
async def fetch_json(session, url):
    for _ in range(MAX_ATTEMPTS):
        try:
            async with session.get(url) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                if r.status == 403:
                    return "403"
        except:
            pass
        await asyncio.sleep(1)
    return None


async def fetch_text(session, url):
    for _ in range(MAX_ATTEMPTS):
        try:
            async with session.get(url) as r:
                if r.status == 200:
                    return await r.text()
                if r.status == 403:
                    return "403"
        except:
            pass
        await asyncio.sleep(1)
    return ""


# ------------------------------
# MAIN ENGINE
# ------------------------------
async def main():
    print("[INIT] Loading mirror...")
    mirror = await fetch_all_mirror()

    appids = [m["appid"] for m in mirror]
    appids.sort()

    db = load_json(OUTPUT, {})
    prog = load_json(PROGRESS_FILE, {"index": 0})

    idx = prog["index"]

    async with aiohttp.ClientSession(headers={"User-Agent": UA[0]}) as sess_api, \
         aiohttp.ClientSession(headers={"User-Agent": UA[1]}) as sess_html:

        backoff_stage = 0

        while idx < len(appids):
            appid = appids[idx]
            print(f"[FETCH] {idx+1}/{len(appids)} → AppID {appid}")

            js = await fetch_json(sess_api, DETAIL_URL.format(appid=appid))
            if js == "403":
                wait = BACKOFF_STEPS[min(backoff_stage, len(BACKOFF_STEPS)-1)]
                print(f"[403] Blocked. Sleeping {wait/60:.0f} minutes...")
                time.sleep(wait)
                backoff_stage += 1
                continue

            base = parse_store_api(appid, js or {})
            if not base:
                idx += 1
                save_progress(idx)
                continue

            html = await fetch_text(sess_html, HTML_URL.format(appid=appid))
            if html == "403":
                wait = BACKOFF_STEPS[min(backoff_stage, len(BACKOFF_STEPS)-1)]
                print(f"[403] HTML Blocked. Sleep {wait/60:.0f}m...")
                time.sleep(wait)
                backoff_stage += 1
                continue

            text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
            prot = detect_protection(base, text)

            full = dict(base)
            full["protection"] = prot
            full["last_update"] = now()

            db[str(appid)] = full
            save_json(OUTPUT, db)

            idx += 1
            save_progress(idx)

            backoff_stage = 0
            time.sleep(FETCH_DELAY)


def save_progress(idx):
    save_json(PROGRESS_FILE, {"index": idx})


async def fetch_all_mirror():
    async with aiohttp.ClientSession(headers={"User-Agent": UA[0]}) as sess:
        data = await fetch_json(sess, APPID_MIRROR)
    return data or []


if __name__ == "__main__":
    asyncio.run(main())
