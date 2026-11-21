# AI Logistics Voice Agent

A **multilingual, AI‑powered voice agent** for handling inbound calls for a logistics company. It supports English, German, and Polish, enabling callers to:

- **Track shipments**
- **Request new transports / quotes**
- **Report driver issues**

The solution is built entirely in Python, uses SQLite for persistence, and leverages modern cloud services for telephony, speech, and LLM processing.

---

## Table of Contents
1. [Architecture](#architecture)
2. [Features](#features)
3. [Prerequisites](#prerequisites)
4. [Installation & Setup](#installation--setup)
5. [Running the Agent](#running-the-agent)
6. [Testing](#testing)
7. [Contribution Guide](#contribution-guide)
8. [License](#license)

---

## Architecture

| Component | Service | Role |
|---|---|---|
| **Telephony** | Twilio / Plivo | Receives the incoming call and forwards it to LiveKit via SIP |
| **Media & Turn‑taking** | **LiveKit** (cloud) | Handles audio routing, VAD, and turn detection |
| **Speech‑to‑Text** | Deepgram (via LiveKit Inference) | Converts caller speech to text |
| **LLM** | **Groq** (`openai/gpt-oss-20b`) | Generates the next response and extracts slot values |
| **Text‑to‑Speech** | Cartesia (via LiveKit Inference) | Synthesises the agent’s replies in the chosen language |
| **Database** | SQLite (`logistics.db`) | Stores shipments, customers, transport requests and call logs |
| **Business Logic** | Python modules (`inbound.py`, `ivr.py`, `agent.py`, …) | Orchestrates the flow, validates data and triggers alerts |

---

## Features
- **Three‑language IVR** (English, German, Polish) with DTMF navigation
- **Slot‑based conversational flow** – the LLM only asks for required information
- **Automatic urgency detection** – alerts ops when a driver issue is critical
- **Persisted call data** – JSON transcript, summary and database entry per call
- **Offline test mode** – run the full conversation locally without any keys
- **Extensible** – add new questions, languages, or integrate a real TMS API

---

## Prerequisites
- **Python 3.10+** (recommended via a virtual environment)
- **Git** (to clone the repo and push changes)
- **LiveKit account** – project with API keys
- **Groq account** – API key for the LLM
- (Optional) **Twilio / Plivo** – if you want to connect a real phone number

---

## Installation & Setup
```bash
# Clone the repository
git clone https://github.com/Dhivyadhyarini/Multilingual-Logistics-Voice-Agent.git
cd Multilingual-Logistics-Voice-Agent/transport_call_agent/ai-logistics-agent

# Create a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create a starter .env file (do NOT commit the real secrets!)
cp .env.example .env
# Edit .env and fill in your LiveKit and Groq keys, plus any optional settings
```

---

## Running the Agent
### 1️⃣ Offline test (no keys required)
```bash
python test_offline.py
```
You should see a series of **PASS** messages confirming that all flows work.

### 2️⃣ Interactive console mode (uses your keys)
```bash
python inbound.py console
```
Select language and flow via the `.env` configuration.

### 3️⃣ Production mode (real phone calls)
```bash
# Start the agent
python inbound.py dev

# Register the inbound trunk and dispatch rule (run once)
python tasks.py setup --number +48YOUR_NUMBER
```
Follow the instructions in the console to complete the LiveKit ↔ telephony wiring.

---

## Testing
- **Unit tests** are located in `test_offline.py` – run them with `pytest` if you have it installed.
- Use the SQLite seed script to generate demo data:
```bash
python seed_data.py --phone +48YOUR_MOBILE
```

---

## Contribution Guide
1. **Fork** the repository.
2. Create a **feature branch**:
   ```bash
   git checkout -b my‑feature
   ```
3. Make your changes, ensure the offline tests pass, and run `git status` to verify no secrets are staged.
4. Commit with a clear message and **push**:
   ```bash
   git push origin my‑feature
   ```
5. Open a **Pull Request** with a description of the changes.

**Important:** Never commit real API keys. Use the placeholder values in `.env.example` and keep `.env` in `.gitignore`.

---

## License
This project is licensed under the **MIT License** – see the `LICENSE` file for details.

---

*For any questions or assistance, feel free to open an issue on GitHub.*