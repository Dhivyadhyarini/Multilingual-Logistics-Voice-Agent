# AI Logistics Voice Agent (Inbound) - Hi-Tec

An AI phone agent that **answers calls** for a transport company (Hi-Tec, Wyszków, Poland).
Callers press keys to pick a language and a reason, then talk to the AI in **English, German or Polish**.
No web UI: it is Python + SQLite + a phone number.

```
Caller dials  ->  Twilio / Plivo  ->  LiveKit SIP  ->  agent (this project)
                                                          |
                    IVR: press 1/2/3 language, then 1/2/3 reason
                                                          |
        1 Track shipment | 2 New transport request / quote | 3 Driver issue
                                                          |
                      SQLite database  +  calls/CALL-001.json  +  ops alert
```

---

## 1. The tech stack, in simple words

| Part | What we use | What it does |
|---|---|---|
| Phone line | **Twilio or Plivo** | Owns the phone number and sends the call to LiveKit |
| Call + audio engine | **LiveKit** (Cloud + Agents framework) | Receives the call, runs the agent, sends audio both ways |
| Ears (speech to text) | **Deepgram nova-3** (via LiveKit Inference) | Turns the caller's voice into text |
| Brain | **Groq** (`openai/gpt-oss-20b`) | Only *reads* each reply and returns JSON (what did they say?) |
| Mouth (text to speech) | **Cartesia sonic-3** (via LiveKit Inference) | Speaks English, German and Polish |
| Turn taking | Silero VAD + LiveKit turn detector | Knows when the caller has finished talking |
| Database | **SQLite** (one file, `logistics.db`) | Shipments, customers, requests, calls |
| Language | **Python 3.10+** | Everything |

**Important idea (same as your recruiter agent):** the *code* decides what to ask next and what to save.
The LLM never "chats freely", so it cannot invent shipment data. It is fast, cheap and predictable.

---

## 2. What each file does

| File | Job |
|---|---|
| `inbound.py` | **Start here.** Receives the call, sets up the voice pipeline, runs IVR then agent, saves the call |
| `ivr.py` | Keypad menus: language (1/2/3) then reason (1/2/3) |
| `language.py` | All English/German/Polish text, goodbyes, language switching |
| `agent.py` | The conversation engine (classify reply -> fill slots -> next question) |
| `shipment.py` | Flow 1: track a shipment |
| `transport.py` | Flow 2: new transport request / quote |
| `driver.py` | Flow 3: driver delivery issue |
| `escalation.py` | Urgency check, ops alerts, hand-off to a human |
| `tools.py` | The agent's database/alert tools (async wrappers) |
| `database.py`, `models.py`, `db_queries.py` | SQLite connection, tables, all queries |
| `seed_data.py` | Fake shipments/customers for testing |
| `summary.py` | One short summary per call (Groq) |
| `tasks.py` | Creates the LiveKit inbound trunk + dispatch rule |
| `utils.py` | Small helpers (tracking number parsing, dates...) |
| `test_offline.py` | Plays fake calls without any account or internet |
| `calls/` | One `CALL-001.json` per call + `ops_notifications.jsonl` |

Not included on purpose: `scheduler.py` and `dial.py` (they are only for outbound calls).

---

## 3. Setup, step by step

### Step 1: Install
```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```
Python 3.10 or newer. If you already have the recruiter project's venv, you can reuse it.

### Step 2: Get your keys and fill `.env`
1. **LiveKit**: create a project at cloud.livekit.io -> Settings -> Keys. Fill `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.
2. **Groq**: console.groq.com -> API Keys -> `GROQ_API_KEY`.
3. Deepgram and Cartesia need **no extra key** here, because they run through LiveKit Inference (billed on your LiveKit plan).
   If you prefer your own Cartesia/Deepgram accounts, swap `build_stt()` / `build_tts()` in `inbound.py` for the plugin versions.

### Step 3: Create the database
```bash
python seed_data.py --phone +48XXXXXXXXX     # put YOUR mobile number to be a "known customer"
```
Test tracking numbers: `TRK-48291` (in transit), `TRK-10001` (delivered), `TRK-20002` (delayed), `TRK-30003` (new).

### Step 4: Run the offline test (no phone, no keys)
```bash
python test_offline.py
```
You should see PASS lines and "All offline tests passed." This proves the flows, database and 3 languages work.

### Step 5: Talk to it on your laptop (microphone)
```bash
python inbound.py download-files      # one time: downloads the small VAD / turn-detector models
python inbound.py console
```
Console mode skips the keypad. Choose the language and flow in `.env` with `CONSOLE_LANG` and `CONSOLE_PURPOSE`.

### Step 6: Connect a real phone number
1. **Start the agent** and leave it running:
   ```bash
   python inbound.py dev
   ```
2. **Tell LiveKit what to do with calls** (once):
   ```bash
   python tasks.py setup --number +48XXXXXXXXX      # the number you bought, E.164 format
   ```
   This creates an *inbound trunk* and a *dispatch rule* that sends every new call to `AGENT_NAME`.
   (You can do the same by hand in LiveKit Cloud -> Telephony.)
3. **Point your phone provider at LiveKit.** In LiveKit Cloud -> Settings, copy your **SIP URI** (looks like `xxxx.sip.livekit.cloud`).
   - **Twilio:** Elastic SIP Trunking -> create a trunk -> *Origination* -> add `sip:YOUR-SIP-URI` -> *Numbers* -> add your number to the trunk.
   - **Plivo:** Zentrunk -> create an *Origination URI* with your LiveKit SIP URI -> create an incoming trunk using it -> attach your number.
   Provider screens change often, so also check their "LiveKit SIP" guides.
4. **Call your number.** You should hear the language menu.

> The number `+48 800 100 200` in the infographics is an example. Use a number you actually own.
> Polish toll-free (800) numbers may not be sold by every provider, so a normal Polish number is fine for testing.

### Step 7: See what was saved
```bash
python db_queries.py
```
Prints the latest calls, transport requests and shipments with open issues.
Every call also creates `calls/CALL-001.json` (transcript, summary, collected data). Ops alerts go to `calls/ops_notifications.jsonl`.

---

## 4. How a call works

1. Caller dials. The agent joins and finds the caller's number. If it is in `customers`, we already know who they are.
2. **IVR:** "For English press 1, Für Deutsch 2, Dla języka polskiego 3", then the reason menu in that language.
3. **Flows** (each one is a list of "slots" to fill):
   - **Track:** asks the tracking number -> looks it up -> reads status, location, ETA.
   - **Request:** asks company/name (only if unknown), pickup, delivery, cargo, weight, temperature (+range), ADR (+class), dates, special needs -> saves `transport_requests` row -> alerts ops.
   - **Driver issue:** asks shipment number, problem, waiting time, location -> updates the shipment -> alerts ops -> if urgent, transfers to a dispatcher.
4. The caller can say "English please", "auf Deutsch", "po polsku" **at any time**, or "I want a quote" to jump flows, or "I need a person".
5. The code says goodbye, hangs up, then saves the call (database + JSON + summary).

Urgent means: keywords such as accident, breakdown, stolen, damaged, cooling failure (also in German/Polish), or waiting >= `URGENT_WAIT_MINUTES`.

---

## 5. Changing things

- **Add a question to a flow:** add a `Slot(...)` in `transport.py`, add `"ask_<slot id>"` in `language.py` (3 languages), add the column in `models.py` / `db_queries.py`.
- **Change what the agent says:** edit the `T` dictionary in `language.py`.
- **Company info the agent may quote:** `COMPANY_FACTS` in `.env` (default is taken from hi-tec24.pl).
- **Add a language:** see the note at the top of `language.py`.
- **Real shipment data:** replace the queries in `db_queries.py` (or point `get_shipment_by_tracking` at your TMS API). Nothing else changes.
- **Switch SQLite to PostgreSQL later:** only `database.py` and `db_queries.py` need changes.

---

## 6. Live transfer to a human (optional)

Set `OPERATIONS_PHONE` (E.164) to enable it. The agent then uses a SIP transfer (REFER).
Your phone provider must allow it (Twilio: enable call transfer on the trunk; check Plivo's support).
If it is empty or the transfer fails, the agent says someone will call back, and the ops alert is still written.

## 7. Troubleshooting

| Problem | Try |
|---|---|
| Call rings but nobody answers | Is `python inbound.py dev` running? Does `AGENT_NAME` match? Run `python tasks.py list` |
| Keypad presses ignored | Your provider must send DTMF as RFC 2833 / telephone-event (default on Twilio and Plivo) |
| Polish speech ends too early or late | Set `TURN_MODEL=off` in `.env` |
| English accent not understood | Change `STT_LANGUAGE` (en, en-IN, en-GB, en-US) |
| `Missing GROQ_API_KEY` | Fill `.env` and restart |
| Agent says a shipment is not found | Run `python seed_data.py` and use `TRK-48291` |
| Want to see the logs | Run with `python inbound.py dev --log-level debug` |

## 8. Before going live

- Tell callers they may be recorded (the greeting already says this). Transcripts contain personal data, so decide how long to keep `calls/` and the `calls` table (GDPR applies in Poland/EU).
- Run with `python inbound.py start` on a server (production mode) instead of `dev`.
- Do a few real test calls in all three languages, including a noisy line, before sharing the number.
