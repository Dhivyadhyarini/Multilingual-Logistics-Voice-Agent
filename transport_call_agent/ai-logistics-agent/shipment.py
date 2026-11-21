"""Flow 1: Track a shipment - read it out, then keep talking about it: questions, changes, missing info."""
from __future__ import annotations

from language import (AND, CHANGE_TPL, FIELD_LABELS, STATUS, YESNO_WORDS, fmt_eta, fmt_num, t)
from models import BaseFlow, FlowResult, Slot
from utils import detect_field, is_question, normalize_value, spell
import re
import tools
from config import MAX_WEIGHT_KG

# What a caller may change on an existing shipment (ids = shipment columns, except the temperature range)
EDIT_FIELDS = [
    Slot("pickup_location", "text", "the pickup city or address (only the place name)"),
    Slot("delivery_location", "text", "the delivery city / destination / drop-off (only the place name)"),
    Slot("cargo_type", "text", "the type of cargo"),
    Slot("weight_kg", "number", "the weight in kilograms"),
    Slot("temperature_controlled", "yesno", "whether the cargo is temperature-controlled"),
    Slot("temperature_range", "range", "temperature range in Celsius; fill temperature_min_c and temperature_max_c"),
    Slot("adr", "yesno", "whether it is ADR dangerous goods"),
    Slot("adr_class", "text", "the ADR class"),
]
_NUM_KEYS = ("temperature_min_c", "temperature_max_c")


def edit_slot_for_key(key: str) -> Slot | None:
    for s in EDIT_FIELDS:
        if s.id == key or (s.kind == "range" and key in _NUM_KEYS):
            return s
    return None


def current_values(s: dict) -> dict:
    """Current values of the editable fields (None = not on file), keyed like the LLM 'values'."""
    return {f.id: s.get(f.id) for f in EDIT_FIELDS if f.kind != "range"} | {
        "temperature_min_c": s.get("temperature_min_c"), "temperature_max_c": s.get("temperature_max_c")}


def shipment_facts(s: dict) -> str:
    """Plain facts the LLM may use to answer a question about this shipment (nothing else)."""
    rows = [("tracking number", s["tracking_number"]), ("status", s["status"]),
            ("pickup", s["pickup_location"]), ("delivery", s["delivery_location"]),
            ("current location", s["current_location"]), ("cargo", s["cargo_type"]),
            ("weight kg", s["weight_kg"]),
            ("temperature controlled", bool(s["temperature_controlled"])),
            ("temperature range C", f"{s['temperature_min_c']} to {s['temperature_max_c']}"
             if s["temperature_min_c"] is not None else None),
            ("ADR", f"yes, class {s['adr_class']}" if s["adr"] else "no"),
            ("delivery window start", s["eta_start"]), ("delivery window end", s["eta_end"]),
            ("issue reported", s["issue_text"])]
    return "; ".join(f"{k}: {v}" for k, v in rows if v not in (None, ""))


def missing_labels(lang: str, s: dict) -> list[str]:
    """Details not on file yet, so the caller can add them."""
    L = FIELD_LABELS[lang]
    out = [L[k] for k in ("pickup_location", "delivery_location", "cargo_type", "weight_kg") if s.get(k) in (None, "")]
    if s.get("temperature_controlled") and s.get("temperature_min_c") is None:
        out.append(L["temperature_range"])
    return out


def prepare_changes(shipment: dict, values: dict) -> tuple[dict, list]:
    """LLM values -> {db column: clean value} for fields that really change. Returns (changes, unusable keys)."""
    changes: dict = {}
    bad: list = []
    for key, raw in values.items():
        slot = edit_slot_for_key(key)
        if slot is None:
            continue
        try:
            val = normalize_value("number" if key in _NUM_KEYS else slot.kind, raw, key)
        except Exception:
            val = None
        if val is None or (key == "weight_kg" and not 0 < val <= MAX_WEIGHT_KG):
            bad.append(key)
            continue
        changes[key] = val
    if "temperature_min_c" in changes or "temperature_max_c" in changes:     # combine with the value on file
        lo = changes.get("temperature_min_c", shipment.get("temperature_min_c"))
        hi = changes.get("temperature_max_c", shipment.get("temperature_max_c"))
        if lo is not None and hi is not None:
            changes["temperature_min_c"], changes["temperature_max_c"] = min(lo, hi), max(lo, hi)
            changes.setdefault("temperature_controlled", True)
        else:
            changes.pop("temperature_min_c", None)
            changes.pop("temperature_max_c", None)
            bad.append("temperature_range")
    if changes.get("temperature_controlled") is False:
        changes["temperature_min_c"] = changes["temperature_max_c"] = None
    if changes.get("adr") is False:
        changes["adr_class"] = None
    same = {k for k, v in changes.items() if _norm(shipment.get(k)) == _norm(v)}
    if not set(_NUM_KEYS) <= same:                  # the range is one change: keep both ends if either moved
        same -= set(_NUM_KEYS)
    return {k: v for k, v in changes.items() if k not in same}, bad


def _norm(v):
    return bool(v) if isinstance(v, bool) else (float(v) if isinstance(v, (int, float)) else v)


def _fmt(lang: str, key: str, v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool) or key in ("temperature_controlled", "adr"):
        return YESNO_WORDS[lang][0 if v else 1]
    return fmt_num(v) if isinstance(v, (int, float)) else str(v)


def describe_changes(lang: str, shipment: dict, changes: dict) -> str:
    """'change the delivery location from Berlin to Guindy and set the weight to 800'."""
    L, (with_old, no_old) = FIELD_LABELS[lang], CHANGE_TPL[lang]
    parts = []
    for key in changes:
        if key == "temperature_max_c" and "temperature_min_c" in changes:
            continue
        if key in _NUM_KEYS:
            label = L["temperature_range"]
            old = (f"{fmt_num(shipment['temperature_min_c'])} - {fmt_num(shipment['temperature_max_c'])}"
                   if shipment.get("temperature_min_c") is not None else None)
            new = "-" if changes.get("temperature_min_c") is None else \
                f"{fmt_num(changes['temperature_min_c'])} - {fmt_num(changes['temperature_max_c'])}"
        else:
            label, old = L[key], shipment.get(key)
            new = _fmt(lang, key, changes[key])
            old = None if old in (None, "") else _fmt(lang, key, old)
        parts.append(with_old.format(f=label, old=old, new=new) if old else no_old.format(f=label, new=new))
    return AND[lang].join([", ".join(parts[:-1]), parts[-1]] if len(parts) > 1 else parts)


def status_line(lang: str, s: dict) -> str:
    """The spoken status of a shipment (also used to answer 'where is it / when will it arrive' without the LLM)."""
    status = STATUS[lang].get(s["status"], s["status"])
    if s["status"] == "delivered":
        return t(lang, "track_delivered", tn=spell(s["tracking_number"]),
                 location=s["current_location"] or s["delivery_location"], eta=fmt_eta(lang, None, s["eta_end"]))
    return t(lang, "track_found", tn=spell(s["tracking_number"]), status=status,
             location=s["current_location"] or "-", eta=fmt_eta(lang, s["eta_start"], s["eta_end"]))


_WHEN = re.compile(r"\b(when|eta|arriv\w*|how\s+long|wann|kiedy|ankunft|dostaw\w*\s+czas)\b", re.I)
_STATUS = re.compile(r"\b(status|where|location|delay\w*|on\s+time|wo|standort|gdzie)\b", re.I)
_FACT = {"en": "The {f} is {v}.", "de": "{f}: {v}.", "pl": "{f}: {v}."}
_KG = {"en": " kilograms", "de": " Kilogramm", "pl": " kilogramów"}
_FACT_KEYS = ("weight_kg", "cargo_type", "pickup_location", "delivery_location", "adr")


def local_answer(lang: str, s: dict, text: str) -> str | None:
    """Plain facts about the shipment answered with code, so a slow or failing LLM never blocks a simple question."""
    if not is_question(text):
        return None
    if _WHEN.search(text):
        return status_line(lang, s)
    key = detect_field(text)
    if key in _FACT_KEYS and s.get(key) not in (None, ""):
        unit = _KG[lang] if key == "weight_kg" else ""
        return _FACT[lang].format(f=FIELD_LABELS[lang][key], v=_fmt(lang, key, s[key]) + unit)
    if _STATUS.search(text):
        return status_line(lang, s)
    return None


class TrackFlow(BaseFlow):
    name = "track"
    call_type = "track"
    slots = [
        Slot("tracking_number", "tracking",
             "the shipment tracking number, format TRK- followed by digits (spoken digits converted to numbers)"),
    ]

    async def validate(self, slot: Slot, state) -> str | None:
        tn = state.data["tracking_number"]
        shipment = await tools.get_shipment(tn)
        if shipment:
            state.shipment = shipment
            return None
        n = state.counters["not_found"] = state.counters.get("not_found", 0) + 1
        if n >= 2:
            state.counters["want_human"] = True   # two misses in a row: offer a person instead
        return t(state.lang, "track_not_found", tn=spell(tn))

    async def finish(self, state, ctx=None) -> FlowResult:
        s, lang = state.shipment, state.lang
        msgs = [status_line(lang, s)]
        state.collected.append({"flow": "track", "tracking_number": s["tracking_number"], "status": s["status"]})
        return FlowResult(messages=msgs, next_stage="manage")     # stay on this shipment: questions