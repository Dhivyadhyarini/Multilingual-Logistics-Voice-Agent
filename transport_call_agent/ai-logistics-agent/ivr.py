"""IVR: the keypad menus (DTMF). Step 1 = language, step 2 = what the caller wants.

Speed rules:
  * A key press stops the menu INSTANTLY (interrupt is called inside the DTMF event, not after).
  * Keys pressed early (type-ahead, e.g. "2" then "1") are remembered, so the next menu is skipped.
  * An "invalid key" message is joined to the menu text: ONE spoken prompt, not two.
  * Menus are repeated only when the key was wrong or nothing was pressed.
"""
from __future__ import annotations

import asyncio
import logging
import os

from language import (IVR_KEYS_LANG, IVR_KEYS_PURPOSE, IVR_LANGUAGE_MENU, IVR_PURPOSE_MENU, Voices, t)

logger = logging.getLogger("logistics-agent")

KEY_WAIT = float(os.getenv("IVR_KEY_WAIT", "4"))   # seconds to wait for a key after the menu ends (was 6)


class IVR:
    def __init__(self, session, voices: Voices, state):
        self.session, self.voices, self.state = session, voices, state
        self._keys: asyncio.Queue[str] = asyncio.Queue()
        self._handle = None          # the menu that is speaking right now
        self._active = True          # keys are only used while the IVR is running

    # Connected to the room's "sip_dtmf_received" event in inbound.py
    def on_dtmf(self, event) -> None:
        if not self._active:
            return
        self._keys.put_nowait(str(event.digit))
        handle = self._handle
        if handle is not None:       # stop talking at once
            try:
                handle.interrupt(force=True)
            except Exception as e:
                logger.info(f"could not interrupt menu: {e}")

    def _flush(self) -> None:
        while not self._keys.empty():
            self._keys.get_nowait()

    def _buffered(self) -> str | None:
        return None if self._keys.empty() else self._keys.get_nowait()

    async def _say_or_key(self, text: str, lang: str) -> str | None:
        """Speak one segment. Returns a digit if pressed before or while it plays."""

        # Type-ahead: key was already pressed
        if (key := self._buffered()) is not None:
            return key

        self.voices.apply(lang, stt=False)
        self.state.log.append({
            "role": "ivr",
            "lang": lang,
            "text": text
        })

        handle = self.session.say(text, allow_interruptions=False)
        self._handle = handle

        key_task = asyncio.ensure_future(self._keys.get())
        play_task = asyncio.ensure_future(handle.wait_for_playout())

        try:
            done, _ = await asyncio.wait(
                {key_task, play_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if key_task in done:
                return key_task.result()

            return None

        finally:
            self._handle = None

            for task in (key_task, play_task):
                if not task.done():
                    task.cancel()

    async def _speak(self, segments: list[tuple[str, str]]) -> str | None:
        """Say each (text, language) segment in order; stop at the first key press."""
        for text, lang in segments:
            digit = await self._say_or_key(text, lang)
            if digit is not None:
                return digit
        return None

    async def _choose(self, segments, valid: dict, wrong_lang: str, tries: int = 3) -> str | None:
        invalid_note = ""
        for _ in range(tries):
            digit = self._buffered()                         # type-ahead: no need to speak at all
            if digit is None:
                spoken = segments
                if invalid_note:                             # join "invalid" + menu into ONE prompt
                    first_text, first_lang = segments[0]
                    spoken = [(f"{invalid_note} {first_text}", first_lang), *segments[1:]]
                digit = await self._speak(spoken)
            if digit is None:
                try:
                    digit = await asyncio.wait_for(self._keys.get(), timeout=KEY_WAIT)
                except asyncio.TimeoutError:
                    invalid_note = ""
                    continue                                 # no key: play the menu again
            if digit in valid:
                self.state.log.append({"role": "caller", "lang": wrong_lang, "text": f"[pressed {digit}]"})
                return digit
            invalid_note = t(wrong_lang, "ivr_invalid")
        return None

    async def run(self) -> tuple[str, str | None]:
        """Returns (language, purpose). purpose is None if the caller never chose."""
        self._flush()                                        # drop stray tones from call set-up
        try:
            digit = await self._choose(IVR_LANGUAGE_MENU, IVR_KEYS_LANG, "en")
            lang = IVR_KEYS_LANG.get(digit, "en")            # no choice -> English
            digit = await self._choose([(IVR_PURPOSE_MENU[lang], lang)], IVR_KEYS_PURPOSE, lang)
            return lang, IVR_KEYS_PURPOSE.get(digit)
        finally:
            self._active = False
            self._handle = None