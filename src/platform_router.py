from __future__ import annotations

"""
platform_router.py - Smart Platform Selection

Decides which social platforms to post a deal on based on product type.
Only recommends platforms that have a configured Postiz integration ID.

Routing rules:
  - Twitter: always (primary monetization channel)
  - Instagram: visual products (TVs, monitors, cameras, headphones, watches)
  - LinkedIn: professional/productivity (laptops, desktops, docks, printers)
  - Reddit: gaming (PS5, Xbox, Steam Deck, GPUs, controllers)
  - Facebook: smart home (Alexa, Echo, Nest, Ring, Roomba)
  - TikTok: impulse buys under $100 (earbuds, chargers, accessories)
"""

from config.settings import PLATFORM_IDS

# Keyword -> platform mapping. Order matters: first match wins per platform.
_PLATFORM_RULES: list[tuple[str, list[str]]] = [
    ("instagram", [
        "tv", "television", "oled", "qled", "monitor", "display", "projector",
        "camera", "gopro", "lens", "drone",
        "headphones", "earbuds", "airpods", "speaker", "soundbar",
        "apple watch", "smartwatch", "fitbit", "garmin",
    ]),
    ("linkedin", [
        "laptop", "macbook", "chromebook", "notebook", "desktop", "imac",
        "monitor", "display", "docking station", "thunderbolt",
        "printer", "scanner", "webcam", "keyboard", "mouse",
        "nas", "external drive",
    ]),
    ("reddit", [
        "ps5", "playstation", "xbox", "nintendo", "switch", "steam deck",
        "gaming", "controller", "console",
        "gpu", "graphics card", "rtx", "radeon",
        "mechanical", "rgb",
        "vr", "oculus", "meta quest",
    ]),
    ("facebook", [
        "alexa", "echo", "google nest", "smart home", "thermostat",
        "ring doorbell", "security camera", "robot vacuum", "roomba",
        "fire stick", "roku", "chromecast", "apple tv",
    ]),
    ("tiktok", [
        "earbuds", "airpods", "charger", "power bank", "usb-c",
        "flash drive", "phone case", "cable",
    ]),
]

# TikTok price ceiling for impulse-buy targeting
_TIKTOK_PRICE_CEILING = 100.0


def match_platforms(deal: dict) -> set[str]:
    """Match platforms by keyword rules only, ignoring Postiz config.
    Used by content generation to decide what content to generate.
    """
    title_lower = deal.get("title", "").lower()
    price = deal.get("deal_price", 0) or 0
    platforms = {"twitter"}
    for platform, keywords in _PLATFORM_RULES:
        if platform == "tiktok" and price > _TIKTOK_PRICE_CEILING:
            continue
        for kw in keywords:
            if kw in title_lower:
                platforms.add(platform)
                break
    return platforms


def select_platforms(deal: dict) -> list[str]:
    """Select which platforms to post a deal on based on product keywords.

    Args:
        deal: Deal dict with at least 'title' and optionally 'deal_price'.

    Returns:
        List of platform names (e.g. ["twitter", "instagram"]).
        Only includes platforms with configured Postiz integration IDs.
    """
    title_lower = deal.get("title", "").lower()
    price = deal.get("deal_price", 0) or 0

    platforms = {"twitter"}  # always post to Twitter

    for platform, keywords in _PLATFORM_RULES:
        if platform == "tiktok" and price > _TIKTOK_PRICE_CEILING:
            continue  # TikTok only for cheap impulse buys
        for kw in keywords:
            if kw in title_lower:
                platforms.add(platform)
                break

    # Filter to only platforms with configured integration IDs
    configured = [p for p in platforms if PLATFORM_IDS.get(p)]
    return configured if configured else ["twitter"]


def describe_platforms(platforms: list[str]) -> str:
    """Human-readable summary for Discord cards."""
    icons = {
        "twitter": "X/Twitter",
        "instagram": "Instagram",
        "linkedin": "LinkedIn",
        "reddit": "Reddit",
        "facebook": "Facebook",
        "tiktok": "TikTok",
        "bluesky": "Bluesky",
        "threads": "Threads",
    }
    names = [icons.get(p, p) for p in platforms]
    return ", ".join(names)
