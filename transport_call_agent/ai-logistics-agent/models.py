"""Data shapes: the SQLite schema, the per-call state, and the base class every flow uses.

No LiveKit code in here, so it can be tested without any phone setup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from utils import LOCATION_KEYS, plausible_place

# ---------------------------------------------------------------------------
# SQLite schema (4 simple tables, as in the infographic)
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name       TEXT NOT NULL,
    contact_name       TEXT,
    phone              TEXT,
    email              TEXT,
    country            TEXT,
    preferred_language TEXT DEFAULT 'en'
);
CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone);

CREATE TABLE IF NOT EXISTS shipments (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_number        TEXT UNIQUE NOT NULL,
    customer_id            INTEGER REFERENCES customers(id),
    pickup_location        TEXT,
    delivery_location      TEXT,
    cargo_type             TEXT,
    weight_kg              REAL,
    temperature_controlled INTEGER DEFAULT 0,
    temperature_min_c      REAL,
    temperature_max_c      REAL,
    adr                    INTEGER DEFAULT 0,
    adr_class              TEXT,
    status                 TEXT DEFAULT 'new',      -- new | in_transit | delayed | delivered
    current_location       TEXT,
    eta_start              TEXT,                    -- ISO datetime (start of delivery window)
    eta_end                TEXT,                    -- ISO datetime (end of delivery window)
    driver_name            TEXT,
    driver_phone           TEXT,
    issue_status           TEXT DEFAULT 'none',     -- none | reported
    issue_text             TEXT,
    issue_waiting_min      INTEGER,
    issue_reported_at      TEXT
);

CREATE TABLE IF NOT EXISTS transport_requests (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id            INTEGER REFERENCES customers(id),
    request_type           TEXT DEFAULT 'quote',    -- quote | booking
    pickup_location        TEXT,
    delivery_location      TEXT,
    cargo_type             TEXT,
    weight_kg              REAL,
    temperature_controlled INTEGER DEFAULT 0,
    temperature_min_c      REAL,
    temperature_max_c      REAL,
    adr                    INTEGER DEFAULT 0,
    adr_class              TEXT,
    pickup_date            TEXT,
    delivery_date          TEXT,
    special_requirements   TEXT,
    status                 TEXT DEFAULT 'new',      -- new | in_progress | quoted
    created_at             TEXT
);

CREATE TABLE IF NOT EXISTS calls (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    call_code            TEXT UNIQUE,               -- CALL-001 (also the JSON file name)
    phone_number         TEXT,
    call_type            TEXT,                      -- track | request | issue
    language             TEXT,
    customer_id          INTEGER REFERENCES customers(id),
    shipment_id          INTEGER REFERENCES shipments(id),
    transport_request_id INTEGER REFERENCES transport_requests(id),
    transcript           TEXT,
    summary              TEXT,
    urgency              TEXT DEFAULT 'low',        -- low | normal | high
    escalated            INTEGER DEFAULT 0,
    outcome              TEXT,
    started_at           TEXT,
    ended_at             TEXT
);
"""


# ---------------------------------------------------------------------------
# Flow building blocks
# ---------------------------------------------------------------------------
@dataclass
class Slot:
    """One piece of information a flow must collect from the caller."""
    id: str
    kind: str          # text | yesno | number | date | tracking | minutes | range | email
    expect: str        # plain-English description for the LLM classifier
    required_if: Callable[["CallState"], bool] = lambda s: True
    optional: bool = False   # ONLY optional slots may be skipped; mandatory ones are asked again


@dataclass
class FlowResult:
    messages: list[str] = field(default_factory=list)  # things to say once the flow is done
    transfer: bool = False                             # hand the caller to a human right now
    next_stage: str | None = None                      # "manage" = keep talking about the shipment just read


class BaseFlow:
    name = ""          # track | request | issue
    call_type = ""
    slots: list[Slot] = []
    needs_confirmation = False   # True: read a summary and wait for YES before finish() saves anything

    @staticmethod
    def _keys(slot: Slot) -> tuple:
        return ("temperature_min_c", "temperature_max_c") if slot.kind == "range" else (slot.id,)

    def slot_done(self, slot: Slot, data: dict) -> bool:
        if slot.kind == "range":
            return "temperature_min_c" in data and "temperature_max_c" in data
        return slot.id in data

    def open_slots(self, state: "CallState") -> list[Slot]:
        return [s for s in self.slots
                if s.required_if(state) and not self.slot_done(s, state.data)]

    def required_missing(self, state: "CallState") -> list[Slot]:
        """Mandatory slots still empty. finish() must never run while this is non-empty."""
        return [s for s in self.open_slots(state) if not s.optional]

    def slot_for_key(self, key: str) -> Slot | None:
        for s in self.slots:
            if s.id == key or (s.kind == "range" and key in ("temperature_min_c", "temperature_max_c")):
                return s
        return None

    def prune(self, state: "CallState") -> None:
        """Drop answers that stopped being relevant after a correction (e.g. 'not temperature controlled')."""
        for s in self.slots:
            if not s.required_if(state):
                for k in self._keys(s):
                    state.data.pop(k, None)
                state.field_status.pop(s.id, None)

    def audit(self, state: "CallState") -> list:
        """Remove stored values that cannot be right (a 'place' that is really 'But I'm') so they are asked again.
        This is the safety net: whatever extractor produced the value, garbage can never reach the DB."""
        bad = []
        for k in LOCATION_KEYS:
            if k in state.data and not plausible_place(state.data[k]):
                state.data.pop(k, None)
                state.field_status.pop(k, None)
                bad.append(k)
        return bad

    def prefill(self, state: "CallState") -> None:
        """Hook: fill slots from what we already know (e.g. the customer's e-mail on file)."""

    def field_report(self, state: "CallState") -> dict:
        """collected / missing / invalid / corrected / confirmed, for logging and the JSON file."""
        rep = {"collected": [], "missing": [], "invalid": [], "corrected": [], "confirmed": []}
        for s in self.slots:
            if not s.required_if(state):
                continue
            status = state.field_status.get(s.id)
            if self.slot_done(s, state.data):
                rep["collected"].append(s.id)
                if status in ("corrected", "confirmed"):
                    rep[status].append(s.id)
            else:
                rep["missing"].append(s.id)
                if status == "invalid":
                    rep["invalid"].append(s.id)
        return rep

    def mark_confirmed(self, state: "CallState") -> None:
        for s in self.slots:
            if s.required_if(state) and self.slot_done(s, state.data):
                state.field_status[s.id] = "confirmed"

    def summary(self, state: "CallState") -> str:
        """Spoken read-back ending in a yes/no question. Only used when needs_confirmation is True."""
        return ""

    async def validate(self, slot: Slot, state: "CallState") -> str | None:
        """Called right after a slot is filled. Return a message to reject the value, or None to accept."""
        return None

    async def finish(self, state: "CallState", ctx=None) -> FlowResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Call state
# ---------------------------------------------------------------------------
@dataclass
class CallState:
    phone: str | None = None
    sip_identity: str | None = None
    lang: str = "en"
    flow: BaseFlow | None = None
    data: dict = field(default_factory=dict)       # answers for the CURRENT flow
    flows_done: list = field(default_factory=list)
    call_type: str = ""                            # first flow chosen: track | request | issue
    stage: str = "ivr"                             # ivr | slots | confirm | manage | more
    log: list = field(default_factory=list)        # transcript, both sides
    outcome: str = "in_progress"
    live: bool = False                             # True once the IVR is finished
    ending: bool = False
    silence_prompts: int = 0
    attempts: dict = field(default_factory=dict)
    counters: dict = field(default_factory=dict)
    turns: int = 0
    ack_i: int = 0
    last_prompt: str = ""
    # per-field tracking for the CURRENT flow: slot id -> collected | invalid | corrected | confirmed
    field_status: dict = field(default_factory=dict)
    confirmed: bool = False                        # caller said YES to the summary
    pending: dict = field(default_factory=dict)    # shipment changes waiting for the caller's YES (DB column -> value)
    # linked records
    customer: dict | None = None
    shipment: dict | None = None
    transport_request_id: int | None = None
    # urgency / escalation
    urgency: str = "low"
    escalated: bool = False
    # everything collected, kept for the JSON file
    collected: list = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)