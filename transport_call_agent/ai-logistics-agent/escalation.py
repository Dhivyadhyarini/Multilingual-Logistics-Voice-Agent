"""Urgency check, notifying the operations team, and handing the caller to a human."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path

from livekit import api
from dotenv import load_dotenv
load_dotenv()
OPERATIONS_PHONE = os.getenv("OPERATIONS_PHONE", "").strip()
TRANSFER_DESTINATION = os.getenv("TRANSFER_DESTINATION", "").strip()

logger = logging.getLogger("logistics-agent")

OPERATIONS_PHONE = os.getenv("OPERATIONS_PHONE", "").strip()      # e.g. +48297430880 ; empty = no live transfer
OPS_WEBHOOK_URL = os.getenv("OPS_WEBHOOK_URL", "").strip()        # optional Slack / Teams style webhook
NOTIFY_FILE = Path("calls") / "ops_notifications.jsonl"
URGENT_WAIT_MIN = int(os.getenv("URGENT_WAIT_MINUTES", "90"))
WAIT_NORMAL_MIN = 30

# Words that make an issue urgent (English / German / Polish)
URGENT_RE = re.compile(
    r"\b(accident|crash|injur\w*|police|fire|stolen|theft|broke\s?down|breakdown|damaged|leak\w*|"
    r"temperature (alarm|excursion|problem)|cooling (failed|failure|broken)|fridge|customs|"
    r"unfall|panne|polizei|gestohlen|diebstahl|beschädigt|kühlung|zoll|"
    r"wypadek|awaria|policja|skradzion\w*|kradzież|uszkodz\w*|chłodni\w*|temperatur\w* (alarm|problem)|cło)\b",
    re.I)


def assess_urgency(issue_text: str | None, waiting_min: int | None) -> str:
    """Returns 'low' | 'normal' | 'high'."""
    if issue_text and URGENT_RE.search(issue_text):
        return "high"
    if waiting_min is not None and waiting_min >= URGENT_WAIT_MIN:
        return "high"
    if waiting_min is not None and waiting_min >= WAIT_NORMAL_MIN:
        return "normal"
    return "low"


def _post_webhook(text: str) -> None:
    req = urllib.request.Request(OPS_WEBHOOK_URL, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=8).read()


async def notify_ops(event: str, payload: dict) -> None:
    """Tell the operations team. Always written to a file; also sent to a webhook if OPS_WEBHOOK_URL is set."""
    record = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, **payload}
    def _append() -> None:                      # disk I/O in a worker thread: never stall the live audio loop
        NOTIFY_FILE.parent.mkdir(exist_ok=True)
        with NOTIFY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        await asyncio.to_thread(_append)
    except OSError as e:
        logger.warning(f"could not write ops notification: {e}")
    logger.info(f"OPS NOTIFY: {record}")
    if OPS_WEBHOOK_URL:
        try:
            await asyncio.to_thread(_post_webhook, f"[{event}] " + json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"ops webhook failed: {e}")


def can_transfer(state) -> bool:
    return bool(OPERATIONS_PHONE and state.sip_identity)


async def transfer_to_human(ctx, state) -> bool:
    """Move the caller to the operations phone (SIP REFER). Returns True on success."""
    if not can_transfer(state):
        return False
    try:
        await ctx.api.sip.transfer_sip_participant(
            api.TransferSIPParticipantRequest(
                room_name=ctx.room.name,
                participant_identity=state.sip_identity,
                transfer_to=TRANSFER_DESTINATION or f"tel:{OPERATIONS_PHONE}",
                play_dialtone=False,
            ))
        return True
    except Exception as e:
        logger.warning(
            f"SIP transfer failed | "
            f"room={ctx.room.name} | "
            f"participant={state.sip_identity} | "
            f"destination={TRANSFER_DESTINATION or f'tel:{OPERATIONS_PHONE}'} | "
            f"error={e}"
        )
        return False