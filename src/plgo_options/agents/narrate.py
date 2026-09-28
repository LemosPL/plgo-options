"""Claude turns finished numbers into a short desk note. Never computes.

Same split as brief_composer: code produces every number; the model only writes
the prose around them and may not add figures. If the API is unconfigured or
fails, the deterministic text goes out unchanged.
"""

from __future__ import annotations

from plgo_options.config import ANTHROPIC_API_KEY, ANTHROPIC_BRIEF_MODEL

SYSTEM = (
    "You write internal desk notes for an ETH and FIL options book run by Lucas (London) "
    "and Chris (New York) under a written operating manual. You receive a FACTS block "
    "produced by code. Rewrite it as a crisp note for a trader at a screen: lead with what "
    "needs doing now and by whom, then the numbers. Rules: use ONLY numbers that appear in "
    "FACTS, copied exactly; never invent prices, sizes or recommendations; never suggest an "
    "option trade that FACTS does not already list; keep every 'REJECTED' and 'Chris' routing "
    "exactly as given; plain text with short lines, no markdown tables; under 250 words."
)


async def narrate(agent: str, facts: str, use_ai: bool = True) -> tuple[str, str | None]:
    """Returns (text, error). Falls back to the facts on any problem."""
    if not use_ai or not ANTHROPIC_API_KEY:
        return facts, None if not use_ai else "ANTHROPIC_API_KEY not set"
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        resp = await client.messages.create(
            model=ANTHROPIC_BRIEF_MODEL,
            max_tokens=900,
            system=SYSTEM,
            messages=[{"role": "user", "content": f"AGENT: {agent}\n\nFACTS:\n{facts}"}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        if not text:
            return facts, "empty model response"
        # The facts ride underneath so every number is auditable in the same message.
        return f"{text}\n\n---\n{facts}", None
    except Exception as e:
        return facts, f"{type(e).__name__}: {e}"[:300]
