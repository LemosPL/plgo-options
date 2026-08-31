"""Slack delivery for the Action Radar brief and proximity pings.

Deliberately an incoming webhook rather than a Slack app: the brief is one-way,
so there's no OAuth, no bot token, no scopes to manage — a single URL held in
Secret Manager. If we later want to reply to the bot, that's when an app earns
its keep.

Never raises. A Slack outage must not take down the endpoint that generated the
brief — the brief is still recorded in the journal and readable in the UI.
"""

from __future__ import annotations

import httpx

from plgo_options.config import SLACK_WEBHOOK_URL

TIMEOUT = 10.0

# Slack rejects payloads over 40k; blocks cap at 3k chars of text each.
MAX_TEXT = 38_000


async def post_message(text: str, webhook_url: str | None = None) -> dict:
    """Post ``text`` (Slack mrkdwn) to the webhook.

    Returns ``{"delivered": bool, "detail": str}`` — the caller reports this
    rather than treating a delivery failure as a request failure.
    """
    url = (webhook_url or SLACK_WEBHOOK_URL or "").strip()
    if not url:
        return {"delivered": False, "detail": "SLACK_WEBHOOK_URL not configured"}
    if not text.strip():
        return {"delivered": False, "detail": "empty message"}

    body = text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n…(truncated)"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, json={"text": body, "mrkdwn": True})
        if resp.status_code == 200 and resp.text.strip() in ("ok", ""):
            return {"delivered": True, "detail": "ok"}
        return {"delivered": False,
                "detail": f"slack returned {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:                       # network, DNS, timeout, ...
        return {"delivered": False, "detail": f"{type(e).__name__}: {e}"}


def is_configured() -> bool:
    return bool((SLACK_WEBHOOK_URL or "").strip())
