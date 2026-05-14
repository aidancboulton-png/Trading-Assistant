# Conviction Capital — Discord Agent

A Discord bot that translates everything in your community into the
**5-level layered intelligence format** (Simple → Why → Impact → Advanced →
Opportunity → "How does this affect MY life?") and helps you build the
community itself.

It is **not a signals bot.** It is a translator and a builder. That is the
only thing that justifies a paid membership long-term.

---

## Setup (one-time, ~10 minutes)

### 1. Create the Discord application & bot
You must do this part yourself — I can't create accounts on your behalf.

1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it "Conviction Capital"
3. Sidebar → **Bot** → **Reset Token** → copy the token (this is your `DISCORD_TOKEN`)
4. Under **Privileged Gateway Intents**, leave them all OFF (we use slash commands only)
5. Sidebar → **OAuth2** → **URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`
6. Open the generated URL in a browser and add the bot to your server.

### 2. Get the channel & guild IDs
In Discord: User Settings → Advanced → **Developer Mode** ON.
Right-click your server icon → **Copy Server ID** (this is `DISCORD_GUILD_ID`).
Right-click the channel you want briefings posted to → **Copy Channel ID**
(this is `DISCORD_BRIEFING_CHANNEL_ID`).

### 3. Set environment variables
Locally, `export` them or put them in a `.env` you load before running.
On Railway, add them in **Variables**.

| Var | Required | Notes |
|---|---|---|
| `DISCORD_TOKEN` | yes | from step 1.3 |
| `ANTHROPIC_API_KEY` | yes | from console.anthropic.com |
| `DISCORD_GUILD_ID` | recommended | makes slash commands appear instantly instead of taking up to 1h |
| `DISCORD_BRIEFING_CHANNEL_ID` | recommended | enables the auto pre-market & wrap posts |
| `BACKEND_URL` | optional | defaults to `http://127.0.0.1:8000` — set to your Railway URL in prod |
| `INTELLIGENCE_MODEL` | optional | defaults to `claude-opus-4-6` |
| `PREMARKET_HOUR_ET` | optional | default 7 |
| `WRAP_HOUR_ET` / `WRAP_MINUTE_ET` | optional | default 16:30 |

### 4. Install & run
```bash
pip install -r requirements.txt
python run_bot.py
```

For production on Railway: add a second service pointing at the same repo
with `python run_bot.py` as the start command (don't run it inside the web
service — they're separate processes).

---

## Slash commands

### Member-facing (intelligence)
- `/explain <topic>` — layered explanation of any market topic, headline, or move. Auto-grounded on live snapshot data.
- `/today` — what's happening in markets right now, layered.
- `/myimpact <topic>` — heavy emphasis on "how does this affect MY life?"
- `/pathway <Beginner|Trader|Wealth|Crypto> [topic]` — next lesson on a path.

### Aidan-facing (builder)
- `/draft <Welcome|Channel|Role|Rules|Pinned> <purpose>` — drafts community copy in CC voice.
- `/lesson <concept>` — full layered lesson on any concept.
- `/onboard` — drafts a 5-step onboarding flow.

### Automatic
- **Pre-market briefing** posted at 7am ET in `DISCORD_BRIEFING_CHANNEL_ID`
- **Market wrap** posted at 4:30pm ET in same channel

---

## Architecture
```
discord_bot/
  config.py          env loading
  data_client.py     httpx client to existing FastAPI (/api/snapshot etc.)
  intelligence.py    Claude-powered layered translator + builder
  digest.py          scheduled briefings (pre-market & wrap)
  bot.py             Discord client + slash command handlers
run_bot.py           entrypoint
```

The bot reuses the existing FastAPI backend for live data — single source of
truth. If `BACKEND_URL` is unreachable, intelligence commands still work
(they just won't be grounded on live numbers).
