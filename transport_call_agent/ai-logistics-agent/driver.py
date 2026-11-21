"""Flow 3: Driver update / delivery issue (retrieve + enter data)."""
from __future__ import annotations

from escalation import assess_urgency, can_transfer
from language import t
from models import BaseFlow, FlowResult, Slot
from utils import spell
import tools


class DriverFlow(BaseFlow):
    name = "issue"
    call_type = "issue"
    slots = [
        Slot("shipment_number", "tracking", "the shipment number, format TRK- followed by digits"),
        Slot("issue", "text", "what the problem is, e.g. consignee not available, damaged goods, breakdown"),
        Slot("waiting_minutes", "minutes", "how long the driver has been waiting, in minutes (convert hours)",
             optional=True),
        Slot("current_location", "text", "where the driver is right now", optional=True),
    ]

    async def validate(self, slot: Slot, state) -> str | None:
        if slot.id != "shipment_number":
            return None
        tn = state.data["shipment_number"]
        shipment = await tools.get_shipment(tn)
        if shipment:
            state.shipment = shipment
            return None
        n = state.counters["not_found"] = state.counters.get("not_found", 0) + 1
        if n >= 2:      # cannot find it twice: log the issue anyway with the number as spoken
            return None
        return t(state.lang, "track_not_found", tn=spell(tn))

    async def finish(self, state, ctx=None) -> FlowResult:
        d = state.data
        tn, issue = d["shipment_number"], d.get("issue", "")
        waiting, location = d.get("waiting_minutes"), d.get("current_location")

        linked = await tools.report_issue(tn, issue, waiting, location)
        urgency = assess_urgency(issue, waiting)
        state.urgency = max(state.urgency, urgency, key=["low", "normal", "high"].index)

        await tools.notify("driver_issue", {
            "shipment": tn, "issue": issue, "waiting_min": waiting, "location": location,
            "urgency": urgency, "phone": state.phone, "shipment_found": linked})
        state.collected.append({"flow": "issue", "shipment": tn, "issue": issue, "waiting_min": waiting,
                                "location": location, "urgency": urgency, "shipment_found": linked})

        msgs = [t(state.lang, "issue_saved", tn=spell(tn))]
        transfer = False
        if urgency == "high":
            state.escalated = True
            if can_transfer(state):
                msgs = [t(state.lang, "issue_saved", tn=spell(tn)), t(state.lang, "issue_urgent")]
                transfer = True
        return FlowResult(messages=msgs, transfer=transfer)
