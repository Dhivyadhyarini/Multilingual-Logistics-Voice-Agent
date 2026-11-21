"""One short English summary of each call, written by Groq. Falls back to a plain summary if the LLM fails."""
from __future__ import annotations

import asyncio
import logging
import os
import ssl

import certifi
import httpx
from openai import AsyncOpenAI

logger = logging.getLogger("logistics-agent")

SUMMARY_TIMEOUT = float(os.getenv("SUMMARY_TIMEOUT", "10"))      # seconds; the call is saved either way

# The expensive part (loading the CA bundle) is done ONCE at import and is loop-independent.
# The HTTP client itself is created per call, inside the running loop, and closed afterwards:
# a client created at import time is bound to a different (closed) event loop -> "Event loop is closed".
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def fallback_summary(state) -> str:
    kinds = {"track": "shipment tracking", "request": "new transport request", "issue": "driver issue"}
    parts = [f"Caller used the {kinds.get(state.call_type, 'inbound')} line in {state.lang.upper()}."]
    for item in state.collected:
        if item["flow"] == "track":
            parts.append(f"Tracked {item['tracking_number']} (status: {item['status']}).")
        elif item["flow"] == "request":
            parts.append(f"Request {item['request']}: {item.get('pickup_location')} to {item.get('delivery_location')}, "
                         f"{item.get('cargo_type')}, {item.get('weight_kg')} kg.")
        elif item["flow"] == "update":
            parts.append(f"Updated {item['shipment']}: {', '.join(item['changes'])}.")
        elif item["flow"] == "issue":
            parts.append(f"Issue on {item['shipment']}: {item['issue']} (urgency {item['urgency']}).")
    parts.append(f"Outcome: {state.outcome}.")
    return " ".join(parts)


async def _call_llm(state, key: str) -> str:
    transcript = "\n".join(f"{m['role']}: {m['text']}" for m in state.log)
    async with httpx.AsyncClient(verify=_SSL_CTX, timeout=httpx.Timeout(SUMMARY_TIMEOUT)) as http:
        client = AsyncOpenAI(api_key=key, base_url="https://api.groq.com/openai/v1",
                             http_client=http, max_retries=0)
        resp = await client.chat.completions.create(
            model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            temperature=0,
            messages=[
                {"role": "system", "content":
                    "Summarise this logistics phone call in English in 2-3 short sentences: what the caller "
                    "wanted, the key details (tracking number, route, cargo, issue), and the result. "
                    "Use only facts from the transcript."},
                {"role": "user", "content": transcript[:12000]},
            ])
    return (resp.choices[0].message.content or "").strip()


async def summarise(state) -> str:
    key = os.getenv("GROQ_API_KEY")
    if not key or not state.log:
        return fallback_summary(state)
    try:
        # hard deadline: a slow LLM must never hold up the end of the call
        text = await asyncio.wait_for(_call_llm(state, key), timeout=SUMMARY_TIMEOUT + 2)
        return text or fallback_summary(state)
    except Exception as e:                      # CancelledError is not an Exception: shutdown still works
        logger.warning(f"summary LLM failed ({type(e).__name__}), using plain summary: {e}")
        return fallback_summary(state)