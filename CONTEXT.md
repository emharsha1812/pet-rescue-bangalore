# Pet Rescue & Adoption Coordinator — Project Context

> **For Codex**: this is the source of truth for the project. Refer back here whenever you're unsure about schemas, tool signatures, agent behavior, or conventions. PLAN.md tells you what to build in what order; this file tells you what each piece looks like.

---

## 1. Problem statement

Bengaluru's stray and rescue ecosystem is fragmented across CUPA, Charlie's Animal Rescue, CARE, Krupa, individual rescuers, neighborhood WhatsApp groups, vet directories, and missing-pet pages. When someone finds an injured animal at 2am, knowing which 24×7 vet to call and which rescuer is awake matters more than any single piece of information. Adopters face the same fragmentation: animals are listed across many orgs with no unified search.

We are building an AI agent that:

1. Aggregates available animals across multiple orgs into one searchable surface.
2. Surfaces 24×7 emergency vets near the user's location.
3. Identifies on-call rescuers in the user's area.
4. Provides vetted first-aid protocols for common emergency scenarios.

The agent operates in **two distinct modes** based on the user's message:

- **Emergency mode**: triggered by phrases like "injured", "found", "bleeding", "hit by", "abandoned", "urgent". Output is calm, action-ordered, contacts first.
- **Adoption mode**: triggered by phrases like "looking for", "want to adopt", "considering". Output is warm, exploratory.

---

## 2. User journeys

### Journey A — Emergency (2am stray)
> User: "Found injured dog near Forum Mall right now, what do I do?"

Agent calls in parallel: `find_emergency_vet`, `find_active_rescuers`, `get_protocol`.

Response: 2–3 nearest 24×7 vets with phone numbers and distances, 1–2 on-call rescuers covering the area, brief first-aid steps.

**Hard requirement**: zero invented contacts. Only what tools returned.

### Journey B — Adoption
> User: "Looking for a calm small dog, I work from home, have a toddler."

Agent calls: `search_animals(query="calm gentle small dog good with kids", size="small", good_with_kids=true)`.

Response: 3–5 matched animals with photos, why-this-matches lines, source org and contact for each.

### Journey C — Filtered browsing
> User: "Show puppies under 3 months across all shelters."

Agent calls: `search_animals(query="puppy", species="dog", max_age_months=3)`.

Response: filtered list grouped by source org.

---

## 3. Tech stack (versions matter)

| Component | Version / Detail |
|---|---|
| Python | 3.11 |
| Elastic Cloud | trial cluster, Elasticsearch 8.15+ (must support `retriever` API with `rrf`) |
| Open Crawler | Elastic's open-source crawler, Docker |
| Embeddings | `jina-embeddings-v3` via REST API, **dim=1024**, similarity=cosine. Tasks: `retrieval.passage` for indexing, `retrieval.query` for search |
| LLM | AWS Bedrock, latest available Claude Sonnet (verify model ID in Bedrock console, e.g. `us.anthropic.claude-sonnet-4-5-...`). **Use the `converse` API, not `invoke_model`.** Native parallel tool-use enabled |
| Backend | FastAPI + uvicorn |
| Web UI | Streamlit |
| WhatsApp | Twilio WhatsApp Sandbox (number `+1 415 523 8886`) |
| Tunnel | ngrok (to expose FastAPI for Twilio webhook) |

---

## 4. Architecture

```
[Open Crawler (Docker)]
        │ crawls 3 shelter sites
        ▼
[raw_listings index]
        │
        ▼
[Structurer script (Bedrock Claude extracts fields)]
        │
        ▼
[Jina v3 embedding (passage)]
        │
        ▼
[animals index] ◄────── [bulk_load.py for curated JSON]
        ▲
        │
[vets, rescuers, protocols indices]
        ▲
        │
[User] ──web/WhatsApp──► [FastAPI /chat or /whatsapp]
                                 │
                                 ▼
                       [Bedrock agent with 4 tools]
                                 │
                                 ▼
                       [ES queries: hybrid (BM25 + kNN + RRF), geo, term]
```

---

## 5. Project layout

```
pet-rescue/
├── data/
│   ├── animals.json
│   ├── vets.json
│   ├── rescuers.json
│   └── protocols.json
├── crawler/
│   ├── crawler.yml
│   └── docker-compose.yml
├── ingest/
│   ├── __init__.py
│   ├── create_indices.py
│   ├── bulk_load.py
│   └── structurer.py
├── backend/
│   ├── __init__.py
│   ├── main.py            # FastAPI app
│   ├── agent.py           # Bedrock loop
│   ├── tools.py           # 4 tools
│   ├── prompts.py         # system prompts
│   └── es_client.py       # shared ES client
├── ui/
│   └── app.py             # Streamlit
├── .env
├── requirements.txt
└── README.md
```

---

## 6. Environment variables (`.env`)

```
ELASTIC_CLOUD_ID=...
ELASTIC_API_KEY=...
JINA_API_KEY=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-...   # verify in Bedrock console
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
NGROK_AUTH_TOKEN=...
```

---

## 7. Elasticsearch indices

### 7.1 `animals`

```json
{
  "mappings": {
    "properties": {
      "name": {"type": "text"},
      "species": {"type": "keyword"},
      "breed": {"type": "keyword"},
      "age_months": {"type": "integer"},
      "sex": {"type": "keyword"},
      "size": {"type": "keyword"},
      "vaccinated": {"type": "boolean"},
      "neutered": {"type": "boolean"},
      "good_with_kids": {"type": "boolean"},
      "good_with_dogs": {"type": "boolean"},
      "temperament_tags": {"type": "keyword"},
      "description": {"type": "text"},
      "description_vector": {
        "type": "dense_vector",
        "dims": 1024,
        "index": true,
        "similarity": "cosine"
      },
      "photo_url": {"type": "keyword"},
      "source_org": {"type": "keyword"},
      "source_url": {"type": "keyword"},
      "location": {"type": "geo_point"},
      "date_listed": {"type": "date"}
    }
  }
}
```

### 7.2 `vets`

```json
{
  "mappings": {
    "properties": {
      "name": {"type": "text"},
      "address": {"type": "text"},
      "location": {"type": "geo_point"},
      "phone": {"type": "keyword"},
      "hours": {"type": "object", "enabled": true},
      "emergency_capable": {"type": "boolean"},
      "has_surgery": {"type": "boolean"},
      "specialties": {"type": "keyword"}
    }
  }
}
```

`hours` is a flat object: keys `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`. Each value is a string `"HH:MM-HH:MM"`, or `"closed"`, or `"00:00-23:59"` for 24×7. Time is local (Asia/Kolkata).

### 7.3 `rescuers`

```json
{
  "mappings": {
    "properties": {
      "name": {"type": "text"},
      "contact": {"type": "keyword"},
      "areas_covered": {"type": "keyword"},
      "on_call_hours": {"type": "object", "enabled": true},
      "capabilities": {"type": "keyword"},
      "animals_handled": {"type": "keyword"}
    }
  }
}
```

`on_call_hours` has the same shape as `vets.hours`.
`capabilities` examples: `transport`, `fostering`, `first_aid`, `surgery_funding`.
`animals_handled` examples: `dog`, `cat`, `bird`, `cattle`.

### 7.4 `protocols`

```json
{
  "mappings": {
    "properties": {
      "scenario": {"type": "keyword"},
      "title": {"type": "text"},
      "body": {"type": "text"},
      "body_vector": {
        "type": "dense_vector",
        "dims": 1024,
        "index": true,
        "similarity": "cosine"
      },
      "severity": {"type": "keyword"}
    }
  }
}
```

`severity`: `low`, `medium`, `critical`.

### 7.5 `raw_listings` (Open Crawler output)

```json
{
  "mappings": {
    "properties": {
      "url": {"type": "keyword"},
      "title": {"type": "text"},
      "body": {"type": "text"},
      "source_org": {"type": "keyword"},
      "crawled_at": {"type": "date"},
      "structured": {"type": "boolean"}
    }
  }
}
```

`structured` is set to `true` after the structurer processes the doc.

---

## 8. Data sources

### 8.1 Sites to crawl (pick 3)

- Charlie's Animal Rescue — `https://charliesanimalrescue.com`
- CUPA Bangalore — `https://cupabangalore.org`
- Krupa Loving Animals — `https://krupa.org.in`
- People for Animals Bangalore — `https://pfabangalore.org`

Verify each has a clean "available animals" listing page before locking in. Pick the cleanest three.

### 8.2 Curated data targets

- `animals.json`: 30–40 entries copied from real listings.
- `vets.json`: 15–20 24×7 Bangalore vets (Cessna Lifeline, DCC Animal Hospital, Doggy World, etc.).
- `rescuers.json`: 8–10 entries — org hotlines + individual rescuers from public Instagram bios.
- `protocols.json`: 5 first-aid scenarios — injured stray, hit by vehicle, suspected poisoning, abandoned puppies, transport tips. Always include "consult a vet immediately" footer.

---

## 9. Agent tools (Bedrock `toolConfig`)

The agent uses Bedrock's `converse` API with `toolConfig`. Four tools are registered. Parallel tool use is enabled (`toolChoice: {"auto": {}}`).

### 9.1 `search_animals`

**Purpose**: hybrid search over the `animals` index.

**Parameters**:
- `query` (string, required): natural language description.
- `species` (string, optional): "dog", "cat", etc.
- `size` (string, optional): "small" / "medium" / "large".
- `max_age_months` (integer, optional).
- `good_with_kids` (boolean, optional).
- `good_with_dogs` (boolean, optional).
- `top_k` (integer, default 5).

**Implementation**:
1. Embed `query` via Jina (`jina-embeddings-v3`, task=`retrieval.query`).
2. Build ES request using `retriever` API with `rrf`:
   - `standard` retriever with `multi_match` over `name^2`, `breed^1.5`, `description`, plus `bool` `filter` for structured params (each only added if param is non-null).
   - `knn` retriever on `description_vector`: `k=20`, `num_candidates=100`, with the same `filter` array.
   - `rrf` parameters: `rank_window_size=50`, `rank_constant=20`.
3. Return top `top_k` hits as list of dicts containing all source fields plus `_score`.

### 9.2 `find_emergency_vet`

**Purpose**: nearest 24×7 emergency vets, currently open.

**Parameters**:
- `lat` (float, required).
- `lon` (float, required).
- `radius_km` (integer, default 5).

**Implementation**:
1. ES `bool` query, `filter`:
   - `geo_distance`: `distance: f"{radius_km}km"`, `location: {lat, lon}`.
   - `term`: `emergency_capable: true`.
2. Sort by `_geo_distance` ascending. Size 10.
3. In Python, filter results by `hours[today_weekday]` covering current Asia/Kolkata time. Compute `distance_km` per hit.
4. Return top 5 open vets.

### 9.3 `find_active_rescuers`

**Purpose**: rescuers covering an area, currently on-call.

**Parameters**:
- `area` (string, required): e.g., "Koramangala", "Whitefield".
- `time_iso` (string, required): ISO8601 timestamp.

**Implementation**:
1. ES `bool` `filter` with `term: {areas_covered: area}`. Size 20.
2. In Python, filter by `on_call_hours[weekday]` covering `time_iso`'s time-of-day.
3. **Fallback**: if no matches, drop the area filter and return any on-call rescuer in Bangalore (mark these as `area_match: false` in the result so the agent can mention they're covering a wider region).

### 9.4 `get_protocol`

**Purpose**: retrieve relevant first-aid protocol.

**Parameters**:
- `scenario` (string, required): natural language description.

**Implementation**:
1. Embed `scenario` via Jina (task=`retrieval.query`).
2. kNN on `body_vector`, `k=1`, `num_candidates=20`.
3. Return the protocol doc.

---

## 10. Agent system prompt requirements

The system prompt **must enforce all seven** of these explicitly:

1. **Mode detection**: classify the user's message at the start of each turn as `emergency` or `adoption`. Triggers — emergency: injured, hurt, found, bleeding, dying, abandoned, urgent, hit by, attacked, sick, "right now". Adoption: looking for, want to adopt, considering, "I'd like a", thinking about a pet. Tone differs:
   - Emergency: calm, action-ordered. Contacts first, then what to do, then what NOT to do. No fluff. No "I'm sorry to hear that".
   - Adoption: warm, conversational, may ask one clarifying question if intent is ambiguous.

2. **Parallel tool calls in emergency mode**: in one turn, call `find_emergency_vet`, `find_active_rescuers`, AND `get_protocol` together. Do not serialize.

3. **Strict no-fabrication rule** (CRITICAL): NEVER produce a phone number, name, address, or organization that did not come from a tool result. If tools returned empty, say "I don't have a verified contact for that area right now" and suggest backup steps (e.g., "Call BBMP Animal Helpline 080-22221188" — only if this number is in the rescuers index).

4. **Source attribution**: every animal mentioned must include `source_org` and a clickable `source_url`. Every vet/rescuer must come from a tool call.

5. **Channel-aware formatting**: backend passes `channel` ("web" or "whatsapp") in system prompt. WhatsApp = max 6 short lines, no markdown headings, plain text with `•` bullets. Web = richer formatting allowed (still concise).

6. **No medical certainty**: protocol responses always end with "Always confirm with a vet immediately — this is general guidance only."

7. **Bangalore default**: assume Bengaluru unless user states another city.

---

## 11. API endpoints (FastAPI)

### 11.1 `POST /chat`
Request body:
```json
{"message": "string", "session_id": "string"}
```
Response body:
```json
{
  "reply": "string",
  "structured_results": {
    "animals": [...],
    "vets": [...],
    "rescuers": [...],
    "protocols": [...]
  }
}
```
Conversation history maintained per `session_id` in an in-memory dict.

### 11.2 `POST /whatsapp`
Request: Twilio form-encoded body (`From`, `Body`, `WaId`, etc.).
Response: TwiML `<Response><Message>{reply}</Message></Response>` with `Content-Type: application/xml`.
Use `WaId` as `session_id`. Pass `channel="whatsapp"` to the agent.

### 11.3 `GET /health`
Returns:
```json
{"status": "ok", "indices": {"animals": 35, "vets": 15, ...}}
```

---

## 12. UI requirements (Streamlit)

- **Top of page**: title "🐾 Bengaluru Pet Rescue & Adoption Coordinator" + 3 demo prompt buttons that auto-fill the chat input.
- **Header badge**: "Crawler last ran: X min ago" (read max `crawled_at` from `raw_listings`).
- **Chat**: `st.chat_message` for conversation; `st.chat_input` for input.
- **Below each agent reply**, render structured cards (only if the corresponding list in `structured_results` is non-empty):
  - **Animal card**: photo (st.image, fallback placeholder if URL fails), name + breed, age + sex, "why this matches" line, source_org as link to source_url, contact.
  - **Vet card**: name, distance in km, "Open now" green badge, phone as `tel:` link.
  - **Rescuer card**: name, areas, contact (`tel:` link).
- Use `st.session_state.messages` for history, `st.session_state.session_id` for backend session.
- Default Streamlit theme. Do not over-style.

---

## 13. WhatsApp integration

1. Twilio Sandbox: number `+1 415 523 8886`, join code in Twilio console.
2. ngrok: `ngrok http 8000` to expose FastAPI.
3. Twilio webhook config: paste `https://<ngrok-id>.ngrok.io/whatsapp` as the inbound POST webhook.
4. From your phone (already joined to sandbox), send messages.
5. `/whatsapp` endpoint parses `Body` and `WaId`, calls agent with `channel="whatsapp"`, returns TwiML.

---

## 14. Demo scenarios

The live demo must show these three flows working:

1. **Emergency**: "Found injured dog near Forum Mall, what do I do?"
   - Expected: 2–3 nearest 24×7 vets, 1–2 on-call rescuers, brief first-aid.
2. **Semantic adoption**: "Looking for a calm cuddly dog good with my toddler."
   - Expected: 3–5 matches across different orgs, each with why-this-matches.
3. **Filtered adoption**: "Show puppies under 3 months across all shelters."
   - Expected: filtered by `species=dog` and `max_age_months=3`, sorted by `date_listed` desc.

Plus one WhatsApp demo of scenario 1 from the user's phone.

---

## 15. Constraints and non-goals

**Out of scope for v1**:
- User accounts, auth.
- Photo-based lost-pet matching.
- Notifications/scheduling.
- Donation flow.
- Real-time chat with rescuers.
- Multilingual UI.

**Hard constraints**:
- **Zero invented contacts**. Hallucinated phone numbers in emergency mode is a critical failure.
- **All embeddings**: `jina-embeddings-v3` at dim 1024.
- **All hybrid queries**: use the new ES `retriever` API with `rrf` (not the legacy `query` with sibling `knn`).
- **Bedrock**: use `converse` API.

---

## 16. Coding conventions

- Python 3.11.
- `python-dotenv` for `.env` loading; load at top of every entry-point module.
- ES client: official `elasticsearch` package (v8.x). Use `helpers.bulk` for bulk ops.
- Logging: stdlib `logging`, `INFO` level by default. Format: `%(asctime)s %(levelname)s %(name)s: %(message)s`.
- Errors: catch and log at the agent boundary; UI shows graceful fallback ("Something went wrong, try again").
- All scripts runnable from project root: `python -m ingest.create_indices`, `python -m ingest.bulk_load`, etc.
- Sync code unless async is required. FastAPI handlers can be sync.
- No global state except `es_client` singleton and FastAPI session dict.
- One module = one responsibility. No 800-line files.

---

## 17. Glossary

- **RRF (Reciprocal Rank Fusion)**: combines BM25 and kNN result rankings into a single score using `1/(k + rank)` per result list. Surfaces results that rank well in either lexical or semantic, without needing score normalization.
- **Hybrid search**: any combination of lexical (BM25) and semantic (vector) retrieval. Here it's BM25 + Jina kNN, fused via RRF.
- **Open Crawler**: Elastic's open-source web crawler. Configured per-domain in YAML, outputs documents directly to an ES index.
- **`converse` API**: Bedrock's modern chat API supporting tool use, replacing `invoke_model`.
- **TwiML**: Twilio Markup Language; XML response format for Twilio webhook handlers.
