"""The AI voice agent: turn handling + conversation logic.

How it works:
  * The CODE owns the script: which question comes next, what is saved, when the call ends.
  * The LLM (Groq) only CLASSIFIES each reply and EXTRACTS values (JSON). It never talks directly,
    so it cannot make things up, and every spoken line comes from language.py.

What is new in this version
  * The LLM sees the full list of fields WITH their current values, so one sentence can fill several
    fields, in any order, and corrections ("actually it is Guindy") change the right field.
  * Values are always BARE ("Guindy", never "the destination is Guindy"): the prompt asks for it and
    clean_text_value() enforces it, also in the no-LLM fallback.
  * Classifier failures never loop: ONE attempt per turn, no hidden retries inside LiveKit, and after
    3 failures in a row the LLM is skipped for a short cool-down (local extraction takes over).
  * Mandatory fields can never be skipped; the operation is only saved after the caller says YES to a
    read-back summary (new requests) or to the exact change (existing shipments).
  * After tracking a shipment the caller can ask about it, add missing details and change fields.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date

from livekit import api
from livekit.agents import Agent, JobContext, StopResponse, llm

try:
    from livekit.agents import APIConnectOptions
except ImportError:                                            # older livekit-agents
    from livekit.agents.types import APIConnectOptions

import classifier
import tools
from config import OFFICE_PHONE, WEIGHT_CONFIRM_BELOW_KG, WEIGHT_CONFIRM_DECIMALS
from driver import DriverFlow
from faq import faq_answer
from escalation import can_transfer, transfer_to_human
from language import (
    ACKS, BYE_RE, COMPANY, COMPANY_FACTS, FIELD_LABELS, HUMAN_RE,
    LANG_NAMES, NONE_WORDS, PHONE_Q_RE, REPEAT_RE, T, Voices,
    detect_switch, fmt_date, fmt_num, prompt_for_slot, spoken_phone, t, yes_no
)
from models import CallState
from shipment import (EDIT_FIELDS, TrackFlow, current_values, describe_changes, edit_slot_for_key,
                      local_answer, prepare_changes, shipment_facts)
from transport import TransportFlow
from utils import (FREE_TEXT_KEYS, is_non_answer, temperature_numbers, PastDate, clean_text_value, detect_field, extract_route, find_tracking, looks_like_hallucination,
                   normalize_value, numwords_to_digits, parse_json_object, parse_spoken_date, schema_violation, spell, spoken_digits,
                   to_email, to_number, is_filler, is_fragment, parse_change, plausible_place, is_field_phrase,
                   is_question, NAME_KEYS, TRK_FUZZY_RE, is_yes_without_detail, extract_places)


logger = logging.getLogger("logistics-agent")

CLASSIFY_TIMEOUT = float(os.getenv("CLASSIFY_TIMEOUT", "6"))
FRAGMENT_HOLD = float(os.getenv("FRAGMENT_HOLD", "0.9"))      # was 2.2               # seconds to wait for the rest of a cut-off sentence ("The destination is ...")
CLASSIFY_MAX_TOKENS = int(os.getenv("CLASSIFY_MAX_TOKENS", "500"))      # many fields in one sentence = longer JSON
CLF_FAIL_LIMIT = int(os.getenv("CLF_FAIL_LIMIT", "3"))                  # consecutive failures before the cool-down
CLF_COOLDOWN = float(os.getenv("CLF_COOLDOWN", "20"))                   # seconds the LLM is skipped afterwards
TRK_MIN_DIGITS = int(os.getenv("TRK_MIN_DIGITS", "5"))                  # shorter = the caller is probably still speaking
PLAYOUT_TIMEOUT = float(os.getenv("PLAYOUT_TIMEOUT", "25"))              # max seconds to wait for a spoken line to finish
MAX_REASK = int(os.getenv("MAX_REASK", "2"))                             # asks of one mandatory field before a human
EDIT_REQUIRES_OWNER = os.getenv("EDIT_REQUIRES_OWNER", "0") == "1"      # only the shipment's customer may edit it
JSON_KWARGS = {"response_format": {"type": "json_object"}, "max_tokens": CLASSIFY_MAX_TOKENS}
# max_retry=0: LiveKit must NOT silently retry a slow/failed classification (each retry costs a full timeout)
CONN = APIConnectOptions(max_retry=0, timeout=CLASSIFY_TIMEOUT)
PLAIN_NUMBER_RE = re.compile(r"[\d\s.,]+")

FLOWS = {"track": TrackFlow, "request": TransportFlow, "issue": DriverFlow}
INTENTS = ("answer", "correct", "update", "question", "confirm_yes", "confirm_no", "repeat", "human",
           "switch_language", "switch_flow", "decline", "unclear")
LOCATION_SLOTS = ("pickup_location", "delivery_location", "current_location")
FREE_TEXT_SLOTS = ("issue", "special_requirements")                     # long answers are normal here
KIND_FORMAT = {"text": "only the value, no extra words", "tracking": "TRK-<digits>", "yesno": "true or false",
               "number": "a plain number", "date": "YYYY-MM-DD (resolve 'in 15 days' from today)",
               "minutes": "integer minutes",
               "range": "numbers", "email": "name@domain.com"}

_INTENT_ALIASES = {"provide_info": "answer", "info": "answer", "additional_info": "answer", "correction": "correct",
                   "reject": "confirm_no", "deny": "confirm_no", "confirm": "confirm_yes", "accept": "confirm_yes",
                   "yes": "confirm_yes", "no": "confirm_no", "change": "update", "edit": "update",
                   "request_update": "update", "ask": "question", "goodbye": "decline", "stop": "decline"}
_KEY_ALIASES = {
    "destination": "delivery_location", "delivery": "delivery_location", "dropoff": "delivery_location",
    "drop_off": "delivery_location", "to": "delivery_location", "delivery_city": "delivery_location",
    "delivery_address": "delivery_location", "destination_location": "delivery_location",
    "origin": "pickup_location", "pickup": "pickup_location", "pick_up": "pickup_location",
    "source": "pickup_location", "from": "pickup_location", "pickup_city": "pickup_location",
    "pickup_address": "pickup_location", "cargo": "cargo_type", "goods": "cargo_type", "weight": "weight_kg",
    "weight_kilograms": "weight_kg", "temperature_min": "temperature_min_c", "min_temperature": "temperature_min_c",
    "temperature_max": "temperature_max_c", "max_temperature": "temperature_max_c",
    "email_address": "email", "e_mail": "email", "email_id": "email", "mail": "email",
    "company": "company_name", "name": "contact_name", "contact": "contact_name",
    "waiting_time": "waiting_minutes", "waiting": "waiting_minutes",
}
_CORRECTION_CUES = re.compile(
    r"\b(actually|instead|change|wrong|sorry|mistake|correction|i meant|eigentlich|ändern|korrigier\w*|falsch|"
    r"zmień|właściwie|pomyłk\w*)\b", re.I)
# Asked when the caller says YES to "any special requirements?" but does not say WHAT (add a language = add a key).
_ASK_SPECIAL_DETAIL = {
    "en": "Sure. What is the special requirement?",
    "de": "Gern. Welche besondere Anforderung haben Sie?",
    "pl": "Dobrze. Jakie jest to szczególne wymaganie?"}
JSON_ERR_KEYS = ("json_validate_failed", "failed to validate json", "failed_generation", "validate json",
                 "adjust your prompt")


def _err_text(e: BaseException) -> str:
    """Whole error chain as text. LiveKit wraps Groq's 400 'Failed to validate JSON' inside 'Connection error.'"""
    parts, seen = [], set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        parts.append(str(e))
        if getattr(e, "status_code", None):
            parts.append(f"status={e.status_code}")
        if getattr(e, "body", None):
            parts.append(str(e.body))
        e = e.__cause__ or e.__context__
    return " ".join(parts)


def _failed_generation(e: BaseException) -> str:
    """Groq returns the model's broken output in body['error']['failed_generation']: often still usable."""
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        body = getattr(e, "body", None)
        if isinstance(body, dict):
            err = body.get("error", body)
            if isinstance(err, dict) and err.get("failed_generation"):
                return str(err["failed_generation"])
        e = e.__cause__ or e.__context__
    return ""


_MSG = {   # small extra messages (kept here so language.py does not need to change)
    "not_editable": {
        "en": "I can't change that detail by phone on an existing shipment. I've passed your request to our operations team.",
        "de": "Dieses Detail kann ich telefonisch bei einer bestehenden Sendung nicht ändern. Ich habe Ihre Anfrage an unser Operations-Team weitergegeben.",
        "pl": "Tego szczegółu nie mogę zmienić telefonicznie w istniejącej przesyłce. Przekazałem prośbę naszemu zespołowi operacyjnemu.",
    },
    "save_failed_callback": {
        "en": "I'm sorry, I could not save your request, so it has not been submitted. Our team has been alerted and will call you back.",
        "de": "Es tut mir leid, ich konnte Ihre Anfrage nicht speichern, sie wurde daher nicht übermittelt. Unser Team ist informiert und ruft Sie zurück.",
        "pl": "Przepraszam, nie udało mi się zapisać zgłoszenia, więc nie zostało wysłane. Nasz zespół został powiadomiony i oddzwoni.",
    },
    "listening": {
        "en": "Go ahead, I'm listening.",
        "de": "Bitte, ich höre zu.",
        "pl": "Proszę mówić, słucham.",
    },
    "date_understood": {
        "en": "That would be {d}.",
        "de": "Das wäre der {d}.",
        "pl": "Czyli {d}.",
    },
    "weight_confirm": {
        "en": "Did you say {w} kilograms? Please say yes, or tell me the correct weight.",
        "de": "Haben Sie {w} Kilogramm gesagt? Bitte sagen Sie ja, oder nennen Sie das richtige Gewicht.",
        "pl": "Czy powiedzieli Państwo {w} kilogramów? Proszę powiedzieć tak lub podać właściwą wagę.",
    },
    "office_phone": {
        "en": "You can reach our office on {p}.",
        "de": "Unser Büro erreichen Sie unter {p}.",
        "pl": "Nasze biuro jest dostępne pod numerem {p}.",
    },
}

_WHY = {   # one-line reason shown when the caller asks "why / what for?" about a question
    "waiting_minutes": {
        "en": "I need this so our dispatcher knows how late the delivery will be.",
        "de": "Das brauche ich, damit unser Disponent die Verspätung einplanen kann.",
        "pl": "Potrzebuję tego, aby dyspozytor mógł uwzględnić opóźnienie.",
    },
}


def _m(lang: str, key: str) -> str:
    return _MSG[key].get(lang, _MSG[key]["en"])

_DATE_QUESTION = re.compile(r"^\s*(?:how|what|when|why|where|which|who|does|do|will|is|are|wie|wann|was|kiedy|jak|czy)\b", re.I)
_PICKUP_WORD = re.compile(r"\b(pick\w*|collect\w*|load\w*|abhol\w*|odbi\w*)\b", re.I)
_DELIVER_WORD = re.compile(r"\b(deliver\w*|arriv\w*|liefer\w*|dostaw\w*)\b", re.I)
_SIMPLE = {   # short, plain rewordings used when the caller says "I didn't understand" (or asks twice)
    "en": {
        "tracking_number": "Please say the shipment number: T R K, then the digits one by one.",
        "shipment_number": "Please say the shipment number: T R K, then the digits one by one.",
        "pickup_location": "Which city are we collecting the goods from?",
        "delivery_location": "Which city should the goods go to?",
        "cargo_type": "What are you sending? For example pallets or medicine.",
        "weight_kg": "How heavy is it? Please say the kilograms.",
        "temperature_controlled": "Must the goods stay cold or warm? Yes or no?",
        "temperature_range": "Which temperatures? For example, from 2 to 8 degrees.",
        "adr": "Are the goods dangerous, like chemicals? Yes or no?",
        "adr_class": "What is the ADR class number?",
        "pickup_date": "On what day should the truck collect the goods?",
        "delivery_date": "By what day must the goods arrive?",
        "email": "What is your email address? Please say it slowly.",
        "company_name": "What is your company's name?",
        "contact_name": "What is your name?",
        "issue": "What is the problem? Please tell me in a few words.",
        "waiting_minutes": "How many minutes have you been waiting?",
        "current_location": "Where are you right now?",
    },
    "de": {
        "tracking_number": "Bitte nennen Sie die Sendungsnummer: T R K und dann die Ziffern, einzeln.",
        "shipment_number": "Bitte nennen Sie die Sendungsnummer: T R K und dann die Ziffern, einzeln.",
        "pickup_location": "In welcher Stadt holen wir die Ware ab?",
        "delivery_location": "In welche Stadt soll die Ware geliefert werden?",
        "cargo_type": "Was verschicken Sie? Zum Beispiel Paletten oder Medikamente.",
        "weight_kg": "Wie schwer ist die Ware? Bitte in Kilogramm.",
        "temperature_controlled": "Muss die Ware gekühlt oder beheizt werden? Ja oder nein?",
        "temperature_range": "Welche Temperatur? Zum Beispiel von 2 bis 8 Grad.",
        "adr": "Ist die Ware gefährlich, zum Beispiel Chemikalien? Ja oder nein?",
        "adr_class": "Wie lautet die ADR-Klasse als Zahl?",
        "pickup_date": "An welchem Tag soll der Lkw die Ware abholen?",
        "delivery_date": "Bis zu welchem Tag muss die Ware ankommen?",
        "email": "Wie lautet Ihre E-Mail-Adresse? Bitte langsam.",
        "company_name": "Wie heißt Ihre Firma?",
        "contact_name": "Wie ist Ihr Name?",
        "issue": "Was ist das Problem? Bitte in wenigen Worten.",
        "waiting_minutes": "Wie viele Minuten warten Sie schon?",
        "current_location": "Wo sind Sie gerade?",
    },
    "pl": {
        "tracking_number": "Proszę podać numer przesyłki: litery T R K, a potem cyfry, jedna po drugiej.",
        "shipment_number": "Proszę podać numer przesyłki: litery T R K, a potem cyfry, jedna po drugiej.",
        "pickup_location": "Z jakiego miasta mamy odebrać towar?",
        "delivery_location": "Do jakiego miasta mamy dostarczyć towar?",
        "cargo_type": "Co Państwo wysyłają? Na przykład palety lub leki.",
        "weight_kg": "Ile waży towar? Proszę podać w kilogramach.",
        "temperature_controlled": "Czy towar musi być chłodzony lub ogrzewany? Tak czy nie?",
        "temperature_range": "Jaka temperatura? Na przykład od 2 do 8 stopni.",
        "adr": "Czy towar jest niebezpieczny, na przykład chemikalia? Tak czy nie?",
        "adr_class": "Jaki jest numer klasy ADR?",
        "pickup_date": "W którym dniu ciężarówka ma odebrać towar?",
        "delivery_date": "Do którego dnia towar ma dotrzeć?",
        "email": "Jaki jest Państwa adres e-mail? Proszę powoli.",
        "company_name": "Jak nazywa się Państwa firma?",
        "contact_name": "Proszę podać imię i nazwisko.",
        "issue": "Jaki jest problem? Proszę krótko.",
        "waiting_minutes": "Ile minut trwa oczekiwanie?",
        "current_location": "Gdzie Państwo są teraz?",
    },
}
_LEAD_CONJ = re.compile(r"^(And|Und|A)\s+")        # "And where should it be delivered?" sounds odd when repeated

def _BOTH_DATES(text: str) -> bool:
    """'pick up tomorrow and deliver in 15 days' holds TWO dates: leave it to the LLM instead of guessing one."""
    return bool(_PICKUP_WORD.search(text) and _DELIVER_WORD.search(text))


_HARD_BYE = re.compile(r"\b(goodbye|bye|auf wiederhören|tschüss|tschuess|do widzenia)\b", re.I)
CLARIFY_RE = re.compile(
    r"\b("
    r"i\s*(didn't|did not)\s*(understand|get|hear)"
    r"|i\s*don't\s*understand"
    r"|what\s*did\s*you\s*say"
    r"|come\s*again"
    r"|say\s*that\s*again"
    r"|please\s*repeat"
    r"|repeat\s*that"
    r"|can\s*you\s*repeat"
    r"|could\s*you\s*repeat"
    r"|sorry"
    r")\b",
    re.I,
)
_EXPLICIT_TRK = TRK_FUZZY_RE            # also matches how the STT mishears it: B R K, E R K ...

# Static part of the classifier prompt: built once, not on every turn.
_INTENT_RULES = (
    "Return exactly ONE JSON object and nothing else: double quotes on every key and string, no comments, no "
    "trailing commas, no markdown. Never put units or words inside a number. Keys: "
    '{"intent":"","values":{},"language":"","flow":"","reply":""}\n'
    'Example: {"intent":"answer","values":{"delivery_location":"Guindy"},"language":"","flow":"","reply":""}\n'
    "DATES. Use YYYY-MM-DD. Relative dates count from today: 'in 15 days', 'after fifteen days' and 'within 15 "
    "days' all mean today + 15 days; 'next Friday' is the coming Friday. If you cannot compute a date, return "
    "the caller's own words (e.g. \"15 days\") and the code will compute it.\n"
    "VALUES. Callers speak in full sentences. Put in \"values\" ONLY the bare value of each field, never the words "
    'around it: "the destination is Guindy" -> {"delivery_location":"Guindy"}; "from Chennai to Guindy" -> '
    "pickup_location Chennai + delivery_location Guindy; \"I need to send 500 kilos of textiles from Chennai to "
    "Guindy on 5 November\" -> pickup_location, delivery_location, cargo_type textiles, weight_kg 500, pickup_date. "
    "Fill EVERY field the caller mentions, in any order, even if it was not the question asked. A bare reply "
    "('Guindy') belongs to the field of the question asked. Use only what they said; never invent. Spoken digits "
    "become digits. Spoken e-mail becomes written ('john dot smith at example dot com' -> john.smith@example.com). "
    "Resolve relative dates from today. Use the exact field keys listed above.\n"
    "intent is exactly one of:\n"
    '- "answer": new information for the fields (also when they ALSO ask something).\n'
    '- "correct": they change something they said earlier (cues: actually, no change that, I meant, sorry, wrong, '
    'instead, "the weight is actually 800"). "values" holds ONLY the new values.\n'
    '- "update": (existing shipment only) they want to change or add shipment data, in plain words like '
    '"I want to change the destination to Guindy". "values" holds the new values.\n'
    '- "confirm_yes": they agree with the summary or change that was just read.\n'
    '- "confirm_no": they reject it; put any corrected values in "values".\n'
    '- "question": they ONLY asked something (about the company, or about the shipment when shipment facts are given).\n'
    '- "repeat": they did not hear and want the question again.\n'
    '- "human": they want a person, colleague or dispatcher.\n'
    '- "switch_language": language = en, de or pl.\n'
    '- "switch_flow": flow = track (track a shipment), request (new transport or quote) or issue (driver delivery problem).\n'
    '- "decline": they want to stop or need nothing else.\n'
    '- "unclear": off-topic, partial or unusable.\n'
    'reply: if they asked ANY question, a direct answer under 25 words in their language, using ONLY the facts '
    'given above; if the facts do not cover it, "". Otherwise "".\n'
    'Unused keys may be empty ("" or {}).'
)

_STAGE_HINT = {
    "slots": "The caller is giving details; a question was just asked.",
    "confirm": "A summary of the collected details was just read and the caller was asked whether everything is "
               "correct. 'yes' -> confirm_yes. 'no' alone -> confirm_no. A changed detail -> correct (new values only).",
    "manage": "The caller has an existing shipment on the line. They may ask about it (question + reply from the "
              "shipment facts), or want to change/add data (update), or answer yes/no to a change just read back.",
    "more": "The caller was asked whether they need anything else.",
}


# ---------------------------------------------------------------------------
# Reading the classifier's answer
# ---------------------------------------------------------------------------
def canon_key(key) -> str:
    k = re.sub(r"[\s\-]+", "_", str(key).strip().lower())
    return _KEY_ALIASES.get(k, k)


def clean_classification(data: dict | None) -> dict | None:
    """Validate the classifier JSON: known intent, dict of non-empty canonical values. None = unusable."""
    if not isinstance(data, dict):
        return None
    intent = str(data.get("intent") or "").strip().lower().replace(" ", "_").replace("-", "_")
    intent = _INTENT_ALIASES.get(intent, intent)
    values = {}
    raw_values = data.get("values")
    for k, v in (raw_values.items() if isinstance(raw_values, dict) else []):
        if v is None or isinstance(v, (dict, list)) or str(v).strip().lower() in ("", "null", "unknown"):
            continue
        values[canon_key(k)] = v
    if intent not in INTENTS:
        if not values:
            return None
        intent = "answer"                              # no intent but real values: treat as an answer
    return {"intent": intent, "values": values, "language": data.get("language") or "",
            "flow": data.get("flow") or "", "reply": str(data.get("reply") or "").strip()}


def _field_lines(state: CallState, stage: str) -> list[str]:
    lines = []
    if stage == "manage" and state.shipment:
        cur = current_values(state.shipment)
        for s in EDIT_FIELDS:
            if s.kind == "range":
                lo, hi = cur["temperature_min_c"], cur["temperature_max_c"]
                now = f"{lo} to {hi}" if lo is not None else "not set"
                lines.append(f"- temperature_min_c, temperature_max_c (numbers) [now: {now}]: {s.expect}")
            else:
                now = cur.get(s.id)
                lines.append(f"- {s.id} ({KIND_FORMAT[s.kind]}) [now: {'not set' if now in (None, '') else now}]: "
                             f"{s.expect}")
    elif state.flow:
        for s in state.flow.slots:
            if s.kind == "range":
                lo, hi = state.data.get("temperature_min_c"), state.data.get("temperature_max_c")
                now = f"{lo} to {hi}" if lo is not None else "not yet"
                lines.append(f"- temperature_min_c, temperature_max_c (numbers) [now: {now}]: {s.expect}")
            else:
                now = state.data.get(s.id, "not yet") if s.id in state.data else "not yet"
                lines.append(f"- {s.id} ({KIND_FORMAT[s.kind]}) [now: {now}]: {s.expect}")
    return lines


def classifier_prompt(state: CallState, question: str, expect: str, stage: str = "slots",
                      with_facts: bool = True) -> str:
    today = date.today()
    facts = f"Company facts: {COMPANY_FACTS}\n" if with_facts else ""
    if stage == "manage" and state.shipment:
        facts += f"Shipment facts: {shipment_facts(state.shipment)}\n"
        if state.pending:
            facts += f"Changes just read back, waiting for yes/no: {state.pending}\n"
    fields = _field_lines(state, stage)
    return (
        f"You read ONE phone reply for {COMPANY}, a transport company. Return ONLY a JSON object.\n"
        f"Caller language: {LANG_NAMES[state.lang]} (speech-to-text may have mistakes; be tolerant). "
        f"Today: {today.isoformat()} ({today.strftime('%A')}).\n"
        f"Situation: {_STAGE_HINT.get(stage, '')}\n"
        f"Last thing we said: {question}\n"
        f"Valid answer: {expect}\n"
        "Fields (exact keys inside \"values\"; [now: ...] is what is stored already):\n"
        + ("\n".join(fields) if fields else "- none") + "\n"
        + facts + "\n" + _INTENT_RULES
    )


@dataclass
class MergeResult:
    newly: list = field(default_factory=list)      # slots filled or changed by this reply
    invalid: list = field(default_factory=list)    # slots where the caller gave something unusable
    old: dict = field(default_factory=dict)        # data key -> previous value (to undo a rejected correction)
    past: bool = False                             # a date in the past was seen
    matched: bool = False                          # at least one value belonged to a known slot


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------
class LogisticsAgent(Agent):
    def __init__(self, state: CallState, ctx: JobContext, classifier, voices: Voices) -> None:
        super().__init__(instructions=f"You are the phone assistant of {COMPANY}.")
        self.state, self.ctx, self._clf, self.voices = state, ctx, classifier, voices
        self._tasks: set[asyncio.Task] = set()
        self._json_mode = True       # switched off automatically if the LLM endpoint rejects it
        self._clf_fail = 0           # consecutive classifier failures
        self._json_errors = 0        # times the model produced invalid JSON (2 -> JSON mode off for this call)
        self._clf_skip_until = 0.0   # LLM is skipped until this monotonic time (circuit breaker)
        self._held = ""              # a cut-off sentence waiting for its continuation
        self._hold_task: asyncio.Task | None = None
        self._hold_count = 0
        self._lock = asyncio.Lock()  # one turn is processed at a time

    # -- speaking helpers ---------------------------------------------------
    def _say(self, text: str, interruptible: bool = True):
        self.state.log.append({"role": "assistant", "lang": self.state.lang, "text": text})
        return self.session.say(text, allow_interruptions=interruptible)   # streams to TTS as it is generated

    def _ask(self, text: str, prefix: str = "") -> None:
        self.state.last_prompt = text
        self._say(f"{prefix} {text}".strip())

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _ack(self) -> str:
        st = self.state
        a = ACKS[st.lang][st.ack_i % len(ACKS[st.lang])]
        st.ack_i += 1
        return a

    def silence_check(self) -> None:
        self._say(f"{t(self.state.lang, 'silence_check')} {self.state.last_prompt}")

    def give_up_silence(self) -> None:
        self._finish("no_response", bye_key="bye_no_response")

    # -- starting -----------------------------------------------------------
    async def begin(self, lang: str, purpose: str | None) -> None:
        """Called by inbound.py once the IVR has finished."""
        st = self.state
        st.lang = lang
        self.voices.apply(lang)
        st.live = True
        if purpose is None:
            self._finish("no_selection", bye_key="ivr_giveup")
            return
        await self.start_flow(purpose)

    async def start_flow(self, name: str, ask: bool = True) -> None:
        st = self.state
        st.flow, st.data, st.attempts, st.counters, st.shipment = FLOWS[name](), {}, {}, {}, None
        st.field_status, st.confirmed, st.pending = {}, False, {}
        st.call_type = st.call_type or st.flow.call_type
        st.stage = "slots"
        st.flow.prefill(st)
        if ask:
            await self._advance()

    async def _advance(self, ack: str = "", reply: str = "") -> None:
        """Ask the next MISSING question; when nothing is missing, read the summary or finish the flow."""
        st = self.state
        st.flow.prune(st)
        st.flow.audit(st)                                # drop impossible values (e.g. a 'place' = "But I'm")
        if bad := self._gate():                          # never read back or save a value the gate rejects
            st.stage, st.confirmed = "slots", False
            self._ask_invalid(bad[0])
            return
        prefix = f"{ack} {reply}".strip()
        open_ = st.flow.open_slots(st)
        if open_:
            if st.stage == "confirm":                    # a correction opened a new question (e.g. temperature)
                st.stage, st.confirmed = "slots", False
            self._ask(prompt_for_slot(st.lang, open_[0].id), prefix)
        elif st.flow.needs_confirmation and not st.confirmed:
            self._summarise(prefix)
        else:
            await self._complete(reply)                  # no "Got it." in front of the final result

    def _gate(self) -> list:
        """Non-LLM gate: drop any stored value that breaks the schema or is a non-answer. Returns the slots dropped."""
        st, flow, bad = self.state, self.state.flow, []
        for s in flow.slots:
            keys = flow._keys(s)
            if not any(k in st.data for k in keys):
                continue
            v = st.data.get(s.id)
            junk = (s.kind == "text" and isinstance(v, str)
                    and (s.id in FREE_TEXT_KEYS or s.id in NAME_KEYS) and is_non_answer(v))
            if junk or schema_violation(s.id, st.data):
                for k in keys:
                    st.data.pop(k, None)
                st.field_status[s.id] = "invalid"
                bad.append(s)
                logger.warning(f"validation gate dropped {s.id!r}: {v!r}")
        return bad

    def _summarise(self, prefix: str = "") -> None:
        st = self.state
        st.stage = "confirm"
        st.counters["await_fix"] = False
        st.counters["confirm_fail"] = 0
        self._ask(st.flow.summary(st), prefix)

    async def _confirm_yes(self) -> None:
        st = self.state
        st.confirmed = True
        st.flow.mark_confirmed(st)
        await self._complete()

    async def _complete(self, prefix: str = "") -> None:
        st = self.state
        flow = st.flow
        flow.audit(st)
        if bad := self._gate():
            st.stage, st.confirmed = "slots", False
            self._ask_invalid(bad[0])
            return
        if missing := flow.required_missing(st):         # never finish while mandatory data is missing
            st.stage = "slots"
            self._ask(prompt_for_slot(st.lang, missing[0].id), f"{prefix} {t(st.lang, 'need_detail')}".strip())
            return
        try:
            result = await flow.finish(st, self.ctx)
        except Exception as e:
            # Saving failed: never say "confirmed" and never loop. Tell ops and hand the caller to a person.
            logger.error(f"could not finish flow '{flow.name}': {type(e).__name__}: {e}")
            self._spawn(tools.notify("save_failed", {"flow": flow.name, "phone": st.phone, "language": st.lang,
                                                    "error": f"{type(e).__name__}: {e}", "data": dict(st.data)}))
            st.stage = "more"                            # whatever happens next, nothing is "confirmed"
            if can_transfer(st):
                await self._to_human("save_failed", intro=t(st.lang, "save_failed_human"))
            else:                                        # deterministic: no LLM, no "callback noted" claim, then hang up
                st.escalated = True
                self._finish("save_failed", text=_m(st.lang, "save_failed_callback"), bye_key="bye_no_response")
            return
        st.flows_done.append(flow.name)
        text = " ".join(result.messages)
        if result.transfer:
            st.stage, st.flow, st.data = "more", None, {}
            await self._to_human("urgent_issue", intro=text)
            return
        if result.next_stage == "manage":                # tracking: stay on this shipment
            st.stage, st.flow, st.data, st.pending = "manage", None, {}, {}
            st.counters["manage_tries"] = 0
            self._ask(t(st.lang, "manage_prompt"), f"{prefix} {text}".strip())
            return
        st.stage, st.flow, st.data = "more", None, {}
        st.counters["more_tries"] = 0
        self._ask(t(st.lang, "anything_else"), f"{prefix} {text}".strip())

    # -- ending the call ----------------------------------------------------
    def _finish(self, reason: str, text: str | None = None, bye_key: str = "bye_completed") -> None:
        """Speak the goodbye, wait until it has fully played, then hang up. Safe to call twice."""
        st = self.state
        if st.ending:
            return
        st.ending, st.outcome = True, reason

        async def _run():
            try:
                bye = t(st.lang, bye_key)
                full = f"{text} {bye}" if text else bye
                handle = self._say(full, interruptible=False)
                await asyncio.wait_for(handle.wait_for_playout(), timeout=PLAYOUT_TIMEOUT)
            except Exception as e:  # never leave the call hanging if speech fails
                logger.warning(f"goodbye playout problem ({type(e).__name__}): {e}")
            await hang_up(self.ctx, st, reason)

        self._spawn(_run())

    async def _to_human(self, reason: str, intro: str = "") -> None:
        """Hand the caller to the operations team (live transfer if configured, otherwise a call-back)."""
        st = self.state
        if st.ending:
            return
        st.escalated = True
        # Notification runs in the background: the caller hears the answer immediately.
        self._spawn(tools.notify("human_requested", {"reason": reason, "phone": st.phone, "language": st.lang,
                                                     "flows": st.flows_done, "collected": st.collected[-2:]}))
        if not can_transfer(st):
            self._finish("escalated", text=t(st.lang, "human_unavailable"))
            return

        st.live = False   # stop handling caller turns while we transfer

        async def _run():
            try:
                handle = self._say(intro or t(st.lang, "human_connect"), interruptible=False)
                await asyncio.wait_for(handle.wait_for_playout(), timeout=PLAYOUT_TIMEOUT)
            except Exception as e:
                logger.warning(f"transfer announcement problem: {e}")
            if await transfer_to_human(self.ctx, st):
                st.ending, st.outcome = True, "transferred"
                await hang_up(self.ctx, st, "transferred")
            else:
                st.live = True
                self._finish("escalated", text=t(st.lang, "transfer_failed"))

        self._spawn(_run())

    # -- the one LLM call per turn -----------------------------------------
    def _note_failure(self, why: str) -> None:
        self._clf_fail += 1
        logger.warning(f"classifier failure #{self._clf_fail}: {why}")
        if self._clf_fail >= CLF_FAIL_LIMIT:
            self._clf_skip_until = time.monotonic() + CLF_COOLDOWN
            self._clf_fail = 0
            logger.warning(f"classifier skipped for {CLF_COOLDOWN:.0f}s: local extraction only")

    async def _classify(self, question: str, expect: str, text: str, stage: str = "slots") -> dict:
        """ONE logical attempt with ONE overall deadline. Never raises. On failure: {'intent': 'unclear', '_failed': True}."""
        failed = {"intent": "unclear", "values": {}, "_failed": True}
        if time.monotonic() < self._clf_skip_until:
            return failed
        system = classifier_prompt(self.state, question, expect, stage, is_question(text))
        user = f"Caller reply: {text}"
        deadline = time.monotonic() + CLASSIFY_TIMEOUT
        use_json = self._json_mode

        for attempt in (1, 2):
            left = deadline - time.monotonic()
            if left < 0.3:
                self._note_failure("timeout")
                return failed
            try:
                raw = await asyncio.wait_for(classifier.complete(system, user, json_mode=use_json), timeout=left)
            except asyncio.TimeoutError:
                self._note_failure("timeout")
                return failed
            except Exception as e:
                full = _err_text(e).lower()
                if any(k in full for k in JSON_ERR_KEYS):
                    salvaged = clean_classification(parse_json_object(_failed_generation(e)))
                    if salvaged:
                        self._clf_fail = 0
                        return salvaged
                    self._json_errors += 1
                    if self._json_errors >= 2:
                        self._json_mode = False
                    if attempt == 1 and use_json:
                        use_json = False                  # retry ONCE, inside the same deadline
                        continue
                    self._note_failure("model returned invalid JSON (plain retry failed too)")
                    return failed
                if attempt == 1 and use_json and any(k in full for k in ("response_format", "json_object", "json mode")):
                    self._json_mode = use_json = False
                    continue
                self._note_failure(f"{type(e).__name__}: {full[:400]}")
                return failed
            data = clean_classification(parse_json_object(raw))
            if data:
                self._clf_fail = 0
                return data
            self._note_failure(f"unusable output {raw[:120]!r}")
            return failed
        self._note_failure("json mode retry exhausted")
        return failed


    # -- turn handling -----------------------------------------------------
    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage):
        st = self.state
        if st.ending or not st.live:
            raise StopResponse()
        text = (new_message.text_content or "").strip()
        if looks_like_hallucination(text):
            logger.info(f"ignored noise transcript: {text!r}")
            raise StopResponse()
        st.log.append({"role": "caller", "lang": st.lang, "text": text})
        st.silence_prompts = 0
        if self._hold_task:                              # the caller kept talking: join the pieces
            self._hold_task.cancel()
            self._hold_task = None
        if self._held:
            text, self._held = f"{self._held} {text}".strip(), ""
        if self._should_hold(text):                      # "I just" / "will" ... wait for the rest, do not answer yet
            self._hold_count += 1
            self._held = text
            self._hold_task = asyncio.create_task(self._release_held())
            raise StopResponse()
        self._hold_count = 0
        await self._process_turn(text)
        raise StopResponse()   # the code already spoke; never let the default LLM reply run

    def _should_hold(self, text: str) -> bool:
        return (self._hold_count < 1 and len(text.split()) <= 12 and is_fragment(text)
                 and yes_no(text) is None and not self.state.ending)

    async def _release_held(self) -> None:
        try:
            await asyncio.sleep(FRAGMENT_HOLD)
        except asyncio.CancelledError:
            return
        text, self._held, self._hold_task, self._hold_count = self._held, "", None, 0
        if text and not self.state.ending and self.state.live:
            await self._process_turn(text)

    async def _process_turn(self, text: str) -> None:
        st = self.state
        async with self._lock:
            t0 = time.monotonic()
            try:
                await self.handle_text(text)
            except Exception:
                logger.exception("turn handling failed")
                if not st.ending:
                    self._say(t(st.lang, "retry"))
            logger.info(f"turn handled in {time.monotonic() - t0:.2f}s: {text[:60]!r}")

    async def handle_text(self, text: str) -> None:
        st = self.state
        st.turns += 1
        words = len(text.split())

        # Cheap local checks first (no network): language switch, human, repeat, goodbye.
        sw = detect_switch(text) if words <= 8 else None
        if sw and sw != st.lang:
            await self._switch_language(sw)
            return
        if HUMAN_RE.search(text):
            await self._to_human("caller_asked")
            return
        if words <= 12 and (CLARIFY_RE.search(text) or REPEAT_RE.search(text)):
            self._repeat(simple=bool(CLARIFY_RE.search(text)))
            return
        # "nothing else" / "that's all" only ends the call when we asked "anything else?" - never in the
        # middle of a question or a confirmation ("that's all correct" must not hang up).
        soft_bye_ok = st.stage in ("more", "manage") and not st.pending
        if words <= 6 and (_HARD_BYE.search(text) or (soft_bye_ok and BYE_RE.search(text))):
            self._finish("completed")
            return
        if (wc := st.counters.get("weight_check")) is not None and st.flow:
            if await self._weight_check(text, wc):
                return
        if st.stage == "slots" and st.flow:
            if is_filler(text):                          # "Hello?" -> just ask again, no strike against the caller
                self._ask(self._current_prompt())
                return
            if words <= 4 and is_fragment(text):         # still a cut-off piece ("The destination is"): invite the rest
                self._ask(self._current_prompt(), _m(st.lang, "listening"))
                return
        if OFFICE_PHONE and PHONE_Q_RE.search(text) and st.stage in ("slots", "confirm", "manage", "more"):
            self._ask(self._current_prompt(), _m(st.lang, "office_phone").format(p=spoken_phone(st.lang, OFFICE_PHONE)))
            return
        # Questions the caller asks in the middle of a call ("Can I get it earlier?", "How much is it?") are answered
        # by code first, then the open question is asked again. No LLM needed, no value is lost.
        if is_question(text) and st.stage in ("slots", "confirm", "manage", "more") and not self._question_is_value(text):
            shipment_mode = bool(st.shipment and st.stage in ("manage", "more"))
            if hit := faq_answer(st.lang, text, shipment_mode):
                answer, event = hit
                if event:
                    self._spawn(tools.notify("caller_request", {
                        "request": event, "shipment": (st.shipment or {}).get("tracking_number"),
                        "phone": st.phone, "said": text}))
                    st.collected.append({"flow": "caller_request", "request": event, "said": text})
                self._ask(self._current_prompt(), answer)
                return
        if await self._take_places(text):                # places given out of order / both in one sentence
            return
        if await self._local_change(text):               # "destination is wrong" / "change the delivery date" ...
            return
        if st.stage == "more":
            await self._handle_more(text)
        elif st.stage == "confirm" and st.flow:
            await self._handle_confirm(text)
        elif st.stage == "manage":
            await self._handle_manage(text)
        elif st.flow:
            await self._handle_slots(text)

    async def _carry_change(self, text: str) -> None:
        """'For TRK 20002 I want to change the destination': after the shipment is read out, act on the change too."""
        found = parse_change(text)
        if self.state.stage == "manage" and found and found["cue"]:
            await self._local_change(text)

    async def _take_places(self, text: str) -> bool:
        """Caller named pickup and/or drop at any moment ("...starting from Hyderabad and my drop will be in
        Delhi") while we asked something else. Code reads both places and stores them. True = handled."""
        st = self.state
        if st.stage not in ("slots", "confirm") or not st.flow or st.flow.slot_for_key("pickup_location") is None:
            return False
        if st.counters.get("fix_key") or is_question(text):      # a pending "what is the new X?" / a real question
            return False
        vals = extract_places(text)
        if not vals:
            return False
        outcome = await self._apply_corrections(vals)           # overwrite=True, validates, asks the next question
        if outcome == "same":
            if st.stage == "confirm":
                self._summarise()
            else:
                self._ask(self._current_prompt())
        return True

    def _question_is_value(self, text: str) -> bool:
        """'Can you deliver it by November 6th?' answers the open question: it is a value, not a side question."""
        st = self.state
        if st.stage == "slots" and st.flow:
            open_ = st.flow.open_slots(st)
            if open_ and open_[0].kind == "date" and parse_spoken_date(text):
                return True
            if open_ and open_[0].kind == "tracking" and find_tracking(text):
                return True
        return False

    def _current_prompt(self) -> str:
        st = self.state
        if (wc := st.counters.get("weight_check")) is not None:
            return _m(st.lang, "weight_confirm").format(w=fmt_num(wc))
        if st.stage == "slots" and st.flow:
            open_ = st.flow.open_slots(st)
            if open_:
                return prompt_for_slot(st.lang, open_[0].id)
        if st.stage == "confirm" and st.flow:
            return st.flow.summary(st)
        if st.stage == "manage":
            if st.pending and st.shipment:
                return t(st.lang, "change_confirm", changes=describe_changes(st.lang, st.shipment, st.pending))
            return t(st.lang, "manage_prompt")
        return t(st.lang, "anything_else")

    def _current_slot_id(self) -> str | None:
        st = self.state
        if st.stage == "slots" and st.flow:
            open_ = st.flow.open_slots(st)
            if open_:
                return open_[0].id
        return None

    def _repeat(self, simple: bool = False) -> None:
        """1st 'repeat' = the same words again. 'I didn't understand' or a 2nd repeat = simpler words."""
        st = self.state
        base, sid = self._current_prompt(), self._current_slot_id()
        reps = st.counters.setdefault("repeats", {})
        key = sid or base
        reps[key] = reps.get(key, 0) + 1
        text = base
        if simple or reps[key] >= 2:
            text = _SIMPLE.get(st.lang, {}).get(sid or "") or base
        stripped = _LEAD_CONJ.sub("", text)
        if stripped != text:
            text = stripped[:1].upper() + stripped[1:]
        st.last_prompt = text
        self._say(f"{t(st.lang, 'repeat_prefix')} {text}")

    async def _switch_language(self, code: str) -> None:
        st = self.state
        st.lang = code
        self.voices.apply(code)
        st.last_prompt = self._current_prompt()
        self._say(f"{t(code, 'lang_switched')} {st.last_prompt}")

    async def _general(self, res: dict) -> bool:
        """Intents that mean the same in every stage. Returns True when handled."""
        st, intent = self.state, res.get("intent")
        if intent == "repeat":
            self._repeat(simple=False)
        elif intent == "human":
            await self._to_human("caller_asked")
        elif intent == "switch_language" and res.get("language") in LANG_NAMES:
            await self._switch_language(res["language"])
        elif intent == "switch_flow" and res.get("flow") in FLOWS and res["flow"] != (st.flow.name if st.flow else None):
            await self.start_flow(res["flow"])
        else:
            return False
        return True

    # -- "anything else?" stage -------------------------------------------
    async def _handle_more(self, text: str) -> None:
        st = self.state
        yn = yes_no(text)
        if yn is False:
            self._finish("completed")
            return
        if find_tracking(text):
            await self.start_flow("track", ask=False)
            await self._handle_slots(text)
            await self._carry_change(text)
            return
        if yn is True:
            self._ask(t(st.lang, "how_can_i_help"))
            return
        res = await self._classify(t(st.lang, "anything_else"),
                                   "whether they need more help and, if so, which kind", text, "more")
        intent = res.get("intent")
        if await self._general(res):
            return
        if intent == "decline":
            self._finish("completed")
        elif intent == "question":
            reply = res.get("reply") or t(st.lang, "followup_later")
            self._ask(t(st.lang, "anything_else"), reply)
        else:
            st.counters["more_tries"] = st.counters.get("more_tries", 0) + 1
            if st.counters["more_tries"] >= 2:
                self._finish("completed")
            else:
                self._ask(t(st.lang, "how_can_i_help"))

    # -- slot filling -------------------------------------------------------
    def _fast_path(self, slot, text: str) -> dict | None:
        """Obvious answers skip the LLM completely (saves a full network round-trip)."""
        n_words = len(text.split())
        if slot.kind == "tracking" and (tn := find_tracking(text)):
            return {"intent": "answer", "values": {slot.id: tn}}
        if slot.kind == "yesno" and (yn := yes_no(text)) is not None:
            return {"intent": "answer", "values": {slot.id: yn}}
        if slot.kind == "number" and PLAIN_NUMBER_RE.fullmatch(text):
            return {"intent": "answer", "values": {slot.id: text}}
        if slot.kind == "email" and (mail := to_email(text)):
            return {"intent": "answer", "values": {slot.id: mail}}
        # A date is read by code in ANY wording: "in 15 days", "after fifteen days", "I want it delivered by
        # November 6th". Only skipped for real questions and when pickup AND delivery are both mentioned.
        if slot.kind == "date" and n_words <= 14 and not _DATE_QUESTION.match(text) and not _BOTH_DATES(text) \
                and (d := parse_spoken_date(text)):
            return {"intent": "answer", "values": {slot.id: d}}
        if is_question(text) or _CORRECTION_CUES.search(text) or re.search(r"\b(hello|hi|hey|guten tag|dzień dobry)\b", text, re.I):
            return None                                   # questions / corrections / greetings need the LLM
        if slot.kind == "number" and n_words <= 4 and not re.search(r"\bton", text, re.I) \
                and (m := re.fullmatch(r"\D{0,12}?(\d[\d\s.,]*)\s*(?:kg|kgs|kilo|kilos|kilogram|kilograms)?\W*", text, re.I)):
            return {"intent": "answer", "values": {slot.id: m.group(1).strip()}}
        if slot.kind == "number" and slot.id == "weight_kg" and n_words <= 14 and not re.search(r"\bton|half", text, re.I):
            # "The approximate weight will be 100 kilograms": exactly one number in the sentence = the weight
            spoken = numwords_to_digits(text)
            if len(re.findall(r"\d[\d.,]*\d|\d", spoken)) == 1:
                return {"intent": "answer", "values": {slot.id: spoken}}
        if slot.kind == "range" and n_words <= 8 and len(temperature_numbers(text)) >= 2:
            return self._local_fallback(slot, text)
        if slot.kind == "minutes" and text.strip(" .,!?").lower() in NONE_WORDS:
            return {"intent": "answer", "values": {slot.id: 0}}          # "none" = no waiting time
        if slot.kind == "minutes" and n_words <= 4 and not re.search(r"hour|stunde|godzin", text, re.I) \
                and (n := to_number(text)) is not None:
            return {"intent": "answer", "values": {slot.id: n}}
        if slot.kind == "text" and slot.id in ("cargo_type", "contact_name") and re.search(r"\d", text):
            return None                                   # "1.5 tonnes" is not a cargo type: let the LLM sort it out
        if slot.kind == "text" and (yes_no(text) is None or slot.id == "special_requirements"):
            if (f := detect_field(text)) and f != slot.id:
                return None                               # caller gave another field out of order
            # short plain answer ("Guindy", "pallets", "from A to B") OR a longer sentence that is just a lead-in
            # around a short value ("I want it to be delivered in Guindy", "The destination is going to be Guindy").
            stripped = clean_text_value(text, slot.id in LOCATION_SLOTS)
            lead_in_only = (n_words <= 14 and len(stripped.split()) <= 4
                            and stripped.lower() != text.strip(" .,;:!?\"").lower())
            if n_words <= 4 or lead_in_only:
                res = self._local_fallback(slot, text)
                if not res.get("_failed"):
                    return res

    def _tracking_pieces(self, text: str) -> tuple[bool, str | None]:
        """Phone speech-to-text often splits a number into pieces ("three zero three" ... "zero zero").
        Short digit groups are kept and the caller is asked for the rest, instead of looking up half a number.
        Returns (handled, number): handled=True means the agent already spoke; number = a complete TRK-... to use."""
        st = self.state
        partial = st.counters.get("trk_partial", "")
        digits = spoken_digits(text)
        if digits is None:
            if partial and len(text.split()) <= 4:         # "that's it / that's all": the short number WAS complete
                st.counters["trk_partial"] = ""
                return False, f"TRK-{partial}"
            return False, None
        if _EXPLICIT_TRK.search(text) or len(digits) >= TRK_MIN_DIGITS:
            partial = ""                                    # a fresh, full attempt: start over
        combined = partial + digits
        if len(combined) >= TRK_MIN_DIGITS:
            st.counters["trk_partial"] = ""
            return False, f"TRK-{combined}"
        st.counters["trk_partial"] = combined
        self._ask(t(st.lang, "track_partial", digits=spell(f"TRK-{combined}")))
        return True, None

    def _local_fallback(self, slot, text: str) -> dict:
        """No LLM available (timeout / bad JSON / cool-down): extract what can be extracted with plain code."""
        vals: dict = {}
        k = slot.kind
        if k == "email":
            if mail := to_email(text):
                vals[slot.id] = mail
        elif k == "tracking":
            if tn := find_tracking(text):
                vals[slot.id] = tn
        elif k in ("number", "minutes"):
            if (n := to_number(text)) is not None:
                vals[slot.id] = text if slot.id == "weight_kg" else n    # keep the unit words: tonnes -> kg later
        elif k == "yesno":
            if (yn := yes_no(text)) is not None:
                vals[slot.id] = yn
        elif k == "range":
            nums = temperature_numbers(text)
            if len(nums) >= 2:
                vals = {"temperature_min_c": min(nums[:2]), "temperature_max_c": max(nums[:2])}
        elif k == "date":
            if not _BOTH_DATES(text) and (d := parse_spoken_date(text)):
                vals[slot.id] = d
        elif k == "text" and slot.id in FREE_TEXT_SLOTS:       # a description: the caller's own words ARE the value
            if 0 < len(text.split()) <= 40 and not is_filler(text) and not is_non_answer(clean_text_value(text)):
                vals[slot.id] = clean_text_value(text)
        elif k == "text":
            if slot.id in ("pickup_location", "delivery_location") and (route := extract_route(text)):
                vals = route
            else:
                cleaned = clean_text_value(text, slot.id in LOCATION_SLOTS)
                if (len(cleaned.split()) <= 4 and (slot.id not in LOCATION_SLOTS or plausible_place(cleaned))
                        and not (slot.id in NAME_KEYS and is_field_phrase(cleaned))):
                    vals[slot.id] = cleaned          # a long sentence / a non-place is NOT a value: ask again
        return {"intent": "answer", "values": vals} if vals else {"intent": "unclear", "values": {}, "_failed": True}

    async def _handle_slots(self, text: str) -> None:
        st, flow = self.state, self.state.flow
        open_ = flow.open_slots(st)
        if not open_:
            await self._advance()
            return
        slot = open_[0]
        question = prompt_for_slot(st.lang, slot.id)

        if slot.id == "special_requirements" and is_yes_without_detail(text):
            self._ask(_ASK_SPECIAL_DETAIL.get(st.lang) or _ASK_SPECIAL_DETAIL["en"])   # "yes" -> ask WHAT
            return

        forced = None
        if slot.kind == "tracking":
            handled, forced = self._tracking_pieces(text)
            if handled:
                return
        res = ({"intent": "answer", "values": {slot.id: forced}} if forced else self._fast_path(slot, text))
        if res is None:
            res = await self._classify(question, slot.expect, text, "slots")
            if res.get("_failed"):
                res = self._local_fallback(slot, text)
        intent, reply = res.get("intent"), res.get("reply", "")

        if intent == "question":
            why = _WHY.get(slot.id, {}).get(st.lang) or _WHY.get(slot.id, {}).get("en")
            self._ask(question, reply or why or t(st.lang, "followup_later"))
        elif intent == "decline":
            self._finish("completed")
        elif await self._general(res):
            pass
        elif intent in ("correct", "update"):
            await self._apply_corrections(res.get("values") or {}, reply)
        elif intent == "answer":
            await self._take_answer(slot, res, text, reply)
        else:
            await self._unclear(slot)

    def _merge(self, values: dict, overwrite: bool = False) -> MergeResult:
        """Put the extracted values into state.data. overwrite=True is for corrections."""
        st, flow = self.state, self.state.flow
        pending, res = dict(values), MergeResult()
        for _ in range(3):   # extra passes let "temperature controlled, 2 to 8" fill both slots in one turn
            progressed = False
            for key, raw in list(pending.items()):
                slot = flow.slot_for_key(key)
                if slot is None and key in ("tracking_number", "shipment_number"):
                    slot = flow.slot_for_key("shipment_number" if key == "tracking_number" else "tracking_number")
                if slot is None:
                    pending.pop(key)
                    continue
                res.matched = True
                if not slot.required_if(st):
                    continue                      # not needed yet: retry on the next pass
                data_key = key if slot.kind == "range" else slot.id
                had_old = data_key in st.data
                if had_old and not overwrite:
                    pending.pop(key)
                    continue
                pending.pop(key)
                try:
                    value = normalize_value(slot.kind, raw, slot.id)
                except PastDate:
                    res.past = True
                    continue
                if value is None:
                    if slot not in res.invalid:
                        res.invalid.append(slot)
                    continue
                if had_old and st.data[data_key] == value:
                    continue                      # same value again: not a change
                if had_old:
                    res.old[data_key] = st.data[data_key]
                st.data[data_key] = value
                progressed = True
                if flow.slot_done(slot, st.data) and slot not in res.newly:
                    res.newly.append(slot)
                    st.field_status[slot.id] = "corrected" if had_old else "collected"
            if not pending or not progressed:
                break
        # Schema guard: a value of the right kind but outside its allowed range is NOT written (treated as invalid).
        for slot in list(res.newly):
            if schema_violation(slot.id, st.data):
                for k in flow._keys(slot):
                    if k in res.old:
                        st.data[k] = res.old[k]
                    else:
                        st.data.pop(k, None)
                st.field_status.pop(slot.id, None)
                res.newly.remove(slot)
                if slot not in res.invalid:
                    res.invalid.append(slot)
        return res

    async def _validate_merge(self, m: MergeResult) -> str | None:
        """Business rules on what just changed. On failure the change is undone and the message returned."""
        st, flow = self.state, self.state.flow
        for s in m.newly:
            msg = await flow.validate(s, st)
            if not msg and s.id == "weight_kg" and self._weight_suspicious(st.data.get("weight_kg")):
                st.counters["weight_check"] = st.data["weight_kg"]       # keep it aside until the caller says yes
                st.counters["weight_check_tries"] = 0
                msg = _m(st.lang, "weight_confirm").format(w=fmt_num(st.data["weight_kg"]))
            if msg:
                for k in flow._keys(s):
                    if k in m.old:
                        st.data[k] = m.old[k]
                    else:
                        st.data.pop(k, None)
                        st.field_status.pop(s.id, None)
                return msg
        return None

    def _weight_suspicious(self, v) -> bool:
        """A very light or fractional weight is usually a mis-heard number: read it back before keeping it."""
        if not isinstance(v, (int, float)) or self.state.counters.get("weight_ok") == v:
            return False
        return v < WEIGHT_CONFIRM_BELOW_KG or (WEIGHT_CONFIRM_DECIMALS and float(v) != int(v))

    async def _weight_check(self, text: str, pending) -> bool:
        """Caller answers "Did you say 2.2 kilograms?". True = handled."""
        st = self.state
        yn = yes_no(text)
        new = None
        if yn is None and to_number(text) is not None:
            try:
                new = normalize_value("number", text, "weight_kg")
            except Exception:
                new = None
        if yn is True or (new is not None and new == pending):
            for k in ("weight_check", "fix_key"):
                st.counters.pop(k, None)
            st.counters["weight_ok"] = pending
            st.data["weight_kg"] = pending
            st.field_status["weight_kg"] = "collected"
            ack = t(st.lang, "corrected_ack") if st.stage == "confirm" else self._ack()
            await self._advance(ack)
            return True
        if yn is False:
            st.counters.pop("weight_check", None)
            if st.stage == "confirm":
                st.counters["fix_key"] = "weight_kg"          # the next reply is the new weight
            self._ask(prompt_for_slot(st.lang, "weight_kg"))
            return True
        if new is not None:                                  # a different number: the normal path checks it again
            st.counters.pop("weight_check", None)
            return False
        tries = st.counters["weight_check_tries"] = st.counters.get("weight_check_tries", 0) + 1
        if tries >= MAX_REASK:                               # off-topic again and again: ask the weight afresh
            st.counters.pop("weight_check", None)
            self._ask(prompt_for_slot(st.lang, "weight_kg"), t(st.lang, "retry"))
        else:
            self._ask(_m(st.lang, "weight_confirm").format(w=fmt_num(pending)))
        return True

    def _ask_invalid(self, slot) -> None:
        """The caller said something for this field, but it is unusable (e.g. a broken e-mail)."""
        st = self.state
        st.field_status[slot.id] = "invalid"
        n = st.attempts[slot.id] = st.attempts.get(slot.id, 0) + 1     # invalid answers used to loop forever
        if slot.optional and n >= MAX_REASK:
            st.data[slot.id] = None                                   # give up on an optional detail, move on
            self._spawn(self._advance())
            return
        if n >= MAX_REASK:
            self._spawn(self._to_human("unclear_input", intro=t(st.lang, "unclear_human")))
            return
        key = f"invalid_{slot.id}"
        if key in T:
            self._ask(t(st.lang, key))
        else:
            self._ask(prompt_for_slot(st.lang, slot.id), t(st.lang, "retry"))

    async def _take_answer(self, slot, res: dict, text: str, reply: str) -> None:
        st, flow = self.state, self.state.flow
        m = self._merge(res.get("values") or {})
        if not m.newly and m.past:
            self._ask(t(st.lang, "date_past"))
            return
        if not m.matched and slot.kind == "text" and not flow.slot_done(slot, st.data):
            cleaned = clean_text_value(text, slot.id in LOCATION_SLOTS)    # LLM said "answer" with a wrong key
            if (len(cleaned.split()) <= 6 and (slot.id not in LOCATION_SLOTS or plausible_place(cleaned))
                    and normalize_value("text", cleaned, slot.id)):        # label echoes / filler are rejected here
                st.data[slot.id] = normalize_value("text", cleaned, slot.id)
                st.field_status[slot.id] = "collected"
                m.newly.append(slot)
        if not m.newly:
            if m.invalid:
                self._ask_invalid(m.invalid[0])
            else:
                await self._unclear(slot)
            return
        if msg := await self._validate_merge(m):
            if st.counters.get("want_human"):
                await self._to_human("shipment_not_found", intro=t(st.lang, "not_found_human"))
            else:
                self._ask(msg)
            return
        for s in m.invalid:
            st.field_status[s.id] = "invalid"      # unusable extra value: it will simply be asked for later
        st.attempts.pop(slot.id, None)
        if slot.kind == "date" and st.data.get(slot.id) and not reply:
            # say the calculated date back ("in 15 days" -> "That would be November 18th") so a wrong guess is heard
            reply = _m(st.lang, "date_understood").format(d=fmt_date(st.lang, st.data[slot.id]))
        await self._advance(self._ack(), reply)

    async def _apply_corrections(self, values: dict, reply: str = "") -> str:
        """The caller changed something already given. Returns changed | same | nomatch | handled."""
        st = self.state
        m = self._merge(values, overwrite=True)
        if not m.matched:
            if st.stage == "confirm":
                st.counters["await_fix"] = True
                self._ask(t(st.lang, "confirm_fix_ask"), t(st.lang, "retry"))
            else:
                self._ask(self._current_prompt(), t(st.lang, "retry"))
            return "nomatch"
        if m.past and not m.newly:
            self._ask(t(st.lang, "date_past"))
            return "handled"
        if msg := await self._validate_merge(m):
            self._ask(msg)
            return "handled"
        if m.invalid and not m.newly:
            self._ask_invalid(m.invalid[0])
            return "handled"
        if not m.newly:
            return "same"
        await self._advance(t(st.lang, "corrected_ack"), reply)
        return "changed"

    def _slot_for(self, key: str):
        st = self.state
        if st.stage == "manage":
            return edit_slot_for_key(key)
        return st.flow.slot_for_key(key) if st.flow else None

    @staticmethod
    def _values_for(slot, text: str) -> dict:
        """Turn the words the caller said for ONE field into {key: value} with plain code. {} = unusable."""
        rest, k = (text or "").strip(), slot.kind
        if not rest:
            return {}
        if k == "range":
            nums = temperature_numbers(rest)
            return ({"temperature_min_c": min(nums[:2]), "temperature_max_c": max(nums[:2])}
                    if len(nums) >= 2 else {})
        if k == "date":
            d = parse_spoken_date(rest)
            return {slot.id: d} if d else {}
        if k == "email":
            mail = to_email(rest)
            return {slot.id: mail} if mail else {}
        if k in ("number", "minutes"):
            n = to_number(rest)
            return {slot.id: rest if slot.id == "weight_kg" else n} if n is not None else {}
        if k == "yesno":
            yn = yes_no(rest)
            return {slot.id: yn} if yn is not None else {}
        if k == "text":
            val = clean_text_value(rest, slot.id in LOCATION_SLOTS)
            if slot.id in LOCATION_SLOTS and not plausible_place(val):
                return {}
            if slot.id in NAME_KEYS and is_field_phrase(val):
                return {}                                 # "Delivery detail" names a field, it is not a value
            return {slot.id: val} if val else {}
        return {}

    def _ask_field(self, slot, prefix: str = "") -> None:
        st = self.state
        label = FIELD_LABELS[st.lang].get(slot.id)
        if label and slot.kind in ("text", "number"):
            self._ask(t(st.lang, "change_value_ask", f=label), prefix)     # "What should the new weight be?"
        elif f"ask_{slot.id}" in T:
            self._ask(prompt_for_slot(st.lang, slot.id), prefix)
        else:
            self._ask(t(st.lang, "no_change_understood"), prefix)

    async def _not_editable(self, key: str, rest) -> None:
        st = self.state
        self._spawn(tools.notify("change_requested", {
            "shipment": st.shipment["tracking_number"], "requested": {key: rest},
            "reason": "not_editable_by_phone", "phone": st.phone}))
        self._ask(t(st.lang, "manage_more"), _m(st.lang, "not_editable"))

    async def _apply_field(self, slot, rest) -> None:
        """Caller named a field (and maybe the new value). Value known -> apply. Value unknown -> ask for it."""
        st = self.state
        values = self._values_for(slot, rest) if rest else {}
        if not values:
            st.counters["fix_key"] = slot.id            # the NEXT reply is the value for this field
            tries = st.counters["fix_tries"] = st.counters.get("fix_tries", 0) + (1 if rest else 0)
            if tries >= 3:
                st.counters.pop("fix_key", None)
                st.counters["fix_tries"] = 0
                self._ask(t(st.lang, "no_change_understood" if st.stage == "manage" else "confirm_fix_ask"))
                return
            self._ask_field(slot, t(st.lang, "retry") if rest else "")
            return
        if st.stage == "manage":
            st.counters.pop("fix_key", None)
            await self._not_editable(slot.id, rest)
            return
        outcome = await self._apply_corrections(values)
        if outcome == "handled":                         # value rejected: keep waiting for a better one
            st.counters["fix_key"] = slot.id
            return
        st.counters.pop("fix_key", None)
        st.counters["fix_tries"] = 0
        if outcome == "same":                            # nothing changed: say something anyway
            if st.stage == "confirm":
                self._summarise()
            else:
                self._ask(self._current_prompt())

    async def _local_change(self, text: str) -> bool:
        """No-LLM handling of corrections, so a failing classifier can never block them. True = handled."""
        st = self.state
        if st.stage not in ("slots", "confirm", "manage"):
            return False
        manage = st.stage == "manage"
        if manage and st.pending and yes_no(text) is not None:
            return False                                 # yes/no to a change that was just read back
        found = parse_change(text)
        fix_key = st.counters.get("fix_key")

        if fix_key:                                      # we asked: "what is the new <field>?"
            key, rest = fix_key, text
            if found and found["key"] != fix_key and (found["cue"] or is_field_phrase(text)):
                key, rest = found["key"], found["rest"]  # they changed their mind about WHICH field
            elif is_field_phrase(text):
                rest = None                              # they only repeated the field name: ask for the value again
            elif found and found["key"] == fix_key and found["rest"]:
                rest = found["rest"]
            slot = self._slot_for(key)
            if slot is None or "?" in text or (slot.kind != "yesno" and yes_no(text) is not None):
                st.counters.pop("fix_key", None)
                return False
            await self._apply_field(slot, rest)
            return True

        if not found:
            return False
        key, slot = found["key"], self._slot_for(found["key"])
        if slot is None:
            if manage and (found["cue"] or found["rest"]):
                await self._not_editable(key, found["rest"])   # e.g. delivery DATE of an existing shipment
                return True
            return False
        if manage:
            await self._not_editable(key, found["rest"])
            return True
        if st.stage == "slots":
            if not found["cue"]:
                return False
            open_ = st.flow.open_slots(st)
            if open_ and key == open_[0].id:
                return False                             # plain answer to the question just asked
        await self._apply_field(slot, found["rest"])
        return True

    async def _unclear(self, slot) -> None:
        """Polite re-ask. Optional fields can be skipped; mandatory ones are asked again, then a human helps."""
        st = self.state
        n = st.attempts[slot.id] = st.attempts.get(slot.id, 0) + 1
        if slot.optional and n >= 2:
            st.data[slot.id] = None
            await self._advance()
            return
        if n >= MAX_REASK:
            await self._to_human("unclear_input", intro=t(st.lang, "unclear_human"))
            return
        prefix = t(st.lang, "retry")
        if n >= 2 and not slot.optional:
            prefix += " " + t(st.lang, "need_detail")
        self._ask(prompt_for_slot(st.lang, slot.id), prefix)

    # -- confirmation stage (new requests) ----------------------------------
    async def _handle_confirm(self, text: str) -> None:
        st = self.state
        yn = yes_no(text)
        if yn is True:
            await self._confirm_yes()
            return
        if yn is False:
            if st.counters.get("await_fix"):         # "no" to "which detail?": read it back again
                self._summarise()
            else:
                st.counters["await_fix"] = True
                self._ask(t(st.lang, "confirm_fix_ask"))
            return
        res = await self._classify(st.last_prompt, "yes/no to the summary, or the detail they want to change",
                                   text, "confirm")
        if res.get("_failed"):
            n = st.counters["confirm_fail"] = st.counters.get("confirm_fail", 0) + 1
            st.counters["await_fix"] = True
            if n == 1:
                self._ask(t(st.lang, "confirm_fix_ask"))
            elif n == 2:
                self._ask(t(st.lang, "confirm_fix_list"), t(st.lang, "retry"))     # offer concrete examples
            else:
                await self._to_human("unclear_input", intro=t(st.lang, "unclear_human"))
            return
        st.counters["confirm_fail"] = 0
        intent, values = res["intent"], res.get("values") or {}
        if intent == "question":
            self._ask(st.last_prompt, res.get("reply") or t(st.lang, "followup_later"))
        elif intent == "decline":
            self._finish("completed")
        elif await self._general(res):
            return
        elif values and intent in ("correct", "update", "answer", "confirm_no", "confirm_yes"):
            outcome = await self._apply_corrections(values, res.get("reply", ""))
            if outcome == "same":                    # nothing really changed
                if intent == "confirm_yes":
                    await self._confirm_yes()
                else:
                    self._summarise()
            elif outcome == "nomatch" and intent == "confirm_yes":
                await self._confirm_yes()
        elif intent == "confirm_yes":
            await self._confirm_yes()
        else:                                        # confirm_no without details, or unclear
            st.counters["await_fix"] = True
            self._ask(t(st.lang, "confirm_fix_ask"))

    # -- existing shipment: questions, additions, changes ---------------------
    def _edit_block(self) -> str | None:
        st, shp = self.state, self.state.shipment
        if shp["status"] == "delivered":
            return "change_locked"
        if EDIT_REQUIRES_OWNER and (not st.customer or st.customer.get("id") != shp.get("customer_id")):
            return "change_owner"
        return None

    async def _handle_manage(self, text: str) -> None:
        st, shp = self.state, self.state.shipment
        if shp is None:
            st.stage = "more"
            self._ask(t(st.lang, "anything_else"))
            return
        if _EXPLICIT_TRK.search(text) and (tn := find_tracking(text)) and tn != shp["tracking_number"]:
            await self.start_flow("track", ask=False)        # a different shipment
            await self._handle_slots(text)
            await self._carry_change(text)
            return
        yn = yes_no(text)
        if st.pending:
            if yn is True:
                await self._apply_pending()
                return
            if yn is False:
                st.pending = {}
                self._ask(t(st.lang, "change_cancelled"))
                return
            slot = edit_slot_for_key(st.counters.get("last_change_key", ""))
            if slot and not is_question(text) and (vals := self._values_for(slot, text)):
                await self._queue_changes(vals)          # a new value instead of yes/no: "Guindy" replaces the old one
                return
        elif yn is False:                                    # "no" to "anything else about this shipment?"
            st.stage = "more"
            self._ask(t(st.lang, "anything_else"))
            return
        elif yn is True:
            self._ask(t(st.lang, "manage_prompt"))
            return

        if not st.pending and (ans := local_answer(st.lang, shp, text)):
            self._ask(t(st.lang, "manage_more"), ans)    # status / weight / destination ... answered without the LLM
            return
        res = await self._classify(st.last_prompt, "a question about the shipment, or a change to it", text, "manage")
        intent, values, reply = res.get("intent"), res.get("values") or {}, res.get("reply", "")
        if res.get("_failed"):
            n = st.counters["manage_fail"] = st.counters.get("manage_fail", 0) + 1
            if is_question(text):
                self._ask(t(st.lang, "manage_more"), t(st.lang, "followup_later"))
            elif n >= 3:
                st.counters["manage_fail"] = 0
                st.stage = "more"
                self._ask(t(st.lang, "anything_else"))
            else:
                self._ask(t(st.lang, "manage_help"), t(st.lang, "retry") if n == 1 else "")
        elif intent == "decline":
            st.pending = {}
            st.stage = "more"
            self._ask(t(st.lang, "anything_else"))
        elif await self._general(res):
            return
        elif st.pending and intent == "confirm_yes" and not values:
            await self._apply_pending()
        elif st.pending and intent == "confirm_no" and not values:
            st.pending = {}
            self._ask(t(st.lang, "change_cancelled"))
        elif values and intent in ("update", "correct", "answer", "confirm_no", "confirm_yes"):
            key = next(iter(values))
            await self._not_editable(key, values[key])
        elif intent == "question":
            self._ask(t(st.lang, "manage_more") if not st.pending else self._current_prompt(),
                      reply or t(st.lang, "followup_later"))
        else:
            st.counters["manage_tries"] = st.counters.get("manage_tries", 0) + 1
            if st.counters["manage_tries"] >= 3:
                st.stage = "more"
                self._ask(t(st.lang, "anything_else"))
            else:
                self._ask(t(st.lang, "manage_help"))

    async def _queue_changes(self, values: dict) -> None:
        """Existing shipments cannot be modified."""
        key = next(iter(values)) if values else "details"
        await self._not_editable(key, values.get(key) if values else "")

    async def _apply_pending(self) -> None:
        self.state.pending = {}


# ---------------------------------------------------------------------------
async def hang_up(ctx: JobContext, state: CallState, reason: str):
    state.ending = True
    if state.outcome in ("in_progress", "completed"):
        state.outcome = reason
    await asyncio.sleep(0.3)      # let the last audio frames leave before the room closes
    try:
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
    except Exception as e:   # console mode has no real room
        logger.info(f"delete_room skipped: {e}")
    finally:
        ctx.shutdown(reason=reason)