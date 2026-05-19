#!/usr/bin/env python3
"""
Price scraper for OffGamers competitor dashboard — smart navigation mode.

- Reads urls.json with one URL per competitor (homepage).
- For each (category, product), derives the proper product page URL via nav.py.
- Runs the adapter to extract the price from that page.
- Writes prices.json the dashboard fetches on load.
"""

import asyncio
import json
import os
import re
import sys
import traceback
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx

# Local module — must sit next to this script
import nav

try:
    from playwright.async_api import async_playwright, Page
    _HAVE_PLAYWRIGHT = True
except ImportError:
    _HAVE_PLAYWRIGHT = False
    Page = None  # type: ignore

ROOT = Path(__file__).parent
URLS_FILE = ROOT / "urls.json"
PRICES_FILE = ROOT / "prices.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Optional: Firecrawl API for SPA sites. Set FIRECRAWL_API_KEY repo secret.
FIRECRAWL_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    text = text.replace(",", "")
    m = re.search(r"(\d+\.\d+|\d+)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def denom_keywords(product_name: str) -> List[str]:
    """Strings that likely appear near the matching price on the page."""
    keywords: List[str] = []
    for m in re.finditer(
            r"(USD|EUR|GBP|JPY|CNY|CAD|MYR|RM|QAR|USDT|USDC|HKD|TWD|IDR|AUD)\s?(\d[\d,]*)",
            product_name, re.I):
        cur, val = m.group(1).upper(), m.group(2)
        keywords += [f"{cur}{val}", f"{cur} {val}", f"{val} {cur}", f"${val}"]
    # Also raw "N Points" / "N Diamonds" style for game currencies
    for m in re.finditer(r"(\d{1,3}(?:,\d{3})*)\s*(Points|Diamonds|Crystals|Coins|Lunites|Shard|Rbx|RP)",
                         product_name, re.I):
        val, label = m.group(1), m.group(2)
        keywords += [f"{val} {label}", val]
    return keywords


async def httpx_get(url: str) -> str:
    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}, follow_redirects=True) as client:
        r = await client.get(url)
        return r.text


async def firecrawl_scrape(url: str) -> Optional[str]:
    """If FIRECRAWL_API_KEY is set, fetch a JS-rendered page via Firecrawl."""
    if not FIRECRAWL_KEY:
        return None
    api = "https://api.firecrawl.dev/v1/scrape"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(api,
                              json={"url": url, "formats": ["markdown"], "waitFor": 2000},
                              headers={"Authorization": f"Bearer {FIRECRAWL_KEY}"})
        if r.status_code == 200:
            data = r.json()
            return data.get("data", {}).get("markdown") or data.get("markdown")
    return None


def extract_price_near_keyword(text: str, keyword: str, window: int = 400) -> Optional[float]:
    """Find a $-prefixed or numeric price within `window` chars after `keyword`."""
    for m in re.finditer(re.escape(keyword), text, re.I):
        end = m.end()
        snippet = text[end: end + window]
        # Try various price formats
        for pattern in [
            r"\$\s?(\d+(?:[.,]\d{1,2})?)",   # $9.92  or  $ 9.92
            r"(\d+\.\d{2})\s*(?:USD|EUR|GBP|JPY|MYR|RM)",  # 9.92 USD
            r"From\s*\$\s*(\d+(?:[.,]\d{1,2})?)",
            r"\b(\d+\.\d{2})\b",             # generic float
        ]:
            pm = re.search(pattern, snippet)
            if pm:
                val = parse_price(pm.group(1))
                if val and 0.1 <= val <= 100000:
                    return val
    return None


# ---------------------------------------------------------------------------
# Per-competitor scrapers
# Each takes (competitor_key, homepage, products) and returns
# {product_name: price}.  Internally each derives the URL via nav.derive_url.
# ---------------------------------------------------------------------------

async def scrape_static(competitor_key: str, homepage: str, products: List[str]) -> Dict[str, float]:
    """Generic static-HTML scraper.  Used for SeaGM/Codashop/Kinguin/G2A etc."""
    out: Dict[str, float] = {}
    # Cache pages we've already fetched — many products share a category URL
    page_cache: Dict[str, str] = {}

    for product in products:
        target_url = nav.derive_url(competitor_key, homepage, product)
        if not target_url:
            continue
        try:
            if target_url not in page_cache:
                page_cache[target_url] = await httpx_get(target_url)
            html = page_cache[target_url]
        except Exception as e:
            print(f"  [{competitor_key}] fetch fail {target_url[:80]}: {e}")
            continue

        for kw in denom_keywords(product):
            val = extract_price_near_keyword(html, kw)
            if val:
                out[product] = val
                break

    return out


async def scrape_playwright(competitor_key: str, homepage: str, products: List[str],
                            page: "Page") -> Dict[str, float]:
    """Generic JS-rendered scraper using Playwright."""
    out: Dict[str, float] = {}
    page_cache: Dict[str, str] = {}

    for product in products:
        target_url = nav.derive_url(competitor_key, homepage, product)
        if not target_url:
            continue
        try:
            if target_url not in page_cache:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)
                page_cache[target_url] = await page.content()
            html = page_cache[target_url]
        except Exception as e:
            print(f"  [{competitor_key}] pw fail {target_url[:80]}: {e}")
            continue

        for kw in denom_keywords(product):
            val = extract_price_near_keyword(html, kw)
            if val:
                out[product] = val
                break

    return out


async def scrape_firecrawl(competitor_key: str, homepage: str, products: List[str]) -> Dict[str, float]:
    """Firecrawl-backed scraper (handles JS, anti-bot, proxies)."""
    out: Dict[str, float] = {}
    if not FIRECRAWL_KEY:
        return out
    page_cache: Dict[str, str] = {}

    for product in products:
        target_url = nav.derive_url(competitor_key, homepage, product)
        if not target_url:
            continue
        try:
            if target_url not in page_cache:
                md = await firecrawl_scrape(target_url)
                page_cache[target_url] = md or ""
            text = page_cache[target_url]
        except Exception as e:
            print(f"  [{competitor_key}] firecrawl fail {target_url[:80]}: {e}")
            continue
        if not text:
            continue

        for kw in denom_keywords(product):
            val = extract_price_near_keyword(text, kw)
            if val:
                out[product] = val
                break
    return out


# Strategy per competitor — pick scrape_static / scrape_playwright / scrape_firecrawl.
# Switch a competitor to "firecrawl" once you've set FIRECRAWL_API_KEY in repo secrets.
STRATEGIES = {
    "Seagm":       "static",
    "Codashop":    "static",
    "Kinguin":     "static",
    "G2A":         "static",
    "G2G":         "playwright",   # try playwright first; flip to "firecrawl" if you have credits
    "OG":          "playwright",
    "Eneba":       "playwright",
    "MooGold":     "playwright",
    "ItemkuEN":    "playwright",
    "LapakGaming": "static",
    "Unipin":      "static",
}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main() -> int:
    if not URLS_FILE.exists():
        print(f"ERROR: {URLS_FILE} missing. Export it from the dashboard "
              f"('Export URLs JSON') and commit it to the repo.")
        return 1

    config = json.loads(URLS_FILE.read_text())
    categories: Dict[str, dict] = config.get("categories", {})

    out: Dict = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prices": {}
    }

    need_browser = any(s == "playwright" for s in STRATEGIES.values())
    pw = None
    page = None
    if need_browser and _HAVE_PLAYWRIGHT:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()

    total_prices = 0

    for cat_name, cat in categories.items():
        products: List[str] = cat.get("products", [])
        comp_urls: Dict[str, str] = cat.get("competitors", {})
        if not products or not comp_urls:
            continue
        cat_out: Dict[str, Dict[str, float]] = out["prices"].setdefault(cat_name, {})

        print(f"\n=== {cat_name} — {len(products)} products ===")
        for comp_key, homepage in comp_urls.items():
            if not homepage:
                continue
            strategy = STRATEGIES.get(comp_key, "static")
            print(f"  [{comp_key}] {strategy} · {homepage}")
            try:
                if strategy == "static":
                    prices = await scrape_static(comp_key, homepage, products)
                elif strategy == "firecrawl":
                    prices = await scrape_firecrawl(comp_key, homepage, products)
                elif strategy == "playwright" and page:
                    prices = await scrape_playwright(comp_key, homepage, products, page)
                else:
                    prices = await scrape_static(comp_key, homepage, products)

                for product, price in prices.items():
                    cat_out.setdefault(product, {})[comp_key] = price
                print(f"    → captured {len(prices)} prices")
                total_prices += len(prices)
            except Exception as e:
                print(f"    ERROR: {e}")
                traceback.print_exc(limit=2)

    if pw:
        await pw.stop()

    PRICES_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nWrote {PRICES_FILE.name}: {total_prices} prices captured.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
