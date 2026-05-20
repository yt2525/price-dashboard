"""
nav.py — Derive a competitor's product page URL from their homepage + product name.

Strategies (in order):
  1. URL_TEMPLATES — per-denomination templated URL (e.g. Eneba). Replace
     {denom} with the denomination extracted from the product name.
  2. URL_PATTERNS  — per-brand fixed path on the site.
  3. SEARCH_URLS   — fallback: send the product name to the site's search.
"""

import re
import urllib.parse
from typing import Optional


# -------------------------------------------------------------------
# Per-competitor brand → relative path map.
# Keyword on the left matched (case-insensitive) against the product name.
# The relative path appended to the homepage URL.
# -------------------------------------------------------------------
URL_PATTERNS = {

    "OG": {
        "playstation": "playstation-store-gift-cards/playstation-store-gift-cards-us",
        "psn":         "playstation-store-gift-cards/playstation-store-gift-cards-us",
        "steam":       "steam-wallet-codes/steam-wallet-codes-us",
        "itunes":      "apple-itunes-gift-cards/itunes-gift-cards-us",
        "apple gift":  "apple-gift-cards/apple-gift-cards-us",
        "razer gold":  "razer-gold-pin/razer-gold-pin-us",
        "amazon":      "amazon-gift-cards/amazon-gift-cards-us",
        "spotify":     "spotify-gift-cards/spotify-gift-cards-us",
        "discord":     "discord-nitro/discord-nitro-1-year",
        "genshin":     "genshin-impact-genesis-crystals/genshin-impact-genesis-crystals-direct-top-up",
        "valorant":    "valorant-points/valorant-points-global",
        "mobile legend":"mobile-legends-bang-bang/mobile-legends-bang-bang-diamonds",
        "honkai":      "honkai-star-rail/honkai-star-rail-oneiric-shard",
        "zenless":     "zenless-zone-zero/zenless-zone-zero-monochrome",
        "wuthering":   "wuthering-waves/wuthering-waves-lunites",
        "pokemon":     "pokemon-go/pokemon-go-pokecoins",
    },

    "Seagm": {
        "playstation": "playstation-network-card-psn-united-states",
        "psn":         "playstation-network-card-psn-united-states",
        "steam":       "steam-wallet-code-us",
        "itunes":      "itunes-gift-card-us",
        "apple gift":  "apple-gift-card-us",
        "razer gold":  "razer-gold-pin-us",
        "amazon":      "amazon-gift-card-us",
        "spotify":     "spotify-gift-card-us",
        "genshin":     "genshin-impact-direct-top-up",
        "valorant":    "riot-access-code-us",
        "mobile legend":"mobile-legends-bang-bang-diamonds",
        "honkai":      "honkai-star-rail-oneiric-shards",
        "zenless":     "zenless-zone-zero-monochrome",
        "pokemon":     "pokemon-go-pokecoins-direct-top-up",
    },

    "G2G": {
        # G2G category listing pages — Playwright/Firecrawl required (JS-rendered)
        "playstation": "categories/playstation-network-gift-cards",
        "psn":         "categories/playstation-network-gift-cards",
        "steam":       "categories/steam-wallet-codes",
        "itunes":      "categories/itunes-gift-cards",
        "apple gift":  "categories/apple-gift-cards",
        "razer gold":  "categories/razer-gold-pins",
        "amazon":      "categories/amazon-gift-cards",
    },

    "Kinguin": {
        # Kinguin: top-level category page lists all denominations as tiles.
        # Needs Playwright/Firecrawl — listing is JS-rendered.
        "playstation": "c/44853/playstation-network-card",
        "psn":         "c/44853/playstation-network-card",
        "steam":       "c/36789/steam-wallet",
        "itunes":      "c/4185/itunes",
        "apple gift":  "c/153414/apple-gift-cards",
        "razer gold":  "c/92501/razer-gold",
        "amazon":      "c/13945/amazon",
        "spotify":     "c/22315/spotify",
        "discord":     "c/80901/discord",
    },

    "G2A": {
        # G2A category pages — Playwright/Firecrawl required.
        "playstation": "category/psn-c1567",
        "psn":         "category/psn-c1567",
        "steam":       "category/steam-keys-c1",
        "razer gold":  "category/razer-gold-c10283",
        "spotify":     "category/spotify-c12036",
    },

    "Codashop": {
        "playstation": "en-us/playstation-vouchers",
        "psn":         "en-us/playstation-vouchers",
        "spotify":     "en-us/spotify",
        "razer gold":  "en-us/razer-gold",
        "genshin":     "en-us/genshin-impact",
        "valorant":    "en-us/valorant",
        "mobile legend":"en-us/mobile-legends",
        "honkai":      "en-us/honkai-star-rail",
        "pokemon":     "en-us/pokemon-go",
    },

    "LapakGaming": {
        "playstation": "en-us/voucher-playstation-network-psn",
        "psn":         "en-us/voucher-playstation-network-psn",
        "steam":       "en-us/steam-wallet",
        "razer gold":  "en-us/razer-gold",
        "spotify":     "en-us/spotify",
        "genshin":     "en-us/genshin-impact",
        "valorant":    "en-us/valorant",
        "mobile legend":"en-us/mobile-legends",
        "honkai":      "en-us/honkai-star-rail",
        "pokemon":     "en-us/pokemon-go",
    },

    "Eneba": {
        # Eneba: per-denomination URLs are clean (no product IDs).
        # Template via URL_TEMPLATES below.
    },

    "MooGold": {
        # MooGold: single brand page with denominations behind radio buttons.
        # Needs MooGold-specific Playwright scraper (see scrape.py).
        "playstation": "product/psn-card-us/",
        "psn":         "product/psn-card-us/",
        "steam":       "product/steam-wallet-us/",
        "itunes":      "product/itunes-gift-card-us/",
        "apple gift":  "product/apple-gift-card-us/",
        "razer gold":  "product/razer-gold-pin-us/",
        "amazon":      "product/amazon-gift-card-us/",
        "genshin":     "product/genshin-impact-genesis-crystal/",
        "valorant":    "product/valorant-points/",
        "honkai":      "product/honkai-star-rail/",
    },

    "ItemkuEN": {
        "playstation": "en/c/psn-gift-card",
        "psn":         "en/c/psn-gift-card",
        "steam":       "en/c/steam-wallet",
        "genshin":     "en/c/genshin-impact",
        "valorant":    "en/c/valorant-points",
        "mobile legend":"en/c/mobile-legends-diamond",
    },
}


# Per-denomination URL templates. {denom} placeholder is replaced with
# the numeric denomination extracted from the product name.
# Use these for sites where each denomination has its own URL but the
# URL is templatable (no unique product IDs).
URL_TEMPLATES = {
    "Eneba": {
        # /en-us/ region — serves USD prices natively rather than MYR
        "psn":         "en-us/psn-playstation-network-card-{denom}-usd-usa-psn-key-united-states",
        "playstation": "en-us/psn-playstation-network-card-{denom}-usd-usa-psn-key-united-states",
        "steam":       "en-us/steam-gift-card-{denom}-usd-steam-key-united-states",
        "itunes":      "en-us/itunes-{denom}-usd-itunes-key-united-states",
        "apple gift":  "en-us/apple-gift-card-{denom}-usd-apple-gift-card-key-united-states",
        "razer gold":  "en-us/razer-gold-pin-{denom}-usd-razer-gold-key-united-states",
        "amazon":      "en-us/amazon-gift-card-{denom}-usd-amazon-key-united-states",
        "spotify":     "en-us/spotify-premium-gift-card-{denom}-usd-spotify-key-united-states",
    },
}


SEARCH_URLS = {
    "OG":          "https://www.offgamers.com/search?q={q}",
    "G2G":         "https://www.g2g.com/results?service_id=lgc_service_1&q={q}",
    "Kinguin":     "https://www.kinguin.net/listing?phrase={q}",
    "Seagm":       "https://www.seagm.com/en-us/search?q={q}",
    "G2A":         "https://www.g2a.com/search?query={q}",
    "Codashop":    "https://www.codashop.com/en-us/search?q={q}",
    "LapakGaming": "https://www.joytify.com/en-us/search?q={q}",
    "Eneba":       "https://www.eneba.com/store/search?text={q}",
    "MooGold":     "https://moogold.com/?s={q}",
    "ItemkuEN":    "https://www.itemku.com/en/search?keyword={q}",
}


_DENOM_RE = re.compile(
    r"(USD|EUR|GBP|JPY|CNY|CAD|MYR|RM|QAR|USDT|USDC|HKD|TWD|IDR|AUD)\s?(\d[\d,]*)",
    re.I,
)


def extract_denom(product_name: str) -> Optional[str]:
    """Return the numeric denomination from a product name, e.g. '50' from 'PSN USD50'."""
    m = _DENOM_RE.search(product_name)
    if m:
        return m.group(2).replace(",", "")
    # Also handle "Points/Diamonds/etc" formats
    m = re.search(r"(\d{1,3}(?:,\d{3})*)\s*(?:Points|Diamonds|Crystals|Coins|Lunites|Shard|Rbx|RP)",
                  product_name, re.I)
    if m:
        return m.group(1).replace(",", "")
    return None


def _normalize_homepage(homepage: str) -> str:
    if not homepage:
        return ""
    homepage = homepage.strip().rstrip("/")
    m = re.match(r"^(https?://[^/]+)", homepage)
    return m.group(1) if m else homepage


def derive_url(competitor_key: str, homepage: str, product_name: str) -> Optional[str]:
    base = _normalize_homepage(homepage)
    if not base:
        return None
    name = product_name.lower()

    # 1) Try templated URL (per-denomination)
    templates = URL_TEMPLATES.get(competitor_key, {})
    if templates:
        denom = extract_denom(product_name)
        if denom:
            for keyword in sorted(templates.keys(), key=len, reverse=True):
                if keyword in name:
                    return f"{base}/{templates[keyword].format(denom=denom)}"

    # 2) Try fixed pattern (per-brand)
    patterns = URL_PATTERNS.get(competitor_key, {})
    for keyword in sorted(patterns.keys(), key=len, reverse=True):
        if keyword in name:
            return f"{base}/{patterns[keyword]}"

    # 3) Fallback to search
    template = SEARCH_URLS.get(competitor_key)
    if template:
        return template.format(q=urllib.parse.quote_plus(product_name))

    return None
