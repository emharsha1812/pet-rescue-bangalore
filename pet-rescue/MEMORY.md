# Project Progress — Pet Rescue & Adoption Coordinator

## What has been done

### Pre-flight
- `scripts/preflight.py` — checks Elasticsearch, Bedrock, and EIS. All three passed:
  - Elasticsearch 9.4.0 (Elastic Cloud, `us-central1`)
  - Bedrock `global.anthropic.claude-sonnet-4-6` responds OK
  - EIS `.jina-embeddings-v5-text-small` returns dim=1024 vectors

---

### Architecture (confirmed deviations from original CONTEXT.md)

- **No Jina API key / direct REST calls.** Embeddings go through **Elastic Inference Service (EIS)**.
- Inference ID: `.jina-embeddings-v5-text-small`
- Index fields use `semantic_text` type with `inference_id` instead of `dense_vector`.
- `copy_to` pattern:
  - `animals.description` (text, BM25) → `animals.description_semantic` (semantic_text, vector)
  - `protocols.body` (text, BM25) → `protocols.body_semantic` (semantic_text, vector)
- EIS embeds automatically on document index — no explicit embedding step in `bulk_load.py`.
- Hybrid search: BM25 `multi_match` + `semantic` query, fused via **RRF** (`rank_window_size=50`, `rank_constant=20`).
- Bedrock model: `global.anthropic.claude-sonnet-4-6` via `bedrock-runtime` `converse` API.
- AWS region: `us-east-1`.

---

### Indices (live on Elastic Cloud)

All 5 created via `ingest/create_indices.py --reset`:

| Index | Key fields | Semantic field |
|---|---|---|
| `animals` | name, species, breed, age_months, sex, size, vaccinated, neutered, good_with_kids, good_with_dogs, temperament_tags, description, photo_url, source_org, source_url, location (geo_point), date_listed | `description_semantic` |
| `vets` | name, address, location (geo_point), phone, hours (object), emergency_capable, has_surgery, specialties | none |
| `rescuers` | name, contact, areas_covered (keyword[]), on_call_hours (object), capabilities, animals_handled | none |
| `protocols` | scenario, title, body, severity | `body_semantic` |
| `raw_listings` | url, title, body, source_org, crawled_at, structured | none |

**hours / on_call_hours format:** `"HH:MM-HH:MM"` per weekday key (`mon`–`sun`). `"00:00-23:59"` = 24×7. `"closed"` = not available.

---

### Data ingested (`ingest/bulk_load.py`)

| File | Index | Count |
|---|---|---|
| `data/animals.json` | `animals` | 33 docs |
| `data/vets.json` | `vets` | 15 docs |
| `data/rescuers.json` | `rescuers` | 10 docs |
| `data/protocols.json` | `protocols` | 5 docs |

- All 24×7 emergency vets have `emergency_capable: true` and all hours set to `"00:00-23:59"`.
- Rescuers have `areas_covered` as keyword arrays (e.g. `["Koramangala", "HSR Layout"]`).
- Protocols: `scenario` is a keyword slug (e.g. `"hit_by_vehicle"`). `severity` values: `"critical"`, `"moderate"`, etc.

---

### backend/tools.py — 4 agent tool functions (DONE, verified)

All functions return Python dicts/lists ready for JSON serialization. No external embedding calls — EIS handles all embedding inside ES.

#### `search_animals(query, species, size, max_age_months, good_with_kids, good_with_dogs, top_k=5) -> list[dict]`
- Hybrid RRF search: BM25 `multi_match` on `name^2, breed^1.5, description` + `semantic` on `description_semantic`.
- Optional ES filter clauses: `term` on species/size, `range` on age_months, `term` on booleans.
- Returns `_source` dicts augmented with `_score`.

#### `find_emergency_vet(lat, lon, radius_km=5) -> list[dict]`
- ES bool query: `geo_distance` filter + `term: {emergency_capable: true}`.
- Sorted by `_geo_distance` ascending. Size 10.
- Post-filtered in Python for open-now using `_covers_time()` helper (Asia/Kolkata time).
- Returns up to 5 results, each augmented with `distance_km` from sort metadata.

#### `find_active_rescuers(area, time_iso) -> list[dict]`
- ES query: `term: {areas_covered: area}`. Size 20.
- Post-filtered in Python for on-call using same `_covers_time()` helper.
- `area_match: True` on matched results.
- **Fallback**: if area query returns no on-call rescuers, re-runs with `match_all`, takes first 5 on-call, sets `area_match: False`. Agent uses this flag to phrase the response correctly.

#### `get_protocol(scenario) -> dict | None`
- Pure `semantic` query on `body_semantic`, size 1.
- Returns the top hit's `_source` or `None`.

#### Private helper: `_covers_time(hours: dict, dt: datetime) -> bool`
- Parses `"HH:MM-HH:MM"` slot for the weekday of `dt`. Returns False on malformed/missing entries without crashing.

**Verified with `python -m backend.tools`** — all 4 tools returned non-empty results against live Elastic Cloud.

---

### backend/prompts.py — system prompt (DONE)

`build_system_prompt(channel: str, current_time: str) -> str`

Enforces 7 rules:
1. Never invent phone numbers, names, addresses, or orgs — only use tool result values.
2. Every animal must cite `source_org` and `source_url` verbatim.
3. Every vet/rescuer/org must trace to a tool call this turn.
4. Protocol responses end with: `"Always confirm with a vet immediately — this is general guidance only."`
5. Default city Bengaluru — other cities get a polite rejection, no tool calls.
6. Do not summarize conversation history at the start of replies.
7. Empty/error tool results acknowledged briefly; no invented fallback.

Mode detection:
- **EMERGENCY**: injured, hurt, bleeding, found, dying, hit by, attacked, sick, abandoned, urgent, right now, what do I do, help → call `find_emergency_vet` + `find_active_rescuers` + `get_protocol` in parallel, contacts first.
- **ADOPTION**: looking for, want to adopt, considering, I'd like a, thinking about, show me, browse → call `search_animals` with extracted filters.

Hardcoded Bangalore lat/lon lookup table for area name → coordinates (Forum Mall, Indiranagar, Whitefield, Jayanagar, HSR Layout, fallback center).

Channel formatting:
- `web`: markdown allowed (bold, bullets).
- `whatsapp`: max 6 short lines, plain text only, no markdown headers.

---

### backend/agent.py — Bedrock converse loop (DONE)

- `_sessions: dict[str, list[dict]] = {}` — in-memory conversation history per session. Restart wipes it (intentional for hackathon).
- `_bedrock_client` initialized at module load from `AWS_REGION` env var.
- `TOOL_CONFIG` — Bedrock `toolSpec` entries for all 4 tools with JSON Schema input schemas.

#### `run_agent(message, session_id, channel="web") -> tuple[str, dict]`
1. Appends user message to session history.
2. Builds system prompt with current Asia/Kolkata time.
3. Loops up to 5 iterations:
   - Calls `bedrock_client.converse(modelId=BEDROCK_MODEL_ID, messages=history, system=..., toolConfig=..., inferenceConfig={maxTokens:2048})`.
   - Appends assistant message to history.
   - If `stopReason != "tool_use"`: extracts text blocks, returns `(text, structured_results)`.
   - Otherwise: dispatches all `toolUse` blocks via `_execute_tool`, appends all `toolResult` blocks in a single user message turn.
4. Returns fallback message if loop exhausted.

#### `_execute_tool(name, input_args, tool_use_id, structured) -> dict`
- Dispatches to the correct tool function via name matching.
- Extends `structured["animals" | "vets" | "rescuers"]` or appends to `structured["protocols"]`.
- **Critical**: Bedrock requires `toolResult.content[].json` to be a JSON object (dict), NOT an array. List results are wrapped as `{"results": [...]}` before returning to Bedrock.
- On exception: logs with traceback, returns `toolResult` with `status: "error"`.

---

### backend/main.py — FastAPI app (DONE)

#### `GET /health`
- Queries `es.count(index=...)` for all 5 indices.
- Returns `{"status": "ok", "indices": {name: count}}`. Count is `null` if query fails (never crashes health).

#### `POST /chat`
- Request: `{message: str, session_id: str}`
- Response: `{reply: str, structured_results: dict}`
- Calls `run_agent(..., channel="web")`.
- On exception: logs traceback, returns 200 with graceful error message and empty structured_results. Never returns 500.

#### `POST /whatsapp`
- Twilio webhook. Reads `From`, `Body`, `WaId` from form-encoded body.
- Calls `run_agent(Body, session_id=WaId, channel="whatsapp")`.
- Returns TwiML XML. Reply is XML-escaped (`xml.sax.saxutils.escape`).
- On exception: returns TwiML with polite error message, status 200.

#### App config
- CORS: `allow_origins=["*"]` (Streamlit runs on localhost:8501).
- `load_dotenv()` at top.
- Logging at INFO level.

---

### ui/app.py — Streamlit frontend (DONE)

Single-file Streamlit app. Run with `streamlit run ui/app.py` from inside `pet-rescue/`.

#### Layout
- Wide mode (`st.set_page_config(layout="wide")`).
- `st.columns([2, 3])` — left 40% chat, right 60% map + cards.
- Thin header strip above columns: health badge (green) or unreachable error (red).

#### Session state keys
| Key | Type | Purpose |
|---|---|---|
| `messages` | `list[dict]` | Full chat history `{role, content}` |
| `session_id` | `str` | UUID, passed to `/chat` for Bedrock session continuity |
| `last_structured` | `dict` | Latest `structured_results` from `/chat` — drives map and cards |
| `response_count` | `int` | Increments on each reply; used as folium map `key` to force re-render |
| `pending_prompt` | `str | None` | Set by demo buttons → consumed at top of next rerun |
| `last_user_loc` | `tuple | None` | Parsed lat/lon from user message text; persisted for map blue pin |

#### Health badge (`fetch_health`)
- `@st.cache_data(ttl=30)` on the inner `_fetch_health_cached()` which **raises** on failure.
- Outer `fetch_health()` catches and returns `None`.
- Raising (not returning None) prevents caching of failed health checks — badge clears immediately when backend comes up, no 30s stale wait.

#### Demo buttons
- Three `st.link_button`-style buttons via `st.columns(3)` inside left column.
- On click: sets `st.session_state.pending_prompt = prompt` → `st.rerun()`.
- Top of script consumes `pending_prompt` before rendering — treated identically to typed input.

#### Map (`build_map`)
- Library: `folium` + `streamlit_folium.st_folium`.
- Key = `f"map_{response_count}"` — changes each reply, forcing actual re-render.
- **Emergency mode** (vets non-empty): red `+` pins for vets (popup: name, distance, phone as `tel:` link, address, "✓ Open now"), blue `home` pin for parsed user location.
- **Adoption mode** (animals non-empty): color-coded `paw` pins by `source_org`. Fixed palette: `["green","purple","orange","cadetblue","darkblue","darkred","darkgreen"]`. HTML legend injected via `m.get_root().html.add_child(folium.Element(...))`.
- Center: average lat/lon of returned results.
- Empty state: `st.info("Ask a question to see results on the map.")`.

#### Location extraction (`extract_location`)
- Scans user message text for known Bangalore place names (dict hardcoded, mirrors `backend/prompts.py`).
- Returns `(lat, lon)` tuple or `None`. Stored in `last_user_loc` session state.

#### Cards (`render_cards`)
- **Animals**: `min(3, len(animals))` column grid. Each card: photo (HTML `<img>` with `onerror` hide), name/breed/age/sex, temperament_tags (up to 4), `source_org` as markdown link to `source_url`.
- **Vets**: vertical list. Name + distance, "Open now" green badge, `tel:` phone link, address, specialties, **💬 WhatsApp + ✉️ Email contact buttons**.
- **Rescuers**: vertical list. Name, "On-call" / "Wider BLR" badge, `tel:` contact link, areas (up to 5), capabilities, **💬 WhatsApp + ✉️ Email contact buttons**.
- **Protocols**: `st.expander` per protocol, title as header, `body` as markdown.

#### Contact buttons (`_contact_buttons`)
- **WhatsApp**: `https://wa.me/91{digits}?text={url_encoded_template}`. Strips non-digits, strips leading `0`, prepends `91`. Opens WhatsApp Web/app with pre-filled emergency message. Template differs for vets vs. rescuers.
- **Email**: `mailto:?subject=...&body=...` (no To: — data has no email addresses). Opens user's email client with pre-filled subject + body. User fills in recipient manually.
- Rendered as `st.link_button` with `use_container_width=True`.

#### API call (`call_chat`)
- `requests.post("http://localhost:8000/chat", json={...}, timeout=60)`.
- Handles: `Timeout` → timeout message in chat, `ConnectionError` → unreachable message, non-200 → HTTP status in message.
- On error: reply shown in chat, `last_structured` NOT updated (map/cards stay at prior valid state).
- On success: updates `last_structured`, `last_user_loc`, increments `response_count`, calls `st.rerun()`.

#### Bugs fixed during development
1. `SyntaxError` — backslash escape inside f-string (`\"` in f-string not valid < Python 3.12). Fixed by building legend HTML in a list comprehension with temp variable.
2. Stale "backend unreachable" banner — `@st.cache_data` was caching `None` for 30s after backend started. Fixed by raising exceptions in the cached inner function so `st.cache_data` never stores failed results.
3. Port 8000 conflict (`WinError 10013`) on first run — stale Python process from previous uvicorn. Fixed by `Stop-Process -Id <pid> -Force`.
4. `ModuleNotFoundError: No module named 'backend'` — uvicorn launched from wrong directory (`petRescue/` instead of `petRescue/pet-rescue/`). Must `cd pet-rescue` first.

---

### End-to-end verification results

| Test | Outcome |
|---|---|
| `GET /health` | `status: ok`, animals:33, vets:15, rescuers:10, protocols:5, raw_listings:0 |
| Emergency — Forum Mall injured dog | Reply: 2 vets with distances + phones, 3 rescuers, first-aid steps, disclaimer. Map: red vet pins + blue Forum Mall pin. Vet cards with "Open now" badges + WhatsApp/Email buttons. |
| Adoption — calm cuddly dog, toddler | Reply: 5 semantically matched dogs with source_org + source_url. Map: color-coded animal pins with legend. Animal cards in 3-column grid. |
| Filter — puppies under 3 months | Reply: results with age_months ≤ 3, species: dog. Age filter enforced correctly by ES. Map and cards updated. |
| Backend down | UI shows "Backend unreachable" in chat. Map and cards retain last valid state. No crash. |
| Demo buttons | All 3 buttons fire correct prompts, map and cards update correctly on each. |

---

### Files in place

```
scripts/preflight.py       ✓ done
ingest/create_indices.py   ✓ done
ingest/bulk_load.py        ✓ done
ingest/embedder.py         ✓ done (EIS helper, query-time only)
backend/es_client.py       ✓ done
backend/__init__.py        ✓ done
backend/tools.py           ✓ done (4 tool functions, verified)
backend/prompts.py         ✓ done (system prompt, 7 rules, mode detection)
backend/agent.py           ✓ done (Bedrock converse loop, tool dispatch)
backend/main.py            ✓ done (FastAPI: /health, /chat, /whatsapp)
data/animals.json          ✓ done (33 docs)
data/vets.json             ✓ done (15 docs)
data/rescuers.json         ✓ done (10 docs)
data/protocols.json        ✓ done (5 docs)
ui/app.py                  ✓ done (Streamlit UI — verified working)
requirements.txt           ✓ updated (added folium>=0.16.0, streamlit-folium>=0.20.0)
```

---

### What still needs to be built (deferred)

| File | Priority | Notes |
|---|---|---|
| `crawler/crawler.yml` + `crawler/docker-compose.yml` | DEFERRED | Open Crawler config. Only if time permits. |
| `ingest/structurer.py` | DEFERRED | raw_listings → animals pipeline. Depends on crawler. |

---

## How to run

```powershell
# Terminal 1 — backend (must be inside pet-rescue/)
cd pet-rescue
uvicorn backend.main:app --reload --port 8000

# Terminal 2 — frontend (must be inside pet-rescue/)
cd pet-rescue
streamlit run ui/app.py
```

Then open `http://localhost:8501`.

---

## Key env vars (from .env — DO NOT COMMIT)

| Var | Value / note |
|---|---|
| `ELASTIC_CLOUD_ID` | set |
| `ELASTIC_API_KEY` | set |
| `AWS_ACCESS_KEY_ID` | set |
| `AWS_SECRET_ACCESS_KEY` | set |
| `AWS_REGION` | `us-east-1` |
| `BEDROCK_MODEL_ID` | `global.anthropic.claude-sonnet-4-6` |
| `JINA_API_KEY` | empty — not used (EIS handles embeddings) |
