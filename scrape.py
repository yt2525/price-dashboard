#!/usr/bin/env python3
"""
Price scraper for OffGamers competitor dashboard.

Strategies per competitor:
  static     - plain httpx (fastest, free)
  playwright - real headless Chromium (free but slow)
  firecrawl  - Firecrawl API (fast cloud rendering, needs FIRECRAWL_API_KEY)
  moogold    - site-specific Playwright that clicks denomination radios
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

# Site-specific adapters (tile-based extractors, fixes the "face value" bug)
try:
    import adapters
    _HAS_ADAPTERS = True
except ImportError:
    _HAS_ADAPTERS = False
    adapters = None  # type: ignore

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

PER_COMPETITOR_BUDGET_SEC = 240   # 4 minutes per competitor
PLAYWRIGHT_NAV_TIMEOUT_MS = 15000
PLAYWRIGHT_WAIT_MS = 2000


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
        keywords += [f"{cur}{val}", f"{cur} {val}", f"{val} {cur}", f"${val}", f"{val}.00"]
    for m in re.finditer(r"(\d{1,3}(?:,\d{3})*)\s*(Points|Diamonds|Crystals|Coins|Lunites|Shard|Rbx|RP)",
                         product_name, re.I):
        val, label = m.group(1), m.group(2)
        keywords += [f"{val} {label}", val]
    return keywords


def denom_number(product_name: str) -> Optional[str]:
    """The bare denomination number, e.g. '50' from 'USD50' or 'USD 50'."""
    return nav.extract_denom(product_name)


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
                                  json={"url": url, "formats": ["markdown"], "waitFor": 2500},
                                  headers={"Authorization": f"Bearer {FIRECRAWL_KEY}"})
            if r.status_code == 200:
                data = r.json()
                return data.get("data", {}).get("markdown") or data.get("markdown")
            else:
                print(f"    firecrawl http {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"    firecrawl exception: {e}")
    return None


def extract_price_near_keyword(text: str, keyword: str, window: int = 600) -> Optional[float]:
    """Find a $-prefixed or numeric price within `window` chars after `keyword`."""
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
# Generic strategies
# ---------------------------------------------------------------------------

async def scrape_static(competitor_key: str, homepage: str,
                        products: List[str], deadline: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    page_cache: Dict[str, str] = {}
    for product in products:
        if time.time() > deadline:
            break
        target_url = nav.derive_url(competitor_key, homepage, product)
        if not target_url:
            continue
        try:
            if target_url not in page_cache:
                page_cache[target_url] = await httpx_get(target_url)
            html = page_cache[target_url]
        except Exception as e:
            print(f"    [{competitor_key}] static fail: {e}")
            continue
        for kw in denom_keywords(product):
            val = extract_price_near_keyword(html, kw)
            if val:
                out[product] = val
                break
    return out


async def scrape_playwright_generic(competitor_key: str, homepage: str, products: List[str],
                                    page: "Page", deadline: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    page_cache: Dict[str, str] = {}
    for product in products:
        if time.time() > deadline:
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
            print(f"    [{competitor_key}] pw fail: {e}")
            continue
        for kw in denom_keywords(product):
            val = extract_price_near_keyword(html, kw)
            if val:
                out[product] = val
                break
    return out


async def scrape_firecrawl_generic(competitor_key: str, homepage: str,
                                   products: List[str], deadline: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not FIRECRAWL_KEY:
        print(f"    [{competitor_key}] firecrawl: no API key — skip")
        return out
    page_cache: Dict[str, str] = {}
    for product in products:
        if time.time() > deadline:
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
# MooGold-specific Playwright scraper.
# WooCommerce site. Each product page has a <select> dropdown OR radio
# buttons for the "Card Type" variation. Selecting a variation updates
# the price element below.
# ---------------------------------------------------------------------------

async def scrape_moogold(competitor_key: str, homepage: str, products: List[str],
                         page: "Page", deadline: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    # Group products by their derived MooGold page URL
    by_url: Dict[str, List[str]] = {}
    for p in products:
        u = nav.derive_url(competitor_key, homepage, p)
        if u:
            by_url.setdefault(u, []).append(p)

    for url, prods in by_url.items():
        if time.time() > deadline:
            break
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)
            await page.wait_for_timeout(PLAYWRIGHT_WAIT_MS)
            # Wait specifically for the WooCommerce variation form
            try:
                await page.wait_for_selector(".variations, table.variations, form.variations_form",
                                             timeout=8000)
            except Exception:
                pass
        except Exception as e:
            print(f"    [MooGold] nav fail {url[:60]}: {e}")
            continue

        # Check if there's a select dropdown or tile-style buttons
        has_select = await page.locator("select[name^='attribute_'], select.variation-select").count() > 0
        print(f"    [MooGold] {url[-30:]} has_select={has_select} products={len(prods)}")

        for product in prods:
            if time.time() > deadline:
                break
            denom = denom_number(product)
            if not denom:
                continue

            clicked = False
            # Try option labels in order from most specific to least
            label_variants = [
                f"PSN Card {denom} USD",
                f"Steam Wallet {denom} USD",
                f"iTunes Gift Card {denom} USD",
                f"Apple Gift Card {denom} USD",
                f"Razer Gold {denom} USD",
                f"Amazon Gift Card {denom} USD",
                f"{denom} USD",
                f"USD {denom}",
                f"${denom}",
            ]

            if has_select:
                # WooCommerce select dropdown approach
                for label in label_variants:
                    try:
                        sel = page.locator("select[name^='attribute_']").first
                        # Find option whose text contains the label
                        option_value = await sel.evaluate(
                            f"""s => {{
                                for (const o of s.options) {{
                                    if (o.textContent && o.textContent.includes({json.dumps(label)})) return o.value;
                                }}
                                return null;
                            }}"""
                        )
                        if option_value:
                            await sel.select_option(option_value)
                            clicked = True
                            break
                    except Exception:
                        continue
            else:
                # Tile / button approach — click element by visible text
                for label in label_variants:
                    try:
                        loc = page.get_by_text(label, exact=False).first
                        if await loc.count() > 0:
                            await loc.click(timeout=2500)
                            clicked = True
                            break
                    except Exception:
                        continue

            if not clicked:
                continue

            await page.wait_for_timeout(900)

            # MooGold's price after selection appears in .single_variation_wrap or .summary .price
            price_text = ""
            for selector in [
                ".single_variation_wrap .woocommerce-Price-amount",
                ".woocommerce-variation-price .woocommerce-Price-amount",
                ".summary p.price .woocommerce-Price-amount",
                ".single_variation .price",
                "p.price ins .amount",
                "p.price .amount",
                ".price .woocommerce-Price-amount",
            ]:
                try:
                    loc = page.locator(selector).first
                    if await loc.count() > 0:
                        price_text = await loc.text_content(timeout=1500) or ""
                        if price_text.strip():
                            break
                except Exception:
                    continue

            val = parse_price(price_text)
            if val and 0.1 <= val <= 100000:
                out[product] = val
                print(f"      MooGold {product[:40]} -> ${val}")
            else:
                print(f"      MooGold {product[:40]} -> no price (text={price_text[:30]!r})")
    return out


# ---------------------------------------------------------------------------
# Eneba-specific scraper.
# Each denomination has its own URL (templated via nav.URL_TEMPLATES with /en-us/).
# Page shows "From $X.XX" as the lowest offer plus a list of seller offers.
# Uses Firecrawl when key is set (Eneba is heavy JS); falls back to Playwright.
# ---------------------------------------------------------------------------

async def scrape_eneba(competitor_key: str, homepage: str, products: List[str],
                       page: Optional["Page"], deadline: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    page_cache: Dict[str, str] = {}

    for product in products:
        if time.time() > deadline:
            break
        target_url = nav.derive_url(competitor_key, homepage, product)
        if not target_url:
            continue
        try:
            if target_url not in page_cache:
                if FIRECRAWL_KEY:
                    text = await firecrawl_scrape(target_url) or ""
                elif page:
                    await page.goto(target_url, wait_until="domcontentloaded",
                                    timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)
                    await page.wait_for_timeout(PLAYWRIGHT_WAIT_MS)
                    text = await page.content()
                else:
                    text = await httpx_get(target_url)
                page_cache[target_url] = text
            text = page_cache[target_url]
        except Exception as e:
            print(f"    [Eneba] fetch fail: {e}")
            continue

        # Eneba's lowest price appears as "From $9.92" or just "$9.92"
        val = None
        for pattern in [
            r"From\s*\$\s*(\d+(?:\.\d{1,2})?)",
            r"From\s*([\d.,]+)\s*USD",
            r"\$(\d+\.\d{2})",
        ]:
            m = re.search(pattern, text)
            if m:
                val = parse_price(m.group(1))
                if val and 0.5 <= val <= 5000:
                    break
        if val:
            out[product] = val
            print(f"      Eneba {product[:40]} -> ${val}")
    return out


# ---------------------------------------------------------------------------
# Strategy assignment
# ---------------------------------------------------------------------------

# Sites where prices live in static HTML (server-rendered).
STATIC_COMPS = {"Seagm", "Codashop", "LapakGaming", "Unipin"}

# Sites that require JS rendering (SPAs / lazy-loaded prices).
SPA_COMPS = {"OG", "G2G", "Kinguin", "G2A", "Eneba", "ItemkuEN"}


def strategy_for(competitor_key: str) -> str:
    # Dedicated tile-based adapters (Phase 2 — accurate per-site extractors)
    if _HAS_ADAPTERS and competitor_key == "Seagm":
        return "adapter_seagm"
    if _HAS_ADAPTERS and competitor_key == "LapakGaming":
        return "adapter_lapak"
    if _HAS_ADAPTERS and competitor_key == "Codashop":
        return "adapter_codashop"
    if competitor_key == "MooGold":
        return "moogold"
    if competitor_key == "Eneba":
        return "eneba"
    if competitor_key in STATIC_COMPS:
        return "static"
    if competitor_key in SPA_COMPS:
        return "firecrawl" if FIRECRAWL_KEY else "playwright"
    return "static"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    if not URLS_FILE.exists():
        print(f"ERROR: {URLS_FILE} missing.")
        return 1

    config = json.loads(URLS_FILE.read_text())
    categories: Dict[str, dict] = config.get("categories", {})

    out: Dict = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prices": {}
    }

    # Determine if we need Playwright
    needs_browser = False
    for cat in categories.values():
        for k in (cat.get("competitors") or {}).keys():
            s = strategy_for(k)
            if s in ("playwright", "moogold"):
                needs_browser = True
                break
        if needs_browser:
            break

    pw = None
    page = None
    if needs_browser and _HAVE_PLAYWRIGHT:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        page.set_default_navigation_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)

    # Reorganize work by competitor
    by_comp: Dict[str, List[tuple]] = {}
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
    print(f"Strategies: {[(k, strategy_for(k)) for k in by_comp.keys()]}")
    print(f"Firecrawl key set: {bool(FIRECRAWL_KEY)}")

    for comp_key, work in by_comp.items():
        strategy = strategy_for(comp_key)
        deadline = time.time() + PER_COMPETITOR_BUDGET_SEC
        print(f"\n=== [{comp_key}] strategy={strategy} categories={len(work)} budget={PER_COMPETITOR_BUDGET_SEC}s ===")
        for cat_name, homepage, products in work:
            if time.time() > deadline:
                print(f"  [{comp_key}] budget exhausted, skipping {cat_name}")
                continue
            print(f"  [{comp_key}] {cat_name} -> {homepage[:60]}")
            try:
                if strategy == "static":
                    prices = await scrape_static(comp_key, homepage, products, deadline)
                elif strategy == "firecrawl":
                    prices = await scrape_firecrawl_generic(comp_key, homepage, products, deadline)
                elif strategy == "playwright" and page:
                    prices = await scrape_playwright_generic(comp_key, homepage, products, page, deadline)
                elif strategy == "moogold" and page:
                    prices = await scrape_moogold(comp_key, homepage, products, page, deadline)
                elif strategy == "eneba":
                    prices = await scrape_eneba(comp_key, homepage, products, page, deadline)
                elif strategy == "adapter_seagm":
                    prices = await adapters.scrape_seagm(homepage, products)
                elif strategy == "adapter_lapak":
                    prices = await adapters.scrape_lapakgaming(homepage, products)
                elif strategy == "adapter_codashop":
                    prices = await adapters.scrape_codashop(homepage, products)
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
