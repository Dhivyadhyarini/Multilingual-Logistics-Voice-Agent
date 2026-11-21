"""Everything that is language-specific: prompts, goodbyes, IVR menus, and the STT/TTS language switch.

To add a language: add a code to LANG_NAMES / STT_CODES / TTS_CODES / MONTHS / ACKS / STATUS,
then add its text to every entry in T and to the IVR menus.
"""
from __future__ import annotations

import logging
import os
import re

from config import OFFICE_PHONE

logger = logging.getLogger("logistics-agent")

COMPANY = os.getenv("COMPANY_NAME", "Hi-Tec")
COMPANY_FACTS = os.getenv(
    "COMPANY_FACTS",
    "HI-TEC Sp. z o.o. is a Polish transport company in Wyszków. It specialises in temperature-controlled "
    "transport of pharmaceuticals, cosmetics and electronics, ADR dangerous goods, high-value goods and "
    "express delivery. Modern fleet of about 60 vehicles, over 10 years of experience, direct delivery "
    "without reloading or storage. Operates across Europe, the UK, the Balkans and Scandinavia. "
    "Office hours Monday to Friday, 08:00 to 17:00. Email info@hi-tec24.pl.",
)
if OFFICE_PHONE and OFFICE_PHONE not in COMPANY_FACTS:
    COMPANY_FACTS += f" Office phone {OFFICE_PHONE}."       # so the classifier can answer "which number?"

LANG_NAMES = {"en": "English", "de": "German", "pl": "Polish"}
# STT_LANGUAGE (from .env) only controls English, e.g. en-IN for Indian accents.
STT_CODES = {"en": os.getenv("STT_LANGUAGE", "en"), "de": "de", "pl": "pl"}
TTS_CODES = {"en": "en", "de": "de", "pl": "pl"}
IVR_KEYS_LANG = {"1": "en", "2": "de", "3": "pl"}
IVR_KEYS_PURPOSE = {"1": "track", "2": "request", "3": "issue"}

MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
           "September", "Oktober", "November", "Dezember"],
    "pl": ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia",
           "września", "października", "listopada", "grudnia"],
}

ACKS = {
    "en": ["Got it.", "Thanks.", "Okay.", "Perfect.", "Noted."],
    "de": ["Verstanden.", "Danke.", "Okay.", "Perfekt.", "Notiert."],
    "pl": ["Rozumiem.", "Dziękuję.", "Dobrze.", "Świetnie.", "Zanotowane."],
}

STATUS = {
    "en": {"new": "registered", "in_transit": "in transit", "delayed": "delayed", "delivered": "delivered"},
    "de": {"new": "erfasst", "in_transit": "unterwegs", "delayed": "verspätet", "delivered": "zugestellt"},
    "pl": {"new": "zarejestrowana", "in_transit": "w transporcie", "delayed": "opóźniona", "delivered": "dostarczona"},
}

# ---------------------------------------------------------------------------
# IVR (keypad) menus
# ---------------------------------------------------------------------------
IVR_LANGUAGE_MENU = [  # one segment per language so each line is pronounced correctly
    (f"Welcome to {COMPANY}. This call may be recorded. For English, press 1.", "en"),
    ("Für Deutsch, drücken Sie die 2.", "de"),
    ("Dla języka polskiego, naciśnij 3.", "pl"),
]
IVR_PURPOSE_MENU = {
    "en": "If you are a customer and want to track an existing shipment, press 1. "
          "To request a new transport or a quote, press 2. "
          "If you are a driver and want to report an issue or update a delivery, press 3.",
    "de": "Wenn Sie als Kunde eine Sendung verfolgen möchten, drücken Sie die 1. "
          "Wenn Sie einen neuen Transport anfragen oder ein Angebot wünschen, drücken Sie die 2. "
          "Wenn Sie Fahrer sind und ein Lieferproblem melden oder ein Update geben möchten, drücken Sie die 3.",
    "pl": "Jeśli jako klient chcesz sprawdzić status przesyłki, naciśnij 1. "
          "Aby zlecić nowy transport lub uzyskać wycenę, naciśnij 2. "
          "Jeśli jesteś kierowcą i chcesz zgłosić problem z dostawą lub przekazać aktualizację, naciśnij 3.",
}

# ---------------------------------------------------------------------------
# All spoken text. t(lang, "key", name=value)
# ---------------------------------------------------------------------------
T = {
    # --- questions (key = "ask_" + slot id) ---
    "ask_tracking_number": {
        "en": "Please tell me your tracking number, for example T R K, followed by the digits.",
        "de": "Bitte nennen Sie mir Ihre Sendungsnummer, zum Beispiel T R K und dann die Ziffern.",
        "pl": "Proszę podać numer przesyłki, na przykład T R K i cyfry."},
    "track_partial": {
        "en": "I have {digits} so far. Please say the remaining digits.",
        "de": "Ich habe bisher {digits}. Bitte nennen Sie die restlichen Ziffern.",
        "pl": "Mam na razie {digits}. Proszę podać pozostałe cyfry."},
    "ask_shipment_number": {
        "en": "Please give me the shipment number.",
        "de": "Bitte nennen Sie mir die Sendungsnummer.",
        "pl": "Proszę podać numer przesyłki."},
    "ask_company_name": {
        "en": "What is your company name?",
        "de": "Wie heißt Ihr Unternehmen?",
        "pl": "Jak nazywa się Państwa firma?"},
    "ask_contact_name": {
        "en": "And your name, please?",
        "de": "Und Ihr Name, bitte?",
        "pl": "Proszę podać imię i nazwisko."},
    "ask_pickup_location": {
        "en": "Where should we pick up the cargo? A city or address is fine.",
        "de": "Wo soll die Ware abgeholt werden? Eine Stadt oder Adresse genügt.",
        "pl": "Skąd mamy odebrać ładunek? Wystarczy miasto lub adres."},
    "ask_delivery_location": {
        "en": "And where should it be delivered?",
        "de": "Und wohin soll geliefert werden?",
        "pl": "A dokąd ma zostać dostarczony?"},
    "ask_cargo_type": {
        "en": "What type of cargo is it? For example pharmaceutical, pallets or general goods.",
        "de": "Um welche Art von Ladung handelt es sich? Zum Beispiel Pharmazeutika, Paletten oder Stückgut.",
        "pl": "Jaki to rodzaj ładunku? Na przykład farmaceutyki, palety lub drobnica."},
    "ask_weight_kg": {
        "en": "What is the approximate weight in kilograms?",
        "de": "Wie hoch ist das ungefähre Gewicht in Kilogramm?",
        "pl": "Jaka jest przybliżona waga w kilogramach?"},
    "ask_temperature_controlled": {
        "en": "Does the cargo need temperature control?",
        "de": "Muss die Ladung temperaturgeführt transportiert werden?",
        "pl": "Czy ładunek wymaga kontrolowanej temperatury?"},
    "ask_temperature_range": {
        "en": "What temperature range is required, for example 2 to 8 degrees?",
        "de": "Welcher Temperaturbereich ist erforderlich, zum Beispiel 2 bis 8 Grad?",
        "pl": "Jaki zakres temperatury jest wymagany, na przykład od 2 do 8 stopni?"},
    "ask_adr": {
        "en": "Is it dangerous goods, so ADR?",
        "de": "Handelt es sich um Gefahrgut, also ADR?",
        "pl": "Czy to towar niebezpieczny, czyli ADR?"},
    "ask_adr_class": {
        "en": "Which ADR class?",
        "de": "Welche ADR-Klasse?",
        "pl": "Jaka klasa ADR?"},
    "ask_pickup_date": {
        "en": "On which date should we pick it up?",
        "de": "An welchem Datum soll die Abholung erfolgen?",
        "pl": "Na którą datę zaplanować odbiór?"},
    "ask_delivery_date": {
        "en": "And by which date should it be delivered?",
        "de": "Und bis wann soll geliefert werden?",
        "pl": "A do kiedy ma zostać dostarczony?"},
    "ask_special_requirements": {
        "en": "Any special requirements? You can say none.",
        "de": "Gibt es besondere Anforderungen? Sie können auch nein sagen.",
        "pl": "Czy są jakieś szczególne wymagania? Można powiedzieć, że nie."},
    "ask_issue": {
        "en": "What issue are you facing?",
        "de": "Welches Problem liegt vor?",
        "pl": "Jaki jest problem?"},
    "ask_waiting_minutes": {
        "en": "How many minutes of delay or waiting time has this caused? You can say none.",
        "de": "Wie viele Minuten Verzögerung oder Wartezeit hat das verursacht? Sie können auch keine sagen.",
        "pl": "Ile minut opóźnienia lub oczekiwania to spowodowało? Można powiedzieć, że wcale."},
    "ask_current_location": {
        "en": "Where are you right now?",
        "de": "Wo befinden Sie sich gerade?",
        "pl": "Jaka jest Państwa aktualna lokalizacja?"},

    # --- new: e-mail, validation, confirmation ---
    "ask_email": {
        "en": "What is your email address? You can say it like name at example dot com.",
        "de": "Wie lautet Ihre E-Mail-Adresse? Sie können sie zum Beispiel als name at beispiel punkt de sagen.",
        "pl": "Jaki jest Państwa adres e-mail? Można go podać na przykład: imię małpa przykład kropka pl."},
    "invalid_email": {
        "en": "That email address doesn't look complete. Please say it again, for example name at example dot com.",
        "de": "Diese E-Mail-Adresse scheint nicht vollständig zu sein. Bitte sagen Sie sie noch einmal, zum Beispiel name at beispiel punkt de.",
        "pl": "Ten adres e-mail wygląda na niepełny. Proszę podać go jeszcze raz, na przykład imię małpa przykład kropka pl."},
    "invalid_weight_kg": {
        "en": "I couldn't use that weight. Please tell me the weight in kilograms, for example 500.",
        "de": "Dieses Gewicht konnte ich nicht verwenden. Bitte nennen Sie das Gewicht in Kilogramm, zum Beispiel 500.",
        "pl": "Nie udało się odczytać wagi. Proszę podać wagę w kilogramach, na przykład 500."},
    "need_detail": {
        "en": "I still need this detail to complete your request.",
        "de": "Diese Angabe brauche ich noch, um Ihre Anfrage abzuschließen.",
        "pl": "Potrzebuję jeszcze tej informacji, aby dokończyć zgłoszenie."},
    "corrected_ack": {
        "en": "Okay, I've updated that.",
        "de": "Okay, ich habe das geändert.",
        "pl": "Dobrze, zmieniono."},
    "confirm_fix_list": {
        "en": "You can say, for example, the destination, the weight or the email. Or say yes if everything is correct.",
        "de": "Sie können zum Beispiel das Ziel, das Gewicht oder die E-Mail nennen. Oder sagen Sie ja, wenn alles stimmt.",
        "pl": "Mogą Państwo powiedzieć na przykład cel, wagę lub e-mail. Albo tak, jeśli wszystko się zgadza."},
    "change_value_ask": {
        "en": "Sure. What should the new {f} be?",
        "de": "Gern. Wie lautet der neue Wert für {f}?",
        "pl": "Jasne. Jaka ma być nowa wartość: {f}?"},
    "manage_help": {
        "en": "You can ask me about the status, location, cargo, or delivery time.",
        "de": "Sie können mich nach Status, Standort, Ladung oder Lieferzeit fragen.",
        "pl": "Mogą Państwo zapytać o status, lokalizację, ładunek lub czas dostawy."},
    "confirm_fix_ask": {
        "en": "No problem. Which detail would you like to change?",
        "de": "Kein Problem. Welche Angabe möchten Sie ändern?",
        "pl": "Nie ma problemu. Którą informację zmienić?"},

    # --- existing shipment: questions and changes ---
    "manage_prompt": {
        "en": "You can ask me about this shipment.",
        "de": "Sie können mich zu dieser Sendung fragen.",
        "pl": "Mogą Państwo zapytać o tę przesyłkę."},
    "manage_more": {
        "en": "Anything else about this shipment?",
        "de": "Noch etwas zu dieser Sendung?",
        "pl": "Coś jeszcze w sprawie tej przesyłki?"},
    "no_change_understood": {
        "en": "Which detail would you like to change? For example the destination, the weight or the cargo type.",
        "de": "Welche Angabe möchten Sie ändern? Zum Beispiel das Ziel, das Gewicht oder die Ladungsart.",
        "pl": "Którą informację zmienić? Na przykład cel, wagę lub rodzaj ładunku."},
    "change_confirm": {
        "en": "To confirm, I will {changes}. Is that correct?",
        "de": "Zur Bestätigung: Ich werde {changes}. Ist das richtig?",
        "pl": "Podsumowuję: mam {changes}. Czy to się zgadza?"},
    "change_done": {
        "en": "Done. I've updated your shipment and informed our team.",
        "de": "Erledigt. Ich habe Ihre Sendung aktualisiert und unser Team informiert.",
        "pl": "Gotowe. Przesyłka została zaktualizowana, a nasz zespół został poinformowany."},
    "change_failed": {
        "en": "Sorry, I couldn't save that change. I've passed it to our team.",
        "de": "Leider konnte ich die Änderung nicht speichern. Ich habe sie an unser Team weitergegeben.",
        "pl": "Przepraszam, nie udało się zapisać zmiany. Przekazałem ją naszemu zespołowi."},
    "change_cancelled": {
        "en": "Okay, I haven't changed anything. What would you like to change?",
        "de": "Okay, ich habe nichts geändert. Was möchten Sie ändern?",
        "pl": "Dobrze, nic nie zmieniłem. Co chcą Państwo zmienić?"},
    "change_locked": {
        "en": "This shipment has already been delivered, so I can't change it. I've passed your request to our team.",
        "de": "Diese Sendung wurde bereits zugestellt, deshalb kann ich sie nicht ändern. Ich habe Ihre Anfrage an unser Team weitergegeben.",
        "pl": "Ta przesyłka została już dostarczona, więc nie mogę jej zmienić. Przekazałem prośbę naszemu zespołowi."},
    "change_owner": {
        "en": "For security I can't change this shipment from this number. I've passed your request to our team.",
        "de": "Aus Sicherheitsgründen kann ich diese Sendung von dieser Nummer aus nicht ändern. Ich habe Ihre Anfrage an unser Team weitergegeben.",
        "pl": "Ze względów bezpieczeństwa nie mogę zmienić tej przesyłki z tego numeru. Przekazałem prośbę naszemu zespołowi."},
    "missing_info": {
        "en": "These details are not on file yet: {fields}. You can add them if you like.",
        "de": "Diese Angaben fehlen noch: {fields}. Sie können sie gern ergänzen.",
        "pl": "Brakuje jeszcze tych informacji: {fields}. Mogą Państwo je uzupełnić."},

    # --- conversation glue ---
    "repeat_prefix": {"en": "Sure.", "de": "Gern.", "pl": "Oczywiście."},
    "retry": {
        "en": "Sorry, I didn't quite get that.",
        "de": "Entschuldigung, das habe ich nicht ganz verstanden.",
        "pl": "Przepraszam, czy mogę prosić o powtórzenie?"},
    "lang_switched": {
        "en": "Sure, let's continue in English.",
        "de": "Gerne, wir machen auf Deutsch weiter.",
        "pl": "Oczywiście, kontynuujmy po polsku."},
    "silence_check": {
        "en": "Sorry, are you still there?",
        "de": "Entschuldigung, sind Sie noch da?",
        "pl": "Przepraszam, czy nadal Państwo tam są?"},
    "date_past": {
        "en": "That date has already passed. Which date works for you?",
        "de": "Dieses Datum liegt in der Vergangenheit. Welches Datum passt Ihnen?",
        "pl": "Ta data już minęła. Jaka data będzie odpowiednia?"},
    "date_order": {
        "en": "The delivery date cannot be before the pickup date. Which delivery date works?",
        "de": "Das Lieferdatum darf nicht vor dem Abholdatum liegen. Welches Lieferdatum passt?",
        "pl": "Data dostawy nie może być wcześniejsza niż data odbioru. Jaka data dostawy będzie odpowiednia?"},
    "anything_else": {
        "en": "Is there anything else I can help you with?",
        "de": "Kann ich Ihnen sonst noch helfen?",
        "pl": "Czy mogę jeszcze w czymś pomóc?"},
    "how_can_i_help": {
        "en": "How can I help? I can track a shipment, take a transport request, or log a delivery issue.",
        "de": "Wie kann ich helfen? Ich kann eine Sendung verfolgen, eine Transportanfrage aufnehmen oder ein Lieferproblem erfassen.",
        "pl": "W czym mogę pomóc? Mogę sprawdzić przesyłkę, przyjąć zapytanie transportowe lub zgłosić problem z dostawą."},

    # --- results ---
    "track_found": {
        "en": "Your shipment {tn} is {status}. Current location: {location}. Estimated delivery: {eta}.",
        "de": "Ihre Sendung {tn} ist {status}. Aktueller Standort: {location}. Voraussichtliche Lieferung: {eta}.",
        "pl": "Przesyłka {tn} ma status: {status}. Aktualna lokalizacja: {location}. Przewidywana dostawa: {eta}."},
    "track_delivered": {
        "en": "Your shipment {tn} has been delivered in {location}, {eta}.",
        "de": "Ihre Sendung {tn} wurde in {location} zugestellt, {eta}.",
        "pl": "Przesyłka {tn} została dostarczona do miejscowości {location}, {eta}."},
    "track_not_found": {
        "en": "I couldn't find a shipment with the number {tn}. Please say the number again.",
        "de": "Ich konnte keine Sendung mit der Nummer {tn} finden. Bitte nennen Sie die Nummer noch einmal.",
        "pl": "Nie znaleziono przesyłki o numerze {tn}. Proszę podać numer jeszcze raz."},
    "request_saved": {
        "en": "Thank you. I've recorded your request, number {rid}, and sent it to our transport team. You'll receive a quote shortly.",
        "de": "Danke. Ich habe Ihre Anfrage mit der Nummer {rid} erfasst und an unser Transportteam weitergeleitet. Sie erhalten in Kürze ein Angebot.",
        "pl": "Dziękuję. Zapytanie numer {rid} zostało zapisane i przekazane naszemu zespołowi transportu. Wkrótce otrzymają Państwo wycenę."},
    "issue_saved": {
        "en": "Thank you. I've logged the issue for shipment {tn} and notified our operations team.",
        "de": "Danke. Ich habe das Problem zur Sendung {tn} protokolliert und unser Operations-Team informiert.",
        "pl": "Dziękuję. Problem dotyczący przesyłki {tn} został zapisany, a zespół operacyjny został powiadomiony."},
    "issue_urgent": {
        "en": "This sounds urgent, so I'm connecting you to a dispatcher now.",
        "de": "Das klingt dringend, deshalb verbinde ich Sie jetzt mit einem Disponenten.",
        "pl": "To wygląda na pilną sprawę, dlatego łączę z dyspozytorem."},

    # --- human / escalation ---
    "human_connect": {
        "en": "Of course. I'm connecting you to our team.",
        "de": "Natürlich. Ich verbinde Sie mit unserem Team.",
        "pl": "Oczywiście. Łączę z naszym zespołem."},
    "human_unavailable": {
        "en": "Our team can't take the call right now. I've noted your request and someone will call you back shortly.",
        "de": "Unser Team ist gerade nicht erreichbar. Ich habe Ihr Anliegen notiert und wir rufen Sie bald zurück.",
        "pl": "Nasz zespół jest teraz niedostępny. Sprawa została zapisana i wkrótce oddzwonimy."},
    "transfer_failed": {
        "en": "I couldn't connect you right now. I've noted your request and someone will call you back shortly.",
        "de": "Ich konnte Sie gerade nicht verbinden. Ich habe Ihr Anliegen notiert und wir rufen Sie bald zurück.",
        "pl": "Nie udało się teraz połączyć. Sprawa została zapisana i wkrótce oddzwonimy."},

    # --- goodbyes (always spoken by the code, never by the LLM) ---
    "bye_completed": {
        "en": f"Thank you for calling {COMPANY}. Goodbye!",
        "de": f"Vielen Dank für Ihren Anruf bei {COMPANY}. Auf Wiederhören!",
        "pl": f"Dziękujemy za telefon do {COMPANY}. Do usłyszenia!"},
    "bye_no_response": {
        "en": "I can't hear you, so I'll end the call. Please call again any time. Goodbye.",
        "de": "Ich kann Sie leider nicht hören und beende den Anruf. Sie können jederzeit wieder anrufen. Auf Wiederhören.",
        "pl": "Nie słyszę Państwa, więc kończę połączenie. Zapraszamy do ponownego kontaktu. Do usłyszenia."},

    "followup_later": {
        "en": "A colleague will follow up on that.",
        "de": "Ein Kollege meldet sich dazu bei Ihnen.",
        "pl": "Kolega skontaktuje się w tej sprawie."},
    "invalid_adr_class": {
        "en": "I need the ADR class as a number, for example 3 or 6.1. Which class is it?",
        "de": "Ich brauche die ADR-Klasse als Zahl, zum Beispiel 3 oder 6.1. Welche Klasse ist es?",
        "pl": "Potrzebuję klasy ADR jako liczby, na przykład 3 lub 6.1. Jaka to klasa?"},
    "invalid_weight_kg": {
        "en": "The weight must be more than zero. What is the weight in kilograms?",
        "de": "Das Gewicht muss größer als null sein. Wie viele Kilogramm sind es?",
        "pl": "Waga musi być większa od zera. Ile to kilogramów?"},
    "invalid_temperature_range": {
        "en": "I didn't catch a valid temperature range: the two values must differ, lowest first. What range is required, for example 2 to 8 degrees?",
        "de": "Die niedrigste und höchste Temperatur dürfen nicht gleich sein. Welcher Bereich ist erforderlich, zum Beispiel 2 bis 8 Grad?",
        "pl": "Temperatura minimalna i maksymalna nie mogą być takie same. Jaki zakres jest wymagany, na przykład od 2 do 8 stopni?"},
    "save_failed_human": {
        "en": "Sorry, I could not save your request just now. I'll connect you to our team so nothing is lost.",
        "de": "Entschuldigung, ich konnte Ihre Anfrage gerade nicht speichern. Ich verbinde Sie mit unserem Team, damit nichts verloren geht.",
        "pl": "Przepraszam, nie udało mi się teraz zapisać zgłoszenia. Łączę z naszym zespołem, aby nic nie zginęło."},
    "not_found_human": {
        "en": "I still can't find that shipment, so I'll connect you to our team.",
        "de": "Ich finde die Sendung weiterhin nicht, deshalb verbinde ich Sie mit unserem Team.",
        "pl": "Nadal nie mogę znaleźć tej przesyłki, dlatego łączę z naszym zespołem."},
    "unclear_human": {
        "en": "I'm having trouble understanding, so I'll connect you to our team.",
        "de": "Ich habe Schwierigkeiten, Sie zu verstehen, deshalb verbinde ich Sie mit unserem Team.",
        "pl": "Mam trudności ze zrozumieniem, dlatego łączę z naszym zespołem."},

    # --- IVR ---
    "ivr_invalid": {
        "en": "Sorry, that is not a valid option.",
        "de": "Entschuldigung, das ist keine gültige Auswahl.",
        "pl": "Przepraszam, to nie jest prawidłowa opcja."},
    "ivr_giveup": {
        "en": "I didn't receive a selection. Please call again. Goodbye.",
        "de": "Ich habe keine Auswahl erhalten. Bitte rufen Sie erneut an. Auf Wiederhören.",
        "pl": "Nie otrzymano wyboru. Proszę zadzwonić ponownie. Do usłyszenia."},
}


def t(lang: str, key: str, **kw) -> str:
    entry = T[key]
    text = entry.get(lang) or entry["en"]
    return text.format(**kw) if kw else text


def prompt_for_slot(lang: str, slot_id: str) -> str:
    key = f"ask_{slot_id}"
    return t(lang, key) if key in T else t(lang, "how_can_i_help")


def fmt_eta(lang: str, start_iso: str | None, end_iso: str | None) -> str:
    """'3 November between 10:00 and 14:00' in the caller's language."""
    from utils import parse_iso
    s, e = parse_iso(start_iso), parse_iso(end_iso)
    base = e or s
    if base is None:
        return {"en": "not available yet", "de": "noch nicht bekannt", "pl": "jeszcze nieznana"}[lang]
    month = MONTHS[lang][base.month - 1]
    window = s is not None and e is not None and s.date() == e.date() and s != e
    a, b = (s.strftime("%H:%M"), e.strftime("%H:%M")) if window else (base.strftime("%H:%M"), "")
    if lang == "de":
        day = f"am {base.day}. {month}"
        return f"{day} zwischen {a} und {b} Uhr" if window else f"{day} um {a} Uhr"
    day = f"{base.day} {month}"
    if lang == "pl":
        return f"{day} między {a} a {b}" if window else f"{day} o {a}"
    return f"{day} between {a} and {b}" if window else f"{day} at {a}"


# ---------------------------------------------------------------------------
# Labels, dates and read-back summaries
# ---------------------------------------------------------------------------
NONE_WORDS = {"none", "no", "nothing", "n/a", "nein", "keine", "keiner", "nichts", "nie", "brak", "nic"}

FIELD_LABELS = {
    "en": {"pickup_location": "pickup location", "delivery_location": "delivery location", "cargo_type": "cargo type",
           "weight_kg": "weight", "temperature_controlled": "temperature control",
           "temperature_range": "temperature range", "adr": "ADR status", "adr_class": "ADR class",
           "company_name": "company name", "contact_name": "contact name", "special_requirements": "special requirements",
           "current_location": "current location", "issue": "issue"},
    "de": {"pickup_location": "Abholort", "delivery_location": "Lieferort", "cargo_type": "Ladungsart",
           "weight_kg": "Gewicht", "temperature_controlled": "Temperaturführung",
           "temperature_range": "Temperaturbereich", "adr": "ADR-Status", "adr_class": "ADR-Klasse",
           "company_name": "Firmenname", "contact_name": "Ansprechpartner", "special_requirements": "besondere Anforderungen",
           "current_location": "aktueller Standort", "issue": "Problem"},
    "pl": {"pickup_location": "miejsce odbioru", "delivery_location": "miejsce dostawy", "cargo_type": "rodzaj ładunku",
           "weight_kg": "waga", "temperature_controlled": "kontrola temperatury",
           "temperature_range": "zakres temperatury", "adr": "status ADR", "adr_class": "klasa ADR",
           "company_name": "nazwa firmy", "contact_name": "osoba kontaktowa", "special_requirements": "szczególne wymagania",
           "current_location": "aktualna lokalizacja", "issue": "problem"},
}
CHANGE_TPL = {   # (with old value, without old value)
    "en": ("change the {f} from {old} to {new}", "set the {f} to {new}"),
    "de": ("{f} von {old} auf {new} ändern", "{f} auf {new} setzen"),
    "pl": ("zmienić {f} z {old} na {new}", "ustawić {f} na {new}"),
}
AND = {"en": " and ", "de": " und ", "pl": " i "}
YESNO_WORDS = {"en": ("yes", "no"), "de": ("ja", "nein"), "pl": ("tak", "nie")}


def fmt_num(x) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f.is_integer() else str(f)


def fmt_date(lang: str, iso: str | None) -> str:
    """'2026-11-05' -> 'November 5th' / '5. November' / '5 listopada'."""
    from datetime import date
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    month = MONTHS[lang][d.month - 1]
    if lang == "de":
        return f"{d.day}. {month}"
    if lang == "pl":
        return f"{d.day} {month}"
    suffix = "th" if 10 <= d.day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    return f"{month} {d.day}{suffix}"


def spoken_email(lang: str, email: str) -> str:
    at, dot = {"en": (" at ", " dot "), "de": (" at ", " punkt "), "pl": (" małpa ", " kropka ")}[lang]
    return email.replace("@", at).replace(".", dot)


_SUMMARY = {
    "en": dict(open="To confirm, your shipment is from {a} to {b}", cargo=", carrying {c}",
               weight=" weighing {w} kilograms", temp=", kept at {lo} to {hi} degrees Celsius",
               adr=", as ADR dangerous goods", adr_cls=", class {c}", pick=", with pickup on {d}",
               deliv=" and delivery by {d}", special=". Special requirements: {s}",
               who=". The request is for {co}, contact person {n}", email=". Your email is {e}",
               end=". Is everything correct?"),
    "de": dict(open="Zur Bestätigung: Ihre Sendung geht von {a} nach {b}", cargo=", Ladung: {c}",
               weight=", Gewicht {w} Kilogramm", temp=", temperaturgeführt zwischen {lo} und {hi} Grad",
               adr=", als Gefahrgut ADR", adr_cls=", Klasse {c}", pick=", Abholung am {d}",
               deliv=" und Lieferung bis {d}", special=". Besondere Anforderungen: {s}",
               who=". Die Anfrage gilt für {co}, Ansprechpartner {n}", email=". Ihre E-Mail-Adresse lautet {e}",
               end=". Ist alles richtig?"),
    "pl": dict(open="Podsumowuję: przesyłka z {a} do {b}", cargo=", ładunek: {c}",
               weight=", waga {w} kilogramów", temp=", w kontrolowanej temperaturze od {lo} do {hi} stopni Celsjusza",
               adr=", towar niebezpieczny ADR", adr_cls=", klasa {c}", pick=", odbiór dnia {d}",
               deliv=" i dostawa do {d}", special=". Szczególne wymagania: {s}",
               who=". Zapytanie dla firmy {co}, osoba kontaktowa {n}", email=". Państwa adres e-mail to {e}",
               end=". Czy wszystko się zgadza?"),
}


def build_request_summary(lang: str, d: dict, email: str | None = None) -> str:
    """The read-back before a transport request is saved. Ends with the yes/no question."""
    s = _SUMMARY[lang]
    out = s["open"].format(a=d.get("pickup_location", "-"), b=d.get("delivery_location", "-"))
    if d.get("cargo_type"):
        out += s["cargo"].format(c=d["cargo_type"])
    if d.get("weight_kg") is not None:
        out += s["weight"].format(w=fmt_num(d["weight_kg"]))
    if d.get("temperature_controlled") and d.get("temperature_min_c") is not None:
        out += s["temp"].format(lo=fmt_num(d["temperature_min_c"]), hi=fmt_num(d["temperature_max_c"]))
    if d.get("adr"):
        out += s["adr"] + (s["adr_cls"].format(c=d["adr_class"]) if d.get("adr_class") else "")
    if d.get("pickup_date"):
        out += s["pick"].format(d=fmt_date(lang, d["pickup_date"]))
    if d.get("delivery_date"):
        out += s["deliv"].format(d=fmt_date(lang, d["delivery_date"]))
    sp = str(d.get("special_requirements") or "").strip()
    if sp and sp.lower() not in NONE_WORDS:
        out += s["special"].format(s=sp)
    if d.get("company_name"):
        out += s["who"].format(co=d["company_name"], n=d.get("contact_name") or "-")
    if email:
        out += s["email"].format(e=spoken_email(lang, email))
    return out + s["end"]


# ---------------------------------------------------------------------------
# Quick word checks (no LLM needed for the obvious cases)
# ---------------------------------------------------------------------------
_YES = {"yes", "yeah", "yep", "yup", "sure", "correct", "right", "ok", "okay", "absolutely", "please",
        "ja", "jawohl", "genau", "klar", "doch", "richtig", "stimmt", "gerne",
        "tak", "jasne", "dokładnie", "owszem", "oczywiście", "proszę",
        "confirmed", "confirm", "proceed", "submit", "exactly", "perfect", "passt", "zgadza", "potwierdzam"}
_NO = {"no", "nope", "nah", "nein", "nee", "nicht", "kein", "keine", "nie", "nic",
       "not", "wrong", "incorrect", "falsch", "błędne", "źle"}


def yes_no(text: str) -> bool | None:
    if re.search(r"\d", text):          # "yes, 2 to 8 degrees" carries more than a yes: let the LLM read it
        return None
    words = re.findall(r"[^\W\d_]+", text.lower())
    if not words or len(words) > 4:
        return None
    y, n = any(w in _YES for w in words), any(w in _NO for w in words)
    if y and not n:
        return True
    if n and not y:
        return False
    return None


REPEAT_RE = re.compile(
    r"\b(repeat|say that again|pardon|come again|(couldn'?t|can'?t|didn'?t) (hear|catch)|once more|say it again|"
    r"(breaking|cutting) up|cutting out|bad (line|connection)|louder|slower|"
    r"nochmal|noch einmal|wiederholen|wie bitte|lauter|langsamer|"
    r"powtórz|powtorz|jeszcze raz|słucham|nie dosłyszał\w*|głośniej|wolniej)\b", re.I)
CLARIFY_RE = re.compile(
    r"\b((don'?t|didn'?t|do not|did not) understand|what do you mean|not clear|unclear|rephrase|simpler|"
    r"explain (that|it|the question)|"
    r"nicht verstanden|verstehe (ich )?nicht|was meinen sie|nicht klar|anders formulieren|einfacher|"
    r"nie rozumiem|nie zrozumia\w+|co pan\w* ma na my\w+|co masz na my\w+|niejasne|prościej)\b", re.I)

HUMAN_RE = re.compile(
    r"\b(human|real person|operator|dispatcher|someone from your team|talk to (a|an|your) (person|agent|colleague)|"
    r"mensch|mitarbeiter|disponent|echte person|"
    r"człowiek\w*|konsultant\w*|pracownik\w*|dyspozytor\w*|operator\w*)\b", re.I)
BYE_RE = re.compile(
    r"\b(goodbye|bye|that'?s all|that'?s it|nothing else|nothing|no more|no thanks|no thank you|"
    r"auf wiederhören|tschüss|tschuess|das war'?s|das ist alles|sonst nichts|nichts|"
    r"do widzenia|to wszystko|nic więcej|nic|dziękuję to wszystko)\b", re.I)
# "Which number should I call?" (only answered when OFFICE_PHONE is set)
PHONE_Q_RE = re.compile(
    r"\b(which|what)\s+number\b.*\b(call|ring|dial)\b|\b(phone|telephone|contact)\s+number\b|"
    r"\bwhat'?s\s+your\s+number\b|\btelefonnummer\b|\brufnummer\b|\bnumer\s+telefonu\b|"
    r"\bjaki\s+numer\b.*\b(zadzwonić|dzwonić)\b", re.I)
PLUS_WORD = {"en": "plus", "de": "plus", "pl": "plus"}


def spoken_phone(lang: str, phone: str) -> str:
    """'+48221234567' -> 'plus 4 8 2 2 1 2 3 4 5 6 7' so text-to-speech reads it digit by digit."""
    parts = [PLUS_WORD[lang] if c == "+" else c for c in phone if c.isdigit() or c == "+"]
    return " ".join(parts)


_SWITCH = [
    (re.compile(r"\b(english|englisch|angielsku|angielski)\b", re.I), "en"),
    (re.compile(r"\b(german|deutsch|niemiecku|niemiecki)\b", re.I), "de"),
    (re.compile(r"\b(polish|polnisch|polsku|polski)\b", re.I), "pl"),
]


def detect_switch(text: str) -> str | None:
    for rx, code in _SWITCH:
        if rx.search(text):
            return code
    return None


# ---------------------------------------------------------------------------
# Changing the voice languages in the middle of a call
# ---------------------------------------------------------------------------
class Voices:
    """Holds the STT and TTS objects so we can change their language (used by the IVR and the agent)."""

    def __init__(self, stt, tts):
        self.stt, self.tts = stt, tts
        # Both are built in English, so 'en' needs no update_options() (that call restarts the connection).
        self._cur = {"stt": STT_CODES["en"], "tts": TTS_CODES["en"]}

    def apply(self, lang: str, stt: bool = True) -> None:
        for name, obj, code in (("stt", self.stt, STT_CODES[lang]), ("tts", self.tts, TTS_CODES[lang])):
            if obj is None or (name == "stt" and not stt) or self._cur[name] == code:
                continue
            try:
                obj.update_options(language=code)
                self._cur[name] = code
            except Exception as e:  # never break the call over a language switch
                logger.warning(f"could not set {name} language to {code}: {e}")