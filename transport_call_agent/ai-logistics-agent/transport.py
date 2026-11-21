"""Flow 2: New transport request / quote (enter data) - collected, validated, read back, and saved only after YES."""
from __future__ import annotations

from language import NONE_WORDS, build_request_summary, t
from models import BaseFlow, FlowResult, Slot
from config import MAX_WEIGHT_KG
from utils import spell
import tools


class TransportFlow(BaseFlow):
    name = "request"
    call_type = "request"
    needs_confirmation = True
    slots = [
        # Only asked when the caller's phone number is not already a known customer
        Slot("company_name", "text", "the caller's company name", required_if=lambda s: s.customer is None),
        Slot("contact_name", "text", "the caller's own name", required_if=lambda s: s.customer is None),
        Slot("pickup_location", "text", "the pickup city or address (only the place name)"),
        Slot("delivery_location", "text", "the delivery city or address / destination (only the place name)"),
        Slot("cargo_type", "text", "the type of cargo, e.g. pharmaceutical, pallets, cosmetics, electronics"),
        Slot("weight_kg", "number", "the weight in kilograms (convert tonnes to kg)"),
        Slot("temperature_controlled", "yesno", "whether the cargo must be temperature-controlled"),
        Slot("temperature_range", "range",
             "the required temperature range in Celsius; fill BOTH temperature_min_c and temperature_max_c",
             required_if=lambda s: s.data.get("temperature_controlled") is True),
        Slot("adr", "yesno", "whether it is ADR dangerous goods"),
        Slot("adr_class", "text", "the ADR class, e.g. 3 or 8",
             required_if=lambda s: s.data.get("adr") is True, optional=True),
        Slot("pickup_date", "date", "the pickup date"),
        Slot("delivery_date", "date", "the wished delivery date"),
        # MANDATORY: pre-filled from the customer record when we already have it; otherwise it is asked.
        Slot("email", "email", "the caller's email address written like name@example.com "
                               "(spoken 'at' and 'dot' converted)"),
        Slot("special_requirements", "text", "any special requirements; 'none' if they have none", optional=True),
    ]

    def prefill(self, state) -> None:
        on_file = (state.customer or {}).get("email")
        if on_file:
            state.data["email"] = on_file
            state.field_status["email"] = "collected"

    async def validate(self, slot: Slot, state) -> str | None:
        d = state.data
        if slot.id == "weight_kg" and not (0 < d["weight_kg"] <= MAX_WEIGHT_KG):
            return t(state.lang, "invalid_weight_kg")
        if slot.kind == "range":                       # "8 to 2" -> 2 to 8
            lo, hi = d["temperature_min_c"], d["temperature_max_c"]
            if lo > hi:
                d["temperature_min_c"], d["temperature_max_c"] = hi, lo
        if slot.id in ("pickup_date", "delivery_date") and d.get("pickup_date") and d.get("delivery_date"):
            if d["delivery_date"] < d["pickup_date"]:
                return t(state.lang, "date_order")
        return None

    def email_for(self, state) -> str | None:
        return state.data.get("email") or (state.customer or {}).get("email")

    def summary(self, state) -> str:
        return build_request_summary(state.lang, state.data, self.email_for(state))

    async def finish(self, state, ctx=None) -> FlowResult:
        d = state.data
        email = self.email_for(state)
        if not email:                                   # never save without the mandatory e-mail
            raise ValueError("email is mandatory")
        if state.customer is None:   # new customer: store them so the next call is recognised
            cid = await tools.create_customer(d["company_name"], d.get("contact_name"), state.phone, state.lang, email)
            state.customer = {"id": cid, "company_name": d["company_name"], "contact_name": d.get("contact_name"),
                              "email": email}
        elif state.customer.get("email") != email:     # e-mail was missing or corrected during this call
            # Existing customer in DB is not modified; keep email in current state only
            state.customer["email"] = email
        special = str(d.get("special_requirements") or "").strip()
        req = {**d, "customer_id": state.customer["id"], "request_type": "quote",
               "special_requirements": None if special.lower() in NONE_WORDS else special}
        rid = await tools.create_transport_request(req)
        state.transport_request_id = rid
        label = f"REQ-{rid:04d}"
        await tools.notify("new_transport_request", {
            "request": label, "customer": state.customer.get("company_name"), "phone": state.phone, "email": email,
            "from": d.get("pickup_location"), "to": d.get("delivery_location"), "cargo": d.get("cargo_type"),
            "weight_kg": d.get("weight_kg"), "temperature_controlled": d.get("temperature_controlled"),
            "adr": d.get("adr"), "pickup_date": d.get("pickup_date")})
        state.collected.append({"flow": "request", "request": label, "fields": self.field_report(state), **req})
        return FlowResult(messages=[t(state.lang, "request_saved", rid=spell(label))])