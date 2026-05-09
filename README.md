# Bengaluru Pet Rescue & Adoption Coordinator

An AI agent that unifies Bengaluru's fragmented animal rescue ecosystem — matching adopters with animals and guiding people through emergencies in real time.

Built for the Elastic Hackathon using Elasticsearch (EIS + hybrid RRF search), AWS Bedrock (Claude Sonnet), FastAPI, and Streamlit.

---

## What it does

**Emergency mode** — triggered by words like "injured", "found", "hit by", "bleeding"  
Calls `find_emergency_vet` + `find_active_rescuers` + `get_protocol` in parallel. Responds with the nearest open 24×7 vet, an on-call rescuer covering the user's area, and a vetted first-aid protocol. Zero invented contacts.

**Adoption mode** — triggered by "looking for", "want to adopt", "show me"  
Calls `search_animals` with extracted filters. Returns semantically matched animals across orgs with photos, temperament tags, and source attribution.

---

## Architecture

```
User (Web or WhatsApp)
        │
        ▼
FastAPI /chat  /whatsapp
        │
        ▼
AWS Bedrock — Claude Sonnet (converse API, parallel tool use)
        │
   ┌────┴────┐
   │  4 Tools │
   └────┬────┘
        │
        ▼
Elasticsearch (Elastic Cloud)
  ├── animals      — hybrid BM25 + semantic (EIS) via RRF
  ├── vets         — geo_distance + emergency_capable filter
  ├── rescuers     — area term filter + on-call time check
  └── protocols    — semantic search (EIS)
```

Embeddings are handled by **Elastic Inference Service** (`.jina-embeddings-v5-text-small`, dim=1024). No direct Jina REST calls; documents embed automatically at index time.

---

## Stack

| Layer | Technology |
|---|---|
| Search | Elasticsearch 9.4 on Elastic Cloud (`us-central1`) |
| Embeddings | EIS — `.jina-embeddings-v5-text-small` (1024-dim) |
| LLM | AWS Bedrock — `global.anthropic.claude-sonnet-4-6` |
| Backend | FastAPI + uvicorn |
| Web UI | Streamlit + Folium |
| WhatsApp | Twilio Sandbox + ngrok |
| Python | 3.11 |

---

## Project layout

```
pet-rescue/
├── backend/
│   ├── __init__.py
│   ├── agent.py          # Bedrock converse loop, tool dispatch
│   ├── es_client.py      # shared ES client singleton
│   ├── main.py           # FastAPI: /health, /chat, /whatsapp
│   ├── prompts.py        # system prompt, mode detection, channel formatting
│   └── tools.py          # 4 agent tool functions
├── data/
│   ├── animals.json      # 33 animals across Bengaluru shelters
│   ├── vets.json         # 15 vets (all emergency-capable, 24×7)
│   ├── rescuers.json     # 10 rescuers with area + on-call hours
│   └── protocols.json    # 5 first-aid protocols
├── ingest/
│   ├── __init__.py
│   ├── bulk_load.py      # loads data/*.json into ES indices
│   ├── create_indices.py # creates / resets all 5 indices
│   ├── embedder.py       # EIS query-time helper
│   └── structurer.py     # raw_listings → animals (deferred)
├── scripts/
│   └── preflight.py      # checks ES, Bedrock, EIS connectivity
├── ui/
│   └── app.py            # Streamlit frontend
├── .env                  # secrets (never commit)
├── .env.example
└── requirements.txt
```

---

## Setup

### 1. Prerequisites

- Python 3.11
- An Elastic Cloud cluster with a trial or active subscription
- AWS credentials with Bedrock access (`us-east-1`)
- Twilio account + ngrok (for WhatsApp, optional)

### 2. Clone and install

```powershell
git clone <repo-url>
cd petRescue/pet-rescue
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### 3. Configure environment

```powershell
cp .env.example .env
```

Edit `.env`:

```
ELASTIC_CLOUD_ID=...
ELASTIC_API_KEY=...

AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-6

# WhatsApp (optional)
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
NGROK_AUTH_TOKEN=...

# Not used — EIS handles embeddings
JINA_API_KEY=
```

### 4. Preflight check

```powershell
python scripts/preflight.py
```

All three checks (Elasticsearch, Bedrock, EIS) must print `[OK]`.

### 5. Create indices and load data

```powershell
python -m ingest.create_indices --reset
python -m ingest.bulk_load
```

Expected output: `animals:33  vets:15  rescuers:10  protocols:5`

---

## Running

```powershell
# Terminal 1 — backend (run from inside pet-rescue/)
uvicorn backend.main:app --reload --port 8000

# Terminal 2 — frontend (run from inside pet-rescue/)
streamlit run ui/app.py
```

Open `http://localhost:8501`.

### Health check

```
GET http://localhost:8000/health
```

```json
{"status": "ok", "indices": {"animals": 33, "vets": 15, "rescuers": 10, "protocols": 5, "raw_listings": 0}}
```

---

## Demo flows

| Scenario | Sample message |
|---|---|
| Emergency | "Found injured dog near Forum Mall right now, what do I do?" |
| Adoption — semantic | "Looking for a calm cuddly dog good with my toddler." |
| Adoption — filtered | "Show me puppies under 3 months across all shelters." |

The UI renders:
- **Emergency**: red vet pins on map, vet and rescuer cards with WhatsApp/Email contact buttons, first-aid protocol in an expander.
- **Adoption**: color-coded animal pins by org, animal cards with photo, temperament tags, and source link.

---

## WhatsApp (optional)

```powershell
# Terminal 3
ngrok http 8000
```

Paste the ngrok URL (`https://<id>.ngrok.io/whatsapp`) as the Twilio Sandbox inbound webhook. Send messages from a phone already joined to the sandbox.

---

## Agent tools

| Tool | Purpose |
|---|---|
| `search_animals` | Hybrid BM25 + semantic search with optional species/size/age/boolean filters |
| `find_emergency_vet` | Geo-distance query for open 24×7 vets, sorted by distance |
| `find_active_rescuers` | Area term filter + on-call time check; falls back to wider Bengaluru |
| `get_protocol` | Semantic search over first-aid protocols |

Search uses **RRF** (`rank_window_size=50`, `rank_constant=20`) to fuse BM25 and EIS semantic scores without score normalization.

---

## Key constraints

- **No invented contacts** — the agent never fabricates phone numbers, addresses, or org names. Only values returned by tools appear in responses.
- **Source attribution** — every animal result includes `source_org` and `source_url` verbatim.
- **Protocol disclaimer** — all protocol responses end with: *"Always confirm with a vet immediately — this is general guidance only."*
- **Bengaluru only** — queries for other cities receive a polite rejection; no tool calls are made.
