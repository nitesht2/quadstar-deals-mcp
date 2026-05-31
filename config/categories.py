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
            # Broad deal pages (rotate frequently)
            ("https://www.amazon.com/gp/movers-and-shakers/electronics", "Electronics Movers"),
            ("https://www.amazon.com/gp/movers-and-shakers/pc", "PC Movers"),
            ("https://www.amazon.com/Best-Sellers-Electronics/zgbs/electronics", "Electronics Best Sellers"),
            ("https://www.amazon.com/gp/goldbox", "Gold Box"),
            # Category-specific best sellers
            ("https://www.amazon.com/Best-Sellers-Computers-Accessories/zgbs/pc/702327011", "Laptop Best Sellers"),
            ("https://www.amazon.com/Best-Sellers-Computers-Accessories/zgbs/pc/1292115011", "Monitor Best Sellers"),
            ("https://www.amazon.com/Best-Sellers-Electronics/zgbs/electronics/172541", "Headphone Best Sellers"),
            ("https://www.amazon.com/Best-Sellers-Computers-Accessories/zgbs/pc/702348011", "Tablet Best Sellers"),
            ("https://www.amazon.com/Best-Sellers-Computers-Accessories/zgbs/pc/702319011", "Desktop Best Sellers"),
            ("https://www.amazon.com/Best-Sellers-Electronics/zgbs/electronics/1266092011", "Smartwatch Best Sellers"),
            # Deals pages (filtered deals, higher conversion)
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_702327011_dt_sl14_61", "Laptop Deals"),
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_1292115011_dt_sl14_61", "Monitor Deals"),
            # Coupons and outlet (different inventory, rotates daily)
            ("https://www.amazon.com/Coupons?category=Electronics", "Electronics Coupons"),
            ("https://www.amazon.com/Coupons?category=PC", "Computer Coupons"),
            # Additional categories for variety
            ("https://www.amazon.com/Best-Sellers-Computers-Accessories/zgbs/pc/3015433011", "Storage Best Sellers"),
            ("https://www.amazon.com/Best-Sellers-Computers-Accessories/zgbs/pc/300189", "Networking Best Sellers"),
            ("https://www.amazon.com/Best-Sellers-Electronics/zgbs/electronics/7072561011", "Camera Best Sellers"),
            ("https://www.amazon.com/Best-Sellers-Video-Games/zgbs/videogames/14775003011", "Gaming Accessories"),
            # More deals pages
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_172541_dt_sl14_61", "Headphone Deals"),
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_702319011_dt_sl14_61", "Desktop Deals"),
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
            ("https://www.amazon.com/Best-Sellers-Kitchen-Dining/zgbs/kitchen", "Kitchen Best Sellers"),
            ("https://www.amazon.com/Best-Sellers-Home-Kitchen/zgbs/hi", "Home Best Sellers"),
            ("https://www.amazon.com/gp/movers-and-shakers/kitchen", "Kitchen Movers"),
            ("https://www.amazon.com/Coupons?category=Kitchen", "Kitchen Coupons"),
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_284507_dt_sl14_61", "Home Deals"),
            ("https://www.amazon.com/Best-Sellers-Tools-Home-Improvement/zgbs/hi/510182", "Tools Best Sellers"),
            ("https://www.amazon.com/gp/movers-and-shakers/hi", "Home Movers"),
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
            ("https://www.amazon.com/Best-Sellers-Sports-Outdoors/zgbs/sporting-goods", "Sports Best Sellers"),
            ("https://www.amazon.com/gp/movers-and-shakers/sporting-goods", "Sports Movers"),
            ("https://www.amazon.com/Coupons?category=Sports", "Sports Coupons"),
            ("https://www.amazon.com/deals?ref=dlx_deals_gd_dcl_img_1_3375251_dt_sl14_61", "Sports Deals"),
            ("https://www.amazon.com/Best-Sellers-Sports-Outdoors/zgbs/sporting-goods/3407901", "Exercise Best Sellers"),
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
