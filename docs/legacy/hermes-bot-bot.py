import os
import asyncio
import discord
from discord.ext import commands
from discord.ui import Button, View
from dotenv import load_dotenv
import json
from datetime import datetime
import requests

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))
POSTIZ_URL = os.getenv('POSTIZ_API_URL')
POSTIZ_KEY = os.getenv('POSTIZ_API_KEY')
TWITTER_ID = os.getenv('POSTIZ_TWITTER_ID')
AFF_TAG = os.getenv('AMAZON_AFFILIATE_TAG')

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

DEALS_DB = '/Users/nitesh/Projects/quadstar-deals/data/deals.json'  # Shared

class DealButtonView(View):
    def __init__(self, deal_id):
        super().__init__(timeout=None)
        self.deal_id = deal_id

    @discord.ui.button(label='Approve & Post Peak', style=discord.ButtonStyle.green)
    async def approve_peak(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        status = schedule_deal(self.deal_id, platform='twitter')
        embed = discord.Embed(title=f"Deal {self.deal_id} Approved", description=status, color=0x00ff00)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label='Post Now', style=discord.ButtonStyle.blurple)
    async def post_now(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        status = schedule_deal(self.deal_id, now=True)
        embed = discord.Embed(title=f"Deal {self.deal_id} Posting Now", description=status, color=0x0099ff)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label='Skip', style=discord.ButtonStyle.grey)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("Deal skipped.", ephemeral=True)

    @discord.ui.button(label='Reject', style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        mark_rejected(self.deal_id)
        embed = discord.Embed(title=f"Deal {self.deal_id} Rejected", color=0xff0000)
        await interaction.followup.send(embed=embed, ephemeral=True)

def load_deals():
    try:
        with open(DEALS_DB, 'r') as f:
            return json.load(f)
    except:
        return []

def save_deals(deals):
    with open(DEALS_DB, 'w') as f:
        json.dump(deals, f)

def get_top_unposted(limit=5):
    deals = load_deals()
    unposted = [d for d in deals if not d.get('is_posted')]
    return sorted(unposted, key=lambda d: d.get('discount_pct', 0), reverse=True)[:limit]

def schedule_deal(deal_id, platform='twitter', now=False):
    # Call Hermes tool or Postiz API
    deal = next((d for d in load_deals() if d['id'] == deal_id), None)
    if not deal:
        return "Deal not found"

    postiz_payload = {
        'type': 'schedule',
        'integration_id': TWITTER_ID if platform == 'twitter' else None,
        'content': f"{deal['title']} {deal['deal_price']} ({deal['discount_pct']}%) {deal['affiliate_url']}",
        'scheduled_at': datetime.now().isoformat() if now else None
    }
    headers = {'Authorization': POSTIZ_KEY}
    resp = requests.post(f"{POSTIZ_URL}/public/v1/posts", json=postiz_payload, headers=headers)
    if resp.status_code == 200:
        deal['is_posted'] = True
        save_deals(load_deals())
        return "Scheduled successfully"
    return f"Error: {resp.text}"

def mark_rejected(deal_id):
    deals = load_deals()
    for d in deals:
        if d['id'] == deal_id:
            d['rejected'] = True
    save_deals(deals)

@client.event
async def on_ready():
    print(f'{client.user} logged in!')
    # Restore persistent views for existing deals
    unposted = get_top_unposted(50)
    for deal in unposted:
        if not deal.get('rejected'):
            view = DealButtonView(deal['id'])
            client.add_view(view)

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if client.user.mentioned_in(message):
        content = message.content.replace(f'<@{client.user.id}>', '').strip()
        if 'status' in content.lower():
            unposted = get_top_unposted(3)
            embed = discord.Embed(title="Quadstar Status", color=0x00ff00)
            for deal in unposted:
                embed.add_field(name=deal['title'][:100], value=f"${deal['deal_price']} ({deal['discount_pct']}%)", inline=False)
            await message.reply(embed=embed)
        elif 'send cards' in content.lower() or 'deals' in content.lower():
            unposted = get_top_unposted(1)
            if unposted:
                deal = unposted[0]
                embed = discord.Embed(title=deal['title'], description=f"${deal['deal_price']} ({deal['discount_pct']}%)", color=0x0099ff)
                embed.set_image(url=deal['image_url'])
                view = DealButtonView(deal['id'])
                await message.reply(embed=embed, view=view)
            else:
                await message.reply("No unposted deals. Scrape first?")

client.run(DISCORD_TOKEN)
