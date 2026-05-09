# Execution Plan — 3 Hours

> Read CONTEXT.md first. This file tells you **what to build in what order**; CONTEXT.md tells you **what each piece looks like**.

---

## Pre-flight (must be done before starting the timer)

- [x] Bedrock model access enabled in `us-east-1` (verify with a 1-line `boto3` call).
- [x] Elastic Cloud trial deployment running (have Cloud ID + API key in hand).
- [x] Jina API key obtained.
- [ ] Twilio account created + WhatsApp Sandbox joined from your phone (note the join code).
- [ ] AWS credentials available in shell.
- [ ] ngrok installed and authenticated (`ngrok config add-authtoken ...`).
- [ ] Docker running on your machine (for Open Crawler).

If any unchecked item takes more than 5 minutes, defer it past the relevant block and proceed.

---

## Hour 1 — Data and indices

### 0:00 – 0:10 — Project skeleton + connectivity sanity check

**Build**:
- Create directory tree per CONTEXT.md §5.
- `requirements.txt` with: `elasticsearch`, `boto3`, `fastapi`, `uvicorn`, `streamlit`, `twilio`, `requests`, `python-dotenv`, `pydantic`.
- `.env` populated with all keys from CONTEXT.md §6.
- A throwaway `scripts/preflight.py` that does three things and exits 0 on success:
  1. `Elasticsearch(...).info()` returns cluster name.
  2. `boto3.client("bedrock-runtime").converse(...)` with "hi" returns text.
  3. `requests.post("https://api.jina.ai/v1/embeddings", ...)` with `"test"` returns a 1024-dim vector.

**Checkpoint**: preflight script prints `OK` for all three. **Do not proceed if any fails.**

---

### 0:10 – 0:20 — Indices

**Build**: `ingest/create_indices.py` creates all five indices with mappings per CONTEXT.md §7. Idempotent (deletes-and-recreates if `--reset` flag, otherwise skips existing).

**Run**: `python -m ingest.create_indices --reset`.

**Checkpoint**: in Kibana Dev Tools, `GET _cat/indices?v` shows `animals`, `vets`, `rescuers`, `protocols`, `raw_listings`, all with 0 docs.

---

### 0:20 – 0:30 — Open Crawler kickoff

**Build**:
- `crawler/crawler.yml`: configure 3 chosen domains with seed URLs restricted to adoption-listing paths. Output to ES `raw_listings` index using Cloud ID + API key.
- `crawler/docker-compose.yml`: brings up the Open Crawler container with volume-mounted config.

**Run**: `cd crawler && docker compose up -d`. Tail logs for 30 sec to confirm "Started crawling".

**Move on. Do not wait for completion.** It runs in the background while you do the next blocks.

**Checkpoint**: container running (`docker ps`), no errors in logs.

---

### 0:30 – 0:50 — Curated data files

**Build** (in parallel — open multiple browser tabs):
1. CUPA, Charlie's, and one Instagram rescuer page open. Copy 30–40 listings as plain text into `data/raw_animals.txt`.
2. Google Maps: search "24x7 vet Bangalore". Copy 15 entries (name, address, phone) into `data/raw_vets.txt`.
3. Search Instagram for #bangalorerescue, find 8–10 individual rescuer bios. Copy into `data/raw_rescuers.txt`.

**Codex prompts** (one at a time):
- "Convert this raw text into `data/animals.json` matching the animals schema in CONTEXT.md. Generate plausible Bangalore lat/lon based on the source org's neighborhood. Set `date_listed` to the past 30 days, randomly."
- "Convert this raw vet text into `data/vets.json`. Geocode addresses using approximate Bangalore coordinates. Set `emergency_capable: true` for any clinic that mentions 24x7 or emergency."
- "Convert this raw rescuer text into `data/rescuers.json`."
- "Generate `data/protocols.json` with 5 entries for these scenarios: injured stray, hit by vehicle, suspected poisoning, abandoned puppies, transport tips. End each `body` with the disclaimer from CONTEXT.md §10 rule 6."

**Checkpoint**: 4 valid JSON files in `data/`, each loadable by `json.load`.

---

### 0:50 – 1:00 — Bulk-ingest curated data with embeddings

**Build**: `ingest/bulk_load.py`:
- Reads each JSON.
- For `animals.json`: batches descriptions of 32, calls Jina (`jina-embeddings-v3`, task=`retrieval.passage`, dim=1024). Adds `description_vector` to each doc.
- Same for `protocols.json` over `body` field → `body_vector`.
- `vets.json` and `rescuers.json` indexed without embeddings.
- Uses `elasticsearch.helpers.bulk` for ingestion.

**Run**: `python -m ingest.bulk_load`.

**Checkpoint**:
- `GET animals/_count` ≈ 35.
- `GET vets/_count` ≈ 15.
- `GET rescuers/_count` ≈ 10.
- `GET protocols/_count` = 5.
- Spot-check: `GET animals/_search?size=1` — first hit has `description_vector` of length 1024.

---

## Hour 2 — Agent and backend

### 1:00 – 1:15 — Structurer (raw_listings → animals)

**Build**: `ingest/structurer.py`:
- Scrolls `raw_listings` where `structured: false` or missing.
- For each doc, calls Bedrock `converse` with a system prompt asking it to extract `{name, species, breed, age_months, sex, size, vaccinated, neutered, good_with_kids, good_with_dogs, temperament_tags, description}` from the raw `body`. Output as JSON.
- If body clearly isn't an animal listing, skip (set `structured: true` to avoid reprocessing).
- Embeds the extracted `description` via Jina.
- Indexes to `animals` with `source_org` from the `raw_listings` doc.
- Updates `raw_listings` doc: `structured: true`.
- Cap at **30 docs** for the demo (don't blow Bedrock spend).

**Run**: `python -m ingest.structurer --limit 30`.

**Checkpoint**:
- `animals` count grew (curated 35 + crawled-and-structured ~10–30 = 45–65).
- Spot-check: an animal with `source_org="Charlie's"` (or similar) exists.

---

### 1:15 – 1:30 — Tools module

**Build**: `backend/tools.py` with the 4 tools from CONTEXT.md §9. Each is a plain Python function returning `dict` or `list[dict]`.

**Test in `__main__` block** of the same file:
```
python -m backend.tools
```
Should print results from these calls:
- `search_animals("calm gentle dog", size="small", good_with_kids=True)` → 5 results.
- `find_emergency_vet(12.9716, 77.5946, 5)` → list with `distance_km`.
- `find_active_rescuers("Koramangala", "<current ISO time>")` → at least 1.
- `get_protocol("found injured dog")` → 1 protocol.

**Checkpoint**: all four tools return plausible results from the CLI. **Do not proceed otherwise** — the agent layer is useless if tools are broken.

---

### 1:30 – 1:55 — Agent + FastAPI

**Build**:
- `backend/prompts.py`: system prompt incorporating all 7 rules from CONTEXT.md §10. Take `channel` ("web"/"whatsapp") and `current_time` as template variables.
- `backend/agent.py`:
  - `run_agent(message, session_id, channel)` function.
  - Maintains conversation history per `session_id` in module-level dict.
  - Bedrock `converse` loop with `toolConfig` (the 4 tools' JSON schemas) and `toolChoice: {"auto": {}}`.
  - Loop until `stopReason != "tool_use"`.
  - Collects `structured_results` across tool calls (animals from `search_animals`, vets from `find_emergency_vet`, etc.).
  - Returns `(final_text, structured_results)`.
- `backend/main.py`: FastAPI with `/chat`, `/whatsapp`, `/health` endpoints per CONTEXT.md §11.

**Run**: `uvicorn backend.main:app --reload --port 8000`.

**Test**:
```
curl -X POST localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Found injured dog near Forum Mall right now","session_id":"test1"}'
```
Then test the other two demo prompts.

**Checkpoint**:
- All 3 demo prompts return sane responses.
- No hallucinated phone numbers (sanity check: every phone in the response should appear in `vets.json` or `rescuers.json`).
- Emergency response includes vets AND rescuers AND first-aid (3 parallel tool calls observed).

---

### 1:55 – 2:00 — Buffer

Fix anything broken from the last 60 minutes. **Do not proceed to UI until backend is solid.** UI is cosmetic; backend is the demo.

---

## Hour 3 — UI, WhatsApp, demo polish

### 2:00 – 2:25 — Streamlit web app

**Build**: `ui/app.py` per CONTEXT.md §12.

**Run**: `streamlit run ui/app.py` (separate terminal).

**Test**: click each of the 3 demo prompt buttons. Verify:
- Chat history persists across messages.
- Animal cards render with photos (some may have placeholder if URL fails — fine).
- Vet cards show distance and "Open now" badge.
- Phone numbers are tappable (`tel:` links).

**Checkpoint**: chat works end-to-end, structured cards render correctly.

---

### 2:25 – 2:45 — Twilio WhatsApp

**Build/configure**:
1. Start ngrok in a new terminal: `ngrok http 8000`. Copy the `https://<id>.ngrok.io` URL.
2. Twilio Console → Messaging → Try WhatsApp → Sandbox settings. Set "When a message comes in" to `https://<ngrok-id>.ngrok.io/whatsapp` (POST). Save.
3. From your phone (already joined to sandbox), send: "Found injured dog near Forum Mall right now".
4. Expect TwiML response back in WhatsApp within 5–10 sec.

**Checkpoint**: WhatsApp roundtrip works, response is short (≤6 lines), contains real vet contacts.

**Common gotcha**: if you restart ngrok, the URL changes — re-paste in Twilio config.

---

### 2:45 – 3:00 — Demo polish

1. Run all 3 web prompts + 1 WhatsApp prompt end-to-end. Time them. (Each should be under 15 sec.)
2. Take **screenshots** after each successful response. Save in `demo/screenshots/`. These are your fallback if the live demo dies.
3. Open Kibana, save Discover views for `animals`, `raw_listings`, `vets`. Have these tabs ready.
4. Open Kibana Dev Tools, paste in two queries:
   - **The hybrid query** (full RRF retriever) on "calm cuddly dog for toddler".
   - **The same query as BM25-only** (`multi_match` only) — for the side-by-side wow moment.
5. Write the 6-beat pitch on a sticky note (see below).

---

## 6-beat demo pitch (memorize)

| # | Beat | Time | What happens |
|---|---|---|---|
| 1 | **Stakes** | 30s | "It's 2am. Someone finds an injured dog. They need a vet *and* a rescuer *and* first aid in 30 seconds. Bengaluru has all of this — scattered across CUPA, Charlie's, CARE, Instagram, WhatsApp groups. Nobody can pull it together fast enough." |
| 2 | **Emergency demo** | 45s | Type Forum Mall prompt on web app. Response in <5 sec with vets, rescuers, first aid. Highlight: "Notice — every phone number here came from our index. Zero hallucinated contacts. That's a hard rule." |
| 3 | **Semantic adoption demo** | 60s | Type "calm cuddly dog good with my toddler" on web app. Show 3–5 matches. Open Kibana side-by-side, run the same query as BM25-only — show the results are different/worse. *"This is hybrid search: BM25 plus Jina v3 embeddings, fused with reciprocal rank fusion. Pure keyword would never find these matches."* |
| 4 | **Pipeline reveal** | 30s | Open Kibana, show all 5 indices. Point to `raw_listings` count growing live from Open Crawler. *"Open Crawler ingests messy HTML, Bedrock structures it, Jina embeds it, Elasticsearch indexes it. The pipeline scales to any number of orgs."* |
| 5 | **WhatsApp reveal** | 30s | Send the same emergency prompt from your phone. Response arrives in WhatsApp. Pause for effect. |
| 6 | **Roadmap** | 20s | "Next: 10+ orgs ingested, photo-based lost-pet matching with image embeddings, partnerships with CUPA and Charlie's. Real impact, real animals." |

**Total: ~3.5 minutes.** Practice it once before the demo.

---

## Failure recovery

| Failure | Action |
|---|---|
| Bedrock model not accessible in region | Switch `AWS_REGION` to whichever region your model is in. If it's only Sonnet 3.5 you can access, use that — `converse` API works the same. |
| Open Crawler stuck or failing | Skip it. Demo with curated data only. Show the `crawler.yml` config and say "configured, runs offline; here's the resulting index" with a screenshot. |
| Jina API rate-limited | Cache embeddings to a JSON file on first call; reuse on retry. Add a 1-sec sleep between batches if hitting limits. |
| Streamlit losing chat state | Ensure `st.session_state.messages` is initialized in an `if "messages" not in st.session_state` block at the top. |
| Twilio webhook 502 / no response | ngrok URL changed — re-paste in Twilio config. Check FastAPI logs. |
| Live demo breaks completely | Walk through screenshots from the sticky note. Judges respect calm under pressure. |
| No on-call rescuer found for area | The fallback in `find_active_rescuers` handles this — agent should say "no rescuer in that exact area, here's whoever is on-call in Bangalore right now." |
| Agent hallucinated a phone number | Fix the system prompt: add a final reinforcement "Every phone number, name, address, and org you mention MUST appear in a tool result. If it doesn't, you must remove it." Re-test. |

---

## What NOT to do

- Don't spend more than 10 min on any one Open Crawler config issue. Skip it.
- Don't write authentication. No login.
- Don't add a database. Session state is in-memory.
- Don't theme Streamlit. Default is fine.
- Don't try parallel async in the agent loop. Sync is faster to debug.
- Don't add features not in CONTEXT.md §14 demo scenarios.
- Don't refactor. Ship the working version.
