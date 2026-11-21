"""Every tunable number / word list of the voice agent in ONE place. Each value can be overridden in .env.

Nothing in agent.py / utils.py / language.py hard-codes these any more. No project imports here, so every
other module can import it safely.
"""
from __future__ import annotations

import os


def _float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def _words(name: str, default: str) -> frozenset:
    """Comma separated list from .env -> lower-case set."""
    return frozenset(w.strip().lower() for w in os.getenv(name, default).split(",") if w.strip())


# --- contact -----------------------------------------------------------------------------------------
OFFICE_PHONE = os.getenv("OFFICE_PHONE", "").strip()           # e.g. +48221234567 ; empty = not announced

# --- weight --------------------------------------------------------------------------------------------
MAX_WEIGHT_KG = _float("MAX_WEIGHT_KG", "40000")                # one truck
WEIGHT_CONFIRM_BELOW_KG = _float("WEIGHT_CONFIRM_BELOW_KG", "10")   # lighter than this: read it back first
WEIGHT_CONFIRM_DECIMALS = os.getenv("WEIGHT_CONFIRM_DECIMALS", "1") == "1"   # "2.2 kg": read it back first

# --- temperature range (degrees Celsius) ----------------------------------------------------------------
TEMP_MIN_C = _float("TEMP_MIN_C", "-100")
TEMP_MAX_C = _float("TEMP_MAX_C", "60")

# --- ADR -------------------------------------------------------------------------------------------------
ADR_MAX_CLASS = int(os.getenv("ADR_MAX_CLASS", "9"))            # classes 1 .. 9
ADR_MAX_DIVISION = int(os.getenv("ADR_MAX_DIVISION", "6"))      # divisions such as 1.6, 4.2, 6.1

# --- tracking number: letters the speech-to-text confuses with "T R K" ("B R K", "E R K", "T R C" ...) ----
TRK_FIRST_LETTERS = os.getenv("TRK_FIRST_LETTERS", "tbdegp").lower()
TRK_LAST_LETTERS = os.getenv("TRK_LAST_LETTERS", "kcg").lower()

# --- words that can never be a place name (speech-to-text noise, languages, filler) ---------------------
# NOTE: do not add "am" - it starts real German street names ("Am Markt").
NOT_PLACE_WORDS = _words(
    "NOT_PLACE_WORDS",
    "hindi,english,german,polish,tamil,telugu,urdu,bengali,marathi,punjabi,kannada,malayalam,gujarati,deutsch,"
    "polski,right,now,currently,just,still,accident,waiting,stuck,broken,been,got,met,was,were",
)