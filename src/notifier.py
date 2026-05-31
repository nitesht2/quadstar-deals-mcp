"""
notifier.py - Discord Deal Notifier with AI-Generated Tweets

Uses LLM abstraction layer to generate unique tweet copy for each deal.
No two posts look the same — different hooks, angles, and energy.
LLM provider is swappable via LLM_PROVIDER env var (see src/llm.py).

X algorithm optimization (from twitter/the-algorithm open source):
- Tweet 1: NO links (~50% reach penalty), native image (2x boost), reply bait (54x signal),
  bookmark prompt, 2 hashtags max (3+ = spam penalty), text-rich for detail expand (11x)
- Tweet 2: Reply with affiliate link + CTA (replies boost thread 54x)
"""

import os
import time
import requests
from config.settings import (
    DISCORD_WEBHOOK_URL, DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID,
    BRAND_NAME, MAX_POSTS_PER_RUN, MIN_DISCOUNT_PCT,
)
from src.llm import (
    generate as llm_generate,
    generate_structured as llm_generate_structured,
    LLM_API_KEY, LLM_PROVIDER,
)
from src.database import get_top_unposted_deals, mark_as_posted, update_deal


def _deal_content_schema():
    """Build the Pydantic schema for structured deal content.

    Defined lazily inside a function so pydantic is only imported when
    the structured path is actually taken. The LLM returns an instance
    of this schema, eliminating the TWEET1:/TWEET2:/LINKEDIN: string
    parsing that the legacy path relies on.
    """
    from pydantic import BaseModel, Field

    class DealContent(BaseModel):
        tweet_1: str = Field(
            description=(
                "Tweet 1 under 280 chars. Write like a sharp person sharing a find — "
                "NOT a brand account. NO bullet points. NO checkmarks (✅ or •). NO emojis. "
                "NO ALL CAPS product names. NO hype words. "
                "Structure: one punchy hook sentence (specific, not vague). "
                "Then 2-3 real specs as short punchy sentences — vary the length, "
                "mix short and medium. One reaction line (direct, opinionated). "
                "End with 'Link below.' then '#ad' and one relevant hashtag. "
                "Example rhythm: 'Sony just made their best headphones cheaper. "
                "30hr battery. ANC that actually works. Multipoint for two devices. "
                "Hard to ignore at this price. Link below. #ad #TechDeals' — "
                "that kind of energy. Short sentences hit hard. NO links. NO prices."
            ),
        )
        tweet_2: str = Field(
            description=(
                "Tweet 2 under 280 chars. Short urgent CTA in plain conversational language, "
                "then the affiliate URL on its own line, then "
                "'Follow @quadstardeals for daily tech deals.' "
                "No emojis. No hype. MUST include the URL verbatim and MUST end with "
                "the follow line."
            ),
        )
        linkedin_post: str = Field(
            default="",
            description=(
                "Optional LinkedIn post (800-1200 chars) using Problem-First "
                "structure for high-discount deals or Deal Analyst structure "
                "otherwise. Must include the affiliate URL on its own line. "
                "Empty string when LinkedIn is not a target platform."
            ),
        )

    return DealContent


def _add_reactions(message_id: str):
    """Add 👍 and 👎 reaction buttons to a message."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    base = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}/reactions"
    for emoji in ["%F0%9F%91%8D", "%F0%9F%91%8E"]:
        try:
            requests.put(f"{base}/{emoji}/@me", headers=headers, timeout=5)
        except Exception:
            pass


def _generate_tweets(deal: dict) -> tuple[str, str]:
    """Generate Twitter content. Calls the unified content generator."""
    content = _generate_content(deal)
    return content["tweet_1"], content["tweet_2"]


# Banned AI vocabulary — words that instantly flag content as AI-generated.
# Source: config/brand_voice.md (from ~/.voice/03-universal-rules.md)
_BANNED_AI_WORDS = [
    # AI clichés (from brand voice — full list in config/brand_voice.md)
    "pivotal", "crucial", "vibrant", "delve", "delves", "delving",
    "tapestry", "foster", "fosters", "fostering", "showcase", "showcases",
    "showcasing", "underscore", "underscores", "underscoring", "testament",
    "landscape", "groundbreaking", "renowned", "nestled", "boasts",
    "exemplify", "exemplifies", "garner", "garners", "garnered",
    "intricate", "intricacies", "pioneering", "trailblazing", "unleash",
    "transformative", "redefine", "seamless", "robust", "game-changer",
    "leverage", "leveraging", "empower", "empowering", "streamline",
    "next-gen", "frictionless", "elevate", "innovative",
    "cutting-edge", "cutting edge", "unprecedented", "intuitive",
    "state-of-the-art", "democratize", "accelerate",
    "hyper-personalized", "revolutionize", "revolutionary",
    "proactive", "scalable", "optimize", "breakthrough", "disruptive",
    "reimagine", "agile", "future-proof", "AI-powered", "result-driven",
    "results-driven", "paradigm", "paradigm-shift", "paradigm-shifting",
    "synergy", "synergize", "groundbreaking",
    "plug-and-play", "turnkey", "holistic", "align",
    "smart", "intelligent", "efficient", "dynamic", "reliable",
    "immersive", "predictive", "integrated",
    "unparalleled", "versatile", "visionary", "world-class",
    # Sentence structures that scream AI (handled in prompt, but also blocked here)
    "not only", "but also",
]


def _truncate_tweet(text: str, limit: int = 280) -> str:
    """Truncate to X's limit at a word/line boundary, not mid-word.

    Walks back from the limit to the last whitespace so we never chop "#Lapto"
    or "Wi-F". If the resulting text is materially shorter, add an ellipsis —
    otherwise just return clean text.
    """
    if len(text) <= limit:
        return text

    # Prefer breaking at a line break, then any whitespace, working backwards
    budget = limit - 1  # leave room for the ellipsis
    cut = text.rfind("\n", 0, budget)
    if cut < budget - 60:  # line break too far back, try any whitespace
        cut = text.rfind(" ", 0, budget)
    if cut < 0:  # no whitespace — last resort
        cut = budget

    truncated = text[:cut].rstrip()
    # Only add ellipsis if we cut mid-thought (not at a natural boundary)
    if not truncated.endswith((".", "!", "?", ":", ";")):
        truncated = truncated + "…"
    return truncated


def _clean_ai_words(text: str) -> str:
    """Remove banned AI vocabulary from generated text."""
    import re
    result = text
    replacements = {
        "pivotal": "important", "crucial": "important", "vibrant": "active",
        "delve": "explore", "delves": "explores", "delving": "exploring",
        "tapestry": "mix", "foster": "build", "fosters": "builds",
        "showcase": "show", "showcases": "shows", "showcasing": "showing",
        "underscore": "show", "underscores": "shows", "underscoring": "showing",
        "testament": "proof", "landscape": "space",
        "groundbreaking": "new", "renowned": "well-known",
        "nestled": "located", "boasts": "has",
        "enhancing": "improving", "enhance": "improve", "enhances": "improves",
        "exemplifies": "shows", "exemplify": "show",
        "garner": "get", "garners": "gets", "garnered": "got",
        "enduring": "lasting", "fostering": "building",
        "interplay": "connection",
        "intricate": "detailed", "intricacies": "details",
    }
    for bad, good in replacements.items():
        result = re.sub(rf'\b{bad}\b', good, result, flags=re.IGNORECASE)
    # Remove multi-word phrases
    for phrase in ["commitment to", "serves as", "stands as", "marks a"]:
        result = re.sub(rf'{re.escape(phrase)}', "is", result, flags=re.IGNORECASE)
    return result


# Four distinct structural formats for Tweet 1. The LLM is told to pick exactly ONE
# per deal (deterministically seeded from deal_id). This kills the "every post looks
# identical" problem without sacrificing the deal-post flavor.
_TWEET_FORMATS = {
    "contrast_flip": """[Setup line in normal sentence case — what most people expect or what usually happens]

[Product name]
[The flip — what makes this one different, with the deal signal woven in. Normal sentence case.]

[Link callout with an emoji]
[Optional reply hook — one casual question to invite a reply, e.g. "Home office or travel use?"]
#ad #[OneRelevantHashtag]""",

    "question_inline": """[Question hook in sentence case — something a shopper would actually wonder]

[one-line answer or take — honest, specific]

[Product name]
[inline specs separated by commas — e.g. "A16 chip, 128GB, Liquid Retina, Wi-Fi 6"]

[reaction that folds in the deal signal naturally]
[link callout with an emoji]
#ad #[OneRelevantHashtag]""",

    "number_hook": """[Lead with a specific number — the price, the discount, or a key stat. One punchy sentence. No spec lists.]

[Product name]
[one sentence of context — why that number matters, written as a reaction, not a spec list]

[link callout with an emoji]
[Optional reply hook — one casual question to invite a reply, e.g. "anyone else been watching this one?"]
#ad #[OneRelevantHashtag]""",

    "prose_no_bullets": """[2–3 short sentences in normal sentence case. First sentence is the hook. Second is the "why" with the deal signal woven in. Optional third: a specific spec.]

[Product name in line with the prose — not isolated]

[Link callout with an emoji]
#ad #[OneRelevantHashtag]""",
}


def _pick_tweet_format(deal: dict) -> tuple[str, str]:
    """Pick the tweet format based on the deal's strongest signal.

    Signal priority (first match wins):
    1. is_lowest_ever         → number_hook   (the price number IS the story)
    2. discount_pct >= 40%    → contrast_flip (expectation vs. reality gap)
    3. star_rating >= 4.5
       AND review_count >= 500 → question_inline (social proof invites conversation)
    4. fallback               → prose_no_bullets (human voice, no gimmick)

    Deterministic per deal so the same deal always gets the same format —
    A/B comparisons and regenerations stay consistent.
    """
    if deal.get("is_lowest_ever"):
        name = "number_hook"
    elif (deal.get("discount_pct") or 0) >= 40:
        name = "contrast_flip"
    elif (deal.get("star_rating") or 0) >= 4.5 and (deal.get("review_count") or 0) >= 500:
        name = "question_inline"
    else:
        name = "prose_no_bullets"
    return name, _TWEET_FORMATS[name]


# LinkedIn posting is not production-ready yet. Flip this env var to "true" when
# the LinkedIn Postiz integration is wired up. Until then, we skip LinkedIn
# generation entirely — saves tokens and avoids surfacing half-baked content.
LINKEDIN_ENABLED = os.getenv("LINKEDIN_ENABLED", "false").lower() == "true"


def _load_brand_voice() -> str:
    """Load QuadStar brand voice doc. Returns empty string if file not found."""
    voice_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "brand_voice.md")
    try:
        with open(voice_path) as f:
            return f.read()
    except (FileNotFoundError, IOError):
        return ""


def _load_anti_ai_rules() -> str:
    """Load condensed anti-AI writing rules from ~/.voice/anti-ai-rules.md.

    Returns empty string if file not found so the system prompt degrades
    gracefully rather than crashing.
    """
    voice_dir = os.path.expanduser("~/.voice")
    rules_path = os.path.join(voice_dir, "anti-ai-rules.md")
    try:
        with open(rules_path) as f:
            return f.read()
    except (FileNotFoundError, IOError):
        return ""


def _load_marketing_hooks() -> str:
    """Load deal-specific marketing psychology rules (rules 1-9).

    Distilled from MARKETING GENIUS.md + COPYWRITING.md.
    Rule 10 (one ask per tweet) is excluded — Twitter's algorithm suppresses
    linked tweets, so link placement is handled separately in the tweet format.
    Returns empty string if file not found.
    """
    hooks_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "marketing_hooks.md")
    try:
        with open(hooks_path) as f:
            return f.read()
    except (FileNotFoundError, IOError):
        return ""


def _build_system_prompt() -> str:
    """Build the system prompt — static across calls, so Anthropic caches it.

    Everything that does NOT depend on the specific deal goes here: all 4 tweet
    formats, rules, banned vocabulary, response format. The user prompt only
    contains the per-deal bits (product, signals, chosen format name).
    """
    formats_text = "\n\n".join(
        f"FORMAT [{name}]:\n{template}"
        for name, template in _TWEET_FORMATS.items()
    )

    brand_voice = _load_brand_voice()
    brand_voice_section = f"\n\n====== BRAND VOICE ======\n{brand_voice}" if brand_voice else ""

    anti_ai_rules = _load_anti_ai_rules()
    anti_ai_section = f"\n\n====== ANTI-AI WRITING RULES ======\n{anti_ai_rules}" if anti_ai_rules else ""

    marketing_hooks = _load_marketing_hooks()
    marketing_section = f"\n\n====== MARKETING PSYCHOLOGY (apply to every tweet) ======\n{marketing_hooks}" if marketing_hooks else ""

    return f"""You write 2-tweet bundles for a tech affiliate deal account on X/Twitter (@quadstardeals).{brand_voice_section}{anti_ai_section}{marketing_section}
Every output is exactly: TWEET1 text, then TWEET2 text. Nothing else. No preamble, no "Here is…", no markdown headers.

====== AVAILABLE TWEET1 FORMATS ======
The user will tell you which format to use. Use EXACTLY that format. Do not blend formats.

{formats_text}

====== TWEET2 STRUCTURE ======
[Urgent one-liner CTA, first word capitalized — e.g. "Grab it before it's gone." / "This won't last long." / "Don't sleep on this one."]

[affiliate URL from the user prompt]

Follow @quadstardeals for daily tech deals.

====== DEAL SIGNALS USAGE ======
When the user prompt lists DEAL SIGNALS, you MUST fold exactly ONE of them naturally into Tweet 1 (hook OR reaction line, NOT as a separate bullet). Discount % and was/now prices are the currency of deal accounts — leaving them out makes the post read like a product description, not a deal.

====== HARD RULES ======
- Prices MUST come from the DEAL SIGNALS in the user prompt. Use the exact was/now figures provided. Never invent or estimate prices.
- No slang (no tbh, ngl, lowkey, gonna)
- Features must be REAL specs from the product title. Do NOT invent specs.
- Format adherence: if the chosen format uses ✅, use ✅. If it uses →, use →. If it says no bullets, use NO bullets. Do not default to ✅ bullets when the format doesn't call for them.
- Never start Tweet 1 or Tweet 2 with a preamble like "Here's", "Okay,", "Sure,". Jump straight into the tweet.
- Blank line between sections within a tweet.
- Tweet 1: under 280 characters, NO links.
- Tweet 2: under 280 characters, MUST end with: Follow @quadstardeals for daily tech deals.
- Every claim needs a specific fact. No vague praise.

====== BANNED AI VOCABULARY ======
Never use: pivotal, crucial, vibrant, delve, tapestry, foster, showcase, underscore, testament, landscape, groundbreaking, renowned, nestled, boasts, exemplifies, garner, enduring, intricate.

====== BANNED AI PATTERNS ======
Never use:
- "Not just X, but Y"
- "Serves as" / "stands as" / "marks a"
- "Commitment to..."
- Three-adjective rule-of-three stacks ("sleek, elegant, powerful")
- "Despite challenges…" wrap-up pattern

Use plain verbs like "is" and "has" instead of fancy substitutes.

====== EXAMPLES (copy the structure, not the content) ======

EXAMPLE A — FORMAT contrast_flip:
TWEET1:
Most budget headphones lose the bass the second you turn on ANC.

Bose QuietComfort 45
These don't. 40% off and still the benchmark for noise cancellation at this price.

Deal link 👇
Home office or commute use?
#ad #HeadphoneDeals

TWEET2:
Grabbed one last week. Worth every penny.

https://amzn.to/example

Follow @quadstardeals for daily tech deals.

---

EXAMPLE B — FORMAT number_hook:
TWEET1:
$179 for a Wi-Fi 6 mesh system that covers 5,500 sq ft.

TP-Link Deco XE75 Pro (2-pack)
That's less than half what this cost a year ago. Lowest tracked price right now.

Deal link 👇
#ad #HomeNetworking

TWEET2:
Don't sleep on this one.

https://amzn.to/example

Follow @quadstardeals for daily tech deals.

====== RESPONSE FORMAT ======
Respond EXACTLY as:

TWEET1:
[tweet 1 content]

TWEET2:
[tweet 2 content]
"""


# Estimated use-life in days per product category keyword.
# Used to compute price-per-day signal for higher-ticket items.
_USE_LIFE_DAYS: list[tuple[list[str], int]] = [
    (["headphone", "earbud", "airpod"], 3 * 365),
    (["laptop", "notebook", "chromebook", "macbook"], 4 * 365),
    (["monitor", "display"], 5 * 365),
    (["tablet", "ipad"], 4 * 365),
    (["smartwatch", "apple watch", "galaxy watch"], 3 * 365),
    (["router", "mesh", "wifi", "wi-fi"], 5 * 365),
    (["tv ", "television", "qled", "oled", "4k tv", "smart tv"], 7 * 365),
    (["camera", "mirrorless", "dslr"], 5 * 365),
    (["keyboard", "mouse", "trackpad"], 5 * 365),
    (["speaker", "soundbar"], 5 * 365),
]


def _price_per_day(deal: dict) -> str | None:
    """Return a price-per-day string if the deal is compelling enough to mention.

    Only emits when: price > $50, daily cost < $0.40, and the category has a
    known use-life estimate. Returns None otherwise.
    """
    price = deal.get("deal_price") or deal.get("price") or deal.get("sale_price")
    if not price or price < 50:
        return None
    title_lower = (deal.get("title") or "").lower()
    days = None
    for keywords, life_days in _USE_LIFE_DAYS:
        if any(kw in title_lower for kw in keywords):
            days = life_days
            break
    if not days:
        return None
    daily = price / days
    if daily >= 0.40:
        return None
    return f"${daily:.2f}/day over {days // 365} years of use"


def _build_signals(deal: dict) -> str:
    """Build the DEAL SIGNALS block shared by content generation and A/B variants.

    Centralised here so both callers stay in sync. Returns an empty string when
    there are no signals worth injecting.
    """
    discount = deal.get("discount_pct") or 0
    original_price = deal.get("original_price")
    current_price = deal.get("deal_price") or deal.get("price") or deal.get("sale_price")
    signals: list[str] = []

    # Discount + anchoring: always show was/now when both prices available.
    # Rule of 100: under $100 use %, over $100 use $ amount. Both shown here
    # so the LLM can pick the right framing per deal.
    if discount >= 20:
        signals.append(f"{int(round(discount))}% off right now")
    if original_price and current_price and original_price > current_price:
        signals.append(f"was ${original_price:.0f}, now ${current_price:.2f}")

    # Loss aversion: is_lowest_ever adds urgency on top of the price anchor.
    if deal.get("is_lowest_ever"):
        if original_price and current_price and original_price > current_price:
            drop = original_price - current_price
            signals.append(
                f"lowest price tracked (${drop:.0f} drop). price history says this is rare."
            )
        else:
            signals.append("lowest price ever tracked on this — price history says it won't stay here")

    if deal.get("extra_savings"):
        signals.append(f"extra ${deal['extra_savings']} off stacks on top")
    if deal.get("coupon_code"):
        signals.append(f"coupon code: {deal['coupon_code']}")

    # Social proof — only when rating is genuinely good
    star_rating = deal.get("star_rating")
    review_count = deal.get("review_count")
    if star_rating and star_rating >= 4.0 and review_count and review_count >= 100:
        count_str = f"{review_count:,}" if review_count < 1_000_000 else f"{review_count / 1000:.0f}k"
        signals.append(f"{star_rating}★ rated ({count_str} reviews)")

    # Price-per-day — only for higher-ticket items where the math is striking
    ppd = _price_per_day(deal)
    if ppd:
        signals.append(ppd)

    if not signals:
        return ""
    return (
        "DEAL SIGNALS (fold ONE into Tweet 1 naturally — hook or reaction line):\n"
        + "\n".join(f"- {s}" for s in signals)
    )


def _generate_content(deal: dict) -> dict:
    """Generate platform-specific content for a deal in one LLM call.

    Returns dict with keys: tweet_1, tweet_2, linkedin_post.
    LinkedIn post is only generated if the deal matches LinkedIn keywords.
    """
    if not LLM_API_KEY and LLM_PROVIDER != "ollama":
        print("    No LLM_API_KEY set, using static template. Set LLM_PROVIDER=ollama or add ANTHROPIC_API_KEY.")
        return {
            "tweet_1": _build_tweet_1_template(deal),
            "tweet_2": _build_tweet_2_template(deal),
            "linkedin_post": "",
            "confidence": 0.0,
        }

    print(f"    Calling LLM to generate content for: {deal['title'][:50]}...")

    # LinkedIn generation is disabled by default (placeholder for future rollout).
    # The match_platforms() check still runs so routing metadata stays accurate.
    from src.platform_router import match_platforms
    matched = match_platforms(deal)
    needs_linkedin = LINKEDIN_ENABLED and ("linkedin" in matched)

    # --- Structured-output fast path -------------------------------------
    # When running on a supported provider (Ollama or Anthropic) we ask the
    # LLM for a guaranteed-valid DealContent instance via response_format.
    # This skips the brittle TWEET1:/TWEET2:/LINKEDIN: parser below and
    # also returns a logprobs-derived confidence score the caller can use
    # as an auto-approve gate.
    try:
        url_for_schema = deal.get("affiliate_url") or deal.get("source_url", "")
        schema = _deal_content_schema()
        structured_prompt = (
            f"Write social media content for this product. Sound like a sharp person sharing a real find — not a brand, not an AI.\n\n"
            f"Product: {deal['title']}\n"
            f"Affiliate URL (put this in tweet_2 and linkedin_post): {url_for_schema}\n"
            f"{'LinkedIn is a target platform — fill linkedin_post.' if needs_linkedin else 'LinkedIn not needed — leave linkedin_post as an empty string.'}\n\n"
            "WRITING RULES (non-negotiable):\n"
            "- No bullet points. No checkmarks (✅ • -). No lists. Write in sentences.\n"
            "- No emojis. Ever.\n"
            "- No ALL CAPS words or product names.\n"
            "- Short sentences. Vary the length. Mix 3-word punches with 10-word sentences.\n"
            "- Be specific, not vague. Say '30hr battery' not 'long battery life'.\n"
            "- Take a stance. Be direct. No hedging ('may', 'could', 'often considered').\n"
            "- No meta-commentary. Don't announce what you're about to say. Just say it.\n"
            "- No rule-of-three lists. No 'not only... but also...' constructions.\n"
            "- Prices must come from the deal signals provided. Use exact was/now figures. Never invent prices.\n"
            "- Features must be REAL specs implied by the product name — never fabricate.\n"
            "- Tweet 1 must NOT contain any URL.\n"
            "- Tweet 2 must contain the affiliate URL verbatim and end with: Follow @quadstardeals for daily tech deals.\n"
            "- Never use these words: pivotal, crucial, vibrant, delve, tapestry, foster, "
            "showcase, underscore, testament, landscape, groundbreaking, renowned, boasts, "
            "exemplifies, leverage, synergy, seamless, robust, transformative, innovative, "
            "game-changer, empower, streamline, holistic, paradigm, disruptive, unleash, "
            "elevate, dynamic, immersive, intuitive, state-of-the-art, unprecedented.\n"
        )
        obj, confidence = llm_generate_structured(structured_prompt, schema)
        if obj is not None:
            t1 = _clean_ai_words(obj.tweet_1.strip())
            t2 = _clean_ai_words(obj.tweet_2.strip())
            li = _clean_ai_words(obj.linkedin_post.strip()) if obj.linkedin_post else ""

            # Safety nets: same invariants the legacy parser enforces.
            if "http" in t1.lower():
                t1 = _build_tweet_1_template(deal)
            if url_for_schema and url_for_schema not in t2:
                t2 = t2.rstrip() + f"\n\n{url_for_schema}"
            if "@quadstardeals" not in t2:
                t2 = t2.rstrip() + "\n\nFollow @quadstardeals for daily tech deals."
            if needs_linkedin and li and url_for_schema and url_for_schema not in li:
                li = li.rstrip() + f"\n\n{url_for_schema}"
            if len(t1) > 280:
                t1 = t1[:277] + "..."
            if len(t2) > 280:
                t2 = t2[:277] + "..."

            print(f"    Structured content OK (confidence={confidence:.2f})")
            return {
                "tweet_1": t1,
                "tweet_2": t2,
                "linkedin_post": li if needs_linkedin else "",
                "confidence": confidence,
            }
    except Exception as e:
        print(f"    Structured generation failed ({e}); falling back to string parser")

    try:
        style_brief = ""
        try:
            from src.tweet_learner import get_style_insights
            style_brief = get_style_insights()
        except Exception:
            pass

        from src.tweet_learner import get_style_guidance
        style_hint = get_style_guidance()

        url = deal.get("affiliate_url") or deal["source_url"]
        coupon_info = f"Coupon code: {deal['coupon_code']}" if deal.get("coupon_code") else ""
        signals_block = _build_signals(deal)

        format_name, _ = _pick_tweet_format(deal)
        print(f"    Format selected for this deal: {format_name}")

        # Per-call context goes in the USER prompt. The system prompt carries all
        # the static scaffolding (rules, formats, banned words, response format).
        user_parts = [
            f"Product: {deal['title']}",
        ]
        if coupon_info:
            user_parts.append(coupon_info)
        if signals_block:
            user_parts.append(signals_block)
        user_parts.append(f"Use TWEET1 FORMAT: {format_name}")
        user_parts.append(f"Affiliate URL for Tweet 2: {url}")
        if style_brief:
            user_parts.append(style_brief)
        if style_hint:
            user_parts.append(style_hint)

        user_prompt = "\n\n".join(user_parts)
        system_prompt = _build_system_prompt()

        text = llm_generate(user_prompt, max_tokens=600, system=system_prompt)
        if not text:
            return {
                "tweet_1": _build_tweet_1_template(deal),
                "tweet_2": _build_tweet_2_template(deal),
                "linkedin_post": "",
                "confidence": 0.0,
            }

        # confidence=1.0 is the neutral default when provider doesn't expose logprobs
        # (Kimi/OpenAI path). Downgraded to 0.5 below if we fall back to a template.
        result = {"tweet_1": "", "tweet_2": "", "linkedin_post": "", "confidence": 1.0}

        # Parse LinkedIn first (if present) since it comes last
        linkedin_post = ""
        if needs_linkedin and "LINKEDIN:" in text:
            li_parts = text.split("LINKEDIN:")
            linkedin_post = _clean_ai_words(li_parts[-1].strip())
            # Ensure affiliate link is in LinkedIn post
            if url not in linkedin_post:
                linkedin_post = linkedin_post.rstrip() + f"\n\n{url}"
            text = li_parts[0]  # Remove LinkedIn section before parsing tweets

        # Parse tweets
        if "TWEET1:" in text and "TWEET2:" in text:
            parts = text.split("TWEET2:")
            tweet_1 = _clean_ai_words(parts[0].replace("TWEET1:", "").strip())
            tweet_2 = _clean_ai_words(parts[1].strip())

            # Safety: no link in tweet 1
            if "http" in tweet_1.lower():
                tweet_1 = _build_tweet_1_template(deal)

            # Safety: ensure affiliate link in tweet 2
            if url not in tweet_2:
                asin = deal.get("asin", "")
                if asin and asin in tweet_2:
                    import re as _re
                    tweet_2 = _re.sub(r'https?://\S+', url, tweet_2, count=1)
                else:
                    lines = tweet_2.split('\n')
                    lines.insert(1, "")
                    lines.insert(2, url)
                    tweet_2 = '\n'.join(lines)

            # Ensure follow line
            if "@quadstardeals" not in tweet_2:
                tweet_2 = tweet_2.rstrip() + "\n\nFollow @quadstardeals for daily tech deals."

            # Truncate at word/line boundaries (no mid-word chopping)
            result["tweet_1"] = _truncate_tweet(tweet_1)
            result["tweet_2"] = _truncate_tweet(tweet_2)
        else:
            # Parse failed — template fallback, reduce confidence
            result["tweet_1"] = _build_tweet_1_template(deal)
            result["tweet_2"] = _build_tweet_2_template(deal)
            result["confidence"] = 0.5

        result["linkedin_post"] = linkedin_post
        return result

    except Exception as e:
        print(f"    LLM error, falling back to template: {e}")
        import traceback
        traceback.print_exc()

    return {
        "tweet_1": _build_tweet_1_template(deal),
        "tweet_2": _build_tweet_2_template(deal),
        "linkedin_post": "",
        "confidence": 0.0,
    }


def _extract_features(title: str) -> list[str]:
    """Extract real product features/specs from the Amazon product title."""
    import re
    features = []
    title_lower = title.lower()

    # Size/inches
    m = re.search(r'(\d{2,3})["\s-]?\s*(?:inch|in\b|")', title_lower)
    if m:
        features.append(f'{m.group(1)}" screen')

    # Resolution
    for res, label in [("4k", "4K UHD resolution"), ("8k", "8K resolution"),
                       ("1080p", "1080p Full HD"), ("2k", "2K QHD"),
                       ("1440p", "1440p QHD"), ("retina", "Retina display")]:
        if res in title_lower:
            features.append(label)
            break

    # Wireless/Bluetooth
    if "bluetooth" in title_lower:
        m = re.search(r'bluetooth\s*([\d.]+)', title_lower)
        features.append(f"Bluetooth {m.group(1)}" if m else "Bluetooth enabled")
    elif "wireless" in title_lower:
        features.append("Wireless connectivity")

    # Battery
    m = re.search(r'(\d+)\s*h(?:our|r)?\s*battery', title_lower)
    if m:
        features.append(f"{m.group(1)}h battery life")

    # Storage/RAM
    for pattern, label in [(r'(\d+)\s*tb\b', '{}TB storage'), (r'(\d+)\s*gb\s*(?:ssd|storage|rom)', '{}GB storage'),
                           (r'(\d+)\s*gb\s*ram', '{}GB RAM')]:
        m = re.search(pattern, title_lower)
        if m:
            features.append(label.format(m.group(1)))

    # Noise canceling
    if "noise cancel" in title_lower or "anc" in title_lower:
        features.append("Active noise canceling")

    # Waterproof
    m = re.search(r'(ip[x\d]{2,3})', title_lower)
    if m:
        features.append(f"{m.group(1).upper()} waterproof")
    elif "waterproof" in title_lower or "water resistant" in title_lower:
        features.append("Waterproof design")

    # WiFi
    m = re.search(r'wi-?fi\s*(\d[a-z]?)', title_lower)
    if m:
        features.append(f"WiFi {m.group(1)}")

    # USB-C
    if "usb-c" in title_lower or "usb c" in title_lower or "type-c" in title_lower:
        features.append("USB-C charging")

    # Smart features
    if "alexa" in title_lower:
        features.append("Alexa built-in")
    elif "google assistant" in title_lower:
        features.append("Google Assistant")

    # HDR
    if "hdr" in title_lower:
        features.append("HDR support")

    # Dolby
    if "dolby" in title_lower:
        features.append("Dolby audio")

    # Mechanical keyboard
    if "mechanical" in title_lower:
        features.append("Mechanical switches")

    # RGB
    if "rgb" in title_lower:
        features.append("RGB lighting")

    # Processor
    for chip in ["m4", "m3", "m2", "m1", "i9", "i7", "i5", "ryzen 9", "ryzen 7", "ryzen 5", "snapdragon"]:
        if chip in title_lower:
            features.append(f"{chip.upper()} processor")
            break

    # OLED/AMOLED/LED
    for panel in ["oled", "amoled", "qled", "mini-led", "mini led"]:
        if panel in title_lower:
            features.append(f"{panel.upper()} display")
            break

    # Megapixels
    m = re.search(r'(\d+)\s*mp', title_lower)
    if m:
        features.append(f"{m.group(1)}MP camera")

    return features[:3]  # Max 3 features


def generate_price_drop_content(drop_info: dict) -> dict:
    """Generate LLM-first content for a price drop repost.

    LLM writes all content. Templates are last-resort fallback only.

    Args:
        drop_info: {asin, title, old_price, new_price, drop_pct,
                    original_posted_price, is_lowest_90d, is_lowest_ever,
                    affiliate_url, image_url, deal}

    Returns:
        {tweet_1, tweet_2, linkedin_post}
    """
    title = drop_info.get("title", "")
    new_price = drop_info.get("new_price", 0)
    original_posted = drop_info.get("original_posted_price", drop_info.get("old_price", 0))
    drop_pct = drop_info.get("drop_pct", 0)
    savings = original_posted - new_price
    url = drop_info.get("affiliate_url", "")
    is_lowest_90d = drop_info.get("is_lowest_90d", False)
    is_lowest_ever = drop_info.get("is_lowest_ever", False)

    badge = ""
    if is_lowest_ever:
        badge = "ALL-TIME LOWEST PRICE."
    elif is_lowest_90d:
        badge = "Lowest price in 90 days."

    # Check if LinkedIn is needed
    from src.platform_router import match_platforms
    deal = drop_info.get("deal", {})
    needs_linkedin = "linkedin" in match_platforms(deal) if deal else False

    # No LLM available - use templates
    if not LLM_API_KEY and LLM_PROVIDER != "ollama":
        return {
            "tweet_1": _build_price_drop_tweet_1(drop_info, badge),
            "tweet_2": _build_price_drop_tweet_2(drop_info),
            "linkedin_post": _build_price_drop_linkedin(drop_info, badge) if needs_linkedin else "",
        }

    print(f"    Generating price drop content for: {title[:50]}...")

    linkedin_section = ""
    if needs_linkedin:
        linkedin_section = f"""

LINKEDIN:
Write a LinkedIn post (800-1200 chars) about a price drop on a tech product.
Product: {title}
Was ${original_posted:.2f}, now ${new_price:.2f} ({drop_pct:.0f}% off, save ${savings:.0f}).
{"Badge: " + badge if badge else ""}
Include key specs relevant to work/productivity.
Include affiliate link: {url}
End with 2-3 hashtags (#PriceDrop #TechDeals + category).
Tone: data-driven, helpful. Like a colleague sharing a price alert. No urgency hype."""

    from src.tweet_learner import get_style_guidance
    style_hint = get_style_guidance()
    style_section = f"\n\nStyle guidance from past performance:\n{style_hint}" if style_hint else ""

    try:
        prompt = f"""Write social media posts about a PRICE DROP on a tech product.

Product: {title}
Was: ${original_posted:.2f}
Now: ${new_price:.2f}
You save: ${savings:.0f} ({drop_pct:.0f}% off)
{"Badge: " + badge if badge else "No badge."}

TWEET1:
Write a tweet (under 280 chars) with this structure:
- Open with "PRICE DROP ALERT" or similar alert-style header
- Product name (shortened if needed)
- Was/Now prices with dollar savings and % off
- {"Include badge: " + badge if badge else ""}
- End with: Link below
- End with: #ad #PriceDrop
- NO links in tweet 1

TWEET2:
Write a short creative CTA (1-2 sentences max) that makes people want to click.
DO NOT include any prices, dollar amounts, or specific savings. Amazon prices change dynamically and may differ by the time someone clicks.
Just drive curiosity and urgency without mentioning numbers.
Then add the affiliate URL on its own line: {url}

Rules:
- Keep both tweets under 280 chars each
- Tweet 1: NO links, include specific prices and savings
- Tweet 2: NO prices or dollar amounts at all, just a creative hook + the URL
- NEVER use: pivotal, crucial, vibrant, delve, tapestry, foster, showcase, underscore, testament, landscape, groundbreaking, renowned, game-changer, revolutionize
- Use plain language. Short punchy sentences. No filler.
{linkedin_section}

Respond as TWEET1: then TWEET2:{"then LINKEDIN:" if needs_linkedin else ""} with the text.{style_section}"""

        text = llm_generate(prompt, max_tokens=1200 if needs_linkedin else 600)
        if not text:
            return {
                "tweet_1": _build_price_drop_tweet_1(drop_info, badge),
                "tweet_2": _build_price_drop_tweet_2(drop_info),
                "linkedin_post": _build_price_drop_linkedin(drop_info, badge) if needs_linkedin else "",
            }

        result = {"tweet_1": "", "tweet_2": "", "linkedin_post": ""}

        # Parse LinkedIn
        linkedin_post = ""
        if needs_linkedin and "LINKEDIN:" in text:
            li_parts = text.split("LINKEDIN:")
            linkedin_post = _clean_ai_words(li_parts[-1].strip())
            if url and url not in linkedin_post:
                linkedin_post = linkedin_post.rstrip() + f"\n\n{url}"
            text = li_parts[0]

        # Parse tweets
        if "TWEET1:" in text and "TWEET2:" in text:
            parts = text.split("TWEET2:")
            tweet_1 = _clean_ai_words(parts[0].replace("TWEET1:", "").strip())
            tweet_2 = _clean_ai_words(parts[1].strip())

            if "http" in tweet_1.lower():
                tweet_1 = _build_price_drop_tweet_1(drop_info, badge)
            if url and url not in tweet_2:
                tweet_2 = tweet_2.rstrip() + f"\n\n{url}"
            if "@quadstardeals" not in tweet_2:
                tweet_2 = tweet_2.rstrip() + "\n\nFollow @quadstardeals for daily tech deals."
            result["tweet_1"] = _truncate_tweet(tweet_1)
            result["tweet_2"] = _truncate_tweet(tweet_2)
        else:
            result["tweet_1"] = _build_price_drop_tweet_1(drop_info, badge)
            result["tweet_2"] = _build_price_drop_tweet_2(drop_info)

        result["linkedin_post"] = linkedin_post
        return result

    except Exception as e:
        print(f"    LLM error for price drop content: {e}")

    return {
        "tweet_1": _build_price_drop_tweet_1(drop_info, badge),
        "tweet_2": _build_price_drop_tweet_2(drop_info),
        "linkedin_post": _build_price_drop_linkedin(drop_info, badge) if needs_linkedin else "",
    }


def _build_price_drop_tweet_1(drop_info: dict, badge: str) -> str:
    """Fallback template for price drop tweet 1 (only used if LLM fails)."""
    title = drop_info.get("title", "")[:60]
    new_price = drop_info.get("new_price", 0)
    original = drop_info.get("original_posted_price", drop_info.get("old_price", 0))
    drop_pct = drop_info.get("drop_pct", 0)
    savings = original - new_price

    badge_line = f"\n{badge}" if badge else ""
    tweet = (
        f"PRICE DROP ALERT\n\n"
        f"{title}\n"
        f"Was ${original:.2f} > Now ${new_price:.2f}\n"
        f"You save ${savings:.0f} ({drop_pct:.0f}% off)"
        f"{badge_line}\n\n"
        f"Link below\n#ad #PriceDrop"
    )
    return _truncate_tweet(tweet)


def _build_price_drop_tweet_2(drop_info: dict) -> str:
    """Fallback template for price drop tweet 2 (only used if LLM fails)."""
    url = drop_info.get("affiliate_url", "")
    return f"Check the link before it's gone.\n\n{url}"


def _build_price_drop_linkedin(drop_info: dict, badge: str) -> str:
    """Fallback template for price drop LinkedIn post."""
    title = drop_info.get("title", "")[:80]
    new_price = drop_info.get("new_price", 0)
    original = drop_info.get("original_posted_price", drop_info.get("old_price", 0))
    drop_pct = drop_info.get("drop_pct", 0)
    url = drop_info.get("affiliate_url", "")

    features = _extract_features(drop_info.get("title", ""))
    specs = "\n".join(f"- {f}" for f in features) if features else "- Check the listing for full specs"

    badge_line = f"\n{badge}\n" if badge else ""

    return (
        f"Price update on a product I posted about earlier.\n\n"
        f"{title}\n"
        f"Was ${original:.2f} when I shared it. Now ${new_price:.2f} ({drop_pct:.0f}% lower).\n"
        f"{badge_line}\n"
        f"Key specs:\n{specs}\n\n"
        f"{url}\n\n"
        f"#PriceDrop #TechDeals"
    )


def _build_tweet_1_template(deal: dict) -> str:
    """Fallback template for Tweet 1 — Hormozi hook + bullet features from title."""
    import random
    title = deal["title"]
    # Shorten title: take first ~40 chars up to last complete word
    short = title[:40].rsplit(" ", 1)[0] if len(title) > 40 else title

    # Dynamic hooks — mix of CAPS and normal case for variety
    hooks = [
        "STOP SCROLLING. THIS DEAL WON'T LAST.",
        "This has no business being this cheap.",
        "I FOUND THE DEAL EVERYONE'S SLEEPING ON.",
        "Most people miss deals like this.",
        "THIS IS THE ONE YOU'VE BEEN WAITING FOR.",
        "Don't sleep on this one.",
    ]

    temptations = [
        "am I tempted? absolutely.",
        "might have to grab this one.",
        "this one's dangerous 👀",
        "yeah, I'm not passing on this.",
        "adding to cart as we speak.",
    ]

    # Dynamic hashtag
    title_lower = title.lower()
    hashtag_map = {
        "laptop": "#LaptopDeal", "macbook": "#AppleDeal", "ipad": "#AppleDeal",
        "iphone": "#AppleDeal", "airpod": "#AppleDeal", "apple": "#AppleDeal",
        "headphone": "#AudioDeals", "earbuds": "#AudioDeals", "speaker": "#AudioDeals",
        "monitor": "#MonitorDeal", "display": "#MonitorDeal",
        "keyboard": "#GamingSetup", "mouse": "#GamingSetup", "gaming": "#PCGaming",
        "tablet": "#TabletDeal", "watch": "#WearableTech", "smart": "#SmartHome",
        "camera": "#CameraDeal", "tv": "#TVDeal", "television": "#TVDeal",
        "router": "#SmartHome", "ssd": "#PCGaming", "gpu": "#PCGaming",
    }
    tag = "#TechDeal"
    for kw, t in hashtag_map.items():
        if kw in title_lower:
            tag = t
            break

    # Extract real features from product title instead of generic ones
    features = _extract_features(title)
    # Pad with category-specific fallbacks if we didn't find 3
    category_features = {
        "tv": ["Smart TV platform", "Slim bezel design", "Multiple HDMI ports"],
        "laptop": ["Lightweight design", "Fast SSD storage", "Long battery life"],
        "headphone": ["Premium sound quality", "Comfortable fit", "Built-in mic"],
        "earbuds": ["Compact charging case", "Touch controls", "Clear call quality"],
        "speaker": ["360° sound", "Portable design", "Long battery life"],
        "monitor": ["Adjustable stand", "Eye care technology", "Slim bezels"],
        "keyboard": ["Anti-ghosting keys", "Ergonomic design", "Plug and play"],
        "mouse": ["Precision sensor", "Ergonomic grip", "Long battery life"],
        "watch": ["Health tracking", "Water resistant", "Multi-day battery"],
        "tablet": ["Portable design", "Touch screen", "Long battery life"],
        "camera": ["Auto focus", "Image stabilization", "Compact body"],
        "router": ["Wide coverage", "Dual band", "Easy setup"],
    }
    fallback_feats = ["Top rated on Amazon", "Highly reviewed", "Ships fast"]
    for kw, feats in category_features.items():
        if kw in title_lower:
            fallback_feats = feats
            break
    while len(features) < 3:
        features.append(fallback_feats[len(features)] if len(features) < len(fallback_feats) else "Highly rated")

    link_callouts = [
        "Grab the link below 👇",
        "Link in the reply 🔗",
        "Check the comment for the link ⬇️",
        "Deal link right below 👀",
        "Link is in the reply 🔗",
    ]

    tweet = (
        f"{random.choice(hooks)}\n\n"
        f"{short}\n\n"
        f"✅ {features[0]}\n"
        f"✅ {features[1]}\n"
        f"✅ {features[2]}\n\n"
        f"{random.choice(temptations)}\n\n"
        f"{random.choice(link_callouts)}\n"
        f"#ad {tag}"
    )

    return _truncate_tweet(tweet)


def _build_tweet_2_template(deal: dict) -> str:
    """Fallback template for Tweet 2 — minimal with link."""
    import random
    url = deal.get("affiliate_url") or deal["source_url"]

    intros = [
        "Grab it before it's gone.",
        "This won't last long.",
        "Don't sleep on this one.",
        "Limited stock, act fast.",
        "Get it while it's still available.",
        "Here before the price goes back up.",
        "If you've been waiting, this is it.",
    ]

    lines = [random.choice(intros), "", url]
    if deal.get("coupon_code"):
        lines.append(f"\nCode: {deal['coupon_code']}")
    lines.append("")
    lines.append("Follow @quadstardeals for daily tech deals.")
    return "\n".join(lines)


def _send_webhook(content: str = None, embed: dict = None, suppress_embeds: bool = False) -> str | None:
    """Send a message to Discord webhook. Returns message ID or None."""
    payload = {"username": f"{BRAND_NAME} Deals"}

    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]
    if suppress_embeds:
        payload["flags"] = 4  # SUPPRESS_EMBEDS — no link previews

    try:
        url = f"{DISCORD_WEBHOOK_URL}?wait=true"
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return response.json().get("id")
    except Exception as e:
        print(f"    Error sending to Discord: {e}")
        return None


def send_deal_to_discord(deal: dict) -> bool:
    """Send a deal to Discord as separate messages for easy mobile copy.

    Instructions and tweet content are in SEPARATE messages so Discord
    mobile 'Copy Text' only grabs the tweet text, nothing else.
    """
    tweet_1, tweet_2 = _generate_tweets(deal)

    # Message 1: Instruction for Tweet 1
    _send_webhook(content="**Tweet 1** — copy the message below and post with the image:", suppress_embeds=True)
    time.sleep(0.3)

    # Message 2: Tweet 1 content ONLY — nothing else in this message
    msg1_id = _send_webhook(content=tweet_1, suppress_embeds=True)
    if not msg1_id:
        return False
    _add_reactions(msg1_id)
    time.sleep(0.5)

    # Message 3: Product image
    if deal.get("image_url") and not deal["image_url"].startswith("data:"):
        image_embed = {
            "title": "📸 Save this image → attach to Tweet 1",
            "image": {"url": deal["image_url"]},
            "color": 0xFFD700,
        }
        _send_webhook(embed=image_embed)
        time.sleep(0.5)

    # Message 4: Instruction for Tweet 2
    _send_webhook(content="**Tweet 2** — copy the message below and reply to Tweet 1:", suppress_embeds=True)
    time.sleep(0.3)

    # Message 5: Tweet 2 content ONLY — nothing else in this message
    _send_webhook(content=tweet_2, suppress_embeds=True)
    time.sleep(0.5)

    # Message 6: Clickable link + separator
    deal_url = deal.get("affiliate_url") or deal["source_url"]
    msg6 = f"🔗 **Clickable link:** {deal_url}\n👍 = more like this | 👎 = skip these\n▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
    _send_webhook(content=msg6, suppress_embeds=True)

    deal["discord_message_id"] = msg1_id
    return True


def generate_deal_content(deal: dict) -> dict:
    """Public wrapper for content generation. Returns {tweet_1, tweet_2, linkedin_post}.
    Used by the agent and Discord bot to preview content before scheduling.
    """
    return _generate_content(deal)


def generate_ab_variants(deal: dict) -> tuple[dict, dict]:
    """Generate two structurally different tweet variants for A/B testing.

    Variant A uses the deal's natural format (deal_id % 4).
    Variant B uses an offset of +2 to guarantee a different structural style
    (e.g. contrast_flip vs number_hook, question_inline vs prose_no_bullets).

    Both variants share the same system prompt (caching benefit) and signals block.
    Returns (variant_a, variant_b) where each is {tweet_1, tweet_2}.
    """
    if not LLM_API_KEY and LLM_PROVIDER != "ollama":
        content = generate_deal_content(deal)
        return content, content

    url = deal.get("affiliate_url") or deal["source_url"]
    coupon_info = f"Coupon code: {deal['coupon_code']}" if deal.get("coupon_code") else ""

    signals_block = _build_signals(deal)

    # Pick two formats: natural slot and +2 offset for structural contrast
    deal_id = deal.get("id", 0) or hash(deal.get("asin", "") + deal.get("title", ""))
    keys = list(_TWEET_FORMATS.keys())
    fmt_a = keys[deal_id % len(keys)]
    fmt_b = keys[(deal_id + 2) % len(keys)]

    system_prompt = _build_system_prompt()

    def _build_user_prompt(fmt_name: str) -> str:
        parts = [f"Product: {deal['title']}"]
        if coupon_info:
            parts.append(coupon_info)
        if signals_block:
            parts.append(signals_block)
        parts.append(f"Use TWEET1 FORMAT: {fmt_name}")
        parts.append(f"Affiliate URL for Tweet 2: {url}")
        return "\n\n".join(parts)

    def _parse_variant(text: str) -> dict | None:
        if not text or "TWEET1:" not in text or "TWEET2:" not in text:
            return None
        parts = text.split("TWEET2:")
        t1 = _clean_ai_words(parts[0].replace("TWEET1:", "").strip())
        t2 = _clean_ai_words(parts[1].strip())
        if "http" in t1.lower():
            return None
        if url not in t2:
            lines = t2.split("\n")
            lines.insert(1, "")
            lines.insert(2, url)
            t2 = "\n".join(lines)
        if "@quadstardeals" not in t2:
            t2 = t2.rstrip() + "\n\nFollow @quadstardeals for daily tech deals."
        return {"tweet_1": _truncate_tweet(t1), "tweet_2": _truncate_tweet(t2)}

    fallback = generate_deal_content(deal)

    try:
        text_a = llm_generate(_build_user_prompt(fmt_a), max_tokens=600, system=system_prompt)
        variant_a = _parse_variant(text_a) or fallback
    except Exception:
        variant_a = fallback

    try:
        text_b = llm_generate(_build_user_prompt(fmt_b), max_tokens=600, system=system_prompt)
        variant_b = _parse_variant(text_b) or fallback
    except Exception:
        variant_b = fallback

    return variant_a, variant_b


def notify_deals() -> int:
    """Send top unposted deals to Discord."""
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set. Skipping notifications.")
        return 0

    deals = get_top_unposted_deals(limit=MAX_POSTS_PER_RUN, min_discount=MIN_DISCOUNT_PCT)

    if not deals:
        print("No new deals to send.")
        return 0

    sent = 0
    for deal in deals:
        if not (deal.get("discount_pct") or 0) >= 1:
            print(f"  Skipping (no discount): {deal['title'][:60]}")
            continue
        if send_deal_to_discord(deal):
            mark_as_posted(deal["id"])
            if deal.get("discord_message_id"):
                update_deal(deal["id"], {"discord_message_id": deal["discord_message_id"]})
            sent += 1
            print(f"  Sent to Discord: {deal['title'][:60]}...")
            time.sleep(1)

    print(f"Sent {sent} deals to Discord.")
    return sent
