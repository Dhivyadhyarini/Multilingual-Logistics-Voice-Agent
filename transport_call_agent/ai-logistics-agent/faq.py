"""Answers to the side questions callers ask in the middle of a call, written in plain code (no LLM, no network).

faq_answer(lang, text, shipment_mode) -> (answer, ops_event | None) or None.
  * shipment_mode=True : the caller is talking about an EXISTING shipment ("Can I get it earlier?" is a request that
                         the dispatchers must handle, so an ops event is returned and the agent notifies the team).
  * shipment_mode=False: the caller is filling in a new request / quote.
Anything not recognised returns None and goes on to the normal LLM path (which knows the company facts).
"""
from __future__ import annotations

import os
import re

from language import spoken_email

COMPANY_EMAIL = os.getenv("COMPANY_EMAIL", "info@hi-tec24.pl")

_EARLIER = re.compile(
    r"\b(earlier|sooner|faster|quicker|express|rush|asap|expedite|speed\s+up|as\s+soon\s+as|"
    r"früher|schneller|eilig|beschleunig\w*|"
    r"wcześniej|szybciej|przyspiesz\w*|ekspres\w*)\b", re.I)
_PRICE = re.compile(
    r"\b(price|prices|cost|costs|how\s+much|rates?|fees?|charges?|quote|"
    r"preis\w*|kosten|wie\s+viel|angebot|"
    r"cena|ceny|koszt\w*|ile\s+kosztuje|wycen\w*)\b", re.I)
_HOURS = re.compile(
    r"\b(opening\s+hours|office\s+hours|working\s+hours|business\s+hours|are\s+you\s+open|when\s+are\s+you\s+open|"
    r"öffnungszeiten|geöffnet|godziny\s+(?:pracy|otwarcia)|czynne)\b", re.I)
_CONTACT = re.compile(
    r"\b(?:your|the\s+company'?s?)\s+(?:e-?mail|email\s+address|address|contact)\b|\bwhere\s+are\s+you\s+(?:located|based)\b|"
    r"\bihre\s+(?:e-?mail|adresse)\b|\bwaszy?\s+(?:e-?mail|adres)\b|\bgdzie\s+jeste[śs]cie\b", re.I)

_ANSWERS = {
    "earlier_new": {
        "en": "Yes, express delivery is possible. Tell me the date you need, and our team will confirm the earliest option with your quote.",
        "de": "Ja, ein Expressversand ist möglich. Nennen Sie mir das gewünschte Datum, und unser Team bestätigt mit dem Angebot die früheste Option.",
        "pl": "Tak, dostawa ekspresowa jest możliwa. Proszę podać potrzebną datę, a nasz zespół potwierdzi najszybszą opcję razem z wyceną."},
    "earlier_shipment": {
        "en": "I can't change the delivery time myself, but I've passed your request for an earlier delivery to our dispatchers, and they will get back to you.",
        "de": "Die Lieferzeit kann ich nicht selbst ändern, aber ich habe Ihren Wunsch nach einer früheren Lieferung an unsere Disponenten weitergegeben. Sie melden sich bei Ihnen.",
        "pl": "Nie mogę sam zmienić terminu dostawy, ale przekazałem prośbę o wcześniejszą dostawę naszym dyspozytorom, którzy się skontaktują."},
    "price": {
        "en": "Our transport team prepares the price once the request is complete, and you will receive the quote by email.",
        "de": "Unser Transportteam erstellt den Preis, sobald die Anfrage vollständig ist, und Sie erhalten das Angebot per E-Mail.",
        "pl": "Cenę przygotowuje nasz zespół transportu po uzupełnieniu zapytania, a wycenę otrzymają Państwo e-mailem."},
    "hours": {
        "en": "Our office is open Monday to Friday, from 8 AM to 5 PM.",
        "de": "Unser Büro ist von Montag bis Freitag, 8 bis 17 Uhr, geöffnet.",
        "pl": "Nasze biuro jest czynne od poniedziałku do piątku, od 8:00 do 17:00."},
    "contact": {
        "en": "We are based in Wyszków, Poland, and you can email our office at {mail}.",
        "de": "Wir sitzen in Wyszków in Polen, und Sie erreichen unser Büro per E-Mail unter {mail}.",
        "pl": "Mamy siedzibę w Wyszkowie, a biuro można zapytać e-mailem pod adresem {mail}."},
}


def faq_answer(lang: str, text: str, shipment_mode: bool = False) -> tuple[str, str | None] | None:
    def pick(key: str, **kw) -> str:
        entry = _ANSWERS[key]
        return (entry.get(lang) or entry["en"]).format(**kw)

    if _EARLIER.search(text):
        if shipment_mode:
            return pick("earlier_shipment"), "earlier_delivery"
        return pick("earlier_new"), None
    if _HOURS.search(text):
        return pick("hours"), None
    if _CONTACT.search(text):
        return pick("contact", mail=spoken_email(lang, COMPANY_EMAIL)), None
    if _PRICE.search(text):
        return pick("price"), None
    return None