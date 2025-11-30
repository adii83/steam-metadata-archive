#!/usr/bin/env python3
"""
SELF-DRIVING STEAM SCRAPER (LOCAL MODE)
---------------------------------------
- Strict mirror (jsnli)
- Fetch 1 appid per cycle (1 detik)
- Auto-backoff: 10m → 30m → 1h
- Auto-resume progress
- Strict fields:
    appid, title, header,
    genre, short_description,
    developers, publishers,
    release_date,
    price_display, price_normalized,
    protection,
    last_update
"""

import argparse
import sys
import asyncio
import aiohttp
import json
import re
import time
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from typing import Optional, Dict, Any, List
from html import unescape
 

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

AUTO_PUSH_EVERY = 1000

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
    # Write compact JSON without indentation or trailing newlines
    path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


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

    # PRICE
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

    # (Screenshots and trailer fields removed — no longer produced)

    return {
        "appid": appid,
        "title": info.get("name"),
        "header": header,
        "genre": genres or None,
        "short_description": info.get("short_description"),
        "developers": info.get("developers", []),
        "publishers": info.get("publishers", []),
        "release_date": (info.get("release_date") or {}).get("date"),
        "price_display": disp,
        "price_normalized": norm,
    }


def build_header_candidates(header_url: str, screenshots: List[str]):
    """
    Header resmi + screenshot (API → fallback HTML).
    Maksimal 7 entry: 1 header + 6 screenshot.
    """
    out = []
    seen = set()

    # always push header first
    if header_url and header_url not in seen:
        out.append(header_url)
        seen.add(header_url)

    for url in screenshots:
        if url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= 7:
            break

    return out


# ------------------------------
# PROTECTION ENGINE
# ------------------------------
def detect_protection(base: dict, txt: str, raw_html: Optional[str] = None) -> Optional[bool]:
    """Detect whether the store page indicates additional protection/launcher requirements.

    Parameters:
    - base: metadata dict with 'publishers' and 'developers'
    - txt: plain text extracted from page (lowercased expected)
    - raw_html: raw html source (optional) — used to detect explicit DRM_notice divs

    Returns True for detected protection, None for inconclusive/unknown.
    """
    t = (txt or "").lower()

    # Helper: extract DRM/anticheat div text blocks (normalized, lowercased)
    def extract_drm_text_blocks(html: str) -> List[str]:
        out = []
        if not html:
            return out
        soup = BeautifulSoup(html, "lxml")
        pattern = re.compile(r'(?:drm[_\-\s]?notice|anticheat_section)', re.IGNORECASE)
        for div in soup.find_all("div"):
            classes = div.get("class")
            if not classes:
                continue
            class_str = " ".join(classes)
            if pattern.search(class_str):
                text = div.get_text(" ", strip=True) or ""
                text = unescape(text)
                text = re.sub(r'[\s\u00A0]+', ' ', text).strip().lower()
                out.append(text)
        return out

    def find_phrases_in_blocks(blocks: List[str], phrases: List[str]) -> List[str]:
        found = []
        for block in blocks:
            for p in phrases:
                pl = p.lower()
                try:
                    pat = r"\b" + re.escape(pl) + r"\b"
                    if re.search(pat, block):
                        if p not in found:
                            found.append(p)
                except re.error:
                    if pl in block and p not in found:
                        found.append(p)
        return found

    # If raw_html is provided, only inspect DRM/anticheat UI blocks for the
    # specific phrase list. If nothing matches inside those blocks, return
    # None (inconclusive) so we don't mislabel pages based on other text.
    if raw_html:
        blocks = extract_drm_text_blocks(raw_html)
        html_check_phrases = [
            "ea app",
            "requires ea account",
            "requires ubisoft account",
            "requires rockstar account",
            "requires activision",
            "requires epic account",
            "login with epic",
            # launcher/publisher keywords (kept as phrases to detect explicit launcher notices)
            "electronic arts", "ea games", "ea sports",
            "ubisoft", "uplay", "ubisoft connect",
            "rockstar games", "social club",
            "activision", "battle.net", "battlenet",
            "epic games", "epic online services", "denuvo",
        ]

        # check denuvo inside blocks as well
        for b in blocks:
            if 'denuvo' in b:
                return True

        phrases_found = find_phrases_in_blocks(blocks, html_check_phrases)
        if phrases_found:
            return True
        return None

    # If raw_html not provided, fall back to older heuristics (denuvo / anti-cheat)
    # NOTE: publisher/developer substring heuristics were removed to match
    # the analyzer logic where launcher keywords are only trusted when
    # observed inside DRM/anticheat UI blocks.
    if 'denuvo' in t:
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


def extract_screenshots_from_html(html: str) -> List[str]:
    """
    Fallback screenshot extractor dari HTML Steam Store.
    Kompatibel untuk game lama yang tidak punya JSON screenshots.
    """
    soup = BeautifulSoup(html, "lxml")

    urls = set()

    # Cari semua tag img yang mengandung kata "image" atau "screenshot"
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        low = src.lower()

        if any(ext in low for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            if "header" not in low and "icon" not in low:
                urls.add(src)

    return list(urls)[:6]


async def fetch_screenshots_from_steamdb(session, appid: int) -> List[str]:
    """Fetch screenshots from SteamDB page and normalize URLs.
    Prefer shared.fastly.steamstatic.com where possible.
    """
    url = f"https://steamdb.info/app/{appid}/screenshots/"
    out = []

    try:
        async with session.get(url, headers={"User-Agent": UA[2]}) as r:
            if r.status != 200:
                return []
            html = await r.text()
    except:
        return []

    soup = BeautifulSoup(html, "lxml")

    for img in soup.select("img.screenshot-image"):
        src = img.get("src") or ""
        if not src:
            continue

        # --- NORMALISASI URL ---
        # case 1: //shared.fastly.steamstatic.com
        if src.startswith("//"):
            src = "https:" + src

        # case 2: /appmedia/... (harus ganti domain)
        elif src.startswith("/"):
            src = "https://steamdb.info" + src

        # case 3: nama file doang → SKIP (tidak valid)
        elif not src.startswith("http"):
            continue

        # Filter hanya image
        if src.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            # force shared.fastly.steamstatic.com when possible
            # if src contains a steamstatic domain, prefer shared.fastly
            if "steamstatic.com" in src and "shared.fastly.steamstatic.com" not in src:
                # fallback: replace common domains with shared.fastly
                src = src.replace("cdn.akamai.steamstatic.com", "shared.fastly.steamstatic.com").replace("store.akamai.steamstatic.com", "shared.fastly.steamstatic.com")

            out.append(src)

        if len(out) >= 6:
            break

    return out


# ------------------------------
# PLAYWRIGHT / DYNAMIC FALLBACK
# ------------------------------
# Playwright/dynamic rendering removed — analyzer subprocess is used as canonical fallback.


def run_analyzer_subprocess(appid: int, cwd: Path = None) -> Optional[Dict[str, Any]]:
    """Call the analyzer script as a subprocess with --json output and parse result."""
    try:
        script = Path(__file__).parent / 'analyze_protection_1020790.py'
        cmd = [sys.executable, str(script), '--appid', str(appid), '--json']
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        if proc.returncode != 0:
            return None
        out = proc.stdout.strip()
        if not out:
            return None
        return json.loads(out)
    except Exception:
        return None



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

            # header candidates: only `header` remains (screenshots/trailer fields removed)
            prot = detect_protection(base, text, html)

            # If static HTML is inconclusive, first try analyzer subprocess (parity)
            if prot is None:
                analyzer_res = run_analyzer_subprocess(appid, cwd=Path(__file__).parent)
                if analyzer_res is not None:
                    # Use analyzer decision (no cookie-related logging)
                    prot = analyzer_res.get('decision')
                else:
                    # Analyzer subprocess unavailable — no dynamic fallback configured.
                    print(f"[WARN] Analyzer subprocess failed for AppID {appid}; protection remains inconclusive.")

            full = dict(base)
            full["protection"] = prot
            full["last_update"] = now()

            db[str(appid)] = full
            save_json(OUTPUT, db)

            idx += 1
            save_progress(idx)

            # === AUTO-PUSH SETIAP 200 APP ===
            if idx % AUTO_PUSH_EVERY == 0:
                git_autopush()

            backoff_stage = 0
            time.sleep(FETCH_DELAY)


def save_progress(idx):
    save_json(PROGRESS_FILE, {"index": idx})


async def fetch_all_mirror():
    async with aiohttp.ClientSession(headers={"User-Agent": UA[0]}) as sess:
        data = await fetch_json(sess, APPID_MIRROR)
    return data or []


async def run_test(appids: list):
    """Run quick test: fetch store API for given appids and save results to `steam_data_test.json`"""
    print(f"[TEST] Fetching {len(appids)} appids: {appids}")
    results = {}

    async with aiohttp.ClientSession(headers={"User-Agent": UA[0]}) as sess_api, \
         aiohttp.ClientSession(headers={"User-Agent": UA[1]}) as sess_html:

        for appid in appids:
            print(f"[TEST] AppID {appid}...")
            js = await fetch_json(sess_api, DETAIL_URL.format(appid=appid))
            if js == "403":
                print(f"[TEST] AppID {appid} -> 403 from API")
                continue

            base = parse_store_api(appid, js or {})
            if not base:
                print(f"[TEST] AppID {appid} -> no data from API")
                continue

            html = await fetch_text(sess_html, HTML_URL.format(appid=appid))
            text = BeautifulSoup(html, "lxml").get_text(" ", strip=True) if html else ""
            prot = detect_protection(base, text, html)

            # If static HTML inconclusive, use analyzer subprocess. No dynamic fallback.
            if prot is None:
                analyzer_res = run_analyzer_subprocess(appid, cwd=Path(__file__).parent)
                if analyzer_res is not None:
                    prot = analyzer_res.get('decision')
                else:
                    print(f"[TEST][WARN] Analyzer subprocess failed for AppID {appid}; protection remains inconclusive.")

            full = dict(base)
            full["protection"] = prot
            full["last_update"] = now()

            results[str(appid)] = full

            # print one-app compact JSON to stdout
            print(json.dumps(full, separators=(",", ":"), ensure_ascii=False))

            await asyncio.sleep(FETCH_DELAY)

    # save test output
    out_path = Path("steam_data_test.json")
    out_path.write_text(json.dumps(results, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"[TEST] Saved {len(results)} entries to {out_path}")


def git_autopush():
    try:
        print("[GIT] Auto-push triggered...")

        subprocess.run(["git", "add", "steam_data.json", "progress.json"], check=True)
        subprocess.run(["git", "commit", "-m", "Auto-update (batch 200 apps)"], check=True)
        subprocess.run(["git", "push"], check=True)

        print("[GIT] Auto-push success.")
    except Exception as e:
        print(f"[GIT] Auto-push failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Steam metadata sync / test tool")
    parser.add_argument("--test", action="store_true", help="Run test mode for provided appids")
    parser.add_argument("--appid", nargs="+", type=int, help="One or more appids to test with (--test)")

    args = parser.parse_args()

    if args.test:
        if not args.appid:
            print("ERROR: --test requires --appid <list>")
            sys.exit(2)
        asyncio.run(run_test(args.appid))
    else:
        asyncio.run(main())
