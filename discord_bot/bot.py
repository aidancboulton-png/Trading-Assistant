"""
Conviction Capital Discord bot.

Slash commands fall into two buckets:

  Intelligence (member-facing) — turn live data / events / concepts into the
  5-level layered format.
    /explain       any topic, headline, or asset move
    /today         what happened in markets today (live data grounded)
    /myimpact      "how does this affect MY life?" framing
    /pathway       show next learning step on a chosen pathway

  Builder (Aidan-facing) — help build the community out.
    /draft         draft a welcome / channel description / role copy
    /lesson        layered-intelligence lesson on any topic
    /onboard       onboarding flow draft
"""
import discord
from discord import app_commands

from . import config, data_client, digest, intelligence


intents = discord.Intents.default()
intents.message_content = False  # not needed for slash commands

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def _send_long(interaction: discord.Interaction, text: str) -> None:
    """Discord cap is 2000 chars; chunk and follow up."""
    chunks = list(digest._chunk(text, 1900))
    if not chunks:
        await interaction.followup.send("(empty response)")
        return
    await interaction.followup.send(chunks[0])
    for c in chunks[1:]:
        await interaction.followup.send(c)


# ── Intelligence commands ────────────────────────────────────────────────────

@tree.command(name="explain", description="Layered explanation of any market topic, headline, or move")
@app_commands.describe(topic="What do you want explained? (e.g. 'CPI came in hot', 'why is gold up')")
async def cmd_explain(interaction: discord.Interaction, topic: str):
    await interaction.response.defer(thinking=True)
    try:
        snap = await data_client.snapshot()
        ctx = intelligence.compact_snapshot(snap)
        text = await intelligence.explain(topic, context=ctx)
        await _send_long(interaction, text)
    except Exception as e:
        await interaction.followup.send(f"Engine error: `{e}`")


@tree.command(name="today", description="What's happening in markets right now — layered")
async def cmd_today(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        snap = await data_client.snapshot()
        news = await data_client.news()
        items = news.get("articles") or news.get("items") or []
        heads = "\n".join(f"- {n.get('title') or n.get('headline')}" for n in items[:10] if (n.get('title') or n.get('headline')))
        ctx = intelligence.compact_snapshot(snap) + "\n\nHEADLINES:\n" + heads
        text = await intelligence.explain(
            "What is the most important thing happening in markets right now? "
            "Run it through the 5-level layered format.",
            context=ctx,
        )
        await _send_long(interaction, text)
    except Exception as e:
        await interaction.followup.send(f"Engine error: `{e}`")


@tree.command(name="myimpact", description="How does a topic actually affect YOUR life?")
@app_commands.describe(topic="Topic, headline, or asset (e.g. 'rising bond yields', 'BTC at 70k')")
async def cmd_myimpact(interaction: discord.Interaction, topic: str):
    await interaction.response.defer(thinking=True)
    try:
        text = await intelligence.explain(
            f"Run '{topic}' through the layered format, but spend MOST of the "
            "output on the 'How does this affect MY life?' section. Mortgages, "
            "savings, jobs, retirement, day-to-day cost of living."
        )
        await _send_long(interaction, text)
    except Exception as e:
        await interaction.followup.send(f"Engine error: `{e}`")


@tree.command(name="pathway", description="Get your next lesson on a learning pathway")
@app_commands.describe(track="Which path?", topic="Optional — what you want to learn next")
@app_commands.choices(track=[
    app_commands.Choice(name="Beginner", value="beginner"),
    app_commands.Choice(name="Trader",   value="trader"),
    app_commands.Choice(name="Wealth",   value="wealth"),
    app_commands.Choice(name="Crypto",   value="crypto"),
])
async def cmd_pathway(interaction: discord.Interaction, track: app_commands.Choice[str], topic: str = ""):
    await interaction.response.defer(thinking=True)
    try:
        prompt = (
            f"User is on the '{track.value}' pathway. "
            + (f"They want to learn about: {topic}. " if topic else "Pick the foundational next topic for them. ")
            + "Teach it in the 5-level layered format."
        )
        text = await intelligence.explain(prompt)
        await _send_long(interaction, text)
    except Exception as e:
        await interaction.followup.send(f"Engine error: `{e}`")


# ── Builder commands ─────────────────────────────────────────────────────────

@tree.command(name="draft", description="Draft community copy (welcome / channel desc / role)")
@app_commands.describe(asset_type="What to draft", purpose="What it's for")
@app_commands.choices(asset_type=[
    app_commands.Choice(name="Welcome message",     value="welcome"),
    app_commands.Choice(name="Channel description", value="channel"),
    app_commands.Choice(name="Role description",    value="role"),
    app_commands.Choice(name="Rules / guidelines",  value="rules"),
    app_commands.Choice(name="Pinned post",         value="pinned"),
])
async def cmd_draft(interaction: discord.Interaction, asset_type: app_commands.Choice[str], purpose: str):
    await interaction.response.defer(thinking=True)
    try:
        text = await intelligence.builder(
            f"Draft a {asset_type.name.lower()} for: {purpose}\n\n"
            "Voice: Conviction Capital — institutional but plain English, no hype, "
            "no emojis. Make members feel they're entering a place that respects "
            "their time and intelligence."
        )
        await _send_long(interaction, text)
    except Exception as e:
        await interaction.followup.send(f"Engine error: `{e}`")


@tree.command(name="lesson", description="Generate a layered lesson on any concept")
@app_commands.describe(concept="What to teach (e.g. 'bond yields', 'options gamma', 'estate planning')")
async def cmd_lesson(interaction: discord.Interaction, concept: str):
    await interaction.response.defer(thinking=True)
    try:
        text = await intelligence.explain(
            f"Teach the concept: '{concept}'. Use the 5-level layered format. "
            "End with 'How does this affect MY life?'."
        )
        await _send_long(interaction, text)
    except Exception as e:
        await interaction.followup.send(f"Engine error: `{e}`")


@tree.command(name="onboard", description="Draft a full onboarding flow for new members")
async def cmd_onboard(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        text = await intelligence.builder(
            "Draft a 5-step onboarding flow for new Conviction Capital members. "
            "Each step should: (1) feel like unlocking a level, (2) reinforce we "
            "teach understanding, not signals, (3) point to one of the four "
            "pathways (Beginner / Trader / Wealth / Crypto), (4) end with a clear "
            "'next action' button label. Output as numbered steps with a title, "
            "body, and CTA per step."
        )
        await _send_long(interaction, text)
    except Exception as e:
        await interaction.followup.send(f"Engine error: `{e}`")


# ── Lifecycle ────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    print(f"[bot] logged in as {client.user}")
    try:
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=int(config.DISCORD_GUILD_ID))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
        else:
            synced = await tree.sync()
        print(f"[bot] synced {len(synced)} slash commands")
    except Exception as e:
        print(f"[bot] command sync failed: {e}")
    client.loop.create_task(digest.schedule_loop(client))


def run() -> None:
    config.assert_configured()
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    run()
