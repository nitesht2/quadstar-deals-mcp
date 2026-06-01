"""
categories.py - Product Category Registry

Built-in categories that the scraper understands out of the box.
Add more at runtime via the agent: "add a new category called books with keywords: book, novel, kindle"
Runtime-added categories are persisted to data/custom_categories.json.
"""

from config.settings import TECH_KEYWORDS

CATEGORIES = {
    "tech": {
        "keywords": TECH_KEYWORDS,
        "amazon_urls": [
            # NOTE: Best-seller pages (zgbs/*) deliberately removed — they list
            # popular items without strikethrough/list prices, so original_price
            # is None and computed discount = 0, gated out 100% of the time.
            # Live logs showed 150-206 links per best-seller page extracting to 0
            # deals each. Keeping only Movers / Goldbox / Deals / Coupons URLs
            # cut scrape time ~50% with zero loss of qualifying deals.
            #
            # Movers-and-shakers — biggest price/rank jumps (often big discounts).
            ("https://www.amazon.com/gp/movers-and-shakers/electronics", "Electronics Movers"),
            ("https://www.amazon.com/gp/movers-and-shakers/pc", "PC Movers"),
            ("https://www.amazon.com/gp/goldbox", "Gold Box"),
            # Deals pages — Amazon's filtered "today's deals" for sub-categories.
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_702327011_dt_sl14_61", "Laptop Deals"),
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_1292115011_dt_sl14_61", "Monitor Deals"),
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_172541_dt_sl14_61", "Headphone Deals"),
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_702319011_dt_sl14_61", "Desktop Deals"),
            # Coupons — separate inventory, rotates daily.
            ("https://www.amazon.com/Coupons?category=Electronics", "Electronics Coupons"),
            ("https://www.amazon.com/Coupons?category=PC", "Computer Coupons"),
        ],
        "fast_track_urls": [
            ("https://www.amazon.com/gp/goldbox", "Gold Box"),
            ("https://www.amazon.com/gp/movers-and-shakers/electronics", "Electronics Movers"),
            ("https://www.amazon.com/gp/movers-and-shakers/pc", "PC Movers"),
        ],
        "min_price": 50,
        "max_discount": 85,
        "price_ranges": ["$50-99", "$100-249", "$250-499", "$500-999", "$1000+"],
    },
    "home": {
        "keywords": [
            # Kitchen
            "air fryer", "instant pot", "coffee maker", "espresso", "blender",
            "food processor", "stand mixer", "kitchenaid", "ninja", "vitamix",
            "toaster oven", "microwave", "dishwasher", "refrigerator", "vacuum",
            "dyson", "shark", "roomba", "robot vacuum",
            # Home & Furniture
            "mattress", "pillow", "bedding", "sheets", "towel",
            "humidifier", "air purifier", "hepa", "dehumidifier",
            "smart bulb", "smart plug", "philips hue", "led strip",
            "fire tv", "echo dot", "google nest", "smart display",
            # Tools & Storage
            "power drill", "dewalt", "milwaukee", "tool set",
            "storage rack", "shelving", "organizer",
        ],
        "amazon_urls": [
            # Best-seller pages removed — see tech for rationale (zero-yield).
            ("https://www.amazon.com/gp/movers-and-shakers/kitchen", "Kitchen Movers"),
            ("https://www.amazon.com/gp/movers-and-shakers/hi", "Home Movers"),
            ("https://www.amazon.com/Coupons?category=Kitchen", "Kitchen Coupons"),
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_284507_dt_sl14_61", "Home Deals"),
        ],
        "fast_track_urls": [
            ("https://www.amazon.com/gp/movers-and-shakers/kitchen", "Kitchen Movers"),
            ("https://www.amazon.com/gp/goldbox", "Gold Box"),
        ],
        "min_price": 25,
        "max_discount": 85,
        "price_ranges": ["$25-49", "$50-99", "$100-249", "$250-499", "$500+"],
    },
    "sports": {
        "keywords": [
            # Fitness
            "treadmill", "exercise bike", "elliptical", "rowing machine", "dumbbell",
            "kettlebell", "resistance band", "yoga mat", "foam roller", "pull-up bar",
            "peloton", "nordictrack", "bowflex", "gym",
            # Outdoor & Sports
            "camping", "hiking", "backpack", "tent", "sleeping bag",
            "bike", "bicycle", "helmet", "ski", "snowboard",
            "golf", "tennis racket", "basketball", "football",
            "running shoes", "athletic", "nike", "adidas", "under armour",
            # Wearable Fitness
            "garmin", "fitbit", "polar", "heart rate monitor",
            "fitness tracker", "sports watch",
        ],
        "amazon_urls": [
            # Best-seller pages removed — see tech for rationale (zero-yield).
            ("https://www.amazon.com/gp/movers-and-shakers/sporting-goods", "Sports Movers"),
            ("https://www.amazon.com/Coupons?category=Sports", "Sports Coupons"),
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_3375251_dt_sl14_61", "Sports Deals"),
        ],
        "fast_track_urls": [
            ("https://www.amazon.com/gp/movers-and-shakers/sporting-goods", "Sports Movers"),
            ("https://www.amazon.com/gp/goldbox", "Gold Box"),
        ],
        "min_price": 25,
        "max_discount": 85,
        "price_ranges": ["$25-49", "$50-99", "$100-249", "$250-499", "$500+"],
    },
}


def get_category(name: str) -> dict:
    """Get category config by name. Falls back to 'tech' if not found."""
    import json, os
    custom = {}
    custom_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "custom_categories.json")
    if os.path.exists(custom_path):
        with open(custom_path) as f:
            custom = json.load(f)
    all_cats = {**CATEGORIES, **custom}
    return all_cats.get(name.lower(), CATEGORIES["tech"])


def list_categories() -> list:
    """List all available category names (built-in + runtime-added)."""
    import json, os
    custom = {}
    custom_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "custom_categories.json")
    if os.path.exists(custom_path):
        with open(custom_path) as f:
            custom = json.load(f)
    return list({**CATEGORIES, **custom}.keys())
