#!/usr/bin/env python3
"""
Steam Data TEST MODE – hanya ambil AppID tertentu
Program ini fokus pada:

- appid
- title
- header
- header_candidates
- genre
- short_description
- developers
- publishers
- release_date
- price_display
- price_normalized
- protection (Denuvo / EA / Ubisoft / Rockstar / Activision)

Output: steam_data.json (merge/update hanya app yang dites)
"""

import asyncio
import aiohttp
import json
from pathlib import Path
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

OUTPUT_PATH = Path("steam_data.json")

STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_STORE_PAGE_URL = "https://store.steampowered.com/app/{appid}/"

MAX_CONCURRENCY = 6


def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=20) as resp:
            if resp.status != 200:
                print(f"[WARN] {url} status {resp.status}")
            return await resp.json()
    except Exception as e:
        print(f"[ERROR] JSON {url} -> {e}")
        return None


async def fetch_text(session, url):
    try:
        async with session.get(url, timeout=20) as resp:
            if resp.status != 200:
                print(f"[WARN] {url} status {resp.status}")
            return await resp.text()
    except Exception as e:
        print(f"[ERROR] TEXT {url} -> {e}")
        return ""


def parse_store_api(appid: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d = data.get(str(appid), {})
    if not d or not d.get("success"):
        return None

    info = d.get("data", {})
    if not info:
        return None

    appid_num = info.get("steam_appid") or appid
    header = info.get("header_image")

    genres = ", ".join([g.get("description", "") for g in info.get("genres", [])])

    # -------- PRICE HANDLING (IDR + no-discount) --------
    price = info.get("price_overview")

    if price:
        initial = price.get("initial", 0)   # harga normal * 100
        final = price.get("final", 0)       # harga diskon * 100

        chosen = initial if initial > 0 else final  # prioritas harga normal

        if chosen > 0:
            harga_rupiah = int(chosen / 100)
            price_display = f"Rp {harga_rupiah:,.0f}".replace(",", ".")
            price_normalized = harga_rupiah
        else:
            price_display = "Free"
            price_normalized = 0
    else:
        # FREE GAME
        price_display = "Free"
        price_normalized = 0

    result = {
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

    return result

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

    # remove duplicates
    seen = set()
    cleaned = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            cleaned.append(c)
    return cleaned


# -----------------------------
# FINAL PROTECTION ENGINE
# -----------------------------
def detect_protection_final(base: dict, html_text: str) -> Optional[bool]:
    text = html_text.lower()

    # 1. Denuvo langsung true
    if "denuvo" in text or "denuvo anti-tamper" in text:
        return True

    # 2. Launcher publisher/developer = True
    launcher_keywords = [
        "electronic arts", "ea", "ea games", "ea sports",
        "ubisoft", "ubisoft entertainment", "uplay",
        "rockstar games", "rockstar north", "rockstar",
        "activision", "blizzard", "battle.net", "battlenet",
        "epic games",
        "epic games inc",
        "epic",
    ]

    pubs = [p.lower() for p in base.get("publishers", [])]
    devs = [d.lower() for d in base.get("developers", [])]

    if any(l in p for p in pubs for l in launcher_keywords):
        return True

    if any(l in d for d in devs for l in launcher_keywords):
        return True

    # 3. HTML triggers fallback
    html_triggers = [
        "ea app", "origin", "requires ea account",
        "ubisoft connect", "requires ubisoft account",
        "rockstar games launcher", "social club",
        "battle.net", "battlenet", "requires activision account",
        "epic games account",
        "requires epic account",
        "epic online services",
        "eos",
        "login with epic",

    ]

    if any(t in text for t in html_triggers):
        return True

    # 4. Anti-cheat = bukan proteksi
    anti_cheat = [
        "easy anti-cheat", "eac", "battleye",
        "valve anti-cheat", "vac"
    ]
    if any(a in text for a in anti_cheat):
        return None

    # 5. EULA bukan proteksi
    if "third-party eula" in text or "3rd-party eula" in text:
        return None

    return None


def load_existing_data():
    if not OUTPUT_PATH.exists():
        return {}
    try:
        raw = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        parsed = {}
        for k, v in raw.items():
            try:
                parsed[int(k)] = v
            except:
                pass
        return parsed
    except:
        return {}


def save_data(data: Dict[int, Dict[str, Any]]):
    out = {str(k): v for k, v in sorted(data.items(), key=lambda x: x[0])}
    OUTPUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[DONE] Saved {len(out)} apps → steam_data.json")


async def fetch_app(appid: int, api_session, html_session, sem):
    async with sem:
        print(f"[TEST] Fetching {appid}")

        api_url = f"{STEAM_APPDETAILS_URL}?appids={appid}&cc=id&l=en"
        js = await fetch_json(api_session, api_url)
        base = parse_store_api(appid, js or {})
        if not base:
            print(f"[WARN] No API data for {appid}")
            return None

        # Grab HTML
        store_html = await fetch_text(html_session, STEAM_STORE_PAGE_URL.format(appid=appid))
        soup_text = BeautifulSoup(store_html, "lxml").get_text(" ", strip=True)

        # FINAL PROTECTION
        protection = detect_protection_final(base, soup_text)

        full = dict(base)
        full["protection"] = protection
        full["last_update"] = now_iso()

        return full


async def run_test(appids: List[int]):
    existing = load_existing_data()
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    }

    async with aiohttp.ClientSession(headers=headers) as api_sess, \
            aiohttp.ClientSession(headers=headers) as html_sess:

        tasks = [
            fetch_app(a, api_sess, html_sess, sem)
            for a in appids
        ]

        results = []
        for coro in asyncio.as_completed(tasks):
            r = await coro
            if r:
                results.append(r)

    print(f"[TEST] Updated {len(results)} apps")

    for item in results:
        existing[int(item["appid"])] = item

    save_data(existing)


# Entry
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test fetch for selected AppIDs")
    parser.add_argument("--apps", nargs="+", type=int, required=True,
                        help="List AppIDs to test, contoh: --apps 730 271590 39210")

    args = parser.parse_args()

    asyncio.run(run_test(args.apps))
