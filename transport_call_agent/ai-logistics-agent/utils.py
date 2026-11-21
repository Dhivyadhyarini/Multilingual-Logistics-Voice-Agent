"""Small helpers: JSON parsing/repair, noise filter, and value cleaning (tracking numbers, numbers, dates, e-mail...)."""
from __future__ import annotations

import calendar
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta

from config import (ADR_MAX_CLASS, ADR_MAX_DIVISION, NOT_PLACE_WORDS, TEMP_MAX_C, TEMP_MIN_C,
                    TRK_FIRST_LETTERS, TRK_LAST_LETTERS)


# ---------------------------------------------------------------------------
# LLM JSON
# ---------------------------------------------------------------------------
def _repair_json(s: str) -> str:
    """Close a JSON object that was cut off (max_tokens, dropped stream) so what arrived is still usable."""
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    fixed = s + ('"' if in_str else "")
    fixed = re.sub(r',?\s*"[^"\\]*"\s*:\s*$', "", fixed)      # dangling  "key":
    fixed = re.sub(r",\s*$", "", fixed)
    return fixed + "".join(reversed(stack))


def parse_json_object(raw: str) -> dict | None:
    """Pull the first {...} block out of an LLM reply (tolerates code fences, chatter and truncation)."""
    if not raw:
        return None
    raw = raw.replace("```json", "").replace("```", "")
    start = raw.find("{")
    if start == -1:
        return None
    end = raw.rfind("}")
    candidates = []
    if end > start:
        candidates.append(raw[start:end + 1])
    candidates.append(_repair_json(raw[start:]))
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def looks_like_hallucination(text: str) -> bool:
    """True for empty / noise-only transcripts and repetition loops."""
    words = re.sub(r"[^\w' ]", " ", text.lower()).split()
    if not words:
        return True
    grams = Counter(tuple(words[i:i + 5]) for i in range(len(words) - 4))
    return bool(grams) and max(grams.values()) >= 3


# ---------------------------------------------------------------------------
# Phone numbers
# ---------------------------------------------------------------------------
def norm_phone(p: str | None) -> str | None:
    if not p:
        return None
    p = re.sub(r"[^\d+]", "", p.replace("sip:", "").split("@")[0])
    return p or None


# ---------------------------------------------------------------------------
# Tracking numbers: "TRK-48291", "trk 48291", "T R K 4 8 2 9 1", or just "48291"
# ---------------------------------------------------------------------------
_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9",
    "null": "0", "eins": "1", "ein": "1", "zwei": "2", "drei": "3", "vier": "4", "fünf": "5", "fuenf": "5",
    "sechs": "6", "sieben": "7", "acht": "8", "neun": "9",
    "zerem": "0", "jeden": "1", "dwa": "2", "trzy": "3", "cztery": "4", "pięć": "5", "piec": "5",
    "sześć": "6", "szesc": "6", "siedem": "7", "osiem": "8", "dziewięć": "9", "dziewiec": "9",
}
_REPEAT_WORDS = {"double": 2, "triple": 3, "doppel": 2, "dubel": 2}


def words_to_digits(text: str) -> str:
    """'T R K three zero double zero three' -> 'T R K 3 0 0 0 3' (English / German / Polish number words)."""
    out, rep = [], 1
    for tok in re.findall(r"[^\W_]+|[^\w\s]", text or "", re.U):
        low = tok.lower()
        if low in _REPEAT_WORDS:
            rep = _REPEAT_WORDS[low]
        elif low in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[low] * rep)
            rep = 1
        else:
            out.append(tok)
            rep = 1
    return " ".join(out)


_TRK_HINT = re.compile(r"tracking|shipment|number|trk|nummer|sendung|numer|przesy", re.I)
# "B R K", "E R K", "T R C" ... are how the speech-to-text often hears "T R K" (letters come from config.py)
TRK_FUZZY_RE = re.compile(rf"\b[{TRK_FIRST_LETTERS}]\s*-?\s*r\s*-?\s*[{TRK_LAST_LETTERS}]\b", re.I)


def fix_trk(text: str) -> str:
    return TRK_FUZZY_RE.sub("TRK", text or "")


def spoken_digits(text: str) -> str | None:
    """The digits in a reply that is ONLY a number (digits, number words, 'TRK', punctuation); else None."""
    conv = words_to_digits(fix_trk(text))
    rest = re.sub(r"\bt\s*-?\s*r\s*-?\s*k\b", "", conv, flags=re.I)
    if re.search(r"[^\W\d_]{2,}", rest):            # real words left: not a bare number
        return None
    digits = re.sub(r"\D", "", rest)
    return digits or None


def find_tracking(text: str) -> str | None:
    text = fix_trk(text)
    for candidate in (text or "", words_to_digits(text)):
        squashed = re.sub(r"[^A-Za-z0-9]", "", candidate).upper()
        m = re.search(r"TRK(\d{3,10})", squashed)
        if m:
            return f"TRK-{m.group(1)}"
        if re.fullmatch(r"\d{4,10}", squashed):          # caller only said the digits
            return f"TRK-{squashed}"
        if _TRK_HINT.search(candidate):                 # "my tracking number is 4 8 2 9 1"
            digits = re.sub(r"\D", "", candidate)
            if 4 <= len(digits) <= 10 and len(candidate.split()) <= 14:
                return f"TRK-{digits}"
    return None


def spell(code: str) -> str:
    """'TRK-48291' -> 'T R K 4 8 2 9 1' so text-to-speech reads it slowly and clearly."""
    return " ".join(ch for ch in code if ch.isalnum())


# ---------------------------------------------------------------------------
# Taking the VALUE out of a sentence:  "The destination is Guindy" -> "Guindy"
# (used on the LLM's values as a safety net, and as the no-LLM fallback)
# ---------------------------------------------------------------------------
_LEAD_FILLER = re.compile(
    r"^(?:(?:well|so|um|uh|er|hmm|okay|ok|yes|yeah|no|nope|actually|also|and|sorry|please|"
    r"nein|ja|äh|ähm|tak|nie|więc)\b[\s,.!:;-]*)+", re.I)
_CHANGE = re.compile(
    r"^(?:i\s+(?:want|would\s+like|need|wish)\s+to\s+)?(?:please\s+)?(?:change|set|make|update)\s+"
    r"(?:it|that|this|(?:the|my|our)\s+[\w\s]{1,30}?)\s+(?:to|into|as)\s+", re.I)
_BE = (r"(?:is\s+going\s+to\s+be|is\s+gonna\s+be|are\s+going\s+to\s+be|is\s+supposed\s+to\s+be|needs?\s+to\s+be|"
       r"has\s+to\s+be|must\s+be|should\s+be|would\s+be|will\s+be|shall\s+be|might\s+be|is|are|was|"
       r"ist|sind|lautet|wäre|wird\s+sein|soll\s+sein|to\s+jest|jest|będzie|:)")
_FIELD_IS = re.compile(
    r"^(?:(?:the|my|our|its|der|die|das|mein|meine|unser|unsere)\s+)?"
    r"(?:(?:approximate|approx\.?|estimated|total|gross|net|exact|final|new|correct|actual|current|rough)\s+)?"
    r"(?:destination(?:\s+(?:city|address|location|place))?|(?:final\s+)?destination|place\s+of\s+(?:delivery|pick[\s-]?up)|"
    r"delivery(?:\s+(?:location|address|place|city|point|destination))?|"
    r"drop[\s-]?off(?:\s+(?:location|address|point|place|city))?|"
    r"pick[\s-]?up(?:\s+(?:location|address|place|city|point))?|origin|source|"
    r"(?:type\s+of\s+|kind\s+of\s+)?cargo(?:\s+type)?|goods|"
    r"company(?:\s+name)?|contact(?:\s+name)?|name|issue|problem|location|current\s+location|"
    r"e-?mail(?:\s+(?:address|id))?|weight|"
    r"zielort|ziel|lieferort|abholort|ladung|firma|firmenname|standort|"
    r"cel|miejsce\s+dostawy|miejsce\s+odbioru|ładunek|towar|nazwa\s+firmy|lokalizacja)"
    r"\s*" + _BE + r"\s*"
    r"(?:(?:actually|really|now|just|only|simply|around|about|approximately|roughly|probably)\s+)*", re.I)
_HEDGE = re.compile(
    r"^(?:(?:i|we)\s+(?:would\s+|will\s+)?(?:say|think|guess|believe|suppose)|probably|maybe|perhaps|let'?s\s+say|"
    r"i\s+suppose)\s+(?:that\s+)?", re.I)
_LEAD_IS = re.compile(
    r"^(?:(?:it|that|this|this\s+one)(?:'s|\s+is|\s+was|\s+will\s+be|\s+would\s+be|\s+should\s+be|\s+is\s+going\s+to\s+be)|"
    r"i\s+am|i'?m|we\s+are|we'?re|i\s+said|i\s+meant|"
    r"i\s+mean|es\s+ist|das\s+ist|ich\s+bin|wir\s+sind|to\s+jest|jestem|jesteśmy)\s+"
    r"(?:(?:in|at|from|to|going\s+to|actually|the|around|about|approximately|roughly|probably|"
    r"shipping|sending|transporting|moving|carrying|delivering)\s+)*", re.I)
# "I want it delivered to Guindy", "We need to pick it up from Chennai", "send it to Guindy"  (locations only)
_ACTION_LOC = re.compile(
    r"^(?:(?:i|we)\s+)?(?:(?:want|need|would\s+like|wish|have)\s+(?:it\s+|them\s+|this\s+)?(?:to\s+be\s+|to\s+)?)?"
    r"(?:will\s+|would\s+|should\s+|can\s+)?"
    r"(?:please\s+)?(?:deliver(?:ed|ing)?|send(?:ing)?|sent|ship(?:ped|ping)?|take|taken|transport(?:ed)?|bring|brought|"
    r"drop(?:ped)?|go(?:ing)?|pick(?:ed)?[\s-]?up|collect(?:ed)?|load(?:ed)?)\s+(?:it\s+|them\s+)?(?:up\s+)?"
    r"(?:to|in|at|from|into|towards|near|nach|von|do|z)\s+", re.I)
_TRAIL_FIELD = re.compile(
    r"^(?P<v>.+?)[\s,]+(?:is|will\s+be|would\s+be|that'?s|it'?s)\s+(?:the\s+|my\s+|our\s+)?"
    r"(?:destination|drop[\s-]?off(?:\s+\w+)?|delivery(?:\s+\w+)?|pick[\s-]?up(?:\s+\w+)?|origin|place)\s*$", re.I)
_LOC_PREP = re.compile(r"^(?:to|from|into|towards|in|at|nach|von|aus)\s+", re.I)


_TRAIL_POLITE = re.compile(r"[\s,.!]*\b(?:please|thanks|thank\s+you|bitte|danke|proszę|dzięki)\b[\s.!]*$", re.I)


def clean_text_value(text: str, location: bool = False) -> str:
    """Strip the words AROUND a value. 'The destination is Guindy.' -> 'Guindy'."""
    s = (text or "").strip()
    out = _TRAIL_POLITE.sub("", _LEAD_FILLER.sub("", s))
    out = _CHANGE.sub("", out)
    out = _HEDGE.sub("", out)
    out = _FIELD_IS.sub("", out)
    if location:
        out = _ACTION_LOC.sub("", out)
    out = _LEAD_IS.sub("", out)
    if location:
        out = _LOC_PREP.sub("", out)
        if m := _TRAIL_FIELD.match(out.strip(" \t.,;:!?\"")):       # "Guindy is the destination"
            out = m.group("v")
    out = out.strip(" \t.,;:!?\"")
    return (out or s)[:200]


_ROUTE = re.compile(r"\b(?:from|von|aus|z|ze)\s+(?P<a>.+?)\s+(?:to|nach|do|until|bis|towards)\s+(?P<b>.+)$", re.I)
_ROUTE_BARE = re.compile(r"^(?P<a>\S.*?)\s+(?:to|nach)\s+(?P<b>\S.*)$", re.I)
_ROUTE_REV = re.compile(r"\b(?:to|nach|do)\s+(?P<b>.+?)\s+(?:from|von|aus|z|ze)\s+(?P<a>.+)$", re.I)
_ROUTE_CUT = re.compile(r"\s+(?:on|with|carrying|by|for|and|am|mit|und|which|that|weighing|containing)\s+.*$", re.I)


def _both_places(a: str, b: str) -> bool:
    """'The destination is going' + 'be Guindy' is NOT a route: both halves must look like place names."""
    return bool(a and b and plausible_place(a) and plausible_place(b))


def extract_route(text: str) -> dict | None:
    """'from Chennai to Guindy' -> pickup/delivery (no-LLM fallback)."""
    m = _ROUTE.search(text or "")
    if not m and len((text or "").split()) <= 7:
        m = _ROUTE_BARE.search((text or "").strip(" .,!?"))      # "Berlin to Warsaw" without "from"
    if not m and (rev := _ROUTE_REV.search(text or "")):          # "deliver to Guindy from Chennai"
        a = rev.group("a").strip(" ,.")
        b = _ROUTE_CUT.sub("", rev.group("b")).strip(" ,.")
        return {"pickup_location": a, "delivery_location": b} if _both_places(a, b) else None
    if not m:
        return None
    a = m.group("a").strip(" ,.")
    b = _ROUTE_CUT.sub("", m.group("b")).strip(" ,.")
    return {"pickup_location": a, "delivery_location": b} if _both_places(a, b) else None


# ---------------------------------------------------------------------------
# E-mail (spoken or written)
# ---------------------------------------------------------------------------
_EMAIL_TOKEN = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}")


def to_email(raw) -> str | None:
    """'john dot smith at example dot com' / 'john.smith@example.com' -> 'john.smith@example.com'; else None."""
    s = str(raw or "").strip().lower()
    if not s:
        return None
    s = re.sub(r"\b(?:[a-z0-9]\s+){2,}[a-z0-9]\b", lambda m: re.sub(r"\s+", "", m.group(0)), s)  # "j o h n"
    s = re.sub(r"\s+(?:underscore|unterstrich|podkreślnik)\s+", "_", s)
    s = re.sub(r"\s+(?:dash|hyphen|minus|bindestrich|myślnik)\s+", "-", s)
    s = re.sub(r"\s+(?:at|małpa|malpa|ät)\s+", "@", s)
    s = re.sub(r"\s+(?:dot|punkt|kropka)\s+", ".", s)
    s = re.sub(r"\s*@\s*", "@", s)
    s = re.sub(r"\s*\.\s*(?=[a-z0-9])", ".", s)
    m = _EMAIL_TOKEN.search(s)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Value normalisation (what the LLM returns -> what we store)
# ---------------------------------------------------------------------------
class PastDate(Exception):
    pass


def to_number(raw) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw)
    m = re.search(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?", s)        # 1,200 or 1,200.5
    if m:
        return float(m.group(0).replace(",", ""))
    m = re.search(r"-?\d+(?:[.,]\d+)?", s)
    return float(m.group(0).replace(",", ".")) if m else None


def to_bool(raw) -> bool | None:
    if isinstance(raw, bool):
        return raw
    s = str(raw or "").strip().lower()
    if s in ("true", "yes", "ja", "tak", "1", "y"):
        return True
    if s in ("false", "no", "nein", "nie", "0", "n"):
        return False
    return None


def to_date(raw) -> str | None:
    """ISO date string; raises PastDate for dates before today."""
    s = str(raw or "").strip()[:10]
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None
    if d < date.today():
        raise PastDate(s)
    return d.isoformat()


# ---------------------------------------------------------------------------
# Number words ("fifteen", "five hundred", "zwanzig", "dwadzieścia") -> digits, for weights and dates
# ---------------------------------------------------------------------------
_NUMW = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "eins": 1, "ein": 1, "eine": 1, "einen": 1, "einem": 1, "einer": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5,
    "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13,
    "vierzehn": 14, "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19,
    "zwanzig": 20, "dreißig": 30, "vierzig": 40, "fünfzig": 50, "sechzig": 60, "siebzig": 70, "achtzig": 80,
    "neunzig": 90,
    "jeden": 1, "jedna": 1, "dwa": 2, "dwie": 2, "trzy": 3, "cztery": 4, "pięć": 5, "sześć": 6, "siedem": 7,
    "osiem": 8, "dziewięć": 9, "dziesięć": 10, "jedenaście": 11, "dwanaście": 12, "trzynaście": 13,
    "czternaście": 14, "piętnaście": 15, "szesnaście": 16, "siedemnaście": 17, "osiemnaście": 18,
    "dziewiętnaście": 19, "dwadzieścia": 20, "trzydzieści": 30, "czterdzieści": 40, "pięćdziesiąt": 50,
    "sześćdziesiąt": 60, "siedemdziesiąt": 70, "osiemdziesiąt": 80, "dziewięćdziesiąt": 90,
    "dwieście": 200, "trzysta": 300, "czterysta": 400, "pięćset": 500, "sześćset": 600, "siedemset": 700,
    "osiemset": 800, "dziewięćset": 900,
}
_SCALE = {"hundred": 100, "hundert": 100, "sto": 100, "thousand": 1000, "tausend": 1000, "tysiąc": 1000,
          "tysiące": 1000, "tysięcy": 1000}
_ALL_ALT = "|".join(re.escape(w) for w in sorted(set(_NUMW) | set(_SCALE), key=len, reverse=True))
_SCALE_ALT = "|".join(re.escape(w) for w in _SCALE)
_NUMRUN = re.compile(r"\b(?:an?\s+(?=(?:" + _SCALE_ALT + r")\b))?(?:" + _ALL_ALT + r")\b"
                     r"(?:[\s-]+(?:and\s+)?(?:" + _ALL_ALT + r")\b)*", re.I)


def _eval_numrun(run: str) -> int:
    total = cur = 0
    for w in re.findall(r"[^\W_]+", run.lower()):
        if w in ("a", "an", "and"):
            continue
        if w in _SCALE:
            if _SCALE[w] == 100:
                cur = (cur or 1) * 100
            else:
                total += (cur or 1) * _SCALE[w]
                cur = 0
        else:
            cur += _NUMW[w]
    return total + cur


def numwords_to_digits(text: str) -> str:
    """'fifteen days' -> '15 days', 'five hundred kilograms' -> '500 kilograms' (English / German / Polish)."""
    return _NUMRUN.sub(lambda m: str(_eval_numrun(m.group(0))), text or "")


# ---------------------------------------------------------------------------
# Dates spoken in any wording (no LLM): ISO, 5/11, 5 November, November 5th, tomorrow, "in 15 days",
# "after two weeks", "next Friday". Relative dates always count from TODAY.
# ---------------------------------------------------------------------------
_MONTH3 = {"jan": 1, "feb": 2, "mar": 3, "mär": 3, "apr": 4, "may": 5, "mai": 5, "maj": 5, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "dez": 12,
           "sty": 1, "lut": 2, "kwi": 4, "cze": 6, "lip": 7, "sie": 8, "wrz": 9, "paź": 10, "lis": 11, "gru": 12}
_UNIT_KIND: dict = {}
for _k, _ws in (("d", "day days tag tage tagen dzień dni dnia"),
                ("w", "week weeks woche wochen tydzień tygodnie tygodni tygodnia"),
                ("m", "month months monat monate monaten miesiąc miesiące miesięcy")):
    for _w in _ws.split():
        _UNIT_KIND[_w] = _k
_REL = re.compile(r"(?<![\w.-])(\d{1,3})\s*-?\s*(" + "|".join(re.escape(w) for w in sorted(_UNIT_KIND, key=len, reverse=True))
                  + r")\b(?!\s+ago)", re.I)
_NEXT = re.compile(r"\b(?:a|an|next)\s+(week|month|fortnight)\b|\bnächste[nr]?\s+(woche|monat)\b|"
                   r"\bw\s+przyszłym\s+(tygodniu|miesiącu)\b|\bza\s+(tydzień|miesiąc)\b|"
                   r"\bin\s+einer\s+(woche)\b|\bin\s+einem\s+(monat)\b", re.I)
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
             "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5,
             "sonnabend": 5, "sonntag": 6, "poniedziałek": 0, "wtorek": 1, "środa": 2, "środę": 2,
             "czwartek": 3, "piątek": 4, "sobota": 5, "sobotę": 5, "niedziela": 6, "niedzielę": 6}
_WEEKDAY_RE = re.compile(r"\b(" + "|".join(_WEEKDAYS) + r")\b", re.I)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def parse_spoken_date(text: str) -> str | None:
    """ISO date string for a spoken date, or None."""
    s = numwords_to_digits((text or "").lower())
    today = date.today()
    if re.search(r"\b(day\s+after\s+tomorrow|übermorgen|pojutrze)\b", s):
        return (today + timedelta(days=2)).isoformat()
    if m := _REL.search(s):                                      # "in 15 days", "after fifteen days", "2 weeks"
        n, unit = int(m.group(1)), _UNIT_KIND[m.group(2).lower()]
        d = _add_months(today, n) if unit == "m" else today + timedelta(days=n * (7 if unit == "w" else 1))
        return d.isoformat()
    if m := _NEXT.search(s):                                     # "next week", "a month"
        word = next(g for g in m.groups() if g).lower()
        if word == "fortnight":
            return (today + timedelta(days=14)).isoformat()
        if word.startswith(("week", "woche", "tydz")):
            return (today + timedelta(days=7)).isoformat()
        return _add_months(today, 1).isoformat()
    if re.search(r"\b(today|heute|dzisiaj|dziś)\b", s):
        return today.isoformat()
    if re.search(r"\b(tomorrow|morgen|jutro)\b", s):
        return (today + timedelta(days=1)).isoformat()
    day = month = year = None
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s):
        year, month, day = map(int, m.groups())
    elif m := re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", s):
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        if year is not None and year < 100:
            year += 2000
    elif m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\.?\s+(?:of\s+)?([a-zäöüłśźżćńó]{3,})", s):
        day, month = int(m.group(1)), _MONTH3.get(m.group(2)[:3])
    elif m := re.search(r"\b([a-zäöüłśźżćńó]{3,})\s+(\d{1,2})(?:st|nd|rd|th)?\b", s):
        day, month = int(m.group(2)), _MONTH3.get(m.group(1)[:3])
    if not day or not month:
        if m := _WEEKDAY_RE.search(s):                           # "next Friday", "on Monday"
            ahead = (_WEEKDAYS[m.group(1).lower()] - today.weekday()) % 7 or 7
            return (today + timedelta(days=ahead)).isoformat()
        return None
    try:
        d = date(year or today.year, month, day)
        if year is None and d < today:
            d = date(today.year + 1, month, day)
    except ValueError:
        return None
    return d.isoformat()


_WEIGHT_UNIT = re.compile(r"(\d[\d.,]*)\s*(kilogramm\w*|kilogram\w*|kgs?|kilos?|tonnes?|tonnen?|tons?|tony|ton|t)\b", re.I)
_TONNE = re.compile(r"\b(?:tonnes?|tons?|tonnen|tonne|tony|ton)\b", re.I)


# Replies that sound like an answer but carry no value ("I know", "not sure", a bare "yes" to a text question).
_NON_ANSWER = re.compile(
    r"^(?:i\s+(?:do\s+)?know|i\s+(?:do\s*n'?t|do\s+not)\s+know|dunno|not\s+sure|no\s+idea|maybe|perhaps|"
    r"whatever|something|anything|yes|yeah|yep|ok|okay|sure|right|correct|hello|hi|"
    r"special\s+requirements?|ich\s+weiß|ich\s+weiss|nicht\s+sicher|wiem|nie\s+wiem)$", re.I)
FREE_TEXT_KEYS = ("special_requirements", "issue")


def is_non_answer(text) -> bool:
    """True when the text is filler / a label echo and must never be stored as a value."""
    s = re.sub(r"[^\w' ]", " ", str(text or "")).strip()
    return (not s or bool(_NON_ANSWER.match(re.sub(r"\s+", " ", s))) or is_field_phrase(s)
            or is_yes_without_detail(s))          # "yes, I have a special requirement" carries no value


def temperature_numbers(text: str) -> list[float]:
    """Numbers of a spoken temperature range. Understands 'minus sixty', '2-8' (a range, not 2 and -8), '+5'."""
    s = numwords_to_digits(str(text or ""))
    s = re.sub(r"\b(?:minus|negative|unter\s+null|ujemn\w*)\s*(?=\d)", "-", s, flags=re.I)
    s = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " to ", s)                 # "2-8" = 2 to 8
    return [float(x.replace(",", ".")) for x in re.findall(r"-?\d+(?:[.,]\d+)?", s)]


def normalize_value(kind: str, raw, slot_id: str | None = None):
    """Return a clean value, or None when it is unusable. May raise PastDate."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if kind == "text":
        if slot_id == "adr_class":           # one class such as 3 or 6.1; "I know", "not sure" -> unusable
            return parse_adr_class(raw)
        s = clean_text_value(str(raw), slot_id in LOCATION_KEYS)
        if (slot_id in NAME_KEYS or slot_id in FREE_TEXT_KEYS) and is_non_answer(s):
            return None                      # "I know", "special requirement", a bare "yes" ...
        if slot_id in LOCATION_KEYS and not plausible_place(s):
            return None                      # "But I'm", "Hello" ... can never become a place
        if slot_id in NAME_KEYS and is_field_phrase(s):
            return None                      # "Delivery detail", "Drop", "the weight" are field names, not values
        return s[:200] if s else None
    if kind == "email":
        return to_email(raw)
    if kind == "tracking":
        return find_tracking(str(raw))
    if kind == "yesno":
        return to_bool(raw)
    if kind in ("number", "range"):
        if isinstance(raw, str):
            raw = numwords_to_digits(raw)      # "five hundred kilograms" -> "500 kilograms"
            if slot_id == "weight_kg" and (m := _WEIGHT_UNIT.search(raw)):
                n = to_number(m.group(1))      # the number NEXT TO the unit: "2 pallets of 500 kg" -> 500
                if n is not None and m.group(2)[0].lower() == "t":
                    n *= 1000                  # "1.5 tonnes" -> 1500 kg
                return n
        n = to_number(raw)
        if n is not None and slot_id == "weight_kg" and isinstance(raw, str) and _TONNE.search(raw):
            n *= 1000                          # "1.5 tonnes" -> 1500 kg, whichever code path read it
        return n
    if kind == "minutes":
        n = to_number(raw)
        return int(round(n)) if n is not None and n >= 0 else None
    if kind == "date":
        s = str(raw)
        d = to_date(s)                         # ISO from the LLM (may raise PastDate)
        if d is None and (iso := parse_spoken_date(s)):
            d = to_date(iso)                   # the LLM kept the caller's words: "15 days", "November 6th"
        return d
    return None


_ADR_RE = re.compile(rf"(?<![\d.])([1-{ADR_MAX_CLASS}])(?:\s*[.,]\s*([1-{ADR_MAX_DIVISION}]))?(?!\d)")
_ADR_STORED_RE = re.compile(rf"[1-{ADR_MAX_CLASS}](?:\.[1-{ADR_MAX_DIVISION}])?")


def parse_adr_class(raw) -> str | None:
    """'class three' / '6.1' / 3 -> '3' / '6.1' / '3'. Anything without a valid class -> None."""
    m = _ADR_RE.search(numwords_to_digits(str(raw)).lower())
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)


def schema_violation(slot_id: str, data: dict) -> str | None:
    """Hard type/range rules on the values already normalised. Returns a language.py key, or None if fine.
    (A value that is not a number at all never gets here: normalize_value() already rejects it.)"""
    if slot_id == "adr_class":
        v = data.get("adr_class")
        if v is not None and not _ADR_STORED_RE.fullmatch(str(v)):
            return "invalid_adr_class"                       # limits come from config.py
    elif slot_id == "weight_kg":
        v = data.get("weight_kg")
        if v is not None and isinstance(v, (int, float)) and v <= 0:
            return "invalid_weight_kg"
    elif slot_id == "temperature_range":
        lo, hi = data.get("temperature_min_c"), data.get("temperature_max_c")
        if lo is not None and hi is not None and (lo == hi or lo > hi or lo < TEMP_MIN_C or hi > TEMP_MAX_C):
            return "invalid_temperature_range"               # "-60 to -60" is a mis-hearing, not a range
    return None


def parse_iso(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None

# ---------------------------------------------------------------------------
# NEW: is this text really a PLACE?  (stops "But I'm", "Hello?", "I don't know" becoming a destination)
# ---------------------------------------------------------------------------
LOCATION_KEYS = ("pickup_location", "delivery_location", "current_location")

_HARD_NOT_PLACE = {
    "but", "and", "or", "so", "well", "um", "uh", "er", "hmm", "hello", "hi", "hey", "yes", "yeah", "yep", "no",
    "nope", "ok", "okay", "i", "i'm", "im", "i've", "ive", "i'll", "ill", "i'd", "my", "me", "we", "we're", "you",
    "your", "it", "it's", "its", "that", "that's", "this", "what", "why", "how", "sorry", "wrong", "actually",
    "want", "would", "like", "need", "change", "wait", "pardon", "thanks", "thank",
    "bye", "not", "don't", "dont", "can", "could", "do", "did", "have", "has", "there", "here", "know", "think",
    "mean", "meant", "said", "say", "tell", "destination", "location", "address",
    "ja", "nein", "äh", "ähm", "also", "aber", "ich", "tak", "nie", "więc", "ale", "ja", "jestem",
}
_HARD_NOT_PLACE |= NOT_PLACE_WORDS          # languages, 'right now' ... (config.py / .env)
_EDGE_NOT_PLACE = {"is", "are", "was", "to", "from", "at", "by", "with", "for", "the", "a", "an", "of", "in"}
_FILLER_ONLY = {"hello", "hi", "hey", "um", "uh", "er", "hmm", "hmmm", "mm", "huh", "what", "pardon", "hallo", "halo",
                "äh", "ähm", "słucham", "halo"}
_FRAGMENT_END = {"but", "and", "or", "so", "i'm", "im", "i", "the", "um", "uh", "er", "to", "from", "my", "is",
                 "because", "then", "i'll", "i've", "we're", "it's", "that's", "aber", "und", "ich", "ale", "i'd",
                 "just", "will", "would", "can", "could", "should", "want", "need", "like", "if", "when", "which",
                 "with", "for", "of", "at", "in", "on", "a", "an", "also", "wait", "waiting", "still", "also",
                 "wanna", "gonna", "pick", "send", "take", "bring", "be", "have", "was", "doch", "weil", "dass", "że", "bo", "żeby", "oraz", "albo"}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w'’-]+", (text or "").lower().replace("’", "'"))


def plausible_place(text) -> bool:
    """True only if text could be a city / address / place name."""
    s = str(text or "").strip().strip(".,;:!?\"' ")
    if not s or len(s) > 80:
        return False
    toks = _tokens(s)
    if not toks or len(toks) > 6:
        return False
    if not any(re.search(r"[^\W\d_]{2,}", tk) for tk in toks):      # needs a real word
        return False
    if any(tk in _HARD_NOT_PLACE for tk in toks):
        return False
    if any(tk in ("from", "to", "into") for tk in toks):             # "Guindy from Chennai" is two places, not one
        return False
    if toks[0] in {"is", "are", "was", "to", "from", "at", "by", "with", "for"} or toks[-1] in _EDGE_NOT_PLACE:
        return False
    if is_field_phrase(s):                                           # "Delivery detail", "Drop", "destination"
        return False
    return True


# Words that only DESCRIBE a field or a change request. A reply made of nothing but these ("Delivery detail",
# "Would like to change the destination", "Drop") names a field: it is never the new value for one.
_META_WORDS = {
    "the", "my", "our", "a", "an", "to", "i", "i'd", "id", "want", "wanna", "would", "like", "please", "can", "you",
    "could", "change", "changing", "update", "edit", "modify", "is", "are", "wrong", "incorrect", "not", "right",
    "correct", "it", "that", "this", "detail", "details", "field", "information", "info", "for", "of", "on", "in",
    "and", "uh", "um", "so", "well", "destination", "delivery", "deliver", "delivered", "drop", "off", "dropoff",
    "pickup", "pick", "up", "origin", "source", "location", "address", "place", "city", "point", "date", "weight",
    "cargo", "type", "goods", "email", "e-mail", "mail", "company", "name", "contact", "special", "requirements",
    "temperature", "range", "adr", "class",
}
NAME_KEYS = LOCATION_KEYS + ("cargo_type", "company_name", "contact_name")


def is_field_phrase(text) -> bool:
    """True when the text only names a field / asks for a change and carries no value."""
    toks = _tokens(str(text or ""))
    return bool(toks) and all(tk in _META_WORDS for tk in toks)


_Q_START = re.compile(r"^(?:what|where|when|which|who|how|is|are|can|could|do|does|tell\s+me|"
                      r"wann|wo|was|welche\w*|wie|gdzie|kiedy|jaka|jaki|jakie|czy|co)\b", re.I)


def is_question(text: str) -> bool:
    s = (text or "").strip()
    return s.endswith("?") or bool(_Q_START.match(s))


def is_filler(text: str) -> bool:
    """'Hello?', 'um', 'hmm' ... nothing the caller said is an answer."""
    toks = _tokens(text)
    return bool(toks) and all(tk in _FILLER_ONLY for tk in toks)


def is_fragment(text: str) -> bool:
    """A cut-off sentence ('But I'm', 'and the', 'from...'): the caller is still talking."""
    s = (text or "").strip()
    toks = _tokens(s)
    if not toks:
        return False
    return toks[-1] in _FRAGMENT_END or s.endswith(("...", "…", " -", "—"))


# ---------------------------------------------------------------------------
# NEW: no-LLM reading of "the destination is wrong" / "change the delivery date" / "Destination."
# ---------------------------------------------------------------------------
_FIELD_WORDS = [      # ORDER MATTERS: dates before places
    ("delivery_date", r"deliver(?:y|ed)?\s+date|date\s+of\s+delivery|lieferdatum|data\s+dostawy"),
    ("pickup_date", r"pick[\s-]?up\s+date|date\s+of\s+pick[\s-]?up|abholdatum|data\s+odbioru"),
    ("delivery_location", r"destination|deliver(?:y|ed)?\s+(?:location|address|place|city|point)|drop[\s-]?off"
                          r"|zielort|\bziel\b|lieferort|lieferadresse|miejsce\s+dostawy|\bcel\b"),
    ("pickup_location", r"pick[\s-]?up(?:\s+(?:location|address|place|city|point))?|origin|\bsource\b|abholort"
                        r"|miejsce\s+odbioru"),
    ("temperature_range", r"temperature|temperatur"),
    ("adr_class", r"adr\s+class"),
    ("adr", r"\badr\b"),
    ("weight_kg", r"weight|gewicht|\bwaga\b"),
    ("cargo_type", r"cargo|goods|ladung|towar|ładunek"),
    ("email", r"e-?mail"),
    ("company_name", r"company|firma"),
    ("contact_name", r"\bname\b"),
    ("special_requirements", r"special"),
    ("delivery_location", r"\bdelivery\b"),            # bare "delivery" = where it goes
]
_FIELD_RES = [(k, re.compile(p, re.I)) for k, p in _FIELD_WORDS]


def detect_field(text: str) -> str | None:
    for key, rx in _FIELD_RES:
        if rx.search(text or ""):
            return key
    return None


_CUE = re.compile(r"\b(wrong|incorrect|not\s+(?:right|correct)|mistake|change|update|modify|edit|different|"
                  r"should\s+be|instead|ändern|falsch|zmień|zmienić|nieprawidłow\w*)\b", re.I)
_VERB = re.compile(r"\b(change|update|set|make|modify|edit|ändern|zmień|zmienić)\b", re.I)
_TO = re.compile(r"\b(?:to|into|as|auf|na)\s+(?P<v>(?:(?!\b(?:to|into|as)\b).)+)$", re.I)
_IS = re.compile(r"\b(?:is|are|should\s+be|will\s+be|ist|jest)\s+(?P<v>.+)$", re.I)
_WRONG_ONLY = re.compile(r"^(?:wrong|incorrect|not\s+(?:right|correct)|a\s+mistake|different|falsch|nieprawidłow\w*)\b", re.I)
_WRONG_END = re.compile(r"\b(?:wrong|incorrect|not\s+(?:right|correct)|falsch)\s*$", re.I)
_AFFIRM = re.compile(r"^(?:correct|right|fine|ok|okay|good|true|yes|perfect|richtig|stimmt|dobrze)\b", re.I)
_TO_NOISE = re.compile(r"^(?:be\s+|a\s+different\s+|another\s+|something\s+else)", re.I)


def parse_change(text: str) -> dict | None:
    """-> {'key', 'cue', 'bare', 'rest'} or None.  rest = the new value as spoken (None = caller gave no value yet)."""
    s = (text or "").strip()
    key = detect_field(s)
    if key is None:
        return None
    verb = bool(_VERB.search(s))
    if s.endswith("?") and not verb:
        return None                                        # a question: let the LLM answer it
    cue = bool(_CUE.search(s))
    if _Q_START.match(s) and not verb and not cue:
        return None                                        # "what is the weight" is a question, not a change
    rest = None
    m = (_TO.search(s) if verb else None) or _IS.search(s)
    if m:
        cand = m.group("v").strip(" .,!?")
        if cand and not _WRONG_END.search(cand) and not _WRONG_ONLY.match(cand) and not _TO_NOISE.match(cand) and not _CUE.match(cand) and not _AFFIRM.match(cand):
            rest = cand
    bare = rest is None and not cue and len(_tokens(s)) <= 3
    if rest is None and not cue and not bare:
        return None
    return {"key": key, "cue": cue, "bare": bare, "rest": rest}

# ---------------------------------------------------------------------------
# NEW: "yes, I have a special requirement" (a yes with NO detail) and places spoken at the wrong moment
# ---------------------------------------------------------------------------
_YES_NO_DETAIL = re.compile(
    r"^(?:(?:yes|yeah|yep|yup|sure|ok|okay|ja|tak)(?:\s+|$))?"
    r"(?:(?:i|we)\s+(?:do\s+)?(?:have|got|need)\s+(?:a\s+|an\s+|some\s+|one\s+)?(?:special\s+)?"
    r"(?:requirements?|requests?|needs?|one)|there\s+(?:is|are)\s+(?:a\s+|some\s+)?(?:special\s+)?"
    r"(?:requirements?|requests?)|(?:i|we)\s+do|there\s+is|"
    r"ich\s+habe\s+(?:eine\s+|einen\s+)?(?:besondere\s+\w+)?|mam\s+(?:szczególne\s+\w+)?)?$", re.I)


def is_yes_without_detail(text) -> bool:
    """'Yes', 'yes, I have a special requirement' ... = the caller said YES but has not said WHAT yet."""
    s = re.sub(r"\s+", " ", re.sub(r"[^\w' ]", " ", str(text or ""))).strip()
    return bool(s) and bool(_YES_NO_DETAIL.match(s))


_PICK_MARK = re.compile(
    r"\b(?:start(?:ing)?|begin(?:ning)?|leav(?:e|ing)|pick(?:ing)?[\s-]?up|collect(?:ing)?|origin|from)\b"
    r"(?:\s+(?:point|location|place|city))?(?:\s+(?:will\s+be|is|would\s+be|should\s+be|going\s+to\s+be))?"
    r"(?:\s+(?:from|in|at|near))?", re.I)
_DROP_MARK = re.compile(
    r"\b(?:drop(?:ping)?(?:[\s-]?off)?|deliver(?:y|ed|ing)?|destination|(?:ship|send)(?:ping|ing)?\s+to|going\s+to)\b"
    r"(?:\s+(?:point|location|place|city))?(?:\s+(?:will\s+be|is|would\s+be|should\s+be|going\s+to\s+be))?"
    r"(?:\s+(?:to|in|at|near))?", re.I)
_PLACE_STOP = {
    "and", "or", "but", "then", "drop", "dropoff", "deliver", "delivery", "delivered", "destination", "pickup",
    "pick", "to", "my", "our", "the", "a", "an", "i", "we", "it", "that", "this", "will", "is", "are", "be", "going",
    "want", "need", "like", "with", "for", "on", "by", "of", "date", "day", "days", "week", "weeks", "month",
    "months", "next", "today", "tomorrow", "tonight", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "please", "so", "um", "uh"}


def _place_after(text: str, rx) -> str | None:
    for m in rx.finditer(text):
        words = []
        for tk in re.findall(r"[^\s,;.!?]+|[,;.!?]", text[m.end():]):
            if tk in ",;.!?" or tk.lower() in _PLACE_STOP or re.search(r"\d", tk):
                break
            words.append(tk)
            if len(words) == 3:
                break
        place = " ".join(words)
        if place and plausible_place(place):
            return place
    return None


def extract_places(text: str) -> dict:
    """'my pickup will be like that, I will be starting from Hyderabad and my drop will be in Delhi'
    -> {'pickup_location': 'Hyderabad', 'delivery_location': 'Delhi'}.  Any one of the two may be missing."""
    text = text or ""
    if route := extract_route(text):
        return route
    out = {}
    if (p := _place_after(text, _PICK_MARK)):
        out["pickup_location"] = p
    if (d := _place_after(text, _DROP_MARK)):
        out["delivery_location"] = d
    if out.get("pickup_location") and out.get("pickup_location") == out.get("delivery_location"):
        out.pop("pickup_location")
    return out