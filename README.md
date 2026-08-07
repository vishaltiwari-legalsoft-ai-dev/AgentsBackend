# AgentOS Backend (FastAPI)

Python API that runs the AgentOS agents (Graphics Designer, Marketing Research,
SEO GEO, Blog Writer) and supporting services. Deploys to Google Cloud Run.

## Layout

```
app/
  main.py              FastAPI app, CORS, error handler, router mounting
  config.py            Typed settings (pydantic-settings) from .env
  security.py          Google Sign-In verification + app JWT issue/verify
  services/
    openrouter.py      LLM (via OpenRouter) + image generation
    firestore_repo.py  Firestore data access
    storage.py         Cloud Storage upload + signed URLs
    canva.py           Canva Connect OAuth + asset import
  routers/             health, auth, brands, library, admin, canva,
                       graphics_designer, creative_agent, marketing_research,
                       seo_geo, blog_writer, reference_library
  ingest.py            Brand Kits ingestion (python -m app.ingest)
agents/                The agent packages (added to sys.path by app/__init__.py)
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

## Auth

Login is Google-only: the frontend posts a Google ID token to
`POST /api/auth/google` and receives an app JWT (HS256, `JWT_SECRET`).
Essentially every other route requires that JWT as a Bearer token; admin and
creator routes check the role claims baked into it.

## Ingest Brand Kits

```bash
python -m app.ingest
```

Reads `BRAND_KITS_DIR`, treats each top-level folder as a brand (folders
starting with `_` are skipped), uploads files to GCS, and records metadata in
Firestore.

## Deploy to Cloud Run

The live service is **`agentsbackend`** (us-central1) — deploys are manual:

```bash
gcloud run deploy agentsbackend \
  --source . \
  --region us-central1 \
  --allow-unauthenticated
```

Runtime env vars are set once on the Cloud Run service and preserved across
deploys (see `deploy.env.yaml.example` and `../credentials.md`). With the
service account attached, leave `GOOGLE_APPLICATION_CREDENTIALS` unset.
