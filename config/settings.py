import os
from dotenv import load_dotenv

load_dotenv(override=True)


# Discord Bot (for reading reactions/feedback)
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

# Per-platform Discord channels (separate approval flows per platform)
# Falls back to DISCORD_CHANNEL_ID if not set
DISCORD_TWITTER_CHANNEL_ID = os.getenv("DISCORD_TWITTER_CHANNEL_ID", "") or DISCORD_CHANNEL_ID
DISCORD_LINKEDIN_CHANNEL_ID = os.getenv("DISCORD_LINKEDIN_CHANNEL_ID", "")

# Discord Webhook
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Reply Guy — separate channel for reply suggestions (keeps deal posts clean)
DISCORD_REPLY_CHANNEL_ID = os.getenv("DISCORD_REPLY_CHANNEL_ID", "")

# X/Twitter API credentials (for reply posting — separate from Postiz)
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
X_API_KEY = os.getenv("X_API_KEY", "")
X_API_SECRET = os.getenv("X_API_SECRET", "")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")

# Amazon Associates
AMAZON_AFFILIATE_TAG = os.getenv("AMAZON_AFFILIATE_TAG", "quadstar0e-20")

# LLM Configuration — DeepSeek Flash (primary) + OpenRouter (free-tier fallback)
# Primary: get your key at https://platform.deepseek.com/api_keys
# Context caching is automatic — repeated system prompts hit cache for $0.0028/1M tokens
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", os.getenv("LLM_API_KEY", ""))
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")

# Fallback: OpenRouter free tier (used automatically when DeepSeek errors/rate-limits)
# Get your key at https://openrouter.ai/keys — free models end in ":free"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/owl-alpha")

# Brand settings — change these in .env to rename without touching any code
BRAND_NAME = os.getenv("BRAND_NAME", "QuadStar Deals")
BRAND_HASHTAG = os.getenv("BRAND_HASHTAG", "#QuadStarDeals")

# Deal sources scraped by src/scraper.py (Playwright, free).
# Slickdeals + DealNews are also covered by src/rss_scraper.py (RSS, free). The
# Playwright path complements RSS by extracting the price/discount data RSS
# items don't always include. Camelcamelcamel removed — its top-drops page is
# behind JS-heavy anti-bot that Playwright can't reliably penetrate; the
# lowest-ever signal we already track from our own price_history covers the
# Camel use case at zero cost.
DEAL_SOURCES = [
    {
        "name": "DealNews",
        "url": "https://www.dealnews.com/c142/Electronics/",
        "type": "aggregator",
    },
]

# Deal filtering
MAX_POSTS_PER_RUN = 10
MIN_DISCOUNT_PCT = 15.0
MIN_DEAL_PRICE = 50.0        # High-ticket items only
MAX_DISCOUNT_PCT = 70.0      # Above this is likely inflated list price — filter out fake discounts
AMAZON_ONLY = True           # Only show deals with Amazon links (you earn commission)

# Unified pipeline thresholds — all three must pass to auto-post.
# Nothing meets criteria → deal is silently skipped. No Discord cards.
# 15 = sanity floor only. The 35 floor double-filtered what the score already
# weighs (discount is 20 of 100 pts) and dropped premium deals before scoring —
# Bose/Sony/Apple rarely discount 35%+. Let the score gate be the real quality
# bar; this just excludes non-deals. NOTE: also set in .env — update both.
PIPELINE_MIN_DISCOUNT = float(os.getenv("PIPELINE_MIN_DISCOUNT", "15"))
# 35, recalibrated against live inventory: fixing the lowest-ever badge over-fire
# (it previously fired for EVERY deal = a phantom +22) dropped honest scores ~17
# pts. 35 restores ~3-4 posts/run from the current queue. Will rise naturally as
# real price history + engagement data accumulate. See qualifies_as_lowest().
PIPELINE_MIN_SCORE = float(os.getenv("PIPELINE_MIN_SCORE", "35"))
PIPELINE_MIN_CONFIDENCE = float(os.getenv("PIPELINE_MIN_CONFIDENCE", "0.85"))
# Max auto-posts to X per calendar day. Best-scored deals post first.
# Prevents flood during big sales (Prime Day etc). Set 0 to disable cap.
PIPELINE_MAX_DAILY_POSTS = int(os.getenv("PIPELINE_MAX_DAILY_POSTS", "4"))
# Max auto-posts per category per calendar day — forces feed variety so one
# hot category (e.g. headphones) can't take every daily slot. Set 0 to disable.
PIPELINE_MAX_PER_CATEGORY_PER_DAY = int(os.getenv("PIPELINE_MAX_PER_CATEGORY_PER_DAY", "2"))

# Deal scoring weights (0-100 composite score).
# Tuned for REVENUE / CLICKS: reward verifiable "real deal" signals that drive a
# converting click. Lowest-ever price is the hardest-to-fake proof a deal is real,
# so it carries the most weight; price sweet-spot ($100-500) is the impulse-buy +
# commission band; trending (repeat appearances) signals genuine demand. Brand is a
# trust multiplier (down-weighted + word-boundary matched, see score_deal). Engagement
# is mostly a constant today (sparse history) so it is the smallest factor.
# Weights MUST sum to 100 so the 0-100 scale and the PIPELINE_MIN_SCORE gate hold.
SCORE_WEIGHT_DISCOUNT = 20
SCORE_WEIGHT_BRAND = 14
SCORE_WEIGHT_PRICE_RANGE = 18
SCORE_WEIGHT_ENGAGEMENT = 8
SCORE_WEIGHT_BADGE = 22
SCORE_WEIGHT_FRESHNESS = 8
SCORE_WEIGHT_TRENDING = 10

# Minimum price drop % from the original posted price for price-drop auto-approve.
# Gates the 15-min timer path alongside PIPELINE_MIN_SCORE + PIPELINE_MIN_CONFIDENCE.
MIN_PRICE_DROP_AUTO_PCT = float(os.getenv("MIN_PRICE_DROP_AUTO_PCT", "5"))

# How many days back to check for a recently-posted ASIN before auto-approving.
# Prevents double-posting the same product within N days.
ASIN_REPOST_COOLDOWN_DAYS = int(os.getenv("ASIN_REPOST_COOLDOWN_DAYS", "7"))

# Known brand tiers for scoring
BRAND_TIER_1 = [
    "apple", "sony", "bose", "samsung", "lg", "dell", "hp", "lenovo",
    "asus", "microsoft", "google", "nvidia", "amd", "intel", "logitech",
    "razer", "corsair", "steelseries", "jbl", "sennheiser",
]
BRAND_TIER_2 = [
    "anker", "tp-link", "netgear", "western digital", "seagate", "crucial",
    "hyperx", "elgato", "shokz", "jabra", "philips", "epson", "brother",
    "roku", "amazon", "fire", "echo", "ring", "eufy", "roborock",
]

# Postiz (self-hosted social media scheduler)
# Start with: docker-compose up in your postiz-app directory → localhost:3000
POSTIZ_API_URL = os.getenv("POSTIZ_API_URL", "http://localhost:3000/api")
POSTIZ_API_KEY = os.getenv("POSTIZ_API_KEY", "")

# Postiz session auth — JWT for media uploads (internal API, not public API)
# Generate from: docker exec postiz env | grep JWT_SECRET
POSTIZ_JWT_SECRET = os.getenv("POSTIZ_JWT_SECRET", "")
POSTIZ_USER_ID = os.getenv("POSTIZ_USER_ID", "")
POSTIZ_USER_EMAIL = os.getenv("POSTIZ_USER_EMAIL", "")

# Postiz integration IDs — one per connected social account.
# Get them after connecting accounts in Postiz UI:
#   curl http://localhost:3000/api/integrations -H "Authorization: Bearer YOUR_KEY"
POSTIZ_TWITTER_ID = os.getenv("POSTIZ_TWITTER_ID", "")
POSTIZ_INSTAGRAM_ID = os.getenv("POSTIZ_INSTAGRAM_ID", "")
POSTIZ_LINKEDIN_ID = os.getenv("POSTIZ_LINKEDIN_ID", "")
POSTIZ_TIKTOK_ID = os.getenv("POSTIZ_TIKTOK_ID", "")
POSTIZ_FACEBOOK_ID = os.getenv("POSTIZ_FACEBOOK_ID", "")
POSTIZ_REDDIT_ID = os.getenv("POSTIZ_REDDIT_ID", "")
POSTIZ_BLUESKY_ID = os.getenv("POSTIZ_BLUESKY_ID", "")
POSTIZ_THREADS_ID = os.getenv("POSTIZ_THREADS_ID", "")

# OpenClaw (optional browser delegation for bot-blocked sites — unused by default).
# Orchestration is the Hermes agent, which drives this backend via /webhook/hermes.
# OPENCLAW_WEBHOOK_URL: your OpenClaw webhook plugin URL
#   e.g. http://localhost:4000/plugins/webhooks/quadstar
# OPENCLAW_SECRET: Bearer token configured in OpenClaw webhook plugin settings
# Leave empty to disable OpenClaw browse delegation (default).
OPENCLAW_WEBHOOK_URL = os.getenv("OPENCLAW_WEBHOOK_URL", "")
OPENCLAW_SECRET = os.getenv("OPENCLAW_SECRET", "")

# Agent profile — selects niche-specific config (keywords, brand tiers, sources,
# Amazon category URLs). Profiles live in profiles/<name>.yaml.
# When unset or "tech" (the default), behaviour is identical to pre-profile pipeline.
# Replicate for a new vertical: copy profiles/tech.yaml → profiles/sneakers.yaml,
# set AGENT_PROFILE=sneakers + PORT=8002 + a separate Discord channel.
AGENT_PROFILE = os.getenv("AGENT_PROFILE", "tech")

# Data directory — isolated per agent profile. Each niche gets its own deals.json,
# price_history.json, etc. Defaults to ./data/<profile> so multiple agents can
# run side-by-side without clobbering each other's data.
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.getenv(
    "DATA_DIR",
    os.path.join(_repo_root, "data", AGENT_PROFILE) if AGENT_PROFILE != "tech"
    else os.path.join(_repo_root, "data"),  # preserves existing data/ layout for tech
)

PLATFORM_IDS = {
    "twitter": POSTIZ_TWITTER_ID,
    "x": POSTIZ_TWITTER_ID,
    "instagram": POSTIZ_INSTAGRAM_ID,
    "linkedin": POSTIZ_LINKEDIN_ID,
    "tiktok": POSTIZ_TIKTOK_ID,
    "facebook": POSTIZ_FACEBOOK_ID,
    "reddit": POSTIZ_REDDIT_ID,
    "bluesky": POSTIZ_BLUESKY_ID,
    "threads": POSTIZ_THREADS_ID,
}

# Price drop auto-repost thresholds
MIN_REPOST_DROP_PCT = float(os.getenv("MIN_REPOST_DROP_PCT", "20"))  # was 15
MIN_ALERT_DROP_PCT = float(os.getenv("MIN_ALERT_DROP_PCT", "10"))
MIN_REPOST_DROP_DOLLARS = float(os.getenv("MIN_REPOST_DROP_DOLLARS", "15"))
FAST_TRACK_MINUTES = int(os.getenv("FAST_TRACK_MINUTES", "15"))
LOWEST_IN_DAYS = int(os.getenv("LOWEST_IN_DAYS", "90"))
# Max price-drop reposts sent to Discord per price-monitor cycle.
# Prevents follower spam when many ASINs drop simultaneously.
# The top N by drop_pct are picked; the rest still generate FYI alerts.
MAX_PRICE_DROP_REPOSTS_PER_CYCLE = int(os.getenv("MAX_PRICE_DROP_REPOSTS_PER_CYCLE", "2"))

# Tech-only keyword filter — deal title must contain at least one
TECH_KEYWORDS = [
    # Computing
    "laptop", "macbook", "chromebook", "notebook", "desktop", "pc", "imac",
    "monitor", "display", "gpu", "graphics card", "processor", "cpu", "ram",
    "ssd", "hard drive", "nvme", "motherboard", "keyboard", "mouse",
    "webcam", "router", "modem", "wifi", "mesh",
    # Mobile & Audio
    "iphone", "ipad", "tablet", "samsung galaxy", "pixel", "smartphone",
    "airpods", "earbuds", "headphones", "speaker", "soundbar",
    # Gaming
    "playstation", "ps5", "xbox", "nintendo", "switch", "gaming",
    "controller", "console", "steam deck",
    # Smart Home & Wearables
    "apple watch", "smartwatch", "fitbit", "garmin", "ring doorbell",
    "echo", "alexa", "google nest", "smart home", "thermostat",
    "security camera", "robot vacuum", "roomba",
    # TVs & Streaming
    "tv", "television", "oled", "qled", "4k", "8k", "projector",
    "fire stick", "roku", "chromecast", "apple tv",
    # Tech Accessories & Appliances
    "charger", "power bank", "usb-c", "thunderbolt", "docking station",
    "printer", "scanner", "nas", "external drive", "flash drive",
    "drone", "camera", "gopro", "lens", "tripod",
    "3d printer", "vr", "oculus", "meta quest",
]


# --- Profile Overrides (Multi-Agent Scalability) ---
# If a profile file exists at profiles/<AGENT_PROFILE>.yaml, its values
# override the defaults above. This lets the same codebase run for tech,
# sneakers, home & improvement, etc. by just setting AGENT_PROFILE=<name>
# and copying profiles/tech.yaml → profiles/<name>.yaml with edits.
# When no profile file exists, behaviour is identical to today (tech defaults).
_profile_path = os.path.join(_repo_root, "profiles", f"{AGENT_PROFILE}.yaml")
if os.path.exists(_profile_path):
    try:
        import yaml  # PyYAML
        with open(_profile_path) as _f:
            _profile = yaml.safe_load(_f) or {}
        # Override brand/identity
        if _profile.get("brand_name"):
            BRAND_NAME = _profile["brand_name"]
        if _profile.get("brand_hashtag"):
            BRAND_HASHTAG = _profile["brand_hashtag"]
        # Override affiliate tag
        if _profile.get("affiliate_tag"):
            AMAZON_AFFILIATE_TAG = _profile["affiliate_tag"]
        # Override niche-specific lists
        if _profile.get("keywords"):
            TECH_KEYWORDS = list(_profile["keywords"])
        if _profile.get("deal_sources"):
            DEAL_SOURCES = list(_profile["deal_sources"])
        if _profile.get("brand_tier_1"):
            BRAND_TIER_1 = list(_profile["brand_tier_1"])
        if _profile.get("brand_tier_2"):
            BRAND_TIER_2 = list(_profile["brand_tier_2"])
        print(f"  [settings] Loaded agent profile: {AGENT_PROFILE} ({BRAND_NAME})")
    except Exception as _exc:
        print(f"  [settings] Profile load failed ({_profile_path}): {_exc} — using defaults")
