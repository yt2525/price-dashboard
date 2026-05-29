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
    r"no\s*offers",        # G2A, Kinguin, Eneba marketplace tiles
    r"no\s*sellers",        # Kinguin / marketplace
    r"currently\s*unavailable",
    r"stock\s*depleted",
    r"item\s*not\s*available",
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
    """Returns {denomination_numeric: float_price or 'N/A'}.

    SeaGM DOM structure for a denomination tile:
      <div class="SKU_type">                            (or .inner wrapper)
        <div class="sku"><span>PSN Card N USD US</span></div>
        <div class="price">
          <span>1.19</span>            ← orange selling price (rgb(255,69,0))
          <del>US$ 1.25</del>          ← strikethrough original
        </div>
      </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    pairs = {}

    # Each denomination is wrapped in .SKU_type (or .inner). Both class names
    # exist nested — selecting either works because we iterate uniquely.
    seen_skus = set()
    containers = soup.select(".SKU_type, .inner")
    for c in containers:
        sku_el = c.select_one(".sku")
        price_el = c.select_one(".price")
        if not sku_el or not price_el:
            continue
        sku_text = sku_el.get_text(separator=" ", strip=True)
        if sku_text in seen_skus:
            continue
        seen_skus.add(sku_text)

        # Extract denomination — handles "PSN Card 25 USD US", "Steam Wallet 50 USD",
        # "iTunes 100 USD US", "Apple Gift Card 200 USD US", etc.
        dm = (re.search(r"\b(\d{1,5})\s*USD\b", sku_text, re.I) or
              re.search(r"USD\s*(\d{1,5})\b", sku_text, re.I))
        if not dm:
            continue
        denom = float(dm.group(1))

        # Unavailability check inside this card
        card_text = c.get_text(separator=" ", strip=True)
        if _is_unavailable(card_text):
            if denom not in pairs:
                pairs[denom] = "N/A"
            continue

        # The first <span> inside .price is the orange selling price.
        # The struck-through original price is in a <del>/<s> or a later span.
        val = None
        first_span = price_el.find("span")
        if first_span:
            val = _parse_price(first_span.get_text(separator=" ", strip=True))

        # Fallback: parse the textContent and pick the smaller of the first two numbers
        if val is None:
            nums = [float(m) for m in re.findall(r"(\d+(?:\.\d{1,2})?)",
                                                 price_el.get_text(separator=" ", strip=True))
                    if 0.01 <= float(m) <= 100000]
            if nums:
                # First number in price element is the orange one
                val = nums[0]

        if val is None:
            continue
        # Sanity: selling price should be no more than 1.5× the face value
        if val > denom * 1.5:
            continue

        if denom not in pairs or pairs[denom] == "N/A":
            pairs[denom] = val

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


# ---------------------------------------------------------------------------
# G2A adapter
# ---------------------------------------------------------------------------
# G2A's category pages are React-rendered and Cloudflare-protected, so a
# plain httpx GET will 403. The orchestrator must supply already-rendered
# HTML (via Playwright OR Firecrawl).
#
# Tile DOM structure (verified in browser):
#   <div class="SearchPage_StyledProductCard..." ...>
#     ... PlayStation Network Gift Card 10 USD ...
#     <span class="font-bold text-foreground text-price-2xl">10.53 USD</span>
#     <span class="line-through text-price-m">11.63 USD</span>  ← original
#   </div>
#
# Each tile carries the denomination (e.g. "Gift Card 10 USD") and the
# selling price (the orange-bold .text-price-2xl). The .line-through is
# the pre-discount price and we ignore it.

async def scrape_g2a(homepage: str, products: List[str], html: str = "") -> Dict:
    """G2A adapter. Caller must pass pre-rendered HTML (from Playwright/
    Firecrawl) because G2A 403s data-center httpx clients.
    """
    out: Dict = {}
    if not _HAS_BS4 or not html:
        return out

    # We expect ONE rendered page per category (the URL_PATTERNS already
    # collapse all denominations of a brand to a single category page).
    # Group products by their derived G2A URL to know which page applies.
    by_url: Dict[str, List[str]] = {}
    for p in products:
        u = nav.derive_url("G2A", homepage, p)
        if u:
            by_url.setdefault(u, []).append(p)

    # If the caller bundled all products under a single page, just parse it.
    denom_price_map = _g2a_extract_pairs(html)
    if not denom_price_map:
        print(f"    [G2A] no ProductCard tiles parsed")
        return out
    print(f"    [G2A] {len(denom_price_map)} denominations found in rendered HTML")

    for _, prods in by_url.items():
        for product in prods:
            denom = _denom_of(product)
            if denom is None:
                continue
            if denom in denom_price_map:
                out[product] = denom_price_map[denom]
            elif float(denom) in denom_price_map:
                out[product] = denom_price_map[float(denom)]
    return out


def _g2a_extract_pairs(html: str):
    """Returns {denomination_numeric: float_price or 'N/A'}.

    Walks every [class*="ProductCard"] tile, reads denomination from the
    tile title ("Gift Card N USD"), reads the selling price from the
    first .text-price-2xl span. Skips tiles whose region/currency isn't USD.
    """
    soup = BeautifulSoup(html, "html.parser")
    pairs: Dict = {}

    # G2A's ProductCard wrapper has a long emotion-styled class.
    # We match any element whose class contains "ProductCard".
    candidates = soup.select('[class*="ProductCard"]')
    if not candidates:
        # Fallback: looser match
        candidates = soup.select('[class*="product-card"], [data-testid*="product"]')

    for card in candidates:
        txt = card.get_text(separator=" ", strip=True)
        if not txt or len(txt) > 1500:
            continue

        # Require USD region — skip GBP, EUR, etc cards
        # Match e.g. "Gift Card 10 USD" or "Steam Gift Card 50 USD"
        dm = re.search(r"Gift Card\s+(\d{1,5})\s+USD\b", txt, re.I)
        if not dm:
            # Some titles say "Top Up 60 USD" or "Wallet Code 25 USD"
            dm = re.search(r"\b(\d{1,5})\s+USD\b\s*Platform", txt, re.I)
        if not dm:
            # Final fallback: only accept if the FIRST denomination is USD
            dm = re.search(r"\b(\d{1,5})\s*(USD|EUR|GBP|JPY)\b", txt, re.I)
            if not dm or dm.group(2).upper() != "USD":
                continue
        denom = float(dm.group(1))

        # Unavailability marker inside card
        if _is_unavailable(txt) or re.search(r"\bno offers\b|\bno sellers\b", txt, re.I):
            if denom not in pairs:
                pairs[denom] = "N/A"
            continue

        # Selling price — the bold orange element
        price_el = card.select_one(".text-price-2xl, .font-bold.text-foreground.text-price-2xl")
        sell_price = None
        if price_el:
            sell_price = _parse_price(price_el.get_text(separator=" ", strip=True))

        if sell_price is None:
            # Fallback: parse all "N.NN USD" patterns from the card text and
            # take the smallest that isn't the strikethrough (heuristic:
            # smaller of the first two).
            nums = [float(m) for m in re.findall(r"(\d+(?:\.\d{1,2}))\s*USD\b", txt)
                    if 0.01 <= float(m) <= 100000]
            if nums:
                sell_price = nums[0]

        if sell_price is None:
            continue
        # Sanity: selling price within 1.5× of denomination (G2A markup is
        # usually +5-15% above face value, sometimes below for promo offers).
        if sell_price > denom * 1.5 or sell_price < denom * 0.3:
            continue

        if denom not in pairs or pairs[denom] == "N/A":
            pairs[denom] = sell_price

    return pairs


# ---------------------------------------------------------------------------
# Kinguin adapter
# ---------------------------------------------------------------------------
# Kinguin's category pages list every regional variant of a brand in one feed
# — EUR, GBP, USD, PLN, etc. We filter to USD-denominated tiles only.
#
# Tile DOM structure (verified in browser):
#   <div itemtype="...schema.org/Product">
#     <... itemprop="name">PlayStation Network Card USD 50 Gift Card US</...>
#     <... itemprop="price" content="42.85">...</...>
#     ...From $42.85... (display text — preferred since this is USD-converted)
#     <... itemprop="availability" href=".../InStock">...
#   </div>
#
# Kinguin is Cloudflare-protected, so the caller MUST hand pre-rendered HTML
# (from Playwright/Patchright/Firecrawl); plain httpx returns 403.

async def scrape_kinguin(homepage: str, products: List[str], html: str = "") -> Dict:
    """Kinguin adapter. Caller must pass pre-rendered HTML (httpx is 403'd
    by Cloudflare)."""
    out: Dict = {}
    if not _HAS_BS4 or not html:
        return out

    by_url: Dict[str, List[str]] = {}
    for p in products:
        u = nav.derive_url("Kinguin", homepage, p)
        if u:
            by_url.setdefault(u, []).append(p)

    denom_price_map = _kinguin_extract_pairs(html)
    if not denom_price_map:
        print(f"    [Kinguin] no USD product tiles parsed")
        return out
    print(f"    [Kinguin] {len(denom_price_map)} USD denominations found")

    for _, prods in by_url.items():
        for product in prods:
            denom = _denom_of(product)
            if denom is None:
                continue
            if denom in denom_price_map:
                out[product] = denom_price_map[denom]
            elif float(denom) in denom_price_map:
                out[product] = denom_price_map[float(denom)]
    return out


def _kinguin_extract_pairs(html: str):
    """Returns {denomination_numeric: float_price or 'N/A'}.

    Walks every [itemtype*="Product"] tile, filters to USD tiles whose
    title matches 'USD N' or 'N USD'. Reads price from 'From $X.XX' display
    text (already USD-converted) and falls back to itemprop=price microdata.
    """
    soup = BeautifulSoup(html, "html.parser")
    pairs: Dict = {}

    tiles = soup.select('[itemtype*="Product"]')
    for tile in tiles:
        txt = tile.get_text(separator=" ", strip=True)
        if not txt or len(txt) > 2000:
            continue

        # Filter to USD tiles. The title is the only reliable indicator —
        # other currencies (EUR, PLN, GBP, JPY) live in the same category.
        name_el = tile.select_one('[itemprop="name"]')
        title = (name_el.get_text(separator=" ", strip=True) if name_el else "").strip()
        if not title:
            title = txt

        # Title must mention USD specifically (avoid grabbing EUR/PLN/GBP tiles)
        dm = re.search(r"USD\s+(\d{1,5})\b", title, re.I) or \
             re.search(r"\b(\d{1,5})\s+USD\b", title, re.I)
        if not dm:
            continue
        denom = float(dm.group(1))

        # Unavailability detection:
        # 1) Microdata schema.org/OutOfStock
        avail_el = tile.select_one('[itemprop="availability"]')
        avail_attr = (
            (avail_el.get("href") or "")
            + " " + (avail_el.get("content") or "")
            + " " + (avail_el.get_text(separator=" ", strip=True) if avail_el else "")
        ) if avail_el else ""
        is_oos = bool(re.search(r"OutOfStock|SoldOut", avail_attr, re.I))
        # 2) Text patterns
        if is_oos or _is_unavailable(txt):
            if denom not in pairs:
                pairs[denom] = "N/A"
            continue

        # Prefer "From $X.XX" display price (USD-converted by Kinguin).
        sell_price = None
        fm = re.search(r"From\s*\$\s*(\d+(?:\.\d{1,2}))", txt)
        if fm:
            sell_price = float(fm.group(1))
        else:
            # Fallback: any $X.XX in the tile
            sm = re.search(r"\$\s*(\d+(?:\.\d{1,2}))", txt)
            if sm:
                sell_price = float(sm.group(1))
        if sell_price is None:
            # Last resort: microdata price (likely in default currency, not USD)
            price_el = tile.select_one('[itemprop="price"]')
            if price_el:
                v = price_el.get("content") or price_el.get_text(separator=" ", strip=True)
                sell_price = _parse_price(v)

        if sell_price is None:
            continue
        # Sanity check: selling price should be 0.3× to 1.5× face value
        if sell_price > denom * 1.5 or sell_price < denom * 0.3:
            continue

        # First tile per denom wins (Kinguin shows results sorted by best)
        if denom not in pairs or pairs[denom] == "N/A":
            pairs[denom] = sell_price

    return pairs


# ---------------------------------------------------------------------------
# Eneba adapter
# ---------------------------------------------------------------------------
# Eneba moved their URL scheme from per-denom slugs under /en-us/ to a single
# /store/{brand}-gift-cards category page that lists every denomination as a
# tile. All tiles in a category live on one URL.
#
# Tile text (verified in browser, CSS classes are randomized so we use text):
#   "PSN Cashback PSN PlayStation Network Card 10 USD (USA) PSN Key
#    UNITED STATES United States From $10.00 -6% $9.37"
#
# Pattern per tile:
#   - "Card N USD"     → denomination N
#   - "From $X.XX"     → face value (we ignore this)
#   - "-N%"            → discount percentage
#   - the SECOND $X.XX → actual selling price (what Eneba charges)
#
# Eneba is Cloudflare/anti-bot protected so the caller MUST supply rendered
# HTML (from Patchright/Firecrawl).

async def scrape_eneba(homepage: str, products: List[str], html: str = "") -> Dict:
    """Eneba adapter. Caller must pass pre-rendered HTML (httpx is 403'd
    by Cloudflare). Parses /store/{brand}-gift-cards category page."""
    out: Dict = {}
    if not _HAS_BS4 or not html:
        return out

    by_url: Dict[str, List[str]] = {}
    for p in products:
        u = nav.derive_url("Eneba", homepage, p)
        if u:
            by_url.setdefault(u, []).append(p)

    denom_price_map = _eneba_extract_pairs(html)
    if not denom_price_map:
        print(f"    [Eneba] no USD product tiles parsed")
        return out
    print(f"    [Eneba] {len(denom_price_map)} USD denominations found")

    for _, prods in by_url.items():
        for product in prods:
            denom = _denom_of(product)
            if denom is None:
                continue
            if denom in denom_price_map:
                out[product] = denom_price_map[denom]
            elif float(denom) in denom_price_map:
                out[product] = denom_price_map[float(denom)]
    return out


def _eneba_extract_pairs(html: str):
    """Returns {denomination_numeric: float_price or 'N/A'}.

    Two-pass approach:
      1. STRICT pattern: "N USD ... From $X.XX ... $Y.YY" — preferred
      2. LOOSE pattern: "N USD ... $Y.YY"               — fallback

    Eneba uses randomized CSS-in-JS class names, so DOM-selector matching
    isn't reliable across deploys. The flat-text pattern is stable enough.
    """
    soup = BeautifulSoup(html, "html.parser")
    pairs: Dict = {}

    # Use the rendered page text — collapses whitespace and joins tile sub-elements
    full = soup.get_text(separator=" ", strip=True)
    full = re.sub(r"\s+", " ", full)

    def _price_ok(denom: float, price: float) -> bool:
        # 50%-120% face value (marketplace usually < face value)
        return 0.5 <= (price / denom) <= 1.2

    # Pass 1 — strict tile pattern with "From $X.XX"
    strict_re = re.compile(
        r"\b(\d{1,5})\s*USD\b[^$]{0,200}?"
        r"From\s*\$\s*\d+(?:\.\d{1,2})?[^$]{0,50}?"
        r"\$\s*(\d+(?:\.\d{1,2}))",
        re.I,
    )
    for m in strict_re.finditer(full):
        denom = float(m.group(1))
        price = float(m.group(2))
        if denom < 1 or denom > 5000:
            continue
        if not _price_ok(denom, price):
            continue
        if denom not in pairs or pairs[denom] == "N/A":
            pairs[denom] = price

    # Pass 2 — looser pattern, only fills denoms strict pass missed.
    # Matches "N USD ... $Y.YY" where Y.YY is the FIRST $-price after
    # the denomination, within 200 chars.
    if not pairs:
        loose_re = re.compile(
            r"\b(\d{1,5})\s*USD\b"
            r"[\s\S]{0,200}?"
            r"\$\s*(\d+(?:\.\d{1,2}))",
            re.I,
        )
        for m in loose_re.finditer(full):
            denom = float(m.group(1))
            price = float(m.group(2))
            if denom < 1 or denom > 5000:
                continue
            if not _price_ok(denom, price):
                continue
            if denom not in pairs:
                pairs[denom] = price

    return pairs
