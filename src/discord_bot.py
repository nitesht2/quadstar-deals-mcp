from __future__ import annotations

"""
discord_bot.py - Discord Deal Cards Bot

Sends interactive deal cards with smart scheduling buttons to a Discord channel.
Multi-message format: Tweet 1 → Image → Tweet 2 → Proposed time + Action buttons.

Buttons:
  🚀 Post Now      → schedules in 5 minutes
  ✅ Approve [time] → schedules at proposed peak time
  🕐 Pick Time     → select from time options
  ⏭ Skip           → dismisses without action (ephemeral)
  ❌ Reject        → marks deal inactive and deletes the card
"""

import asyncio
import os
import discord
from discord.ui import Button, View, Select
from config.settings import (
    DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, BRAND_NAME,
    DISCORD_TWITTER_CHANNEL_ID, DISCORD_LINKEDIN_CHANNEL_ID,
    DISCORD_REPLY_CHANNEL_ID,
)

async def _report_callback_error(interaction: discord.Interaction, where: str, exc: BaseException) -> None:
    """Log a button-callback exception and try to surface it to the user.

    Button callbacks that raise without responding cause Discord to show
    "This interaction failed" with no detail. This helper logs the
    traceback and best-effort responds/edits so the user sees something.
    """
    import traceback
    print(f"  [discord] Callback error in {where}: {exc}", flush=True)
    traceback.print_exc()
    msg = f"Something went wrong ({where}). I logged it — try again if needed."
    try:
        if interaction.response.is_done():
            # We already deferred/responded; use followup
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass  # best-effort only — Discord may have timed out the interaction


async def _send_action_notification(
    interaction: discord.Interaction,
    title: str,
    description: str,
    color_type: str = "success",
    use_channel: bool = False,
):
    """Send a color-coded embed notification after a button action.

    Parameters
    ----------
    interaction : discord.Interaction
        The interaction that triggered the action.
    title : str
        Embed title (e.g. "Deal Scheduled").
    description : str
        Embed body text.
    color_type : str
        One of "success" (green), "action" (gold), "error" (red), "info" (blue).
    use_channel : bool
        If True, send directly to the channel instead of using followup.send().
        Required when the interaction response was already consumed by send_message().
    """
    colors = {
        "success": discord.Color.green(),
        "action": discord.Color.gold(),
        "error": discord.Color.red(),
        "info": discord.Color.blue(),
    }
    embed = discord.Embed(
        title=title,
        description=description,
        color=colors.get(color_type, discord.Color.greyple()),
    )
    try:
        if use_channel:
            await interaction.channel.send(embed=embed)
        else:
            await interaction.followup.send(embed=embed)
    except Exception:
        pass  # non-critical


def _truncate_title(deal_id: int) -> str:
    """Fetch a deal title from the database, truncated to 60 chars."""
    from src.database import get_deal_by_id
    deal = get_deal_by_id(deal_id)
    if not deal:
        return f"Deal #{deal_id}"
    title = deal.get("title", f"Deal #{deal_id}")
    return title[:60] + ("..." if len(title) > 60 else "")


# Map platform names to their Discord channel IDs
PLATFORM_CHANNELS = {
    "twitter": DISCORD_TWITTER_CHANNEL_ID,
    "x": DISCORD_TWITTER_CHANNEL_ID,
    "linkedin": DISCORD_LINKEDIN_CHANNEL_ID,
}

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

# Set by api.py after the event loop starts -- used by agent.py to schedule coroutines
bot_loop: asyncio.AbstractEventLoop | None = None


@bot.event
async def on_ready():
    global bot_loop
    bot_loop = asyncio.get_event_loop()
    # Share the loop with agent.py so it can schedule coroutines from threads.
    # agent_module.bot_loop is read under agent_module.bot_loop_lock, so we
    # publish the value under the same lock for a well-defined happens-before.
    import src.agent as agent_module
    with agent_module.bot_loop_lock:
        agent_module.bot_loop = bot_loop
    # Register persistent views so buttons survive restarts.
    from src.database import get_top_unposted_deals, get_pending_reposts
    try:
        deals = get_top_unposted_deals(limit=50)
        for deal in deals:
            bot.add_view(DealView(deal["id"]))
        print(f"  Registered {len(deals)} persistent deal views")
    except Exception:
        pass
    # Register price drop views for pending reposts (survive restarts)
    try:
        pending = get_pending_reposts()
        for p in pending:
            bot.add_view(PriceDropView(p.get("asin", ""), p.get("deal_id", 0)))
        if pending:
            print(f"  Registered {len(pending)} pending price drop views")
    except Exception:
        pass
    # Start the 15-min auto-approve timer poller
    bot.loop.create_task(_check_pending_reposts())
    print(f"  Discord bot ready: {bot.user}")


def _extract_context_from_message(msg: discord.Message) -> tuple[int, str]:
    """Extract deal_id and ASIN from button custom_ids on a bot message.

    DealView buttons:      deal_now:{deal_id}:{platform}
    PriceDropView buttons: pd_approve:{asin}:{deal_id}:{platform}

    Returns (deal_id, asin). Both may be 0/"" if the message has no buttons.
    """
    for component in msg.components:
        children = getattr(component, "children", [component])
        for item in children:
            cid = getattr(item, "custom_id", "") or ""
            if cid.startswith("pd_") and len(cid.split(":")) >= 3:
                parts = cid.split(":")
                asin = parts[1]
                try:
                    deal_id = int(parts[2])
                except (ValueError, IndexError):
                    deal_id = 0
                return deal_id, asin
            if cid.startswith("deal_") and len(cid.split(":")) >= 2:
                parts = cid.split(":")
                try:
                    return int(parts[1]), ""
                except (ValueError, IndexError):
                    pass
    return 0, ""


@bot.event
async def on_message(message: discord.Message):
    """Two-way agent chat inside Discord.

    Triggers when:
      - User replies to any bot message  → agent gets context (deal_id/ASIN) injected
      - User @mentions the bot           → direct agent command, no card context needed

    The channel ID is used as the MemorySaver thread_id so the agent remembers
    earlier turns in the same channel — "post the second one", "skip that deal"
    all work without repeating which deal you mean.
    """
    if message.author.bot:
        return

    user_text = message.content.strip()
    if not user_text:
        return

    # Check if this is addressed to the agent
    is_reply_to_bot = (
        message.reference is not None
        and isinstance(getattr(message.reference, "resolved", None), discord.Message)
        and message.reference.resolved.author.id == bot.user.id
    )
    is_mention = bot.user in message.mentions

    if not is_reply_to_bot and not is_mention:
        return

    # Strip the @mention text so the agent only sees the actual command
    if is_mention:
        user_text = (
            user_text
            .replace(f"<@{bot.user.id}>", "")
            .replace(f"<@!{bot.user.id}>", "")
            .strip()
        )
    if not user_text:
        await message.reply("What would you like me to do?")
        return

    # If replying to a card, inject the deal_id/ASIN so the agent has context
    context_note = ""
    if is_reply_to_bot:
        ref_msg = message.reference.resolved
        deal_id, asin = _extract_context_from_message(ref_msg)
        if deal_id:
            context_note = f" [deal_id={deal_id}]"
        elif asin:
            context_note = f" [asin={asin}]"

    command = user_text + context_note
    thread_id = str(message.channel.id)

    async with message.channel.typing():
        loop = asyncio.get_event_loop()
        from src.agent import run_agent
        try:
            response = await loop.run_in_executor(None, run_agent, command, thread_id)
        except Exception as exc:
            response = f"Something went wrong: {exc}"

    # Split long responses so Discord's 2000-char limit isn't hit
    if len(response) <= 1900:
        await message.reply(response)
    else:
        chunks = [response[i:i+1900] for i in range(0, len(response), 1900)]
        await message.reply(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)


def _is_deal_stale(deal: dict, max_hours: int = 24) -> bool:
    """Check if a deal is too old to post. Prevents scheduling expired deals."""
    from datetime import datetime
    scraped_at = deal.get("scraped_at", "")
    if not scraped_at:
        return True
    try:
        scraped_time = datetime.fromisoformat(scraped_at)
        age_hours = (datetime.now() - scraped_time).total_seconds() / 3600
        return age_hours > max_hours
    except (ValueError, TypeError):
        return True


def _schedule_deal(deal_id: int, scheduled_at: str = None, platform: str = None) -> str:
    """Schedule a deal to Postiz for a specific platform (or all if platform is None).
    Called from button callbacks. Rejects stale deals.
    """
    from src.database import get_deal_by_id, mark_as_posted, update_deal
    from src.notifier import generate_deal_content
    from src.postiz_client import schedule_post
    from src.platform_router import select_platforms
    from src.tweet_learner import record_tweet

    deal = get_deal_by_id(deal_id)
    if not deal:
        return f"Deal {deal_id} not found"
    if _is_deal_stale(deal):
        return "expired"
    content = generate_deal_content(deal)

    if platform:
        # Schedule to a single platform
        platforms_to_post = [platform]
    else:
        # Legacy: schedule to all matched platforms
        platforms_to_post = select_platforms(deal)

    result = schedule_post(deal, content, platforms_to_post, scheduled_at=scheduled_at)

    if result.get("status") == "ok":
        # Track per-platform status
        platform_status = deal.get("platforms", {})
        for p in platforms_to_post:
            platform_status[p] = {"status": "scheduled", "scheduled_at": scheduled_at}
        update_deal(deal_id, {"platforms": platform_status})

        # Telegram: side-channel, posts immediately (no Postiz, no peak-time scheduling).
        # Guard "telegram" not in platform_status prevents double-posting when
        # _schedule_deal() is called twice (once for twitter, once for linkedin).
        from src.platform_router import should_post_telegram
        if should_post_telegram() and "telegram" not in platform_status:
            try:
                from src.telegram_client import send_deal as _tg_send
                import datetime as _dt
                if _tg_send(deal, content):
                    platform_status["telegram"] = {
                        "status": "posted",
                        "posted_at": _dt.datetime.now().isoformat(),
                    }
                    update_deal(deal_id, {"platforms": platform_status})
            except Exception as _tg_exc:
                print(f"  [telegram] Failed for deal {deal_id}: {_tg_exc}", flush=True)

        # Check if ALL targeted platforms are now scheduled
        all_targeted = select_platforms(deal)
        all_scheduled = all(
            platform_status.get(p, {}).get("status") == "scheduled"
            for p in all_targeted
        )
        if all_scheduled:
            mark_as_posted(deal_id)

        from src.postiz_client import extract_postiz_id
        postiz_id = extract_postiz_id(result)
        if platform in ("twitter", "x", None):
            record_tweet(deal_id, content["tweet_1"], postiz_id, scheduled_at or "")

    return result.get("status", "error")


class DealView(View):
    def __init__(self, deal_id: int = 0, proposed_label: str = "", platform: str = ""):
        super().__init__(timeout=None)
        self.deal_id = deal_id
        self.platform = platform
        short_label = proposed_label[:20] if proposed_label else "peak"
        self.add_item(DealPostNowButton(deal_id, platform))
        self.add_item(DealApproveButton(deal_id, short_label, platform))
        self.add_item(DealPickTimeButton(deal_id, platform))
        self.add_item(DealSkipButton(deal_id, platform))
        self.add_item(DealRejectButton(deal_id, platform))


def _parse_custom_id(custom_id: str) -> tuple[int, str]:
    """Parse deal_id and platform from a custom_id like 'deal_now:42:twitter'."""
    parts = custom_id.split(":")
    deal_id = int(parts[1])
    platform = parts[2] if len(parts) > 2 else ""
    return deal_id, platform


class DealPostNowButton(Button):
    def __init__(self, deal_id: int, platform: str = ""):
        super().__init__(
            label="Post Now", style=discord.ButtonStyle.danger,
            custom_id=f"deal_now:{deal_id}:{platform}",
        )

    async def callback(self, interaction: discord.Interaction):
        # Defer first thing so Discord's 3-second interaction timeout can't
        # fire while we do slow work (Postiz API, file I/O, LLM).
        try:
            await interaction.response.defer()
        except Exception:
            pass  # already acknowledged somehow — fall through
        try:
            did, plat = _parse_custom_id(self.custom_id)
            from src.postiz_client import get_post_now_time

            loop = asyncio.get_event_loop()
            post_time, label = get_post_now_time()
            status = await loop.run_in_executor(None, _schedule_deal, did, post_time, plat or None)
            if status == "expired":
                await interaction.edit_original_response(content="This deal expired. Price may have changed. Skipping.", view=None)
            elif status == "ok":
                await interaction.edit_original_response(content=f"Dropping in **{label}** — Postiz has it.", view=None)
                deal_title = _truncate_title(did)
                await _send_action_notification(
                    interaction,
                    "Deal Posting Soon",
                    f"**{deal_title}** posting in ~5 minutes on {plat or 'all platforms'}",
                    "success",
                )
            else:
                await interaction.edit_original_response(content=f"Couldn't schedule. Error: `{status}`", view=None)
        except Exception as e:
            await _report_callback_error(interaction, "Post Now", e)


class DealApproveButton(Button):
    def __init__(self, deal_id: int, label_hint: str = "peak", platform: str = ""):
        super().__init__(
            label=f"Approve ({label_hint})", style=discord.ButtonStyle.success,
            custom_id=f"deal_approve:{deal_id}:{platform}",
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except Exception:
            pass
        try:
            did, plat = _parse_custom_id(self.custom_id)
            print(f"  [approve] Deal {did} ({plat or 'all'}) approved by {interaction.user}", flush=True)
            from src.postiz_client import resolve_schedule_time

            loop = asyncio.get_event_loop()
            post_time, label = resolve_schedule_time(did)  # prefers Hermes-picked time, else smart fallback
            status = await loop.run_in_executor(None, _schedule_deal, did, post_time, plat or None)
            print(f"  [approve] Schedule result for deal {did} ({plat or 'all'}): {status}", flush=True)
            if status == "expired":
                await interaction.edit_original_response(content="This deal expired. Price may have changed. Skipping.", view=None)
            elif status == "ok":
                await interaction.edit_original_response(content=f"Locked in for **{label}**. Postiz will handle it.", view=None)
                deal_title = _truncate_title(did)
                await _send_action_notification(
                    interaction,
                    "Deal Scheduled",
                    f"**{deal_title}** scheduled for {label} on {plat or 'all platforms'}",
                    "success",
                )
            else:
                await interaction.edit_original_response(content=f"Couldn't schedule (`{status}`). Try again?", view=None)
        except Exception as e:
            await _report_callback_error(interaction, "Approve", e)


class DealPickTimeButton(Button):
    def __init__(self, deal_id: int, platform: str = ""):
        super().__init__(
            label="Pick Time", style=discord.ButtonStyle.primary,
            custom_id=f"deal_pick:{deal_id}:{platform}",
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            did, plat = _parse_custom_id(self.custom_id)
            from src.postiz_client import get_time_options
            options = get_time_options()
            select = TimeSelect(did, options, plat)
            view = View(timeout=60)
            view.add_item(select)
            await interaction.response.send_message(
                "Pick a posting time:", view=view, ephemeral=True
            )
        except Exception as e:
            await _report_callback_error(interaction, "Pick Time", e)


class TimeSelect(Select):
    def __init__(self, deal_id: int, options: list, platform: str = ""):
        self.deal_id = deal_id
        self.platform = platform
        select_options = [
            discord.SelectOption(label=label, value=iso_time)
            for iso_time, label in options
        ]
        super().__init__(
            placeholder="Select posting time...",
            options=select_options,
            custom_id=f"deal_time_select:{deal_id}:{platform}",
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except Exception:
            pass
        try:
            selected_time = self.values[0]
            from src.postiz_client import get_time_options
            options = get_time_options()
            label = next((l for t, l in options if t == selected_time), "Selected time")
            loop = asyncio.get_event_loop()
            status = await loop.run_in_executor(None, _schedule_deal, self.deal_id, selected_time, self.platform or None)
            if status == "expired":
                await interaction.edit_original_response(
                    content="This deal expired. Price may have changed. Skipping.", view=None
                )
            elif status == "ok":
                await interaction.edit_original_response(
                    content=f"Scheduled for **{label}**", view=None
                )
                deal_title = _truncate_title(self.deal_id)
                await _send_action_notification(
                    interaction,
                    "Deal Rescheduled",
                    f"**{deal_title}** rescheduled to {label} on {self.platform or 'all platforms'}",
                    "action",
                )
            else:
                await interaction.edit_original_response(
                    content=f"Failed to schedule: {status}", view=None
                )
        except Exception as e:
            await _report_callback_error(interaction, "Time Select", e)


class DealSkipButton(Button):
    def __init__(self, deal_id: int, platform: str = ""):
        super().__init__(
            label="Skip", style=discord.ButtonStyle.secondary,
            custom_id=f"deal_skip:{deal_id}:{platform}",
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_message("Skipped. It'll stay in the queue.", ephemeral=True)
        except Exception as e:
            await _report_callback_error(interaction, "Skip", e)


class DealRejectButton(Button):
    def __init__(self, deal_id: int, platform: str = ""):
        super().__init__(
            label="Reject", style=discord.ButtonStyle.secondary,
            custom_id=f"deal_reject:{deal_id}:{platform}",
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            did, plat = _parse_custom_id(self.custom_id)
            from src.database import update_deal
            update_deal(did, {"is_active": False})
            await interaction.response.send_message("Rejected. Out of the queue.", ephemeral=True)
            deal_title = _truncate_title(did)
            await _send_action_notification(
                interaction,
                "Deal Rejected",
                f"**{deal_title}** marked inactive",
                "error",
                use_channel=True,
            )
            # Message may already be deleted by the user; tolerate that.
            try:
                await interaction.message.delete()
            except discord.NotFound:
                pass
            except Exception as delete_err:
                print(f"  [discord] Could not delete reject card: {delete_err}", flush=True)
        except Exception as e:
            await _report_callback_error(interaction, "Reject", e)


async def _send_twitter_card(channel, deal: dict, content: dict):
    """Send a Twitter deal card: Tweet 1 → Image → Tweet 2 → Buttons."""
    from src.postiz_client import resolve_schedule_time

    deal_url = deal.get("affiliate_url") or deal.get("source_url", "")
    proposed_time, proposed_label = resolve_schedule_time(deal["id"])  # Hermes-picked time if set

    await channel.send("**Tweet 1** — copy and post with the image below:", suppress_embeds=True)
    await asyncio.sleep(0.3)

    await channel.send(content["tweet_1"], suppress_embeds=True)
    await asyncio.sleep(0.3)

    if deal.get("image_url") and not deal["image_url"].startswith("data:"):
        image_embed = discord.Embed(color=0x1DA1F2)
        image_embed.set_image(url=deal["image_url"])
        await channel.send(embed=image_embed)
        await asyncio.sleep(0.3)

    await channel.send("**Tweet 2** — reply to Tweet 1 with this:", suppress_embeds=True)
    await asyncio.sleep(0.3)

    await channel.send(content["tweet_2"], suppress_embeds=True)
    await asyncio.sleep(0.3)

    discount = deal.get("discount_pct") or 0
    discount_line = f"**Discount:** {int(round(discount))}% off\n" if discount else "**Discount:** not found\n"
    action_msg = (
        f"{discount_line}"
        f"**Link:** {deal_url}\n"
        f"**Platform:** Twitter/X\n"
        f"**Best time:** {proposed_label}"
        + (f"  _( {deal['schedule_reason']} )_" if deal.get("schedule_reason") else "")
    )
    await channel.send(
        action_msg,
        view=DealView(deal["id"], proposed_label, platform="twitter"),
        suppress_embeds=True,
    )


async def _send_linkedin_card(channel, deal: dict, content: dict):
    """Send a LinkedIn deal card: LinkedIn post preview → Image → Buttons."""
    from src.postiz_client import resolve_schedule_time

    deal_url = deal.get("affiliate_url") or deal.get("source_url", "")
    proposed_time, proposed_label = resolve_schedule_time(deal["id"])  # Hermes-picked time if set

    li_post = content.get("linkedin_post", "")
    if not li_post:
        return  # No LinkedIn content generated

    await channel.send("**LinkedIn Post** — preview:", suppress_embeds=True)
    await asyncio.sleep(0.3)

    await channel.send(li_post, suppress_embeds=True)
    await asyncio.sleep(0.3)

    if deal.get("image_url") and not deal["image_url"].startswith("data:"):
        image_embed = discord.Embed(color=0x0A66C2)  # LinkedIn blue
        image_embed.set_image(url=deal["image_url"])
        await channel.send(embed=image_embed)
        await asyncio.sleep(0.3)

    action_msg = (
        f"**Link:** {deal_url}\n"
        f"**Platform:** LinkedIn\n"
        f"**Best time:** {proposed_label}"
    )
    await channel.send(
        action_msg,
        view=DealView(deal["id"], proposed_label, platform="linkedin"),
        suppress_embeds=True,
    )


async def send_auto_approved_notification(deal: dict, content: dict, scheduled_time: str, platforms: list[str]):
    """Send a deal card for an auto-approved deal.

    Mirrors the manual deal card layout (copy preview + image + deal info) but
    replaces the action buttons with a read-only status line showing that the
    deal has already been auto-posted and when it was scheduled.
    """
    channel_id = DISCORD_TWITTER_CHANNEL_ID or DISCORD_CHANNEL_ID
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if not channel:
        return

    from src.platform_router import describe_platforms
    deal_url = deal.get("affiliate_url") or deal.get("source_url", "")
    platform_desc = describe_platforms(platforms)
    discount = deal.get("discount_pct", 0)
    title = deal.get("title", "Unknown Deal")

    # Header separator
    await channel.send(f"{'─' * 40}", suppress_embeds=True)
    await asyncio.sleep(0.2)

    # Tweet copy preview (same as manual card)
    tweet_preview = content.get("tweet_1", "")
    if tweet_preview:
        await channel.send(f"**Auto-Scheduled Tweet:**\n{tweet_preview}", suppress_embeds=True)
        await asyncio.sleep(0.3)

    # Product image (full-width, same as manual card)
    if deal.get("image_url") and not deal["image_url"].startswith("data:"):
        image_embed = discord.Embed(color=0x00CC44)
        image_embed.set_image(url=deal["image_url"])
        await channel.send(embed=image_embed)
        await asyncio.sleep(0.3)

    # Deal info embed with auto-post status
    deal_price = deal.get("deal_price") or deal.get("price")
    original_price = deal.get("original_price")
    category = deal.get("category", "")
    asin = deal.get("asin", "")
    from src.database import score_deal
    deal_score = score_deal(deal)

    price_line = ""
    if deal_price:
        price_line = f"**Price:** ${deal_price:.2f}"
        if original_price and original_price > deal_price:
            price_line += f" ~~${original_price:.2f}~~"
        price_line += "\n"

    meta_line = ""
    if category:
        meta_line += f"📂 {category.title()}  "
    if asin:
        meta_line += f"`{asin}`  "
    meta_line += f"⭐ Score: {deal_score:.0f}"

    status_line = (
        f"{'─' * 30}\n"
        f"✅ **Auto-posted** — no action needed\n"
        f"🕐 **Scheduled:** {scheduled_time}\n"
        f"📣 **Platforms:** {platform_desc}"
    )
    info_msg = (
        f"**{title[:80]}**\n"
        f"{price_line}"
        f"**Discount:** {int(round(discount))}% off\n" if discount else ""
        f"{meta_line}\n\n"
        f"{status_line}"
    )
    # Link buttons so you can verify the deal and tweet yourself
    link_view = View(timeout=None)
    if deal_url:
        link_view.add_item(Button(
            label="🛒 View on Amazon",
            style=discord.ButtonStyle.link,
            url=deal_url,
        ))
    postiz_api = os.getenv("POSTIZ_API_URL", "")
    postiz_url = postiz_api.replace("/api", "").rstrip("/") if postiz_api else ""
    if postiz_url:
        link_view.add_item(Button(
            label="📅 View in Postiz",
            style=discord.ButtonStyle.link,
            url=f"{postiz_url}/launches",
        ))
    await channel.send(info_msg, view=link_view, suppress_embeds=True)


def _get_platform_channel(platform: str):
    """Get the Discord channel object for a platform. Falls back to default channel."""
    channel_id = PLATFORM_CHANNELS.get(platform) or DISCORD_CHANNEL_ID
    if not channel_id:
        return None
    return bot.get_channel(int(channel_id))


async def send_deal_card(deal: dict, content: dict):
    """Send deal cards to per-platform Discord channels.

    For each platform the deal targets, sends a platform-specific card
    to that platform's Discord channel. If no per-platform channel is
    configured, falls back to the default DISCORD_CHANNEL_ID.
    """
    from src.platform_router import select_platforms

    platforms = select_platforms(deal)

    for platform in platforms:
        plat = platform.lower()

        if plat in ("twitter", "x"):
            channel = _get_platform_channel("twitter")
            if not channel:
                print(f"  [discord] No channel for Twitter, skipping card")
                continue
            try:
                await _send_twitter_card(channel, deal, content)
                print(f"  [discord] Twitter card sent: {deal.get('title', '')[:50]}")
            except Exception as e:
                print(f"  [discord] Failed to send Twitter card for deal {deal.get('id')}: {e}")

        elif plat == "linkedin":
            if not DISCORD_LINKEDIN_CHANNEL_ID:
                print(f"  [discord] DISCORD_LINKEDIN_CHANNEL_ID not set, skipping LinkedIn card")
                continue
            channel = _get_platform_channel("linkedin")
            if not channel:
                print(f"  [discord] LinkedIn channel not found, skipping")
                continue
            try:
                await _send_linkedin_card(channel, deal, content)
                print(f"  [discord] LinkedIn card sent: {deal.get('title', '')[:50]}")
            except Exception as e:
                print(f"  [discord] Failed to send LinkedIn card for deal {deal.get('id')}: {e}")

        else:
            # Future platforms: send to default channel with tweet_1
            channel = _get_platform_channel(plat)
            if channel:
                try:
                    await _send_twitter_card(channel, deal, content)
                    print(f"  [discord] {plat} card sent: {deal.get('title', '')[:50]}")
                except Exception as e:
                    print(f"  [discord] Failed to send {plat} card: {e}")


# --- Price Drop Auto-Repost Cards ---

class PriceDropView(View):
    """Discord view for price drop repost cards. Post Now + Cancel + timer label."""
    def __init__(self, asin: str = "", deal_id: int = 0, platform: str = ""):
        super().__init__(timeout=None)
        self.add_item(PriceDropApproveNowButton(asin, deal_id, platform))
        self.add_item(PriceDropRejectButton(asin, deal_id, platform))
        self.add_item(PriceDropTimerButton(asin))


class PriceDropApproveNowButton(Button):
    def __init__(self, asin: str, deal_id: int, platform: str = ""):
        super().__init__(
            label="Post Now",
            style=discord.ButtonStyle.success,
            custom_id=f"pd_approve:{asin}:{deal_id}:{platform}",
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            parts = self.custom_id.split(":")
            asin = parts[1] if len(parts) > 1 else ""
            deal_id = int(parts[2]) if len(parts) > 2 else 0
            from src.database import get_pending_reposts, remove_pending_repost
            pending_list = get_pending_reposts()
            pending = next((p for p in pending_list if p.get("asin") == asin), None)
            if not pending:
                await interaction.response.send_message("Repost already cancelled or posted.", ephemeral=True)
                return
            await interaction.response.defer()
            await _auto_approve_price_drop(pending, triggered_by=str(interaction.user))
            try:
                await interaction.message.delete()
            except discord.NotFound:
                pass
        except Exception as e:
            await _report_callback_error(interaction, "Price Drop Post Now", e)


class PriceDropRejectButton(Button):
    def __init__(self, asin: str, deal_id: int, platform: str = ""):
        super().__init__(
            label="Cancel Auto-post",
            style=discord.ButtonStyle.danger,
            custom_id=f"pd_reject:{asin}:{deal_id}:{platform}",
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            parts = self.custom_id.split(":")
            asin = parts[1] if len(parts) > 1 else ""
            deal_id = int(parts[2]) if len(parts) > 2 else 0
            from src.database import remove_pending_repost
            remove_pending_repost(asin)
            await interaction.response.send_message("Rejected. Price drop repost cancelled.", ephemeral=True)
            deal_title = _truncate_title(deal_id) if deal_id else f"ASIN {asin}"
            await _send_action_notification(
                interaction,
                "Price Drop Rejected",
                f"**{deal_title}** price drop repost cancelled",
                "error",
                use_channel=True,
            )
            try:
                await interaction.message.delete()
            except discord.NotFound:
                pass
            except Exception as delete_err:
                print(f"  [discord] Could not delete price drop card: {delete_err}", flush=True)
        except Exception as e:
            await _report_callback_error(interaction, "Price Drop Reject", e)


class PriceDropTimerButton(Button):
    def __init__(self, asin: str):
        super().__init__(
            label="Auto-posts in 15 min if not rejected",
            style=discord.ButtonStyle.secondary,
            custom_id=f"pd_timer:{asin}",
            disabled=True,
        )


async def _auto_approve_price_drop(pending: dict, triggered_by: str = "timer"):
    """Post a price drop repost. Called by the 15-min timer or the Post Now button.

    triggered_by: display string for the Discord notification ("timer" or Discord username).
    """
    from src.database import record_repost, remove_pending_repost
    from src.postiz_client import bump_schedule
    from src.tweet_learner import record_tweet
    from src.postiz_client import extract_postiz_id

    deal = pending.get("deal", {})
    content = pending.get("content", {})
    asin = pending.get("asin", "")
    platforms = pending.get("platforms", ["twitter"])

    result = bump_schedule(deal, content, platforms)

    if result.get("status") == "ok":
        record_repost(asin, pending.get("new_price", 0))
        postiz_id = extract_postiz_id(result)
        if "twitter" in platforms or "x" in platforms:
            record_tweet(
                pending.get("deal_id", 0),
                content.get("tweet_1", ""),
                postiz_id,
                "",
            )
        print(f"  [price_drop] Posted repost for {asin} (by {triggered_by})", flush=True)

        # Delete original card, then send a fresh notification embed
        channel_id = pending.get("discord_channel_id")
        message_id = pending.get("discord_message_id")
        if channel_id:
            try:
                channel = bot.get_channel(int(channel_id))
                if channel:
                    if message_id:
                        try:
                            msg = await channel.fetch_message(int(message_id))
                            await msg.delete()
                        except discord.NotFound:
                            pass
                    from src.platform_router import describe_platforms
                    title = deal.get("title", asin)[:80]
                    new_price = pending.get("new_price", 0)
                    drop_pct = pending.get("drop_pct", 0)
                    by_note = "Timer expired — no rejection" if triggered_by == "timer" else f"Approved by {triggered_by}"
                    embed = discord.Embed(
                        title=f"Price Drop Posted: {title}",
                        color=0x00CC44,
                        description=(
                            f"**New price:** ${new_price:.2f} (-{drop_pct:.0f}%)\n"
                            f"**Platforms:** {describe_platforms(platforms)}\n"
                            f"**Triggered by:** {by_note}"
                        ),
                    )
                    if deal.get("image_url") and not deal["image_url"].startswith("data:"):
                        embed.set_thumbnail(url=deal["image_url"])
                    embed.set_footer(text="Price drop repost sent to Postiz.")
                    await channel.send(embed=embed)
            except Exception as _e:
                print(f"  [price_drop] Notification failed: {_e}", flush=True)
    else:
        print(f"  [price_drop] Post failed for {asin}: {result}", flush=True)
        # Notify about failure too
        channel_id = pending.get("discord_channel_id")
        if channel_id:
            try:
                channel = bot.get_channel(int(channel_id))
                if channel:
                    title = deal.get("title", asin)[:60]
                    await channel.send(
                        embed=discord.Embed(
                            title=f"Price Drop Post Failed: {title}",
                            color=0xFF0000,
                            description=f"Postiz returned: `{result.get('status', 'error')}`\nASIN: `{asin}`",
                        )
                    )
            except Exception:
                pass

    remove_pending_repost(asin)


async def _check_pending_reposts():
    """Background task: poll pending_reposts.json every 60s for expired timers.

    Auto-approves only when ALL three gates pass:
      1. deal_score >= PIPELINE_MIN_SCORE     (feedback-weighted quality)
      2. content confidence >= PIPELINE_MIN_CONFIDENCE (LLM copy quality)
      3. drop_pct >= MIN_PRICE_DROP_AUTO_PCT (price drop is meaningful)

    Any gate failure → card stays up, waits for manual Post Now / Cancel.
    Logs gate failures only once per ASIN to avoid log spam.
    """
    await bot.wait_until_ready()
    _gate_fail_logged: set = set()  # ASINs already warned — suppress repeat logs
    while not bot.is_closed():
        await asyncio.sleep(60)
        try:
            from src.database import get_expired_pending_reposts, score_deal
            from config.settings import PIPELINE_MIN_SCORE, PIPELINE_MIN_CONFIDENCE, MIN_PRICE_DROP_AUTO_PCT
            expired = get_expired_pending_reposts()
            for pending in expired:
                deal = pending.get("deal", {})
                asin = pending.get("asin", deal.get("asin", ""))
                deal_score = score_deal(deal) if deal else 0
                content_conf = pending.get("content", {}).get("confidence", 1.0)
                drop_pct = pending.get("drop_pct", 0)

                gates = {
                    f"score {deal_score:.0f} >= {PIPELINE_MIN_SCORE}": deal_score >= PIPELINE_MIN_SCORE,
                    f"confidence {content_conf:.2f} >= {PIPELINE_MIN_CONFIDENCE}": content_conf >= PIPELINE_MIN_CONFIDENCE,
                    f"drop {drop_pct:.0f}% >= {MIN_PRICE_DROP_AUTO_PCT}%": drop_pct >= MIN_PRICE_DROP_AUTO_PCT,
                }
                failed = [label for label, ok in gates.items() if not ok]

                if not failed:
                    _gate_fail_logged.discard(asin)  # Reset so next drop logs fresh
                    print(f"  [price_drop] Auto-approving ({', '.join(gates)}): {deal.get('title', '')[:40]}", flush=True)
                    await _auto_approve_price_drop(pending)
                elif asin not in _gate_fail_logged:
                    # Log once, then go silent until it clears or a new drop fires
                    _gate_fail_logged.add(asin)
                    print(f"  [price_drop] Gates failed ({'; '.join(failed)}) — awaiting manual action: {deal.get('title', '')[:40]}", flush=True)
        except Exception as e:
            print(f"  [price_drop] Timer check error: {e}", flush=True)


async def send_price_drop_card(drop_info: dict, content: dict):
    """Send price drop cards to per-platform Discord channels with 15-min auto-approve."""
    from src.platform_router import select_platforms
    from src.database import save_pending_repost
    from config.settings import FAST_TRACK_MINUTES
    from datetime import datetime, timedelta

    deal = drop_info.get("deal", {})
    platforms = select_platforms(deal) if deal else ["twitter"]
    asin = drop_info.get("asin", "")
    auto_approve_at = (datetime.now() + timedelta(minutes=FAST_TRACK_MINUTES)).isoformat()

    for platform in platforms:
        plat = platform.lower()
        channel = _get_platform_channel(plat)
        if not channel:
            continue

        try:
            # Build the embed
            new_price = drop_info.get("new_price", 0)
            old_price = drop_info.get("old_price", 0)
            original = drop_info.get("original_posted_price", old_price)
            drop_pct = drop_info.get("drop_pct", 0)

            badge = ""
            if drop_info.get("is_lowest_ever"):
                badge = "ALL-TIME LOWEST PRICE"
            elif drop_info.get("is_lowest_90d"):
                badge = "Lowest price in 90 days"

            # Build Amazon affiliate URL from ASIN or fall back to deal_url
            from config.settings import AMAZON_AFFILIATE_TAG
            _asin = asin or drop_info.get("asin", "") or deal.get("asin", "")
            _deal_url = drop_info.get("url") or deal.get("url") or deal.get("deal_url") or ""
            if _asin:
                amazon_url = f"https://www.amazon.com/dp/{_asin}?tag={AMAZON_AFFILIATE_TAG}"
            elif _deal_url:
                amazon_url = _deal_url
            else:
                amazon_url = ""

            embed = discord.Embed(
                title=f"PRICE DROP: {drop_info.get('title', '')[:60]}",
                url=amazon_url or discord.Embed.Empty,
                color=0xFF4500,
                description=(
                    f"**Was:** ${original:.2f} (when we posted)\n"
                    f"**Now:** ${new_price:.2f} (-{drop_pct:.0f}%)\n"
                    f"{f'**{badge}**' if badge else ''}\n"
                    f"{f'[🛒 View on Amazon]({amazon_url})' if amazon_url else ''}"
                ),
            )
            if drop_info.get("image_url"):
                embed.set_image(url=drop_info["image_url"])

            # Show platform-specific content preview
            if plat == "linkedin" and content.get("linkedin_post"):
                preview = content["linkedin_post"][:300]
            else:
                preview = content.get("tweet_1", "")

            await channel.send(embed=embed)
            await asyncio.sleep(0.3)
            await channel.send(f"**Preview ({plat}):**\n{preview}", suppress_embeds=True)
            await asyncio.sleep(0.3)

            # Send buttons
            view = PriceDropView(asin, drop_info.get("deal_id", 0), plat)
            from src.database import score_deal as _score_deal
            from config.settings import PIPELINE_MIN_CONFIDENCE, PIPELINE_MIN_SCORE, MIN_PRICE_DROP_AUTO_PCT
            _drop_score = _score_deal(deal) if deal else 0
            _content_conf = content.get("confidence", 1.0)
            _all_gates = (
                _drop_score >= PIPELINE_MIN_SCORE
                and _content_conf >= PIPELINE_MIN_CONFIDENCE
                and drop_pct >= MIN_PRICE_DROP_AUTO_PCT
            )
            if _all_gates:
                status_msg = (
                    f"All gates passed — score {_drop_score:.0f}, "
                    f"confidence {_content_conf:.2f}, drop {drop_pct:.0f}%.\n"
                    f"Auto-posts in {FAST_TRACK_MINUTES} min. Use **Cancel Auto-post** to stop."
                )
            else:
                failed_gates = []
                if _drop_score < PIPELINE_MIN_SCORE:
                    failed_gates.append(f"score {_drop_score:.0f} < {PIPELINE_MIN_SCORE}")
                if _content_conf < PIPELINE_MIN_CONFIDENCE:
                    failed_gates.append(f"confidence {_content_conf:.2f} < {PIPELINE_MIN_CONFIDENCE}")
                if drop_pct < MIN_PRICE_DROP_AUTO_PCT:
                    failed_gates.append(f"drop {drop_pct:.0f}% < {MIN_PRICE_DROP_AUTO_PCT}%")
                status_msg = f"Gates failed: {', '.join(failed_gates)}. Use **Post Now** to approve manually."
            msg = await channel.send(
                status_msg,
                view=view,
                suppress_embeds=True,
            )

            # Amazon link button — separate message so it doesn't conflict with action buttons
            if amazon_url:
                link_view = View(timeout=None)
                link_view.add_item(Button(
                    label="🛒 View on Amazon",
                    style=discord.ButtonStyle.link,
                    url=amazon_url,
                ))
                await channel.send("", view=link_view, suppress_embeds=True)

            # Persist for timer survival
            save_pending_repost({
                "asin": asin,
                "deal_id": drop_info.get("deal_id"),
                "content": content,
                "platforms": platforms,
                "deal": deal,
                "new_price": new_price,
                "drop_pct": drop_pct,
                "auto_approve_at": auto_approve_at,
                "discord_message_id": str(msg.id),
                "discord_channel_id": str(channel.id),
            })

            print(f"  [discord] Price drop card sent ({plat}): {drop_info.get('title', '')[:40]}")
        except Exception as e:
            print(f"  [discord] Price drop card failed ({plat}): {e}")


def reset_batch_times():
    """Prune stale (past) proposed times between deal-card batches (thread-safe).

    Only removes entries whose ISO timestamp has already passed, so any
    future slots reserved by in-flight price-drop auto-approvals are kept
    intact.  Previously this called _batch_proposed.clear(), which raced
    with the 15-min price-drop timer that fires at the same :30 mark as
    _auto_run, causing multiple price-drop posts to land on the same Postiz
    slot.
    """
    from datetime import datetime, timezone
    from src.postiz_client import _batch_proposed, _batch_lock
    now_key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    with _batch_lock:
        stale = {k for k in _batch_proposed if k <= now_key}
        _batch_proposed -= stale


# ── Reply Guy — Discord card + ReplyView ──────────────────────────────────────

# Per-user cooldown: prevents rapid-fire clicks from hitting X API rate limits.
# Keyed by Discord user_id (int). Stores last post timestamp.
_reply_last_post: dict[int, float] = {}
_REPLY_COOLDOWN_SECS = 3  # minimum seconds between reply posts per user


class ReplyOptionButton(Button):
    """Button for A/B/C reply options. Posts the selected reply to X when clicked."""

    def __init__(self, label: str, option_index: int, tweet_id: str, reply_text: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"reply_{tweet_id}_{option_index}")
        self.tweet_id = tweet_id
        self.option_index = option_index
        self.reply_text = reply_text

    async def callback(self, interaction: discord.Interaction):
        import time
        # Rate-limit: reject if user posted a reply less than _REPLY_COOLDOWN_SECS ago
        user_id = interaction.user.id
        now = time.monotonic()
        last = _reply_last_post.get(user_id, 0.0)
        if now - last < _REPLY_COOLDOWN_SECS:
            remaining = round(_REPLY_COOLDOWN_SECS - (now - last), 1)
            await interaction.response.send_message(
                f"⏳ Slow down — wait {remaining}s before posting another reply.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        try:
            from src.tweet_poster import post_reply
            from src.reply_finder import record_reply_feedback

            result = post_reply(self.tweet_id, self.reply_text)
            if result.get("status") == "ok":
                _reply_last_post[user_id] = time.monotonic()
                tweet_url = f"https://twitter.com/i/web/status/{result.get('tweet_id', '')}"
                record_reply_feedback(self.tweet_id, self.option_index, self.reply_text)
                await interaction.edit_original_response(
                    content=(
                        f"✅ **Reply posted** by {interaction.user.display_name}\n"
                        f"Option {self.label}: {self.reply_text}\n"
                        f"[View on X]({tweet_url})"
                    ),
                    view=None,
                )
            elif result.get("status") == "rate_limited":
                await interaction.followup.send("⚠️ X API rate limited — try again in a few minutes.", ephemeral=True)
            else:
                err = result.get("error", "unknown error")
                await interaction.followup.send(f"❌ Failed to post reply: {err}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


class _SkipReplyButton(Button):
    """Dismiss button — removes the reply card without posting."""

    def __init__(self, tweet_id: str):
        super().__init__(
            label="Skip",
            style=discord.ButtonStyle.secondary,
            custom_id=f"reply_skip_{tweet_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=f"⏭️ Skipped by {interaction.user.display_name}",
            view=None,
        )


class ReplyView(View):
    """View with A/B/C reply option buttons and a Skip button."""

    def __init__(self, tweet_id: str, options: list[str]):
        super().__init__(timeout=3600)  # 1 hour timeout
        labels = ["A", "B", "C"]
        for i, (label, text) in enumerate(zip(labels, options)):
            self.add_item(ReplyOptionButton(label, i + 1, tweet_id, text))
        self.add_item(_SkipReplyButton(tweet_id))
        self._message: discord.Message | None = None  # set after send for on_timeout

    async def on_timeout(self) -> None:
        """Edit message when buttons expire so user knows the card is dead."""
        if self._message:
            try:
                await self._message.edit(
                    content=self._message.content + "\n\n⏱️ *Expired — card timed out.*",
                    view=None,
                )
            except Exception:
                pass  # message may have been deleted


async def send_reply_card(
    tweet_id: str,
    tweet_url: str,
    tweet_text: str,
    username: str,
    options: list[str],
) -> None:
    """Send reply option card to the DISCORD_REPLY_CHANNEL_ID channel.

    Shows the original tweet + up to 3 reply options as A/B/C buttons.
    User clicks a button to post that reply directly to X.
    """
    if not DISCORD_REPLY_CHANNEL_ID:
        print("  [reply_finder] DISCORD_REPLY_CHANNEL_ID not set — skipping card", flush=True)
        return

    channel = bot.get_channel(int(DISCORD_REPLY_CHANNEL_ID))
    if not channel:
        print(f"  [reply_finder] Reply channel {DISCORD_REPLY_CHANNEL_ID} not found", flush=True)
        return

    # Header
    await channel.send(f"{'─' * 40}", suppress_embeds=True)
    await asyncio.sleep(0.2)

    # Original tweet
    await channel.send(
        f"**@{username}** tweeted:\n> {tweet_text[:300]}\n[View tweet]({tweet_url})",
        suppress_embeds=True,
    )
    await asyncio.sleep(0.3)

    # Reply options with buttons
    options_text = "\n".join(
        f"**{label}:** {text}"
        for label, text in zip(["A", "B", "C"], options)
    )
    view = ReplyView(tweet_id, options)
    msg = await channel.send(
        f"**Reply options** — click to post:\n{options_text}",
        view=view,
        suppress_embeds=True,
    )
    view._message = msg  # needed so on_timeout() can edit the message
