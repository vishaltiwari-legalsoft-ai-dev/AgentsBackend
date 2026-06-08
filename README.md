# AgentOS Backend (FastAPI + LangGraph)

Python API that runs the AgentOS agent and supporting services. Deploys to
Google Cloud Run.

## Layout

```
app/
  main.py              FastAPI app, CORS, error handler, router mounting
  config.py            Typed settings (pydantic-settings) from .env
  models.py            Pydantic request/response schemas
  security.py          JWT issue/verify + password hashing (bcrypt)
  agent/
    state.py           LangGraph AgentState
    prompts.py         Master Prompt + dual-asset instructions
    nodes.py           Graph nodes (retrieve, intent, analyze, prompt, generate, persist)
    graph.py           StateGraph assembly + run_agent()
  services/
    openrouter.py      LLM (LangChain) + image generation
    firestore_repo.py  Firestore data access
    storage.py         Cloud Storage upload + signed URLs
    canva.py           Canva Connect OAuth + asset import
  routers/             health, agent, brands, references, auth, canva
  ingest.py            Brand Kits ingestion (python -m app.ingest)
Dockerfile             Cloud Run image (Python 3.12)
```

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # fill values (see ../credentials.md)
uvicorn app.main:app --reload --port 8080
```

API docs: <http://localhost:8080/docs>

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/api/health` | – | Liveness |
| POST | `/api/agent` | – | Run the LangGraph agent (`{ "message": "..." }`) |
| GET | `/api/brands` | – | List brands |
| GET | `/api/brands/{id}` | – | Brand detail + creatives |
| POST | `/api/auth/register` / `/api/auth/login` | – | Get a JWT |
| POST | `/api/upload-reference` | Bearer | Upload a reference file (`file`) |
| GET | `/api/references` | Bearer | The user's uploads |
| GET | `/api/canva/authorize` · `/api/canva/callback` | – | Canva OAuth |
| POST | `/api/canva/import` | – | Import an asset into Canva |

The agent response is typed: `{type: "assets" | "brand_analysis" | "message", ...}`.
If Cloud Storage is unconfigured, generated images return as inline data URLs so
the app is demoable during setup.

## Ingest Brand Kits

```bash
python -m app.ingest
```

Reads `BRAND_KITS_DIR` (defaults to `./Brand Kits`), treats each top-level folder
as a brand, uploads files to GCS, and records metadata in Firestore. Never reads
`LS DESIGN PRODUCTIONS/`.

## Deploy to Cloud Run

```bash
gcloud run deploy agentos-backend \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --service-account agentos-backend@PROJECT.iam.gserviceaccount.com
```

Then set env vars in the Cloud Run service (see `../credentials.md`). With the
service account attached, you can omit `GOOGLE_APPLICATION_CREDENTIALS`.
