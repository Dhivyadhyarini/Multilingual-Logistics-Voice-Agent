"""Offline test: plays whole calls against the real flow code with a FAKE phone line and a FAKE LLM.
No LiveKit account, no API keys, no internet needed.

    python test_offline.py

The fake LLM answers by LOOKING AT what the caller said (substring rules), so the tests do not depend on
how many times the agent decides to call the LLM. It can also time out or return garbage on purpose.
"""
import asyncio
import json
import os
import tempfile
from datetime import date, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["CLASSIFY_TIMEOUT"] = "0.3"          # failures must be quick in tests
os.environ.pop("OPERATIONS_PHONE", None)
os.environ.pop("EDIT_REQUIRES_OWNER", None)

import seed_data                      # noqa: E402
import db_queries as db               # noqa: E402
from agent import LogisticsAgent      # noqa: E402
from language import Voices           # noqa: E402
from models import CallState          # noqa: E402

seed_data.seed()
TODAY = date.today()
D_PICK, D_DELIV = TODAY + timedelta(days=30), TODAY + timedelta(days=33)


class FakeHandle:
    async def wait_for_playout(self):
        return None


class FakeSession:
    def __init__(self, said):
        self.said = said

    def say(self, text, allow_interruptions=True):
        self.said.append(text)
        return FakeHandle()


class FakeStream:
    def __init__(self, raw, delay=0.0):
        self.raw, self.delay = raw, delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            if self.delay:
                await asyncio.sleep(self.delay)
            class D: content = self.raw
            class C: delta = D()
            yield C()
        return gen()


class FakeLLM:
    """rules: list of (substring of the caller's words, reply dict | raw string). First match wins."""
    def __init__(self, rules=(), mode="rules"):
        self.rules, self.mode, self.calls, self.prompts = list(rules), mode, 0, []

    async def complete(self, system: str, user: str, json_mode: bool = True) -> str:
        self.calls += 1
        self.prompts.append(system)
        if self.mode == "timeout":
            await asyncio.sleep(5)
            return "{}"
        if self.mode == "garbage":
            return "Sorry, I cannot do that."
        said = user.lower()
        for sub, reply in self.rules:
            if sub.lower() in said:
                return reply if isinstance(reply, str) else json.dumps(reply)
        raise AssertionError(f"FakeLLM has no rule for: {said!r}")

    def chat(self, chat_ctx, **kw):
        self.calls += 1
        msgs = list(chat_ctx.items)
        system = next(m.text_content for m in msgs if m.role == "system")
        said = [m.text_content for m in msgs if m.role == "user"][-1].lower()
        self.prompts.append(system)
        if self.mode == "timeout":
            return FakeStream("{}", delay=5)
        if self.mode == "garbage":
            return FakeStream("Sorry, I cannot do that.")
        for sub, reply in self.rules:
            if sub.lower() in said:
                return FakeStream(reply if isinstance(reply, str) else json.dumps(reply))
        raise AssertionError(f"FakeLLM has no rule for: {said!r}")


class FakeCtx:
    class room: name = "test-room"
    class api:
        class room:
            @staticmethod
            async def delete_room(req): pass
    def shutdown(self, reason=""): self.reason = reason


class TestAgent(LogisticsAgent):
    @property
    def session(self):
        return self._fake


async def make(rules=(), lang="en", phone="+48501234567", mode="rules"):
    said = []
    st = CallState(phone=phone, lang=lang, live=True)
    if phone:
        from tools import find_customer
        st.customer = await find_customer(phone)
    llm = FakeLLM(rules, mode)
    import classifier
    classifier.complete = llm.complete
    ag = TestAgent(st, FakeCtx(), llm, Voices(None, None))
    ag._fake = FakeSession(said)
    return ag, st, said, llm


async def say(ag, said, text):
    n = len(said)
    await ag.handle_text(text)
    await asyncio.sleep(0.01)
    out = " | ".join(said[n:])
    print(f"   caller: {text!r}\n   agent : {out}")
    return out


def check(cond, label):
    print(("   PASS  " if cond else "   FAIL  ") + label)
    assert cond, label


def ans(**values):
    return {"intent": "answer", "values": values}


async def test_track_and_edit():
    print("\n== 1. Track a shipment, ask a question, change fields by plain sentences ==")
    rules = [
        ("temperature controlled", {"intent": "question", "reply": "Yes, it is kept between 2 and 8 degrees."}),
        ("change the destination to guindy", {"intent": "update", "values": {"destination": "Guindy"}}),
        ("weight to 800", {"intent": "update", "values": {"weight": "800 kilos", "cargo": "textiles"}}),
        ("actually make it chennai", {"intent": "correct", "values": {"delivery_location": "Chennai"}}),
    ]
    ag, st, said, llm = await make(rules, phone="+48225550101")
    await ag.begin("en", "track")
    out = await say(ag, said, "T R K 4 8 2 9 1")
    check("in transit" in out and "Berlin" in out and "ask me about this shipment" in out, "status read out, then 'ask or change'")
    check(st.stage == "manage" and st.shipment["tracking_number"] == "TRK-48291", "stays on the shipment")
    out = await say(ag, said, "is it temperature controlled?")
    check("2 and 8" in out, "question answered from shipment facts")
    check("Shipment facts" in llm.prompts[-1] and "Berlin" in llm.prompts[-1], "LLM was given the shipment facts")
    out = await say(ag, said, "I want to change the destination to Guindy")
    check("can't change that detail" in out or "operations team" in out, "change rejected on existing shipment, forwarded to ops")
    check(db.get_shipment_by_tracking("TRK-48291")["delivery_location"] == "Berlin, DE", "existing shipment in DB untouched")
    await say(ag, said, "no")                      # nothing else on this shipment
    await say(ag, said, "no")                      # nothing else at all
    await asyncio.sleep(0.6)
    check(st.outcome == "completed", "call finished")


async def test_not_found():
    print("\n== 2. Unknown tracking number twice -> human ==")
    ag, st, said, _ = await make()
    await ag.begin("en", "track")
    await say(ag, said, "TRK 99999")
    await say(ag, said, "TRK 88888")
    await asyncio.sleep(0.6)
    check(st.escalated and st.outcome == "escalated", "escalated after two misses")


async def test_request():
    print("\n== 3. New request: sentences, out-of-order info, mandatory e-mail, summary, corrections, YES ==")
    rules = [
        ("we are test pharma", ans(company_name="Test Pharma GmbH", contact_name="Anna Schmidt")),
        ("the destination is guindy", ans(destination="the destination is Guindy")),      # sloppy LLM on purpose
        ("sending 500 kilos", ans(pickup_location="Chennai", weight_kg=500, cargo_type="general goods",
                                  pickup_date=D_PICK.isoformat())),
        ("actually pickup is madurai", {"intent": "correct", "values": {"pickup_location": "Madurai"}}),
        ("by the", ans(delivery_date=D_DELIV.isoformat())),
        ("don't have one", {"intent": "unclear"}),
        ("actually the weight is 800", {"intent": "correct", "values": {"weight_kg": 800}}),
        ("destination should be chennai central", {"intent": "confirm_no", "values": {"delivery_location": "Chennai Central"}}),
    ]
    ag, st, said, llm = await make(rules, phone="+48999000111")        # unknown caller
    await ag.begin("en", "request")
    print("   agent :", said[-1])
    out = await say(ag, said, "We are Test Pharma GmbH and my name is Anna Schmidt")
    check(st.data["company_name"] == "Test Pharma GmbH" and st.data["contact_name"] == "Anna Schmidt", "two fields from one sentence")
    check("pick up" in out, "asks the next missing field (pickup)")
    out = await say(ag, said, "The destination is Guindy")
    check(st.data["delivery_location"] == "Guindy", "stored 'Guindy', NOT 'the destination is Guindy'")
    check("pick up" in out, "info given out of order; pickup is still asked")
    out = await say(ag, said, "I'm sending 500 kilos of general goods from Chennai, pickup on the day")
    check(st.data["pickup_location"] == "Chennai" and st.data["weight_kg"] == 500 and st.data["cargo_type"] == "general goods"
          and st.data["pickup_date"] == D_PICK.isoformat(), "4 fields from one sentence")
    check("temperature" in out and "weight" not in out.lower().split("|")[-1], "does not ask again for what was given")
    await say(ag, said, "no")                                               # temperature controlled
    out = await say(ag, said, "actually pickup is Madurai")                 # correction in the middle of the call
    check(st.data["pickup_location"] == "Madurai" and "updated" in out and "dangerous" in out.lower(), "correction applied, same question repeated")
    await say(ag, said, "no")                                               # ADR
    out = await say(ag, said, "by the tenth")
    check("email" in out.lower(), "e-mail is asked (mandatory)")
    out = await say(ag, said, "I don't have one")
    check("email" not in st.data and "email" in out.lower(), "mandatory e-mail is asked again politely, not skipped")
    out = await say(ag, said, "I don't have one")
    check("email" not in st.data and "still need" in out.lower(), "second miss: says the detail is still needed")
    out = await say(ag, said, "it's anna at testpharma dot com")
    check(st.data["email"] == "anna@testpharma.com", "spoken e-mail converted")
    out = await say(ag, said, "none")                                       # special requirements
    check(st.stage == "confirm" and "To confirm" in out and "Is everything correct" in out, "summary read back")
    check("Madurai to Guindy" in out and "500 kilograms" in out and "anna at testpharma dot com" in out, "summary has the key data")
    check(db.recent("transport_requests", 1) == [], "NOTHING saved before the caller confirms")
    out = await say(ag, said, "no")
    check("Which detail" in out, "NO -> asks what to change")
    out = await say(ag, said, "Actually the weight is 800 kilos")
    check("800 kilograms" in out and "Is everything correct" in out, "correction -> summary read again")
    out = await say(ag, said, "no, the destination should be Chennai Central")
    check("Madurai to Chennai Central" in out, "'no' + new value in one sentence")
    check(db.recent("transport_requests", 1) == [], "still nothing saved")
    out = await say(ag, said, "yes")
    row = db.recent("transport_requests", 1)[0]
    check(row["delivery_location"] == "Chennai Central" and row["weight_kg"] == 800 and row["pickup_location"] == "Madurai"
          and row["special_requirements"] is None, "saved ONLY after YES, with the corrected values")
    check("R E Q" in out, "request number read back")
    cust = db.get_customer_by_phone("+48999000111")
    check(cust and cust["email"] == "anna@testpharma.com", "e-mail stored on the new customer")
    check(st.collected[-1]["fields"]["confirmed"], "field status tracked (confirmed)")


async def test_known_customer_email():
    print("\n== 4. Known customer: e-mail on file is used, not asked again ==")
    ag, st, said, _ = await make(phone="+4930555010", lang="de")
    await ag.begin("de", "request")
    check(st.data.get("email") == "jonas@medilogistik.example", "e-mail pre-filled from the customer record")


async def test_issue():
    print("\n== 5. Driver issue, urgent keyword (Polish) ==")
    rules = [("awaria", ans(issue="awaria chłodni, towar się psuje")), ("czterdzieści", ans(waiting_minutes=40))]
    ag, st, said, _ = await make(rules, lang="pl")
    await ag.begin("pl", "issue")
    await say(ag, said, "TRK-48291")
    await say(ag, said, "awaria chłodni, towar się psuje")
    await say(ag, said, "czterdzieści minut")
    await say(ag, said, "Poznań")
    check(st.urgency == "high" and st.escalated, "marked high urgency + escalated")
    r = db.get_shipment_by_tracking("TRK-48291")
    check(r["issue_status"] == "none", "existing shipment was NOT updated in DB (logged to ops only)")


async def test_switch():
    print("\n== 6. Language switch in the middle of a call ==")
    ag, st, said, _ = await make()
    await ag.begin("en", "track")
    out = await say(ag, said, "auf Deutsch bitte")
    check(st.lang == "de" and "Sendungsnummer" in out, "switched to German and re-asked in German")
    out = await say(ag, said, "po polsku")
    check(st.lang == "pl" and "numer przesyłki" in out, "switched to Polish")


async def test_classifier_failures():
    print("\n== 7. Classifier timeout / bad JSON / truncated JSON: no retry loops, the call keeps going ==")
    ag, st, said, llm = await make(mode="timeout", phone="+48999000222")
    await ag.begin("en", "request")                                       # asks company_name
    out = await say(ag, said, "my company is Alpha Logistics and we are in Gdansk")
    check(llm.calls == 1, "timeout: exactly ONE LLM attempt for the turn (no retry)")
    check("company" in out.lower() or "didn't" in out.lower(), "agent re-asks politely, does not crash or store a long sentence")
    check("company_name" not in st.data, "a long sentence is not stored as a value when the LLM is down")

    ag, st, said, llm = await make(mode="garbage", phone="+48999000333")
    await ag.begin("en", "request")
    for _ in range(3):
        await say(ag, said, "my company is Alpha Logistics and we are in Gdansk")
    calls_after_3 = llm.calls
    check(calls_after_3 == 3, "bad JSON: one attempt per turn")
    await say(ag, said, "my company is Alpha Logistics and we are in Gdansk")
    check(llm.calls == calls_after_3, "after 3 failures the LLM is skipped (cool-down) instead of hammered")

    ag, st, said, llm = await make(mode="garbage", phone="+48999000444")
    st.flow = None
    await ag.start_flow("request")
    st.data.update(company_name="X", contact_name="Y")
    out = await say(ag, said, "from Chennai to Guindy")
    check(st.data.get("pickup_location") == "Chennai" and st.data.get("delivery_location") == "Guindy",
          "LLM down: route still extracted locally from 'from X to Y'")

    rules = [("textiles", '{"intent":"answer","values":{"cargo_type":"textiles","weight_kg":')]    # cut off mid-JSON
    ag, st, said, llm = await make(rules, phone="+48501234567")
    await ag.start_flow("request")
    st.data.update(pickup_location="A", delivery_location="B")
    await say(ag, said, "it is a load of textiles weighing around five hundred")
    check(st.data.get("cargo_type") == "textiles", "truncated JSON is repaired and still used")


async def test_context_in_prompt():
    print("\n== 8. The LLM sees what is already collected (context) ==")
    rules = [("hello there", {"intent": "unclear"})]
    ag, st, said, llm = await make(rules, phone="+48501234567")
    await ag.start_flow("request")
    st.data.update(pickup_location="Chennai")
    await say(ag, said, "hello there my friend")
    check("[now: Chennai]" in llm.prompts[-1] and "[now: not yet]" in llm.prompts[-1], "prompt lists every field with its current value")


async def test_spoken_tracking_numbers():
    print("\n== 9. Spoken / fragmented tracking numbers (as in the real call log) ==")
    ag, st, said, llm = await make(phone="+48501234567")
    await ag.begin("en", "track")
    out = await say(ag, said, "three zero")                         # STT gave only the first piece
    check("T R K 3 0 so far" in out.replace("T R K 3 0 ", "T R K 3 0 so far ", 1) and "remaining digits" in out, "short piece is kept, caller asked for the rest")
    check(llm.calls == 0, "no LLM needed for number words")
    out = await say(ag, said, "zero")
    check("T R K 3 0 0" in out and "remaining" in out, "next piece is appended")
    out = await say(ag, said, "zero three.")
    check(st.shipment is not None and st.shipment["tracking_number"] == "TRK-30003", "pieces joined -> TRK-30003 found")
    ag, st, said, llm = await make(phone="+48501234567")
    await ag.begin("en", "track")
    await say(ag, said, "T R K three zero zero zero three")
    check(st.shipment and st.shipment["tracking_number"] == "TRK-30003", "'T R K three zero zero zero three' understood")
    ag, st, said, llm = await make(phone="+48501234567")
    await ag.begin("en", "track")
    await say(ag, said, "my tracking number is four eight two nine one")
    check(st.shipment and st.shipment["tracking_number"] == "TRK-48291", "digits inside a sentence understood")
    ag, st, said, llm = await make(phone="+48501234567")
    await ag.begin("en", "track")
    await say(ag, said, "double zero seven")
    await say(ag, said, "that's it")
    check(st.counters.get("not_found") == 1, "'that's it' -> the short number is looked up as complete")


async def test_groq_json_error():
    print("\n== 10. Groq 'Failed to validate JSON' -> one lenient retry, no failure ==")
    class Flaky(FakeLLM):
        async def complete(self, system: str, user: str, json_mode: bool = True) -> str:
            self.calls += 1
            if json_mode:
                raise RuntimeError("Connection error. (caused by APIError: Failed to validate JSON. Please adjust your prompt. See 'failed_generation' for more details.)")
            return json.dumps(ans(company_name="Alpha Logistics"))

        def chat(self, chat_ctx, **kw):
            self.calls += 1
            if kw.get("extra_kwargs", {}).get("response_format"):
                raise RuntimeError("Connection error. (caused by APIError: Failed to validate JSON. Please adjust your prompt. See 'failed_generation' for more details.)")
            return FakeStream(json.dumps(ans(company_name="Alpha Logistics")))
    said = []
    st = CallState(phone="+48999000555", lang="en", live=True)
    llm = Flaky()
    import classifier
    classifier.complete = llm.complete
    ag = TestAgent(st, FakeCtx(), llm, Voices(None, None))
    ag._fake = FakeSession(said)
    await ag.start_flow("request")
    await say(ag, said, "my company is Alpha Logistics and we are in Gdansk")
    check(st.data.get("company_name") == "Alpha Logistics" and llm.calls == 2, "retried once WITHOUT json mode and got the answer")
    check(ag._json_mode is True, "JSON mode is kept for the next turns")


async def main():
    for fn in (test_track_and_edit, test_not_found, test_request, test_known_customer_email, test_issue,
               test_switch, test_classifier_failures, test_context_in_prompt, test_spoken_tracking_numbers,
               test_groq_json_error):
        await fn()
    print("\nAll offline tests passed.")

asyncio.run(main())