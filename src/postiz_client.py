from __future__ import annotations

"""
postiz_client.py - Postiz Social Media Scheduler

Preserves the two-post format:
  Post 1: tweet_1 → ALL configured platforms (no link, native image)
  Post 2: tweet_2 → X/Twitter only (affiliate link, posted 2 min later as reply)

Postiz integration IDs come from env vars (POSTIZ_TWITTER_ID, etc.).
Auth:
  Public API: raw API key in Authorization header (posts, scheduling)
  Internal API: JWT in 'auth' header (media upload)
"""

import threading
import requests
from config.settings import (
    POSTIZ_API_URL, POSTIZ_API_KEY, PLATFORM_IDS,
    POSTIZ_JWT_SECRET, POSTIZ_USER_ID, POSTIZ_USER_EMAIL,
)


def _headers() -> dict:
    return {
        "Authorization": POSTIZ_API_KEY,
        "Content-Type": "application/json",
    }


def _api_url(path: str) -> str:
    """Build Postiz public API URL."""
    return f"{POSTIZ_API_URL}/public/v1{path}"


def _get_session_jwt() -> str:
    """Generate a JWT for Postiz internal API (media uploads)."""
    if not POSTIZ_JWT_SECRET or not POSTIZ_USER_ID:
        return ""
    try:
        import jwt
        return jwt.encode(
            {"id": POSTIZ_USER_ID, "email": POSTIZ_USER_EMAIL, "activated": True},
            POSTIZ_JWT_SECRET,
            algorithm="HS256",
        )
    except Exception as e:
        print(f"  [postiz] JWT generation failed: {e}")
        return ""


def upload_image(image_url: str) -> dict | None:
    """Upload an image to Postiz via internal API.

    Downloads the image from the URL and uploads it to Postiz's
    /api/media/upload-simple endpoint using JWT session auth.

    Returns {"id": "...", "path": "..."} on success, None on failure.
    """
    token = _get_session_jwt()
    if not token:
        print("  [postiz] Skipping image upload (POSTIZ_JWT_SECRET or POSTIZ_USER_ID not set)")
        return None

    try:
        # Download image
        img_resp = requests.get(image_url, timeout=15)
        img_resp.raise_for_status()

        # Determine filename from URL
        filename = image_url.split("/")[-1].split("?")[0] or "product.jpg"
        if not filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            filename = "product.jpg"

        # Upload to Postiz
        resp = requests.post(
            f"{POSTIZ_API_URL}/media/upload-simple",
            headers={"auth": token},
            files={"file": (filename, img_resp.content, "image/jpeg")},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"  [postiz] Image uploaded: {data.get('id', '')[:12]}...")

        # Postiz returns the external URL (port 4007) but internally the backend
        # serves files on port 5000. readOrFetch() fetches this URL from inside
        # the container, so we must use the internal port.
        path = data["path"]
        internal_path = path.replace("localhost:4007", "localhost:5000")

        return {"id": data["id"], "path": internal_path}
    except Exception as e:
        print(f"  [postiz] Image upload failed: {e}")
        return None


# ── Post format rotation (randomized) ────────────────────────────────────────
# ~25% of posts use single-tweet format (breaks detectable two-post thread).
# Randomized so no fixed pattern X can detect.
import random as _random


def schedule_post(deal: dict, content: dict, platforms: list | str, scheduled_at: str = None) -> dict:
    """
    Schedule a deal to social platforms via Postiz.

    Rotates posting format every 4th post to avoid detectable bot patterns:
    - Posts 0-2: normal two-post thread (tweet_1 hook + tweet_2 reply with link)
    - Post 3: single tweet with link embedded (no reply thread)

    Args:
        deal: Deal dict with title, image_url, affiliate_url, etc.
        content: {tweet_1, tweet_2, linkedin_post} from generate_deal_content()
        platforms: platform name string OR list of names for backward compat
        scheduled_at: ISO datetime string; if None, uses next optimal posting time

    Returns:
        Postiz API response dict with at least {"status": "ok"} on success.
    """
    global _POST_FORMAT_COUNTER

    if not POSTIZ_API_KEY:
        return {"status": "skipped", "reason": "POSTIZ_API_KEY not set"}

    # Normalize platforms to a list
    if isinstance(platforms, str):
        platforms = [platforms]

    post_time = scheduled_at or get_smart_time()[0]

    # Map platform names to Postiz integration IDs
    integrations = []
    for platform in platforms:
        pid = PLATFORM_IDS.get(platform.lower())
        if pid:
            integrations.append({"id": pid, "platform": platform.lower()})
        else:
            print(f"  [postiz] No integration ID for '{platform}' — skipping (set POSTIZ_{platform.upper()}_ID)")

    if not integrations:
        return {"status": "error", "reason": "No valid platform integration IDs configured"}

    # Upload product image to Postiz if available
    tweet1_image = []
    if deal.get("image_url") and not deal["image_url"].startswith("data:"):
        media = upload_image(deal["image_url"])
        if media:
            tweet1_image = [media]

    # Build posts array with platform-specific content
    twitter_platforms = ("twitter", "x")
    posts = []

    for intg in integrations:
        if intg["platform"] in twitter_platforms:
            # Randomized format: ~25% single tweet, ~75% two-post thread
            # Uses random (not counter) so X can't detect a fixed cycle
            use_single_tweet = (_random.random() < 0.25)

            if use_single_tweet:
                # Single tweet with link embedded (no reply thread)
                url = deal.get("affiliate_url") or deal.get("source_url", "")
                single_content = f"{content['tweet_1']}\n\n{url}"
                posts.append({
                    "integration": {"id": intg["id"]},
                    "value": [{"content": single_content, "image": tweet1_image}],
                    "settings": {"who_can_reply_post": "everyone"},
                })
                print(f"  [postiz] Format: SINGLE TWEET (random 25%)")
            else:
                # Normal two-post thread: hook (no link) + reply with link
                posts.append({
                    "integration": {"id": intg["id"]},
                    "value": [
                        {"content": content["tweet_1"], "image": tweet1_image},
                        {"content": content["tweet_2"], "image": []},
                    ],
                    "settings": {"who_can_reply_post": "everyone"},
                })
        elif intg["platform"] == "linkedin":
            # LinkedIn: single post with linkedin-specific content + image
            li_content = content.get("linkedin_post") or content["tweet_1"]
            posts.append({
                "integration": {"id": intg["id"]},
                "value": [{"content": li_content, "image": tweet1_image}],
                "settings": {},
            })
        else:
            # Other platforms: tweet_1 with image
            posts.append({
                "integration": {"id": intg["id"]},
                "value": [{"content": content["tweet_1"], "image": tweet1_image}],
                "settings": {},
            })

    payload = {
        "type": "schedule",
        "date": post_time,
        "shortLink": False,
        "tags": [],
        "posts": posts,
    }

    platform_label = ", ".join(p.lower() for p in platforms)
    try:
        resp = requests.post(
            _api_url("/posts"),
            json=payload,
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        result = {"postiz_response": resp.json()}
        print(f"  [postiz] Scheduled to {platform_label} for {post_time}")
    except Exception as e:
        print(f"  [postiz] Scheduling to {platform_label} failed: {e}")
        return {"status": "error", "reason": str(e)}

    result["status"] = "ok"
    return result


def get_scheduled_posts() -> list[dict]:
    """Fetch all future scheduled posts from Postiz. Returns full post objects."""
    from datetime import datetime, timezone, timedelta
    try:
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = (now + timedelta(days=2)).strftime("%Y-%m-%dT23:59:59.000Z")
        resp = requests.get(
            _api_url(f"/posts?startDate={start}&endDate={end}"),
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            posts = resp.json()
            if isinstance(posts, list):
                # Only return future posts
                return [p for p in posts if p.get("publishDate") or p.get("date")]
        return []
    except Exception as e:
        print(f"  [postiz] Failed to fetch scheduled posts: {e}")
        return []


def delete_post(post_id: str) -> bool:
    """Delete a scheduled post from Postiz."""
    try:
        resp = requests.delete(
            _api_url(f"/posts/{post_id}"),
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code in (200, 204):
            print(f"  [postiz] Deleted post {post_id}")
            return True
        print(f"  [postiz] Delete failed for {post_id}: {resp.status_code}")
        return False
    except Exception as e:
        print(f"  [postiz] Delete error for {post_id}: {e}")
        return False


def bump_schedule(deal: dict, content: dict, platforms: list[str]) -> dict:
    """Schedule a price drop post ASAP, bumping any conflicting post.

    Algorithm:
    1. Get the ASAP slot
    2. Check for conflicts within +/- 10 min window
    3. If conflict: reschedule conflicting post to next available slot
    4. Schedule price drop post at ASAP slot
    """
    from datetime import datetime, timezone, timedelta

    asap_time, asap_label = get_post_now_time()
    asap_dt = datetime.fromisoformat(asap_time.replace(".000Z", "+00:00"))

    # Check for conflicts
    scheduled = get_scheduled_posts()
    conflict = None
    for post in scheduled:
        post_date_str = post.get("publishDate") or post.get("date", "")
        if not post_date_str:
            continue
        try:
            post_dt = datetime.fromisoformat(post_date_str.replace("Z", "+00:00").replace(".000+", "+"))
            if abs((post_dt - asap_dt).total_seconds()) < 600:  # Within 10 min
                conflict = post
                break
        except (ValueError, TypeError):
            continue

    # Bump the conflicting post if found
    if conflict:
        conflict_id = conflict.get("id") or conflict.get("postId", "")
        conflict_date = conflict.get("publishDate") or conflict.get("date", "")
        print(f"  [postiz] Conflict found: post {conflict_id} at {conflict_date[:16]}")

        # Find next available slot after ASAP + 15 min
        next_time, next_label = get_smart_time()

        # Delete the conflicting post and recreate at the new time
        if conflict_id and delete_post(conflict_id):
            # Recreate with original content at new time
            try:
                original_posts = conflict.get("posts", [])
                if original_posts:
                    payload = {
                        "type": "schedule",
                        "date": next_time,
                        "shortLink": False,
                        "tags": [],
                        "posts": original_posts,
                    }
                    resp = requests.post(
                        _api_url("/posts"),
                        json=payload,
                        headers=_headers(),
                        timeout=15,
                    )
                    resp.raise_for_status()
                    print(f"  [postiz] Bumped conflicting post to {next_label}")
            except Exception as e:
                print(f"  [postiz] Failed to reschedule bumped post: {e}")

    # Schedule the price drop post at the ASAP slot
    result = schedule_post(deal, content, platforms, scheduled_at=asap_time)
    if result.get("status") == "ok":
        print(f"  [postiz] Price drop post scheduled at {asap_label} (bumped priority)")
    return result


def extract_postiz_id(result: dict) -> str:
    """Extract the Postiz post ID from a schedule_post result.
    Postiz returns [{"postId": "...", "integration": "..."}] in the response.
    """
    resp = result.get("postiz_response", [])
    if isinstance(resp, list) and resp:
        return resp[0].get("postId", "")
    if isinstance(resp, dict):
        return resp.get("postId", resp.get("id", ""))
    return ""


# Track times proposed in current batch to avoid duplicates within same send.
# Protected by _batch_lock because get_smart_time() / get_post_now_time() /
# reset_batch_times() can be called concurrently from the APScheduler thread,
# the Discord bot thread, and the FastAPI executor pool.
_batch_proposed: set = set()
_batch_lock = threading.Lock()


def _pst_utc_offset_hours(utc_dt=None) -> int:
    """Return the UTC offset in hours for America/Los_Angeles (PST/PDT).

    Uses zoneinfo so DST transitions are handled correctly. Falls back to
    -8 (PST, standard time) if zoneinfo is unavailable for some reason.
    Returned offset is the value added to UTC hours to get local hours
    (so -7 in daylight saving, -8 standard).
    """
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        ref = utc_dt or datetime.now(timezone.utc)
        la = ref.astimezone(ZoneInfo("America/Los_Angeles"))
        off = la.utcoffset()
        return int(off.total_seconds() // 3600) if off else -8
    except Exception:
        return -8


def _get_scheduled_times() -> set:
    """Fetch already-scheduled post times from Postiz to avoid conflicts."""
    from datetime import datetime, timezone, timedelta
    try:
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%dT00:00:00.000Z")
        end = (now + timedelta(days=2)).strftime("%Y-%m-%dT23:59:59.000Z")
        resp = requests.get(
            _api_url(f"/posts?startDate={start}&endDate={end}"),
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            posts = resp.json()
            if isinstance(posts, list):
                return {p.get("publishDate", p.get("date", ""))[:16] for p in posts if p.get("publishDate") or p.get("date")}
        return set()
    except Exception:
        return set()


def get_smart_time() -> tuple[str, str]:
    """Return (iso_string, human_label) for the next smart posting time.

    Peak hours: 5am, 8am, 12pm, 5pm PST.
    Active hours: 4:30am - 7pm PST.
    Avoids times that already have a scheduled post in Postiz.
    Always same-day when possible. Minimum 30 min from now.

    Shuffles candidate order + adds random minute offset so posts don't
    land at the exact same time every day — prevents X pattern detection.
    """
    import random
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    pst_offset = _pst_utc_offset_hours(now)  # DST-aware
    pst_hour = (now.hour + pst_offset) % 24

    # Snapshot booked slots under the lock so the read-modify-write below is atomic.
    with _batch_lock:
        booked = _get_scheduled_times() | set(_batch_proposed)

        # Candidate times: peaks + every 2h during active hours
        candidates_pst = [5, 7, 8, 10, 12, 14, 16, 17, 18]
        # Shuffle so different slot gets picked first each day
        random.shuffle(candidates_pst)

        for candidate in candidates_pst:
            # Also randomize minutes (0-40) so post doesn't land at :00
            random_minutes = random.randint(0, 40)
            if pst_hour < candidate or (pst_hour == candidate and now.minute < 30):
                utc_hour = (candidate - pst_offset) % 24
                proposed = now.replace(hour=utc_hour, minute=random_minutes, second=0, microsecond=0)
                if proposed < now:
                    proposed += timedelta(days=1)
                # Ensure at least 30 min from now
                if (proposed - now).total_seconds() < 1800:
                    continue
                # Skip if this time slot is already booked
                proposed_key = proposed.strftime("%Y-%m-%dT%H:%M")
                if proposed_key in booked:
                    continue
                label = _format_pst_label(proposed, pst_offset)
                iso = proposed.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                _batch_proposed.add(proposed_key)
                return iso, label

        # Past all candidates today. If before quiet hours (7pm PST), stagger by 30 min
        if pst_hour < 19:
            soon = now + timedelta(minutes=30)
            # Stagger if this slot is taken
            while soon.strftime("%Y-%m-%dT%H:%M")[:16] in booked:
                soon += timedelta(minutes=30)
            label = _format_pst_label(soon, pst_offset)
            iso = soon.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            _batch_proposed.add(soon.strftime("%Y-%m-%dT%H:%M"))
            return iso, label

        # In quiet hours (7pm-4:30am PST). Start at tomorrow 5am PST, stagger 2h if taken
        utc_hour = (5 - pst_offset) % 24
        tomorrow = now + timedelta(days=1)
        next_slot = tomorrow.replace(hour=utc_hour, minute=0, second=0, microsecond=0)
        while next_slot.strftime("%Y-%m-%dT%H:%M") in booked:
            next_slot += timedelta(hours=2)
        label = _format_pst_label(next_slot, pst_offset)
        iso = next_slot.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _batch_proposed.add(next_slot.strftime("%Y-%m-%dT%H:%M"))
        return iso, label


def resolve_schedule_time(deal_id: int) -> tuple[str, str]:
    """Pick the post time for a deal.

    Prefers Hermes's chosen time (deal['schedule_at'], ISO) when it's valid
    (parseable + at least 30 min out), nudged 15 min at a time off any slot
    already booked in Postiz. Falls back to the deterministic get_smart_time()
    if Hermes set nothing or set something unusable. So even when Hermes picks,
    the time is guaranteed valid and conflict-free; and if Hermes flakes, a smart
    time still gets chosen — the card is never timeless.
    """
    from datetime import datetime, timezone, timedelta
    try:
        from src.database import get_deal_by_id
        deal = get_deal_by_id(deal_id) or {}
    except Exception:
        deal = {}
    raw = (deal.get("schedule_at") or "").strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            if (dt - now).total_seconds() >= 1800:
                pst_offset = _pst_utc_offset_hours(now)
                with _batch_lock:
                    booked = _get_scheduled_times() | set(_batch_proposed)
                    for _ in range(8):  # nudge off collisions, max ~2h
                        if dt.strftime("%Y-%m-%dT%H:%M") not in booked:
                            break
                        dt += timedelta(minutes=15)
                    label = _format_pst_label(dt, pst_offset)
                    iso = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                    _batch_proposed.add(dt.strftime("%Y-%m-%dT%H:%M"))
                    print(f"  [schedule] using Hermes-picked time {label} for deal {deal_id}")
                    return iso, label
            else:
                print(f"  [schedule] Hermes time for deal {deal_id} too soon — smart fallback")
        except Exception as exc:
            print(f"  [schedule] Hermes schedule_at '{raw}' invalid ({exc}) — smart fallback")
    return get_smart_time()


def get_post_now_time() -> tuple[str, str]:
    """Return (iso_string, label) for posting ASAP. Stagger by 5 min if slot is taken."""
    from datetime import datetime, timezone, timedelta
    with _batch_lock:
        booked = _get_scheduled_times() | set(_batch_proposed)
        soon = datetime.now(timezone.utc) + timedelta(minutes=5)
        # Round to nearest 5-min boundary, then stagger until free
        minutes = (soon.minute // 5) * 5
        soon = soon.replace(minute=minutes, second=0, microsecond=0)
        while soon.strftime("%Y-%m-%dT%H:%M") in booked:
            soon += timedelta(minutes=5)
        _batch_proposed.add(soon.strftime("%Y-%m-%dT%H:%M"))
    minutes_away = int((soon - datetime.now(timezone.utc)).total_seconds() / 60)
    label = f"In ~{minutes_away} minutes"
    return soon.strftime("%Y-%m-%dT%H:%M:%S.000Z"), label


def get_time_options() -> list[tuple[str, str]]:
    """Return list of (iso_string, label) time options for Pick Time menu.
    Only includes times that don't conflict with existing Postiz posts."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    pst_offset = _pst_utc_offset_hours(now)  # DST-aware
    booked = _get_scheduled_times()
    candidates = []

    # Relative options (always show these)
    for mins, label in [(30, "In 30 minutes"), (60, "In 1 hour"), (90, "In 1.5 hours"),
                        (120, "In 2 hours"), (180, "In 3 hours"), (240, "In 4 hours")]:
        candidates.append((now + timedelta(minutes=mins), label))

    # Today's remaining peak hours (PST)
    pst_hour = (now.hour + pst_offset) % 24
    for peak in [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]:
        if pst_hour < peak:
            peak_utc_hour = (peak - pst_offset) % 24
            t = now.replace(hour=peak_utc_hour, minute=0, second=0, microsecond=0)
            if t < now:
                t += timedelta(days=1)
            if (t - now).total_seconds() > 1800:  # at least 30 min away
                candidates.append((t, _format_pst_label(t, pst_offset)))

    # Tomorrow — full set of peak hours
    tomorrow = now + timedelta(days=1)
    for h in [5, 7, 8, 9, 10, 12, 14, 16, 17, 18]:
        utc_h = (h - pst_offset) % 24
        t = tomorrow.replace(hour=utc_h, minute=0, second=0, microsecond=0)
        candidates.append((t, _format_pst_label(t, pst_offset)))

    # Deduplicate by rounded time key, filter booked, keep order
    seen = set()
    options = []
    for t, label in candidates:
        key = t.strftime("%Y-%m-%dT%H:%M")
        if key in booked or key in seen:
            continue
        seen.add(key)
        options.append((t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), label))

    return options[:25]  # Discord select menu max is 25


def _format_pst_label(utc_dt, pst_offset: int | None = None) -> str:
    """Format a UTC datetime as a Pacific-time human-readable label.

    pst_offset is in hours (e.g. -7 during PDT, -8 during PST). If None,
    it's computed from utc_dt so the label reflects the correct offset
    across DST transitions.
    """
    from datetime import datetime, timezone, timedelta
    if pst_offset is None:
        pst_offset = _pst_utc_offset_hours(utc_dt)
    now = datetime.now(timezone.utc)
    pst_hour = (utc_dt.hour + pst_offset) % 24
    pst_min = utc_dt.minute
    am_pm = "AM" if pst_hour < 12 else "PM"
    display_hour = pst_hour % 12 or 12

    # Use the Pacific-local date for day labels so a 7pm PDT slot on
    # day N is not shown as "Tomorrow" just because its UTC hour
    # crossed midnight.
    local_dt = utc_dt + timedelta(hours=pst_offset)
    local_now = now + timedelta(hours=_pst_utc_offset_hours(now))

    if local_dt.date() == local_now.date():
        day = "Today"
    elif local_dt.date() == (local_now + timedelta(days=1)).date():
        day = "Tomorrow"
    else:
        day = local_dt.strftime("%b %d")

    tz_label = "PDT" if pst_offset == -7 else "PST"

    if pst_min == 0:
        return f"{day} {display_hour}:00 {am_pm} {tz_label}"
    return f"{day} {display_hour}:{pst_min:02d} {am_pm} {tz_label}"
