"""Entry point for INBOUND calls.   Run:  python inbound.py dev      (or: python inbound.py console)

First time on a new machine:  python inbound.py download-files
(this downloads the turn-detector model BEFORE any call, so the first caller never waits for it)

Flow of one call:
  caller dials -> Twilio/Plivo -> LiveKit SIP -> this worker joins the room
  -> IVR (language, purpose) -> AI agent (flow) -> goodbye -> save call (DB + JSON)

Every number, path, model name and URL below can be overridden in .env (nothing else is hard-coded).
"""
from __future__ import annotations

import os

# Must run BEFORE numpy / onnxruntime / silero are imported, otherwise the limits are ignored:
# one math thread per process stops the VAD from fighting the audio loop for CPU.
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import asyncio
import importlib
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import certifi
from dotenv import load_dotenv

from livekit import rtc
from livekit.agents import AgentServer, AgentSession, JobContext, JobProcess, cli, room_io
from livekit.plugins import openai, silero, deepgram, cartesia

load_dotenv()

import classifier                              # noqa: E402  (direct Groq client for the per-turn classifier)
import tools                                   # noqa: E402  (after load_dotenv so .env is read first)
from agent import LogisticsAgent, hang_up      # noqa: E402
from database import init_db                   # noqa: E402
from ivr import IVR                            # noqa: E402
from language import TTS_CODES, Voices         # noqa: E402
from models import CallState                   # noqa: E402
from summary import summarise                  # noqa: E402
from utils import norm_phone                   # noqa: E402

# Layer 7 - end-of-turn detection (MultilingualModel). Imported at TOP LEVEL so the model is registered at
# worker start-up (weights: `python inbound.py download-files`) and not during the first call.
try:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
except ImportError:
    MultilingualModel = None
try:
    from livekit.plugins import noise_cancellation
except ImportError:
    noise_cancellation = None

logger = logging.getLogger("logistics-agent")


def _env_float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


AGENT_NAME = os.getenv("AGENT_NAME", "logistics-agent")
CALLS_DIR = Path(os.getenv("CALLS_DIR", "calls"))
HEARTBEAT = Path(os.getenv("HEARTBEAT_FILE", "")) if os.getenv("HEARTBEAT_FILE") \
    else Path(__file__).resolve().parent / "agent_alive.txt"

# ---- Models / providers (all in .env) ------------------------------------------------------------------
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
TTS_MODEL = os.getenv("TTS_MODEL", "cartesia/sonic-3")
CARTESIA_VOICE = os.getenv("CARTESIA_VOICE", "f8f5f1b2-f02d-4d8e-a40d-fd850a487b3d")
STT_PROVIDER = os.getenv("STT_PROVIDER", "deepgram").lower()      # "deepgram" (default) or "groq"
STT_MODEL = os.getenv("STT_MODEL", "deepgram/nova-3")             # used when STT_PROVIDER=deepgram
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en-IN" if STT_PROVIDER == "deepgram" else "en")
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")       # layer 9
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", "0.0")

# ---- Turn taking ---------------------------------------------------------------------------------------
VAD_SILENCE = _env_float("VAD_SILENCE", "0.5")                    # layer 5: Silero min silence (s)
VAD_ACTIVATION = _env_float("VAD_ACTIVATION", "0.6")              # Silero speech threshold
ENDPOINT_MIN = _env_float("ENDPOINT_MIN", "0.7")                  # layer 7: endpointing min delay (s)
ENDPOINT_MAX = _env_float("ENDPOINT_MAX", "4.0")                  # ... and max delay (s)
USE_TURN_MODEL = os.getenv("TURN_MODEL", "on").lower() != "off"
USER_AWAY_TIMEOUT = _env_float("USER_AWAY_TIMEOUT", "15")         # seconds of silence before the "are you there?" prompt
FALSE_INTERRUPTION_TIMEOUT = _env_float("FALSE_INTERRUPTION_TIMEOUT", "1.0")
SILENCE_PROMPTS_MAX = int(os.getenv("SILENCE_PROMPTS_MAX", "3"))  # silence prompts before the call is ended
CALLER_WAIT_TIMEOUT = _env_float("CALLER_WAIT_TIMEOUT", "60")     # seconds to wait for the SIP caller to join

# ---- CPU load ------------------------------------------------------------------------------------------
# Phone audio is 8 kHz, so the VAD gets the same information at half the compute. Allowed: 8000 or 16000.
VAD_SAMPLE_RATE = 8000 if os.getenv("VAD_SAMPLE_RATE", "8000").strip() == "8000" else 16000
# The SIP trunk already runs Krisp (tasks.py: krisp_enabled=True). NOISE_CANCEL=on brings the in-process filter back.
USE_NOISE_CANCEL = os.getenv("NOISE_CANCEL", "off").lower() == "on"
HEARTBEAT_INTERVAL = max(1.0, _env_float("HEARTBEAT_INTERVAL", "10"))

# ---- End-of-call save: must finish well inside LiveKit's shutdown limit --------------------------------
# worst case = SUMMARY_TIMEOUT (summary.py, default 10 s + 2 s) + CALL_SAVE_DEADLINE
CALL_SAVE_DEADLINE = _env_float("CALL_SAVE_DEADLINE", "20")       # seconds to wait for the DB row


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def _require_groq_key() -> str:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("Missing GROQ_API_KEY in .env")
    return key


def build_stt():
    """Layer 6. Default: Deepgram Nova-3 called DIRECTLY with your own key (no LiveKit Inference credits).
    Alternative (STT_PROVIDER=groq): Groq Whisper via the official livekit-plugins-groq package."""
    if STT_PROVIDER == "groq":
        key = _require_groq_key()
        try:
            from livekit.plugins import groq as groq_plugin
        except ImportError as e:
            raise RuntimeError("STT_PROVIDER=groq needs the plugin:  pip install livekit-plugins-groq") from e
        return groq_plugin.STT(model=GROQ_STT_MODEL, language=STT_LANGUAGE, api_key=key)
    key = os.getenv("DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("Missing DEEPGRAM_API_KEY in .env")
    return deepgram.STT(model=STT_MODEL.split("/")[-1], language=STT_LANGUAGE, api_key=key)


def build_llm():
    """Groq-backed LLM used by the LiveKit voice session."""
    return openai.LLM(model=GROQ_MODEL, api_key=_require_groq_key(), base_url=GROQ_BASE_URL,
                      temperature=LLM_TEMPERATURE)


def build_tts():
    key = os.getenv("CARTESIA_API_KEY")
    if not key:
        raise RuntimeError("Missing CARTESIA_API_KEY in .env")
    return cartesia.TTS(model=TTS_MODEL.split("/")[-1], voice=CARTESIA_VOICE,
                        language=TTS_CODES["en"], api_key=key)


def build_vad():
    """Silero VAD tuned for phone audio. 8 kHz halves the compute; force_cpu keeps onnxruntime on the CPU
    provider. Falls back to the plain settings if this plugin version does not accept the extra arguments."""
    try:
        return silero.VAD.load(min_silence_duration=VAD_SILENCE, activation_threshold=VAD_ACTIVATION,
                               sample_rate=VAD_SAMPLE_RATE, force_cpu=True)
    except TypeError as e:
        logger.warning(f"silero.VAD.load rejected sample_rate/force_cpu ({e}); using default settings")
        return silero.VAD.load(min_silence_duration=VAD_SILENCE, activation_threshold=VAD_ACTIVATION)


_heartbeat_started = False


def start_heartbeat() -> None:
    global _heartbeat_started
    if _heartbeat_started:
        return
    _heartbeat_started = True

    def _beat():
        while True:
            try:
                HEARTBEAT.write_text(str(time.time()))
            except OSError:
                pass
            time.sleep(HEARTBEAT_INTERVAL)

    threading.Thread(target=_beat, daemon=True, name="heartbeat").start()


def prewarm(proc: JobProcess):
    """Runs ONCE when a worker process starts, before any call. Everything heavy goes here."""
    start_heartbeat()
    init_db()

    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

    proc.userdata["db_ready"] = True
    proc.userdata["vad"] = build_vad()

    # Import the heavy modules now so the first call does not pay for them.
    for mod in ("openai.resources.chat", "anyio._core._synchronization", "anyio._backends._asyncio",
                "livekit.agents.inference"):
        try:
            importlib.import_module(mod)
        except Exception:
            pass

    proc.userdata["turn_ok"] = MultilingualModel is not None and USE_TURN_MODEL

    try:
        proc.userdata["llm"] = build_llm()
    except Exception as e:
        logger.warning(f"prewarm of LLM client skipped: {e}")


server = AgentServer()
server.setup_fnc = prewarm


async def _warm_connections(*clients) -> None:
    """Open the network connections to LLM / TTS / STT while the phone is still ringing in."""
    for client in clients:
        fn = getattr(client, "prewarm", None)
        if not callable(fn):
            continue
        try:
            res = fn()
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            logger.info(f"prewarm skipped for {type(client).__name__}: {e}")


# ---------------------------------------------------------------------------
@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    is_console = ctx.room.name == "console"
    if not ctx.proc.userdata.get("db_ready"):
        await asyncio.to_thread(init_db)            # never block the event loop

    llm_client = ctx.proc.userdata.get("llm") or build_llm()
    stt, tts = build_stt(), build_tts()
    voices = Voices(stt, tts)
    state = CallState()
    warm_task = asyncio.create_task(_warm_connections(tts, llm_client, stt, classifier.warmer))

    # Layer 7: MultilingualModel end-of-turn detection, falling back to plain VAD if unavailable.
    turn_detector, use_turn_model = "vad", False
    if MultilingualModel is not None and USE_TURN_MODEL:
        try:
            turn_detector, use_turn_model = MultilingualModel(), True
        except Exception as e:
            logger.warning(f"turn-detector model unavailable, falling back to VAD: {e}")
    session = AgentSession(
        stt=stt,
        llm=llm_client,
        tts=tts,
        vad=ctx.proc.userdata["vad"],
        user_away_timeout=USER_AWAY_TIMEOUT,
        turn_handling={
            "turn_detection": turn_detector,
            "endpointing": {"min_delay": ENDPOINT_MIN if use_turn_model else max(ENDPOINT_MIN, 0.4),
                            "max_delay": ENDPOINT_MAX},
            "interruption": {"resume_false_interruption": True,
                             "false_interruption_timeout": FALSE_INTERRUPTION_TIMEOUT},
            # The code (not the LLM) writes every reply, so speculative LLM runs would only waste calls.
            "preemptive_generation": {"enabled": False},
        },
    )
    agent = LogisticsAgent(state, ctx, llm_client, voices)
    ivr = IVR(session, voices, state)

    # ---- Keypad presses (IVR stops talking the instant a key arrives) -----
    @ctx.room.on("sip_dtmf_received")
    def _on_dtmf(ev: rtc.SipDTMF):
        ivr.on_dtmf(ev)

    # ---- Silence handling -------------------------------------------------
    @session.on("user_state_changed")
    def _on_user_state(ev):
        if ev.new_state != "away" or state.ending or not state.live:
            return
        state.silence_prompts += 1
        if state.silence_prompts >= SILENCE_PROMPTS_MAX:
            agent.give_up_silence()
        else:
            agent.silence_check()

    # ---- Caller hung up ---------------------------------------------------
    @ctx.room.on("participant_disconnected")
    def _on_disconnect(p: rtc.RemoteParticipant):
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP and not state.ending:
            state.ending = True
            if state.outcome == "in_progress":
                state.outcome = "caller_hung_up"
            ctx.shutdown(reason="caller_hung_up")

    # ---- Save the call (runs when the call ends, however it ends) ---------
    def _write_json(path: Path, out: dict) -> None:
        CALLS_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    async def save_call():
        ended = datetime.now()
        summary = await summarise(state)                         # has its own deadline (summary.py)
        record = {
            "phone_number": state.phone,
            "call_type": state.call_type or "unknown",
            "language": state.lang,
            "customer_id": (state.customer or {}).get("id"),
            "shipment_id": (state.shipment or {}).get("id"),
            "transport_request_id": state.transport_request_id,
            "transcript": state.log,
            "summary": summary,
            "urgency": state.urgency,
            "escalated": state.escalated,
            "outcome": state.outcome,
            "started_at": state.started_at.isoformat(timespec="seconds"),
            "ended_at": ended.isoformat(timespec="seconds"),
        }
        try:
            _, code = await asyncio.wait_for(tools.save_call(record), timeout=CALL_SAVE_DEADLINE)
        except Exception as e:                                   # includes TimeoutError and "database is locked"
            logger.error(f"could not save call to the database ({type(e).__name__}): {e}")
            code = f"CALL-{ended:%Y%m%d-%H%M%S}"
        path = CALLS_DIR / f"{code}.json"
        out = {"call_code": code, **record, "flows_done": state.flows_done, "collected": state.collected,
               "room": ctx.room.name,
               "stack": {"llm": GROQ_MODEL, "stt": GROQ_STT_MODEL if STT_PROVIDER == "groq" else STT_MODEL,
                         "tts": TTS_MODEL}}
        try:
            await asyncio.to_thread(_write_json, path, out)
        except RuntimeError as e:                                # "can't create new thread at interpreter shutdown"
            logger.info(f"worker threads gone ({e}); writing the call file directly")
            _write_json(path, out)                               # tiny file: fine to write synchronously here
        logger.info(f"Saved {path} (outcome={state.outcome}, type={state.call_type})")

    ctx.add_shutdown_callback(save_call)

    # ---- Audio input: telephony noise cancellation on real calls only (switchable) -----
    audio_in = room_io.AudioInputOptions()
    if USE_NOISE_CANCEL and noise_cancellation is not None and not is_console:
        def _nc(params):
            if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                return noise_cancellation.BVCTelephony()
            return noise_cancellation.BVC()
        audio_in = room_io.AudioInputOptions(noise_cancellation=_nc)
    logger.info(f"audio setup: noise_cancel={'on' if USE_NOISE_CANCEL else 'off'}, vad_rate={VAD_SAMPLE_RATE}, "
                f"turn_model={'on' if use_turn_model else 'off'}")

    await session.start(agent=agent, room=ctx.room, room_options=room_io.RoomOptions(audio_input=audio_in))

    # ---- Who is calling? ---------------------------------------------------
    if is_console:     # testing on your laptop: no phone line, no keypad
        state.phone = os.getenv("CONSOLE_PHONE")
        lang = os.getenv("CONSOLE_LANG", "en")
        purpose = {"1": "track", "2": "request", "3": "issue"}.get(os.getenv("CONSOLE_PURPOSE", "1"))
    else:
        try:
            caller = await asyncio.wait_for(
                ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP),
                timeout=CALLER_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.info("no caller joined the room")
            state.ending, state.outcome = True, "no_caller"
            ctx.shutdown(reason="no_caller")
            return
        state.sip_identity = caller.identity
        state.phone = norm_phone(caller.attributes.get("sip.phoneNumber"))

    # Customer lookup runs IN PARALLEL with the IVR menu: the menu starts speaking immediately.
    async def _lookup():
        try:
            return await tools.find_customer(state.phone)
        except Exception as e:
            logger.warning(f"customer lookup failed: {e}")
            return None

    lookup_task = asyncio.create_task(_lookup())

    if not is_console:
        lang, purpose = await ivr.run()
    state.customer = await lookup_task
    logger.info(f"Inbound call from {state.phone} (known customer: {bool(state.customer)})")
    await warm_task
    if state.ending:
        return
    await agent.begin(lang, purpose)


if __name__ == "__main__":
    start_heartbeat()
    cli.run_app(server)