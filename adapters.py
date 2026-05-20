"""
adapters.py — Site-specific scrapers for OffGamers competitor dashboard.

Each function in this module is responsible for ONE competitor. They all
follow the same signature:

    async def scrape_X(homepage: str, products: List[str], context) -> Dict[str, float]

Where `context` is a small object the orchestrator passes in containing
shared resources (httpx client, Playwright page, Firecrawl credentials).

The key principle of this module: rather than scanning flat HTML text for
denomination strings near prices (which can match the wrong number), each
adapter looks for product *tiles* / *cards* in the DOM, extracts the
denomination and the discounted price as a pair from each card, and
matches against incoming products by denomination number.

This eliminates the false-positive "face value as price" bug.
"""

import re
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

import httpx

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False
    BeautifulSoup = None  # type: ignore

import nav


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# When any of these strings appear in a product tile, mark the denomination
# as "N/A" rather than trying to extract a price.
_UNAVAILABLE_PATTERNS = [
    r"out\s*of\s*stock",
    r"sold\s*out",
    r"\bunavailable\b",
    r"not\s*available",
    r"\bn/?a\b",
    r"discontinued",
    r"temporarily\s*unavailable",
    r"coming\s*soon",
    r"notify\s*me",
]


def _is_unavailable(text: str) -> bool:
    if not text:
        return False
    for p in _UNAVAILABLE_PATTERNS:
        if re.search(p, text, re.I):
            return True
    return False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _denom_of(product_name: str) -> Optional[int]:
    """Numeric denomination from a product name. Returns None if not found."""
    s = nav.extract_denom(product_name)
    if not s:
        return None
    try:
        return int(s.replace(",", ""))
    except ValueError:
        try:
            return int(float(s))
        except Exception:
            return None


def _parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    text = text.replace(",", "").replace("\xa0", " ")
    m = re.search(r"(\d+\.\d{1,2}|\d+)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


async def _httpx_get(url: str) -> str:
    async with httpx.AsyncClient(timeout=20,
                                 headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                                 follow_redirects=True) as client:
        r = await client.get(url)
        return r.text


# ---------------------------------------------------------------------------
# SeaGM adapter
# ---------------------------------------------------------------------------
# SeaGM lists every denomination as a "deno-item" / product tile. Each tile
# carries the denomination label AND the discounted price. We pair them up.

async def scrape_seagm(homepage: str, products: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not _HAS_BS4:
        print("    [SeaGM] BeautifulSoup4 not installed — falling back to skip")
        return out

    # Group products by their derived SeaGM page URL
    by_url: Dict[str, List[str]] = {}
    for p in products:
        u = nav.derive_url("Seagm", homepage, p)
        if u:
            by_url.setdefault(u, []).append(p)

    for url, prods in by_url.items():
        try:
            html = await _httpx_get(url)
        except Exception as e:
            print(f"    [SeaGM] fetch fail {url[:60]}: {e}")
            continue

        # Pair (denomination, price) from each product tile on the page
        denom_price_map = _seagm_extract_pairs(html)
        if not denom_price_map:
            print(f"    [SeaGM] no tiles parsed from {url[-40:]}")
            continue
        print(f"    [SeaGM] {url[-40:]} found {len(denom_price_map)} denominations")

        for product in prods:
            denom = _denom_of(product)
            if denom is None:
                continue
            if denom in denom_price_map:
                out[product] = denom_price_map[denom]
            elif float(denom) in denom_price_map:
                out[product] = denom_price_map[float(denom)]
    return out


def _seagm_extract_pairs(html: str):
    """Returns {denomination_numeric: float_price or 'N/A'}."""
    soup = BeautifulSoup(html, "html.parser")
    pairs = {}

    candidates = soup.select(
        "li.deno-item, .deno-item, .product-item, .denomination-item, "
        ".product-tile, a.deno-link, [data-deno], .product-card"
    )
    for card in candidates:
        text = card.get_text(separator=" ", strip=True)
        dm = re.search(r"(?:USD|US\$|\$)\s*(\d{1,5})\b", text, re.I)
        if not dm:
            dm = re.search(r"\b(\d{1,5})\s*USD\b", text, re.I)
        if not dm:
            continue
        denom = float(dm.group(1))

        # Check if this tile is marked as unavailable
        if _is_unavailable(text):
            if denom not in pairs:
                pairs[denom] = "N/A"
            continue

        price_strs = re.findall(r"\$\s?(\d+(?:\.\d{1,2})?)", text)
        if not price_strs:
            continue
        prices = [float(p) for p in price_strs if 0.01 <= float(p) <= 100000]
        if not prices:
            continue
        candidates_below_face = [p for p in prices if p <= denom * 1.05]
        chosen = min(candidates_below_face) if candidates_below_face else min(prices)
        # Don't overwrite an existing real price with a different one for same denom
        if denom not in pairs or pairs[denom] == "N/A":
            pairs[denom] = chosen

    # Fallback: if the selectors didn't match, scan all anchor/link tags
    if not pairs:
        for a in soup.find_all(["a", "li", "div"], limit=300):
            t = a.get_text(separator=" ", strip=True)
            if len(t) > 200 or len(t) < 5:
                continue
            dm = re.search(r"(?:USD|\$)\s*(\d{1,5})\b", t, re.I)
            pm = re.search(r"\$\s?(\d+(?:\.\d{1,2})?)", t)
            if dm and pm:
                d = float(dm.group(1))
                p = float(pm.group(1))
                if d > 0 and p > 0 and p <= d * 1.1 and d not in pairs:
                    pairs[d] = p
    return pairs


# ---------------------------------------------------------------------------
# LapakGaming / Joytify adapter
# ---------------------------------------------------------------------------
# LapakGaming (now joytify.com) uses Next.js. Product list page renders
# denomination cards server-side. Tile contains denomination + price.

async def scrape_lapakgaming(homepage: str, products: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not _HAS_BS4:
        return out

    by_url: Dict[str, List[str]] = {}
    for p in products:
        u = nav.derive_url("LapakGaming", homepage, p)
        if u:
            by_url.setdefault(u, []).append(p)

    for url, prods in by_url.items():
        try:
            html = await _httpx_get(url)
        except Exception as e:
            print(f"    [LapakGaming] fetch fail: {e}")
            continue

        denom_price_map = _lapak_extract_pairs(html)
        if not denom_price_map:
            print(f"    [LapakGaming] no tiles parsed from {url[-40:]}")
            continue
        print(f"    [LapakGaming] {url[-40:]} found {len(denom_price_map)} denominations")

        for product in prods:
            denom = _denom_of(product)
            if denom is None:
                continue
            if denom in denom_price_map:
                out[product] = denom_price_map[denom]
            elif float(denom) in denom_price_map:
                out[product] = denom_price_map[float(denom)]
    return out


def _lapak_extract_pairs(html: str):
    soup = BeautifulSoup(html, "html.parser")
    pairs = {}

    candidates = soup.select(
        ".price-item, .denomination-item, .product-tile, [class*='Card'], "
        "[class*='card'], .item-card, .price-list-item"
    )
    for card in candidates:
        t = card.get_text(separator=" ", strip=True)
        if len(t) > 400 or len(t) < 5:
            continue
        dm = (re.search(r"(?:USD|US\$|\$)\s*(\d{1,5})\b", t, re.I) or
              re.search(r"\b(\d{1,5})\s*USD\b", t, re.I))
        if not dm:
            continue
        denom = float(dm.group(1))

        if _is_unavailable(t):
            if denom not in pairs:
                pairs[denom] = "N/A"
            continue

        prices = [float(p) for p in re.findall(r"\$\s?(\d+(?:\.\d{1,2})?)", t)
                  if 0.01 <= float(p) <= 100000]
        if not prices:
            continue
        candidates_below = [p for p in prices if p <= denom * 1.05]
        chosen = min(candidates_below) if candidates_below else min(prices)
        if denom not in pairs or pairs[denom] == "N/A":
            pairs[denom] = chosen
    return pairs


# ---------------------------------------------------------------------------
# Codashop adapter
# ---------------------------------------------------------------------------
# Codashop sells at face value. Tiles look like "PSN 25 USD\nFrom\n$25.00".
# Map denomination → "From $X.XX" amount.

async def scrape_codashop(homepage: str, products: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not _HAS_BS4:
        return out

    by_url: Dict[str, List[str]] = {}
    for p in products:
        u = nav.derive_url("Codashop", homepage, p)
        if u:
            by_url.setdefault(u, []).append(p)

    for url, prods in by_url.items():
        try:
            html = await _httpx_get(url)
        except Exception as e:
            print(f"    [Codashop] fetch fail: {e}")
            continue

        denom_price_map = _codashop_extract_pairs(html)
        if not denom_price_map:
            print(f"    [Codashop] no tiles parsed from {url[-40:]}")
            continue
        print(f"    [Codashop] {url[-40:]} found {len(denom_price_map)} denominations")

        for product in prods:
            denom = _denom_of(product)
            if denom is None:
                continue
            if denom in denom_price_map:
                out[product] = denom_price_map[denom]
    return out


def _codashop_extract_pairs(html: str):
    soup = BeautifulSoup(html, "html.parser")
    pairs = {}

    full = soup.get_text(separator="\n", strip=True)
    blocks = re.split(r"\n+", full)
    for i, line in enumerate(blocks):
        m = re.search(r"(?:PSN|Steam|iTunes|Apple|Razer|Amazon|Spotify)?\s*(\d{1,5})\s*USD\b",
                      line, re.I)
        if not m:
            continue
        denom = float(m.group(1))
        # Look in the next 6 lines for either a price OR an unavailability marker
        context = " ".join(blocks[i + 1: min(i + 7, len(blocks))])
        if _is_unavailable(context) or _is_unavailable(line):
            if denom not in pairs:
                pairs[denom] = "N/A"
            continue
        for j in range(i + 1, min(i + 6, len(blocks))):
            pm = re.search(r"\$\s?(\d+(?:\.\d{1,2})?)", blocks[j])
            if pm:
                price = float(pm.group(1))
                if 0.5 <= price <= 100000 and price <= denom * 1.5:
                    if denom not in pairs or pairs[denom] == "N/A":
                        pairs[denom] = price
                    break
    return pairs
