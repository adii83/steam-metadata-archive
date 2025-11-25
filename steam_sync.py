#!/usr/bin/env python3
"""
Steam Metadata Sync (FULL + DAILY, pakai AppID mirror jsnli)

Field per appid:

- appid (int)
- title (str)
- header (str)
- header_candidates (list[str])
- genre (str, comma-separated)
- short_description (str)
- developers (list[str])
- publishers (list[str])
- release_date (str)
- price_display (str, "Rp 150.000" / "Free")
- price_normalized (int, contoh 150000)
- protection (bool|null)
- last_update (ISO datetime)

Mode:
- FULL SCAN  : python steam_sync.py --full
- DAILY SCAN : python steam_sync.py
"""

import asyncio
import aiohttp
import json
import os
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from aiohttp import ClientTimeout
from bs4 import BeautifulSoup

# =====================================================
# CONFIG
# =====================================================

OUTPUT_PATH = Path("steam_data.json")

# AppID mirror (selalu update) dari jsnli/steamappidlist
APPID_SOURCE = "https://raw.githubusercontent.com/jsnli/steamappidlist/refs/heads/master/data/games_appid.json"

# Steam API & Store
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_STORE_PAGE_URL = "https://store.steampowered.com/app/{appid}/"

# Batasan
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "8"))
DAILY_REFRESH_COUNT = int(os.getenv("DAILY_REFRESH_COUNT", "300"))

# Anti-rate-limit config (bisa override via env di GitHub Actions)
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY_MS", "150")) / 1000.0
AIOHTTP_TIMEOUT = float(os.getenv("AIOHTTP_TIMEOUT", "40"))
REQUEST_ATTEMPTS = int(os.getenv("REQUEST_ATTEMPTS", "3"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (X11; Linux x86_64)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5)",
    "Mozilla/5.0 (iPad; CPU OS 16_2 like Mac OS X)",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =====================================================
# GENERIC FETCH (JSON / TEXT) DENGAN RETRY
# =====================================================

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[Dict[str, Any]]:
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        await asyncio.sleep(REQUEST_DELAY)
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[WARN] JSON {url} status {resp.status}")
                    if attempt == REQUEST_ATTEMPTS:
                        return None
                    continue
                return await resp.json()
        except Exception as e:
            print(f"[ERROR] JSON {url} -> {e}")
            if attempt == REQUEST_ATTEMPTS:
                return None
    return None


async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        await asyncio.sleep(REQUEST_DELAY)
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[WARN] TEXT {url} status {resp.status}")
                    if attempt == REQUEST_ATTEMPTS:
                        return ""
                    continue
                return await resp.text()
        except Exception as e:
            print(f"[ERROR] TEXT {url} -> {e}")
            if attempt == REQUEST_ATTEMPTS:
                return ""
    return ""


# =====================================================
# APPID LIST – PAKAI MIRROR GITHUB
# =====================================================

async def get_app_list_from_mirror() -> List[int]:
    """
    Ambil daftar AppID dari mirror:
    https://github.com/jsnli/steamappidlist/tree/master/data

    Format:
    [
      {"appid": 10, "name": "..."},
      {"appid": 20, "name": "..."},
      ...
    ]
    """
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = ClientTimeout(total=AIOHTTP_TIMEOUT)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as sess:
        data = await fetch_json(sess, APPID_SOURCE)

    if not data or not isinstance(data, list):
        print("[ERROR] Mirror invalid / tidak bisa parse AppID list")
        return []

    appids = []
    for item in data:
        try:
            appid = int(item.get("appid"))
            appids.append(appid)
        except:
            continue

    appids = sorted(set(appids))
    print(f"[INFO] AppID mirror total: {len(appids)}")
    return appids


# =====================================================
# PRICE HANDLER (IDR + NON DISKON)
# =====================================================

def parse_price(info: Dict[str, Any]) -> (str, int):
    price = info.get("price_overview")

    if not price:
        # FREE game (no price_overview)
        return "Free", 0

    initial = price.get("initial", 0)          # harga normal *100
    final = price.get("final", 0)              # harga diskon *100
    chosen = initial if initial > 0 else final # prioritaskan harga normal

    if chosen <= 0:
        return "Free", 0

    harga_rp = int(chosen / 100)  # konversi dari *100 ke rupiah
    text = f"Rp {harga_rp:,.0f}".replace(",", ".")
    return text, harga_rp


# =====================================================
# HEADER CANDIDATES
# =====================================================

def build_header_candidates(appid: int, header_from_api: Optional[str]) -> List[str]:
    base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}"

    candidates: List[str] = []
    if header_from_api:
        candidates.append(header_from_api)

    candidates.extend([
        f"{base}/header.jpg",
        f"{base}/header_alt_assets_0.jpg",
        f"{base}/header_alt_assets_1.jpg",
        f"{base}/header_alt_assets_2.jpg",
    ])

    cleaned: List[str] = []
    seen = set()
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            cleaned.append(url)
    return cleaned


# =====================================================
# PROTECTION ENGINE
# =====================================================

def detect_protection(base: Dict[str, Any], html_text: str) -> Optional[bool]:
    """
    Return:
      True → proteksi (Denuvo / EA / Ubisoft / Rockstar / Activision / Epic)
      None → tidak terdeteksi proteksi
    """
    t = html_text.lower()

    # Langsung: Denuvo
    if "denuvo" in t:
        return True

    launcher_keywords = [
        "electronic arts", "ea ", " ea,", " ea)",
        "ubisoft", "uplay", "ubisoft connect",
        "rockstar games", "rockstar north", "social club",
        "activision", "blizzard", "battle.net", "battlenet",
        "epic games", "epic online services",
    ]

    pubs = [p.lower() for p in base.get("publishers", [])]
    devs = [d.lower() for d in base.get("developers", [])]

    if any(l in p for p in pubs for l in launcher_keywords):
        return True
    if any(l in d for d in devs for l in launcher_keywords):
        return True

    html_triggers = [
        "ea app", "origin", "requires ea account",
        "ubisoft connect", "requires ubisoft account",
        "rockstar games launcher", "rockstar social club",
        "requires rockstar account",
        "battle.net", "battlenet", "requires activision account",
        "epic games account", "requires epic account",
    ]
    if any(x in t for x in html_triggers):
        return True

    # Anti-cheat = bukan proteksi game-DRM
    anti_cheat = ["easy anti-cheat", "battleye", "vac", "valve anti-cheat"]
    if any(a in t for a in anti_cheat):
        return None

    # Third-party EULA doang → bukan proteksi
    if "3rd-party eula" in t or "third-party eula" in t:
        return None

    return None


# =====================================================
# PARSE API / LOAD / SAVE
# =====================================================

def parse_store_api(appid: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    entry = data.get(str(appid), {})
    if not entry or not entry.get("success"):
        return None

    info = entry.get("data") or {}
    if not info:
        return None

    appid_num = info.get("steam_appid") or appid
    header = info.get("header_image")

    genres = ", ".join(g.get("description", "") for g in info.get("genres", []) if g.get("description"))

    price_display, price_normalized = parse_price(info)

    result: Dict[str, Any] = {
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


def load_existing_data() -> Dict[int, Dict[str, Any]]:
    if not OUTPUT_PATH.exists():
        return {}
    try:
        raw = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        out: Dict[int, Dict[str, Any]] = {}
        for k, v in raw.items():
            try:
                out[int(k)] = v
            except ValueError:
                continue
        return out
    except Exception as e:
        print(f"[WARN] gagal load JSON lama: {e}")
        return {}


def save_data(data: Dict[int, Dict[str, Any]]) -> None:
    out = {str(k): v for k, v in sorted(data.items(), key=lambda kv: kv[0])}
    OUTPUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[DONE] Saved {len(out)} apps → {OUTPUT_PATH}")


# =====================================================
# FETCH PER APP
# =====================================================

async def fetch_app(
    appid: int,
    api_sess: aiohttp.ClientSession,
    html_sess: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
) -> Optional[Dict[str, Any]]:
    async with sem:
        print(f"[FETCH] AppID {appid}")
        api_url = f"{STEAM_APPDETAILS_URL}?appids={appid}&cc=id&l=en"
        api_json = await fetch_json(api_sess, api_url)

        base = parse_store_api(appid, api_json or {})
        if not base:
            print(f"[WARN] No API data for {appid}")
            return None

        html = await fetch_text(html_sess, STEAM_STORE_PAGE_URL.format(appid=appid))
        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)

        protection = detect_protection(base, text)

        full = dict(base)
        full["protection"] = protection
        full["last_update"] = now_iso()

        return full


# =====================================================
# MAIN LOGIC
# =====================================================

async def main(full_scan: bool = False) -> None:
    # 1. AppID list dari mirror
    all_appids = await get_app_list_from_mirror()
    app_set = set(all_appids)

    # 2. Load JSON lama
    existing = load_existing_data()
    existing_ids = set(existing.keys())

    # 3. Deteksi NEW & REMOVED (follow mirror)
    new_ids = sorted(app_set - existing_ids)
    removed_ids = sorted(existing_ids - app_set)

    if removed_ids:
        print(f"[INFO] Removed {len(removed_ids)} appids (di mirror sudah tidak ada)")
        for rid in removed_ids:
            existing.pop(rid, None)

    print(f"[INFO] Existing in JSON : {len(existing_ids)}")
    print(f"[INFO] New from mirror  : {len(new_ids)}")

    # 4. Tentukan target untuk run ini
    if full_scan or not existing:
        target_ids = all_appids
        mode = "FULL"
    else:
        refresh_sample = []
        if existing:
            sample_size = min(DAILY_REFRESH_COUNT, len(existing))
            refresh_sample = random.sample(list(existing.keys()), sample_size)
        target_ids = sorted(set(new_ids + refresh_sample))
        mode = "DAILY"

    print(f"[MODE] {mode} – total target appids: {len(target_ids)}")

    if not target_ids:
        print("[INFO] Tidak ada yang perlu di-update. Save & exit.")
        save_data(existing)
        return

    # 5. Setup HTTP sessions
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = ClientTimeout(total=AIOHTTP_TIMEOUT)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as api_sess, \
         aiohttp.ClientSession(headers=headers, timeout=timeout) as html_sess:

        tasks = [fetch_app(a, api_sess, html_sess, sem) for a in target_ids]

        updated: List[Dict[str, Any]] = []
        for coro in asyncio.as_completed(tasks):
            item = await coro
            if item:
                updated.append(item)

    print(f"[INFO] Fetched/updated this run: {len(updated)} apps")

    # 6. Merge ke existing
    for item in updated:
        existing[int(item["appid"])] = item

    # 7. Save
    save_data(existing)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Steam metadata sync (full / daily)")
    parser.add_argument("--full", action="store_true", help="Full scan semua AppID")
    args = parser.parse_args()

    asyncio.run(main(full_scan=args.full))
