"""The agent's tools: thin async wrappers around the database and the ops notifications.

SQLite is blocking, so every call runs in a worker thread and never freezes the live call.
All flow files (shipment.py, transport.py, driver.py) go through this file.

* READS get a deadline (DB_READ_TIMEOUT): a stuck read must never keep a caller waiting in silence
  (find_customer runs while the IVR menu plays, and the call only starts once it has finished).
* WRITES get no deadline here: cancelling the wait would not stop the thread, so a write could still land
  while the agent tells the caller it failed. Writes are bounded by database.write_tx instead.
* notify() never raises: a failed ops notification must not turn an already saved request into "save failed".
"""
from __future__ import annotations

import asyncio
import logging
import os

import db_queries as db
from escalation import notify_ops

logger = logging.getLogger("logistics-agent")
READ_TIMEOUT = float(os.getenv("DB_READ_TIMEOUT", "8"))      # seconds


async def _read(fn, *args):
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=READ_TIMEOUT)


async def find_customer(phone: str | None) -> dict | None:
    return await _read(db.get_customer_by_phone, phone)


async def get_shipment(tracking_number: str) -> dict | None:
    return await _read(db.get_shipment_by_tracking, tracking_number)


async def create_customer(company: str, contact: str | None, phone: str | None, lang: str,
                          email: str | None = None) -> int:
    return await asyncio.to_thread(db.create_customer, company, contact, phone, lang, email)


async def update_customer_email(customer_id: int, email: str) -> bool:
    return await asyncio.to_thread(db.update_customer_email, customer_id, email)


async def update_shipment(tracking_number: str, changes: dict) -> bool:
    return await asyncio.to_thread(db.update_shipment_fields, tracking_number, changes)


async def create_transport_request(data: dict) -> int:
    return await asyncio.to_thread(db.create_transport_request, data)


async def report_issue(tracking_number: str, issue: str, waiting_min: int | None, location: str | None) -> bool:
    return await _read(db.report_shipment_issue, tracking_number, issue, waiting_min, location)


async def save_call(record: dict) -> tuple[int, str]:
    return await asyncio.to_thread(db.save_call, record)


async def notify(event: str, payload: dict) -> None:
    try:
        await notify_ops(event, payload)
    except Exception as e:                      # CancelledError is not an Exception: shutdown still works
        logger.warning(f"ops notification '{event}' failed ({type(e).__name__}): {e}")