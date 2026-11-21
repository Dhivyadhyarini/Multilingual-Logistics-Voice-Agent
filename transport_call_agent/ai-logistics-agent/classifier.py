"""The one LLM call per turn: a direct Groq request that returns raw text for agent._classify() to parse.

Why a direct client instead of LiveKit's llm.chat() stream:
  * LiveKit wraps every Groq 400 ("Failed to validate JSON") inside a generic "Connection error", so the real
    reason (and Groq's `failed_generation`, which is usually fine JSON) was lost.
  * Breaking out of a LiveKit stream early could raise inside the pipeline.
  * The OpenAI SDK retries twice by default, each retry costing a full timeout. Here max_retries=0.

The client is created ONCE at import time (certificate loading is slow and must not happen during a call).
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
logger = logging.getLogger("logistics-agent")

_KEY = os.getenv("GROQ_API_KEY")
MODEL = os.getenv("CLASSIFY_MODEL", "openai/gpt-oss-120b")    # layer 9: Groq gpt-oss-120b
MAX_TOKENS = int(os.getenv("CLASSIFY_MAX_TOKENS", "700"))      # gpt-oss counts its (short) reasoning in here
HTTP_TIMEOUT = float(os.getenv("CLASSIFY_TIMEOUT", "6")) + 1.0

_client = (
    AsyncOpenAI(api_key=_KEY, base_url="https://api.groq.com/openai/v1", timeout=HTTP_TIMEOUT, max_retries=0)
    if _KEY else None
)

_PLAIN_SUFFIX = "\nReply with the JSON object only. No markdown, no explanation."
_EXTRA = {"reasoning_effort": "low"} if "gpt-oss" in MODEL else {}

async def complete(system: str, user: str, json_mode: bool = True) -> str:
    """Return the model's raw text. Raises the SDK's own exception on failure (status_code / body intact)."""
    if _client is None:
        raise RuntimeError("Missing GROQ_API_KEY in .env")
    kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    resp = await _client.chat.completions.create(
        model=MODEL,
        temperature=0,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": system if json_mode else system + _PLAIN_SUFFIX},
            {"role": "user", "content": user},
        ],
        extra_body=_EXTRA,
        **kwargs,
    )
    return resp.choices[0].message.content or ""


class _Warmer:
    async def prewarm(self) -> None:
        if _client is None:
            return
        await _client.chat.completions.create(
            model=MODEL, max_tokens=16, extra_body=_EXTRA,
            messages=[{"role": "user", "content": "hi"}])


warmer = _Warmer()