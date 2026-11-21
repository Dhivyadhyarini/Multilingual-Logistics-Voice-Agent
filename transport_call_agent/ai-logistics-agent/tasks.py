"""LiveKit dispatch setup: tells LiveKit "when a call arrives on my number, open a room and send the agent".

    python tasks.py setup --number +48800100200   # create inbound trunk + dispatch rule
    python tasks.py list                          # show what exists
    python tasks.py clean                         # delete them again

(You can also do the same by hand in the LiveKit Cloud dashboard: Telephony -> SIP trunks / Dispatch rules.)
"""
from __future__ import annotations

import argparse
import asyncio
import os

from dotenv import load_dotenv
from livekit import api

load_dotenv()
AGENT_NAME = os.getenv("AGENT_NAME", "logistics-agent")
TRUNK_NAME = "hitec-inbound-trunk"
RULE_NAME = "hitec-inbound-rule"


async def setup(number: str | None) -> None:
    lk = api.LiveKitAPI()
    try:
        trunk = await lk.sip.create_sip_inbound_trunk(api.CreateSIPInboundTrunkRequest(
            trunk=api.SIPInboundTrunkInfo(name=TRUNK_NAME, numbers=[number] if number else [], krisp_enabled=True)))
        print(f"Inbound trunk created: {trunk.sip_trunk_id}")
        rule = await lk.sip.create_sip_dispatch_rule(api.CreateSIPDispatchRuleRequest(
            dispatch_rule=api.SIPDispatchRuleInfo(
                name=RULE_NAME,
                trunk_ids=[trunk.sip_trunk_id],
                rule=api.SIPDispatchRule(dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix="call-")),
                room_config=api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]),
            )))
        print(f"Dispatch rule created: {rule.sip_dispatch_rule_id}  (agent: {AGENT_NAME})")
    finally:
        await lk.aclose()


async def show() -> None:
    lk = api.LiveKitAPI()
    try:
        trunks = await lk.sip.list_sip_inbound_trunk(api.ListSIPInboundTrunkRequest())
        for t in trunks.items:
            print("trunk:", t.sip_trunk_id, t.name, list(t.numbers))
        rules = await lk.sip.list_sip_dispatch_rule(api.ListSIPDispatchRuleRequest())
        for r in rules.items:
            print("rule:", r.sip_dispatch_rule_id, r.name, [a.agent_name for a in r.room_config.agents])
    finally:
        await lk.aclose()


async def clean() -> None:
    lk = api.LiveKitAPI()
    try:
        for r in (await lk.sip.list_sip_dispatch_rule(api.ListSIPDispatchRuleRequest())).items:
            if r.name == RULE_NAME:
                await lk.sip.delete_sip_dispatch_rule(api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=r.sip_dispatch_rule_id))
                print("deleted rule", r.sip_dispatch_rule_id)
        for t in (await lk.sip.list_sip_inbound_trunk(api.ListSIPInboundTrunkRequest())).items:
            if t.name == TRUNK_NAME:
                await lk.sip.delete_sip_trunk(api.DeleteSIPTrunkRequest(sip_trunk_id=t.sip_trunk_id))
                print("deleted trunk", t.sip_trunk_id)
    finally:
        await lk.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["setup", "list", "clean"])
    ap.add_argument("--number", help="your phone number in E.164 format, e.g. +48800100200")
    a = ap.parse_args()
    asyncio.run({"setup": lambda: setup(a.number), "list": show, "clean": clean}[a.action]())
