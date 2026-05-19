#!/usr/bin/env python3
"""
Price scraper for OffGamers competitor dashboard — Firecrawl-enabled.

Strategies (per competitor, configured in STRATEGIES below):
  static     - plain httpx GET. Fastest, free. Works when prices live in HTML.
  playwright - headless Chromium. Free but slow (~5-10s/page). Use sparingly.
  firecrawl  - Firecrawl API. Fast cloud rendering. Needs FIRECRAWL_API_KEY.

Reads urls.json (homepage per competitor per category).
Uses nav.derive_url() to map (homepage, product) -> actual product page URL.
Writes prices.json the dashboard fetches on load.
"""

import asyncio
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx
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

FIRECRAWL_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

# Per-competitor time budget. When exceeded the script moves on.
PER_COMPETITOR_BUDGET_SEC = 240   # 4 minutes
PLAYWRIGHT_NAV_TIMEOUT_MS = 15000
PLAYWRIGHT_WAIT_MS = 1500


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
    keywords: List[str] = []
    for m in re.finditer(
            r"(USD|EUR|GBP|JPY|CNY|CAD|MYR|RM|QAR|USDT|USDC|HKD|TWD|IDR|AUD)\s?(\d[\d,]*)",
            product_name, re.I):
        cur, val = m.group(1).upper(), m.group(2)
        keywords += [f"{cur}{val}", f"{cur} {val}", f"{val} {cur}", f"${val}"]
    for m in re.finditer(r"(\d{1,3}(?:,\d{3})*)\s*(Points|Diamonds|Crystals|Coins|Lunites|Shard|Rbx|RP)",
                         product_name, re.I):
        val, label = m.group(1), m.group(2)
        keywords += [f"{val} {label}", val]
    return keywords


async def httpx_get(url: str) -> str:
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}, follow_redirects=True) as client:
        r = await client.get(url)
        return r.text


async def firecrawl_scrape(url: str) -> Optional[str]:
    if not FIRECRAWL_KEY:
        return None
    api = "https://api.firecrawl.dev/v1/scrape"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(api,
                                  json={"url": url, "formats": ["markdown"], "waitFor": 2000},
                                  headers={"Authorization": f"Bearer {FIRECRAWL_KEY}"})
            if r.status_code == 200:
                data = r.json()
                return data.get("data", {}).get("markdown") or data.get("markdown")
            else:
                print(f"    firecrawl http {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"    firecrawl exception: {e}")
    return None


def extract_price_near_keyword(text: str, keyword: str, window: int = 400) -> Optional[float]:
    for m in re.finditer(re.escape(keyword), text, re.I):
        end = m.end()
        snippet = text[end: end + window]
        for pattern in [
            r"\$\s?(\d+(?:[.,]\d{1,2})?)",
            r"(\d+\.\d{2})\s*(?:USD|EUR|GBP|JPY|MYR|RM)",
            r"From\s*\$\s*(\d+(?:[.,]\d{1,2})?)",
            r"\b(\d+\.\d{2})\b",
        ]:
            pm = re.search(pattern, snippet)
            if pm:
                val = parse_price(pm.group(1))
                if val and 0.1 <= val <= 100000:
                    return val
    return None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

async def scrape_static(competitor_key: str, homepage: str,
                        products: List[str], deadline: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    page_cache: Dict[str, str] = {}
    for product in products:
        if time.time() > deadline:
            print(f"    [{competitor_key}] static: time budget reached")
            break
        target_url = nav.derive_url(competitor_key, homepage, product)
        if not target_url:
            continue
        try:
            if target_url not in page_cache:
                page_cache[target_url] = await httpx_get(target_url)
            html = page_cache[target_url]
        except Exception as e:
            print(f"    [{competitor_key}] static fetch fail: {e}")
            continue
        for kw in denom_keywords(product):
            val = extract_price_near_keyword(html, kw)
            if val:
                out[product] = val
                break
    return out


async def scrape_playwright(competitor_key: str, homepage: str, products: List[str],
                            page: "Page", deadline: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    page_cache: Dict[str, str] = {}
    for product in products:
        if time.time() > deadline:
            print(f"    [{competitor_key}] playwright: time budget reached")
            break
        target_url = nav.derive_url(competitor_key, homepage, product)
        if not target_url:
            continue
        try:
            if target_url not in page_cache:
                await page.goto(target_url, wait_until="domcontentloaded",
                                timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)
                await page.wait_for_timeout(PLAYWRIGHT_WAIT_MS)
                page_cache[target_url] = await page.content()
            html = page_cache[target_url]
        except Exception as e:
            print(f"    [{competitor_key}] playwright fail: {e}")
            continue
        for kw in denom_keywords(product):
            val = extract_price_near_keyword(html, kw)
            if val:
                out[product] = val
                break
    return out


async def scrape_firecrawl(competitor_key: str, homepage: str,
                           products: List[str], deadline: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not FIRECRAWL_KEY:
        print(f"    [{competitor_key}] firecrawl: no API key — skip")
        return out
    page_cache: Dict[str, str] = {}
    for product in products:
        if time.time() > deadline:
            print(f"    [{competitor_key}] firecrawl: time budget reached")
            break
        target_url = nav.derive_url(competitor_key, homepage, product)
        if not target_url:
            continue
        try:
            if target_url not in page_cache:
                md = await firecrawl_scrape(target_url)
                page_cache[target_url] = md or ""
            text = page_cache[target_url]
        except Exception as e:
            print(f"    [{competitor_key}] firecrawl fail: {e}")
            continue
        if not text:
            continue
        for kw in denom_keywords(product):
            val = extract_price_near_keyword(text, kw)
            if val:
                out[product] = val
                break
    return out


# ---------------------------------------------------------------------------
# Strategy assignment per competitor.
# - Static-HTML sites stay on httpx (fastest, free).
# - SPA sites prefer Firecrawl when FIRECRAWL_API_KEY is set;
#   otherwise fall back to Playwright.
# ---------------------------------------------------------------------------

SPA_COMPETITORS = {"OG", "G2G", "Eneba", "MooGold", "ItemkuEN"}

def strategy_for(competitor_key: str) -> str:
    if competitor_key in SPA_COMPETITORS:
        return "firecrawl" if FIRECRAWL_KEY else "playwright"
    return "static"


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

    # Determine if we need a browser (any competitor uses Playwright?)
    needs_browser = False
    sample_keys = set()
    for cat in categories.values():
        for k in (cat.get("competitors") or {}).keys():
            sample_keys.add(k)
    for k in sample_keys:
        if strategy_for(k) == "playwright":
            needs_browser = True
            break

    pw = None
    page = None
    if needs_browser and _HAVE_PLAYWRIGHT:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        page.set_default_navigation_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)

    # Reorganize by competitor first, so we can apply a budget per competitor
    # and short-circuit when one is dragging.
    by_comp: Dict[str, List[tuple]] = {}  # comp_key -> [(cat_name, homepage, products)]
    for cat_name, cat in categories.items():
        products = cat.get("products") or []
        comp_urls = cat.get("competitors") or {}
        if not products or not comp_urls:
            continue
        for comp_key, homepage in comp_urls.items():
            if not homepage:
                continue
            by_comp.setdefault(comp_key, []).append((cat_name, homepage, products))

    total_prices = 0

    for comp_key, work in by_comp.items():
        strategy = strategy_for(comp_key)
        deadline = time.time() + PER_COMPETITOR_BUDGET_SEC
        print(f"\n=== [{comp_key}] strategy={strategy} categories={len(work)} budget={PER_COMPETITOR_BUDGET_SEC}s ===")
        for cat_name, homepage, products in work:
            if time.time() > deadline:
                print(f"  [{comp_key}] budget exhausted, skipping remaining categories")
                break
            print(f"  [{comp_key}] {cat_name} -> {homepage[:60]}")
            try:
                if strategy == "static":
                    prices = await scrape_static(comp_key, homepage, products, deadline)
                elif strategy == "firecrawl":
                    prices = await scrape_firecrawl(comp_key, homepage, products, deadline)
                elif strategy == "playwright" and page:
                    prices = await scrape_playwright(comp_key, homepage, products, page, deadline)
                else:
                    prices = await scrape_static(comp_key, homepage, products, deadline)

                if prices:
                    cat_out = out["prices"].setdefault(cat_name, {})
                    for product, price in prices.items():
                        cat_out.setdefault(product, {})[comp_key] = price
                    total_prices += len(prices)
                    print(f"    -> {len(prices)} prices")
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
