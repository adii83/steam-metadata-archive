import urllib.request
import json
import re
import os
import time
from html import unescape
import urllib.parse

DEFAULT_APPID = 3240220


def build_urls(appid: int):
    return (
        f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=id&l=en",
        f"https://store.steampowered.com/app/{appid}/",
    )
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode('utf-8', errors='ignore')


def extract_inner_html_for_class(html_text, class_pattern):
    # find opening div with class attribute matching pattern
    for m in re.finditer(r'<div[^>]*class=["\']([^"\']*)["\'][^>]*>', html_text, re.IGNORECASE):
        classes = m.group(1)
        if re.search(class_pattern, classes, re.IGNORECASE):
            start_pos = m.end()
            depth = 1
            # iterate over subsequent div tags to find matching closing tag
            for tag in re.finditer(r'<(/?)div\b[^>]*>', html_text[start_pos:], re.IGNORECASE):
                if tag.group(1) == '':
                    depth += 1
                else:
                    depth -= 1
                if depth == 0:
                    end_pos = start_pos + tag.start()
                    return html_text[start_pos:end_pos]
            return None
    return None


def find_phrases_in_drm(html_text, phrases):
    found = []
    inner = extract_inner_html_for_class(html_text, r'drm[_\-\s]?notice')
    if not inner:
        return found
    raw = re.sub(r'<[^>]+>', '', inner)
    raw = unescape(raw)
    inner_text = re.sub(r'[\s\u00A0]+', ' ', raw).strip().lower()
    for p in phrases:
        try:
            pat = r"\b" + re.escape(p) + r"\b"
            if re.search(pat, inner_text):
                found.append(p)
        except re.error:
            if p in inner_text:
                found.append(p)
    return found


def find_all_inner_html_for_class(html_text, class_pattern):
    results = []
    # iterate all opening divs with class attributes
    for m in re.finditer(r'<div[^>]*class=["\']([^"\']*)["\'][^>]*>', html_text, re.IGNORECASE):
        classes = m.group(1)
        if re.search(class_pattern, classes, re.IGNORECASE):
            start_pos = m.end()
            depth = 1
            for tag in re.finditer(r'<(/?)div\b[^>]*>', html_text[start_pos:], re.IGNORECASE):
                if tag.group(1) == '':
                    depth += 1
                else:
                    depth -= 1
                if depth == 0:
                    end_pos = start_pos + tag.start()
                    results.append(html_text[start_pos:end_pos])
                    break
    return results

def analyze_app(appid: int, verbose: bool = True) -> dict:
    DETAIL_URL, HTML_URL = build_urls(appid)

    html = None
    api = None
    try:
        if verbose:
            print('[*] Fetching API...')
        api_text = fetch(DETAIL_URL)
        api = json.loads(api_text)
        if verbose:
            print('[*] API fetched')
    except Exception as e:
        if verbose:
            print('API fetch failed:', e)

    try:
        if verbose:
            print('[*] Fetching HTML...')
        html = fetch(HTML_URL)
        if verbose:
            print('[*] HTML fetched')
    except Exception as e:
        if verbose:
            print('HTML fetch failed:', e)
    # Extract metadata
    publishers = []
    developers = []
    short_desc = ''
    if api:
        d = api.get(str(appid), {})
        if d.get('success') and d.get('data'):
            info = d['data']
            publishers = info.get('publishers', [])
            developers = info.get('developers', [])
            short_desc = info.get('short_description') or ''

    text = (html or '') + '\n' + short_desc
    t = text.lower()

    # keywords
    anti = ["easy anti-cheat", "eac", "battleye", "vac"]

    # When raw HTML is present, only check the specified phrase list.
    # We include launcher-related phrases here so publisher/developer checks are no longer used.
    html_check_phrases = [
        # original html phrases
        "ea app", "drm",
        "requires ea account",
        "requires ubisoft account",
        "requires rockstar account",
        "requires activision",
        "requires epic account",
        "login with epic",
        # launcher/publisher keywords (moved here)
        "electronic arts", "ea", "ea games", "ea sports",
        "ubisoft", "uplay", "ubisoft connect",
        "rockstar games", "social club",
        "activision", "battle.net", "battlenet",
        "epic games", "epic online services", "rockstar", "epic","denuvo",
    ]

    found_phrases = []
    if html:
        found_phrases = find_phrases_in_drm(html, html_check_phrases)
    else:
        found_phrases = []

    # Dynamic/rendered fallback disabled.
    # The Playwright-based cookie rendering and lazy-load trigger logic has been commented out
    # to prevent automated browser rendering. If you need to re-enable dynamic fallback,
    # restore the original block below that handled cookies, Playwright context, page navigation,
    # and DRM extraction.
    fallback_used = None
    cookie_status = 'missing'

    # publisher/developer checks have been merged into html_check_phrases; do not use them separately
    found = {
        'denuvo': ('denuvo' in t),
        'phrases_found': found_phrases,
        'anti': [a for a in anti if a in t],
    }

    if verbose:
        print('\n=== Findings ===')
        print('Publishers:', publishers)
        print('Developers:', developers)
        print('denuvo in text:', found['denuvo'])
        print('html phrases found in raw HTML:', found['phrases_found'])
        print('anti-cheat phrases found:', found['anti'])

    # Decision logic: if any detection check is True -> decision True.
    # If none of the checks indicate protection but anti-cheat tokens are present,
    # treat as inconclusive (None). Only return None if nothing matched.
    any_protection = bool(found['denuvo']) or bool(found['phrases_found'])
    if any_protection:
        decision = True
    else:
        # no explicit protection indicators; if anti-cheat terms exist treat as inconclusive
        if found['anti']:
            decision = None
        else:
            decision = None

    if verbose:
        print('\n=> detect_protection decision (True/None):', decision)

        if decision is True:
            reason = []
            if found['phrases_found']:
                reason.append("phrase(s) matched in HTML: " + ", ".join(found['phrases_found']))
            if found['denuvo']:
                reason.append('`denuvo` found in page text')
            print('Reason(s):', '; '.join(reason))
        elif decision is None:
            if found['anti']:
                print('Reason: anti-cheat terms found (treated as inconclusive).')
            else:
                print('Reason: no matching phrases found; result is inconclusive (null).')

    # return result dict
    return {
        'appid': appid,
        'denuvo': found['denuvo'],
        'phrases_found': found['phrases_found'],
        'anti': found['anti'],
        'decision': decision,
        'fallback_used': fallback_used,
        'cookie_status': cookie_status,
    }
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Analyze protection for a single Steam appid')
    parser.add_argument('--appid', type=int, default=DEFAULT_APPID, help='AppID to analyze')
    parser.add_argument('--json', action='store_true', help='Output minimal JSON (no verbose logs)')
    args = parser.parse_args()

    res = analyze_app(args.appid, verbose=not args.json)
    if args.json:
        import sys
        # output compact JSON with no trailing newline for machine consumption
        sys.stdout.write(json.dumps(res, separators=(",", ":"), ensure_ascii=False))
    else:
        # verbose prints already emitted; also print JSON for completeness
        import sys
        # print compact JSON without indentation and no trailing newline
        sys.stdout.write(json.dumps(res, separators=(",", ":"), ensure_ascii=False))
