"""
nav.py — Derive a competitor's product page URL from their homepage + product name.

Two strategies per competitor:
  1. URL_PATTERNS — hardcoded slug map: if a keyword is in the product name,
     use the known slug. Fast, reliable, no extra fetch.
  2. SEARCH_URLS — fallback: build a search URL using the product name
     as a query. The scraper then visits that page and follows the first
     result link.

Add a new (competitor, brand) by editing URL_PATTERNS below.
Add support for a new competitor by adding entries in both maps and a
search URL template.
"""

from typing import Optional
import re
import urllib.parse


# -------------------------------------------------------------------
# Per-competitor brand → relative path map.
# The keyword on the left is matched (case-insensitive) against the
# product name. The relative path is appended to the homepage URL.
# Order matters — longer / more specific keywords should come first.
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
        "youtube":     "youtube-premium",
        "perplexity":  "perplexity-ai-pro",
        "capcut":      "capcut-pro",
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
        "playstation": "categories/playstation-network-gift-cards",
        "psn":         "categories/playstation-network-gift-cards",
        "steam":       "categories/steam-wallet-codes",
        "itunes":      "categories/itunes-gift-cards",
        "apple gift":  "categories/apple-gift-cards",
        "razer gold":  "categories/razer-gold-pins",
        "amazon":      "categories/amazon-gift-cards",
        "spotify":     "categories/spotify-gift-cards",
        "discord":     "categories/discord-nitro",
        "genshin":     "categories/genshin-impact-account",
        "valorant":    "categories/valorant-account",
        "mobile legend":"categories/mobile-legends-account",
        "honkai":      "categories/honkai-star-rail-account",
        "pokemon":     "categories/pokemon-go-account",
    },

    "Kinguin": {
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
        "playstation": "category/psn-c1567",
        "psn":         "category/psn-c1567",
        "steam":       "category/steam-keys-c1",
        "itunes":      "category/itunes-c70",
        "apple gift":  "category/apple-gift-card-c14117",
        "razer gold":  "category/razer-gold-c10283",
        "amazon":      "category/amazon-gift-card-c12010",
        "spotify":     "category/spotify-c12036",
    },

    "Codashop": {
        "playstation": "en-us/playstation-vouchers",
        "psn":         "en-us/playstation-vouchers",
        "spotify":     "en-us/spotify",
        "itunes":      "en-us/itunes",
        "apple gift":  "en-us/apple-gift-cards",
        "razer gold":  "en-us/razer-gold",
        "genshin":     "en-us/genshin-impact",
        "valorant":    "en-us/valorant",
        "mobile legend":"en-us/mobile-legends",
        "honkai":      "en-us/honkai-star-rail",
        "pokemon":     "en-us/pokemon-go",
    },

    "LapakGaming": {  # also covers joytify.com
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
        "playstation": "store/psn-gift-cards-united-states",
        "psn":         "store/psn-gift-cards-united-states",
        "steam":       "store/steam-gift-cards-united-states",
        "itunes":      "store/itunes-gift-cards-united-states",
        "apple gift":  "store/apple-gift-cards-united-states",
        "razer gold":  "store/razer-gold-united-states",
        "amazon":      "store/amazon-gift-cards-united-states",
        "spotify":     "store/spotify-gift-cards-united-states",
    },

    "MooGold": {
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


# Search URL template per competitor (fallback).
# {q} placeholder gets URL-encoded product name.
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


def _normalize_homepage(homepage: str) -> str:
    """Strip trailing slash and any path so we have just scheme://host."""
    if not homepage:
        return ""
    homepage = homepage.strip().rstrip("/")
    m = re.match(r"^(https?://[^/]+)", homepage)
    return m.group(1) if m else homepage


def _brand_keywords(product_name: str) -> list:
    """Return lowercased product name tokens for brand matching."""
    return product_name.lower()


def derive_url(competitor_key: str, homepage: str, product_name: str) -> Optional[str]:
    """
    Best-guess product page URL for (competitor, product).
    Returns None when neither a pattern nor a search URL is configured.
    """
    base = _normalize_homepage(homepage)
    if not base:
        return None

    name = product_name.lower()
    patterns = URL_PATTERNS.get(competitor_key, {})
    # Try longer / more specific keywords first
    for keyword in sorted(patterns.keys(), key=len, reverse=True):
        if keyword in name:
            return f"{base}/{patterns[keyword]}"

    # Fallback to search if available
    template = SEARCH_URLS.get(competitor_key)
    if template:
        return template.format(q=urllib.parse.quote_plus(product_name))

    return None


def brand_of(product_name: str) -> Optional[str]:
    """Extract a short brand identifier from a product name (for logging)."""
    name = product_name.lower()
    brands = ["playstation", "psn", "steam", "itunes", "apple gift", "razer gold",
              "amazon", "spotify", "discord", "genshin", "valorant", "mobile legend",
              "honkai", "zenless", "wuthering", "pokemon", "youtube",
              "perplexity", "capcut", "nintendo", "xbox", "rewarble", "cashtocode",
              "gash", "mint", "johren", "binance", "flexepin", "nexon", "gocash",
              "ncoin", "ncsoft", "daddyskins", "nutaku", "mycard", "lol", "league",
              "wow", "iptv", "crypto", "rbl"]
    for b in sorted(brands, key=len, reverse=True):
        if b in name:
            return b
    return None
