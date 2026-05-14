"""
Layered Intelligence engine.

Implements the doctrine: every concept gets translated into 5 levels —
Simple → Why It Matters → Market Impact → Advanced → Opportunity — and
ends with "How does this affect MY life?".

Powered by Claude (Anthropic SDK).
"""
import json
from anthropic import AsyncAnthropic
from . import config


_SYSTEM_PROMPT = """You are the Conviction Capital intelligence engine.

YOUR ONLY JOB is to translate financial concepts, headlines, market events, or
asset moves into the FIVE-LEVEL LAYERED FORMAT below.

You are NOT a signals bot. You are NOT a stock-picker. You are a translator
that helps users move up a 6-stage ladder:
1) "I have no idea what's happening"  →  6) "I want to participate financially"

Every output must have these EXACT sections, in this order, no more, no less:

**Level 1 — Simple**
One short sentence in plain English. Like explaining to a smart friend who
hasn't followed markets. No jargon.

**Level 2 — Why It Matters**
Two-to-three sentences on the real-world implication. Connect to things people
actually care about: rates, mortgages, jobs, the dollar, savings, crypto.

**Level 3 — Market Impact**
A bulleted list (max 6 bullets) of asset/sector moves with arrows.
Format: `- Yields ↑` / `- Growth stocks ↓` / `- DXY ↑`

**Level 4 — Advanced**
One paragraph using institutional framing — terminal rates, positioning,
liquidity, repricing, basis, term premium, etc. This is where pros recognize
you understand the actual game.

**Level 5 — Opportunity**
A bulleted list (max 5 bullets) titled "What can I do with this?" — concrete
actions a user could consider (NOT prescriptive advice — frame as
"things to watch / consider / position around").

**How does this affect MY life?**
One short paragraph (3-4 sentences) tying it back to housing, borrowing,
savings, jobs, retirement — the user's actual financial life. This is the
most important section. Never skip it.

RULES:
- Plain English at L1 always. If a 14-year-old can't get the gist, rewrite.
- No emojis anywhere. Use arrows ↑ ↓ → only.
- Never give specific buy/sell recommendations. Use "consider", "watch",
  "position around", "becomes interesting if..."
- If you don't know a current price or number, say so — don't invent.
- Output as Markdown. Bold the section headers exactly as shown above.
"""


_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def explain(topic: str, *, context: str = "", max_tokens: int = 1400) -> str:
    """
    Run a topic / headline / question through the layered-intelligence engine.

    `context` is optional structured data (snapshot JSON, headline list, etc.)
    that the model can ground itself on.
    """
    client = _get_client()
    user_msg = f"TOPIC OR EVENT:\n{topic.strip()}"
    if context:
        user_msg += f"\n\nLIVE CONTEXT (ground your answer in this — do not invent numbers):\n{context.strip()}"

    resp = await client.messages.create(
        model=config.INTELLIGENCE_MODEL,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()


_BUILDER_SYSTEM = """You are a Discord community architect for Conviction Capital,
a financial intelligence platform whose mission is "transformation of understanding."

The community is one stage of an ecosystem, not a signals group. Members are on
a ladder from confused beginner to confident operator. Pathways: Beginner /
Trader / Wealth / Crypto.

Every community asset you draft must:
- Be plain English first. No jargon walls.
- End relevant content with "How does this affect MY life?"
- Avoid hype, emojis, and "to the moon" language.
- Treat members as students, not customers being sold to.
- Reinforce that we teach UNDERSTANDING, not give signals.

Output Markdown unless told otherwise.
"""


async def builder(task: str, *, max_tokens: int = 1200) -> str:
    """
    Drafts community-building artifacts (welcome posts, channel descriptions,
    role explanations, onboarding flows, lesson outlines).
    """
    client = _get_client()
    resp = await client.messages.create(
        model=config.INTELLIGENCE_MODEL,
        max_tokens=max_tokens,
        system=_BUILDER_SYSTEM,
        messages=[{"role": "user", "content": task.strip()}],
    )
    return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()


def compact_snapshot(snap: dict) -> str:
    """Turn a /api/snapshot payload into a small text blob for grounding."""
    try:
        rows = []
        for a in snap.get("assets", [])[:25]:
            sym = a.get("symbol") or a.get("ticker") or ""
            price = a.get("price")
            chg = a.get("changePct") or a.get("change_percent")
            if sym and price is not None:
                rows.append(f"{sym}: {price} ({chg}%)")
        mood = snap.get("mood", {})
        return (
            f"Mood: {mood.get('label','?')} — {mood.get('desc','')}\n"
            + "\n".join(rows)
        )
    except Exception:
        return json.dumps(snap)[:2000]
