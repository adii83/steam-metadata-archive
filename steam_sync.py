#!/usr/bin/env python3
"""
Steam Data TEST MODE – ambil AppID tertentu

Field yang dihasilkan per appid:

- appid (int)
- title (str)
- header (str)
- header_candidates (list[str])
- genre (str)
- short_description (str)
- developers (list[str])
- publishers (list[str])
- release_date (str)
- price_display (str, contoh "Rp 150.000" / "Free")
- price_normalized (int, contoh 150000)
- protection (bool|null)
- last_update (ISO datetime)

"""

import asyncio
import aiohttp
import json
import os
import random
from pathlib import Path
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from aiohttp import ClientTimeout

OUTPUT_PATH = Path("steam_data.json")

STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_STORE_PAGE_URL = "https://store.steampowered.com/app/{appid}/"

MAX_CONCURRENCY = 6

# Anti-rate-limit / runtime configuration
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY_MS", "120")) / 1000
AIOHTTP_TIMEOUT = float(os.getenv("AIOHTTP_TIMEOUT", "30"))
REQUEST_ATTEMPTS = int(os.getenv("REQUEST_ATTEMPTS", "3"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (X11; Linux x86_64)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5)",
    "Mozilla/5.0 (iPad; CPU OS 16_2 like Mac OS X)",
]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def fetch_json(session, url):
    # Retry loop with small sleep between attempts to avoid 429
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        await asyncio.sleep(REQUEST_DELAY)
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[WARN] {url} status {resp.status}")
                    # retry on non-200 statuses up to attempts
                    if attempt == REQUEST_ATTEMPTS:
                        return None
                    continue
                return await resp.json()
        except Exception as e:
            print(f"[ERROR] JSON {url} -> {e}")
            if attempt == REQUEST_ATTEMPTS:
                return None
            # otherwise retry
            continue


async def fetch_text(session, url):
    # Retry with per-attempt delay
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        await asyncio.sleep(REQUEST_DELAY)
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[WARN] {url} status {resp.status}")
                    if attempt == REQUEST_ATTEMPTS:
                        return ""
                    continue
                return await resp.text()
        except Exception as e:
            print(f"[ERROR] TEXT {url} -> {e}")
            if attempt == REQUEST_ATTEMPTS:
                return ""
            continue


# -------------------------------------------------------
# PRICE PARSER (IDR + clean + nondiscount)
# -------------------------------------------------------
def parse_price(info):
    price = info.get("price_overview")

    if not price:
        # FREE
        return "Free", 0

    initial = price.get("initial", 0)          # harga normal *100
    final = price.get("final", 0)              # harga diskon *100
    chosen = initial if initial > 0 else final # prioritaskan harga normal

    if chosen <= 0:
        return "Free", 0

    harga_rp = int(chosen / 100)  # buang *100 → harga murni
    text = f"Rp {harga_rp:,.0f}".replace(",", ".")

    return text, harga_rp


# -------------------------------------------------------
# HEADER BUILDER
# -------------------------------------------------------
def build_header_candidates(appid: int, header_from_api: Optional[str]):
    base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}"

    candidates = []
    if header_from_api:
        candidates.append(header_from_api)

    candidates.extend([
        f"{base}/header.jpg",
        f"{base}/header_alt_assets_0.jpg",
        f"{base}/header_alt_assets_1.jpg",
        f"{base}/header_alt_assets_2.jpg",
    ])

    seen = set()
    cleaned = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            cleaned.append(c)
    return cleaned


# -------------------------------------------------------
# PROTECTION ENGINE FINAL
# -------------------------------------------------------
def detect_protection_final(base: dict, html_text: str) -> Optional[bool]:
    text = html_text.lower()

    # 1. Denuvo
    if "denuvo" in text or "denuvo anti-tamper" in text:
        return True

    # 2. Pub/Dev launcher based
    launcher_keywords = [
        "electronic arts", "ea", "ea games", "ea sports",
        "ubisoft", "ubisoft entertainment", "uplay",
        "rockstar games", "rockstar north", "rockstar",
        "activision", "blizzard", "battle.net", "battlenet",
        "epic games", "epic games inc", "epic",
    ]

    pubs = [p.lower() for p in base.get("publishers", [])]
    devs = [d.lower() for d in base.get("developers", [])]

    if any(l in p for p in pubs for l in launcher_keywords):
        return True
    if any(l in d for d in devs for l in launcher_keywords):
        return True

    # 3. HTML triggers
    html_triggers = [
        "ea app", "origin", "requires ea account",
        "ubisoft connect", "requires ubisoft account",
        "rockstar games launcher", "social club",
        "battle.net", "battlenet", "requires activision account",
        "epic games account", "requires epic account",
        "epic online services", "eos", "login with epic",
    ]
    if any(t in text for t in html_triggers):
        return True

    # 4. Anti-cheat → bukan proteksi
    anti_cheat = ["easy anti-cheat", "eac", "battleye", "vac", "valve anti-cheat"]
    if any(a in text for a in anti_cheat):
        return None

    # 5. EULA bukan proteksi
    if "3rd-party eula" in text or "third-party eula" in text:
        return None

    return None


# -------------------------------------------------------
# PARSE API
# -------------------------------------------------------
def parse_store_api(appid: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d = data.get(str(appid), {})
    if not d or not d.get("success"):
        return None

    info = d.get("data", {})
    if not info:
        return None

    appid_num = info.get("steam_appid") or appid
    header = info.get("header_image")

    genres = ", ".join(g.get("description", "") for g in info.get("genres", []) if g.get("description"))

    # PRICE FIX
    price_display, price_normalized = parse_price(info)

    return {
        "appid": appid_num,
        "title": info.get("name"),
        "header": header,
        "header_candidates": build_header_candidates(appid_num, header),
        "genre": genres or None,
        "short_description": info.get("short_description"),
        "developers": info.get("developers", []),
        "publishers": info.get("publishers", []),
        "release_date": (info.get("release_date") or {}).get("date"),
        "price_display": price_display,
        "price_normalized": price_normalized,
    }


# -------------------------------------------------------
# LOAD/SAVE
# -------------------------------------------------------
def load_existing_data():
    if not OUTPUT_PATH.exists():
        return {}
    raw = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    parsed = {}
    for k, v in raw.items():
        try:
            parsed[int(k)] = v
        except:
            pass
    return parsed


def save_data(data: Dict[int, Dict[str, Any]]):
    out = {str(k): v for k, v in sorted(data.items(), key=lambda x: x[0])}
    OUTPUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[DONE] Saved {len(out)} apps → steam_data.json")


# -------------------------------------------------------
# FETCH
# -------------------------------------------------------
async def fetch_app(appid: int, api_session, html_session, sem):
    async with sem:
        print(f"[TEST] Fetching {appid}")

        api_url = f"{STEAM_APPDETAILS_URL}?appids={appid}&cc=id&l=en"
        js = await fetch_json(api_session, api_url)

        base = parse_store_api(appid, js or {})
        if not base:
            print(f"[WARN] No API data for {appid}")
            return None

        store_html = await fetch_text(html_session, STEAM_STORE_PAGE_URL.format(appid=appid))
        soup_text = BeautifulSoup(store_html, "lxml").get_text(" ", strip=True)

        protection = detect_protection_final(base, soup_text)

        full = dict(base)
        full["protection"] = protection
        full["last_update"] = now_iso()

        return full


# -------------------------------------------------------
# MAIN TEST MODE
# -------------------------------------------------------
async def run_test(appids: List[int]):
    existing = load_existing_data()
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    # rotating user-agent + accept-language to appear more like real traffic
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
    }

    timeout = ClientTimeout(total=AIOHTTP_TIMEOUT)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as api_sess, \
         aiohttp.ClientSession(headers=headers, timeout=timeout) as html_sess:

        tasks = [fetch_app(a, api_sess, html_sess, sem) for a in appids]

        results = []
        for coro in asyncio.as_completed(tasks):
            r = await coro
            if r:
                results.append(r)

    print(f"[TEST] Updated {len(results)} apps")

    for item in results:
        existing[int(item["appid"])] = item

    save_data(existing)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test fetch for selected AppIDs")
    parser.add_argument("--apps", nargs="+", type=int, required=True)
    args = parser.parse_args()

    asyncio.run(run_test(args.apps))
