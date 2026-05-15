#!/usr/bin/env python3
"""
Price scraper for OffGamers competitor dashboard.

- Reads urls.json (committed alongside this file in the repo).
- For each (category, competitor) URL, runs the matching adapter.
- Writes prices.json with the captured prices.
- The dashboard fetches prices.json on load and merges the values
  into any product rows that don't have a manually-entered price.

Adapters live in the ADAPTERS dict below — one function per competitor.
Each adapter takes (url, products, page=None) and returns a dict
{product_name: float_price}.

httpx-only adapters do not need the Playwright browser.
Playwright adapters take an open page object.
"""

import asyncio
import json
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx

# Playwright is only imported when needed (Playwright adapters present)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_price(text: str) -> Optional[float]:
    """Extract the first decimal number from a string. Returns None if none."""
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+\.\d+|\d+)", text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def denom_keywords(product_name: str) -> List[str]:
    """
    From a product name like 'Steam Wallet Code USD25 (US)', return short
    keywords that can be searched in HTML to locate the matching price.
    """
    keywords = []
    # USD25, USD 25, 25 USD, $25
    for m in re.finditer(r"(USD|EUR|GBP|JPY|CNY|CAD|MYR|RM|QAR|USDT|USDC)\s?(\d[\d,]*)",
                         product_name, re.I):
        cur, val = m.group(1), m.group(2)
        keywords.append(f"{cur}{val}")
        keywords.append(f"{cur} {val}")
        keywords.append(f"{val} {cur}")
    return keywords


# ---------------------------------------------------------------------------
# Adapters — one per competitor.
# Add a new one by writing `async def scrape_<comp>` and registering below.
# ---------------------------------------------------------------------------

async def scrape_seagm(url: str, products: List[str], page=None) -> Dict[str, float]:
    """
    SeaGM renders prices in the initial HTML, so a simple GET works.
    Example URL: https://www.seagm.com/playstation-network-card-psn-united-states
    """
    out: Dict[str, float] = {}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as client:
        r = await client.get(url, follow_redirects=True)
        html = r.text

    # SeaGM lists denomination + price like:
    #   "$10 USD ...  $10.00 USD"
    # We look for each product's denomination keyword and a price near it.
    for product in products:
        for kw in denom_keywords(product):
            # Find any occurrence of the keyword, then scan ~300 chars after
            # for a $-prefixed price.
            for m in re.finditer(re.escape(kw), html, re.I):
                window = html[m.end(): m.end() + 400]
                price_m = re.search(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)", window)
                if price_m:
                    val = parse_price(price_m.group(1))
                    if val:
                        out[product] = val
                        break
            if product in out:
                break
    return out


async def scrape_codashop(url: str, products: List[str], page=None) -> Dict[str, float]:
    """
    Codashop ships denomination prices in the initial HTML (server-rendered).
    Example URL: https://www.codashop.com/en-us/playstation-vouchers
    """
    out: Dict[str, float] = {}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as client:
        r = await client.get(url, follow_redirects=True)
        html = r.text

    for product in products:
        for kw in denom_keywords(product):
            m = re.search(re.escape(kw), html, re.I)
            if not m:
                continue
            window = html[m.end(): m.end() + 600]
            # Codashop format: "From $25.00"
            price_m = re.search(r"From\s*\$\s?(\d+(?:\.\d+)?)", window)
            if price_m:
                val = parse_price(price_m.group(1))
                if val:
                    out[product] = val
                    break
    return out


async def scrape_kinguin(url: str, products: List[str], page=None) -> Dict[str, float]:
    """
    Kinguin category pages render the lowest offer price in the HTML.
    Each denomination has its own URL on Kinguin, but the category-level URL
    lists all of them on a single page.
    """
    out: Dict[str, float] = {}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as client:
        r = await client.get(url, follow_redirects=True)
        html = r.text

    for product in products:
        for kw in denom_keywords(product):
            m = re.search(re.escape(kw), html, re.I)
            if not m:
                continue
            window = html[m.end(): m.end() + 800]
            # Common Kinguin pattern: "$86.62" inside a tile
            price_m = re.search(r"\$\s?(\d+(?:\.\d+)?)", window)
            if price_m:
                val = parse_price(price_m.group(1))
                if val:
                    out[product] = val
                    break
    return out


async def scrape_offgamers(url: str, products: List[str], page: "Page") -> Dict[str, float]:
    """
    OffGamers is a Vue SPA. Must use Playwright.
    Strategy: open the page, click each denomination tile, read price.

    NOTE: OffGamers detects region by IP. From GitHub Actions runner (US IP),
    you'll get USD pricing. Adjust mapping logic if needed.
    """
    out: Dict[str, float] = {}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector("text=Denomination", timeout=15000)
    except Exception as e:
        print(f"  [offgamers] page load failed: {e}")
        return out

    for product in products:
        keywords = denom_keywords(product)
        clicked = False
        for kw in keywords:
            # Click the denomination tile by its visible label
            try:
                tile = page.locator(f"text={kw}").first
                if await tile.count() == 0:
                    continue
                await tile.click(timeout=3000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            continue
        await page.wait_for_timeout(700)
        # The price appears in the right-hand sidebar
        try:
            sidebar_text = await page.locator(
                ".price, [class*=price], [class*=Price]"
            ).first.text_content(timeout=2000)
        except Exception:
            sidebar_text = ""
        val = parse_price(sidebar_text)
        if val:
            out[product] = val
    return out


# Stub adapters — fill in when you're ready to add a competitor.
async def stub(url: str, products: List[str], page=None) -> Dict[str, float]:
    return {}


# Register adapters. Each value is (function, needs_browser)
ADAPTERS = {
    "Seagm":       (scrape_seagm,    False),
    "Codashop":    (scrape_codashop, False),
    "Kinguin":     (scrape_kinguin,  False),
    "OG":          (scrape_offgamers, True),
    # Stubs — implement these as you grow:
    "G2G":         (stub, False),
    "G2A":         (stub, False),
    "LapakGaming": (stub, False),
    "Unipin":      (stub, False),
    "Eneba":       (stub, False),
    "MooGold":     (stub, True),
    "ItemkuEN":    (stub, True),
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

    # Determine if we need a browser
    needs_browser = any(
        any(ADAPTERS.get(c, (None, False))[1] for c in cat.get("competitors", {}))
        for cat in categories.values()
    )

    browser_ctx = None
    page = None
    pw = None
    if needs_browser and _HAVE_PLAYWRIGHT:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        browser_ctx = await browser.new_context(user_agent=UA)
        page = await browser_ctx.new_page()

    total_calls = 0
    total_prices = 0

    for cat_name, cat in categories.items():
        products: List[str] = cat.get("products", [])
        comp_urls: Dict[str, str] = cat.get("competitors", {})
        cat_out: Dict[str, Dict[str, float]] = out["prices"].setdefault(cat_name, {})

        print(f"\n=== {cat_name} — {len(products)} products ===")
        for comp_key, url in comp_urls.items():
            if not url:
                continue
            adapter_entry = ADAPTERS.get(comp_key)
            if not adapter_entry:
                print(f"  [{comp_key}] no adapter registered, skipping")
                continue
            adapter_fn, needs_pw = adapter_entry
            if needs_pw and not page:
                print(f"  [{comp_key}] needs browser but Playwright unavailable, skipping")
                continue
            total_calls += 1
            try:
                print(f"  [{comp_key}] {url[:80]}")
                prices = await adapter_fn(url, products, page) if needs_pw else \
                         await adapter_fn(url, products)
                for product, price in prices.items():
                    cat_out.setdefault(product, {})[comp_key] = price
                print(f"    → captured {len(prices)} prices")
                total_prices += len(prices)
            except Exception as e:
                print(f"    ERROR: {e}")
                traceback.print_exc(limit=2)

    if browser_ctx:
        await browser_ctx.close()
    if pw:
        await pw.stop()

    PRICES_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nWrote {PRICES_FILE.name}: {total_calls} adapter calls, "
          f"{total_prices} prices captured.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
