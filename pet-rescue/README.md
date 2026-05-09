# Pet Rescue & Adoption Coordinator

Bengaluru-focused AI agent for animal adoption matching + emergency rescue coordination.
Built for the Elastic hackathon. See `CONTEXT.md` for architecture, `PLAN.md` for execution.

## One-time setup
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your keys
python scripts/preflight.py
```

If preflight prints `[OK]` for all three (Elasticsearch, Bedrock, Jina), you're good to proceed.

## Run order (after preflight passes)
1. `python -m ingest.create_indices --reset`
2. `cd crawler && docker compose up -d && cd ..`     # crawler runs in background
3. (Generate `data/*.json` files via Codex)
4. `python -m ingest.bulk_load`
5. `python -m ingest.structurer --limit 30`
6. `uvicorn backend.main:app --reload --port 8000`   # terminal 1
7. `streamlit run ui/app.py`                          # terminal 2
8. `ngrok http 8000`                                  # terminal 3, paste URL into Twilio
