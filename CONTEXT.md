# AgentOS Backend — Context Document

> Generated from full codebase analysis. Describes what this service does, its
> components, roles, security model, and external integrations.

---

## 1. What This Backend Is

**AgentOS Backend** is a Python **FastAPI** API that powers an AI creative agent for
**LS Design Productions** house of brands. It:

- Runs a **LangGraph** workflow that analyzes brand kits or generates on-brand
  marketing creatives (banners, flyers, brochures, social posts, ads, posters).
- Serves a **Next.js frontend** (hosted on Vercel) over CORS.
- Deploys to **Google Cloud Run** via GitHub Actions CI/CD.

The backend is the "brain" for brand-aware creative generation: it retrieves brand
context from Firestore/GCS, synthesizes a master prompt from brand rules + user
brief, generates dual image assets via OpenRouter, and optionally exports to Canva.

---

## 2. Tech Stack

| Layer | Technology |
|-------|------------|
| Framework | FastAPI + Uvicorn |
| Agent orchestration | LangGraph + LangChain |
| LLM / image generation | OpenRouter (OpenAI-compatible API) |
| Database | Google Cloud Firestore |
| File storage | Google Cloud Storage (GCS) |
| Auth | Google Identity Services (ID tokens) + app JWT (PyJWT, HS256) |
| Design export | Canva Connect (OAuth 2.0 + PKCE) |
| Config | pydantic-settings (`.env`) |
| Container | Docker (Python 3.12-slim) → Cloud Run |

**Key dependencies:** `langchain`, `langgraph`, `google-cloud-firestore`,
`google-cloud-storage`, `httpx`, `PyJWT`, `google-auth`, `pypdf`, `python-docx`,
`svglib`, `pillow`.

---

## 3. Project Structure

```
Backend/
├── app/
│   ├── main.py              # FastAPI entry, CORS, router mounting, error handler
│   ├── config.py            # Typed settings from environment
│   ├── models.py            # Pydantic request/response schemas
│   ├── security.py          # Google ID-token verify + JWT issue/verify
│   ├── ingest.py            # Brand Kits → GCS + Firestore ingestion CLI
│   ├── services/
│   │   ├── firestore_repo.py  # Firestore CRUD (brands, creatives, users, conversations)
│   │   ├── storage.py         # GCS upload, signed URLs, gallery helpers
│   │   ├── openrouter.py      # LLM + image generation + vision OCR
│   │   ├── canva.py           # Canva OAuth + asset import
│   │   ├── imaging.py         # Logo rasterization + compositing
│   │   └── extract.py         # PDF/DOCX/image text extraction
│   └── routers/             # HTTP route modules (see §5)
├── Dockerfile
├── requirements.txt
├── .env.example
└── .github/workflows/deploy-cloudrun.yml
```

---

## 4. Core Domain & Data Model

### 4.1 Firestore Collections

| Collection | Purpose |
|------------|---------|
| `brands` | Brand records: name, metadata (colors, fonts, tone), source folder |
| `creatives` | Brand-kit files linked to a brand (stores `gs://` URIs, not bytes) |
| `reference_creatives` | User-uploaded reference files (per-user) |
| `users` | Google sign-in accounts (email, name, picture, provider) |
| `conversations` | Chat history per user (messages with agent results) |
| `creative_events` | Analytics events for generated creatives (admin dashboard) |

### 4.2 GCS Layout

| Path pattern | Contents |
|--------------|----------|
| `{brand_id}/creatives/{file}` | Ingested brand-kit assets |
| `references/{user_id}/{file}` | User reference uploads |
| `generated/{partition}/{file}` | AI-generated images (never in Firestore) |

**Important design rule:** AI-generated assets live only under `generated/` and are
**never** written to Firestore. This prevents the agent from conditioning on its own
prior outputs and causing quality drift.

### 4.3 Brand Ingestion

Run: `python -m app.ingest`

- Reads `BRAND_KITS_DIR` (each top-level folder = one brand).
- Uploads all files to GCS, records metadata in Firestore with
  `creative_metadata.author = "Marketing Team"`.
- Re-runs are idempotent (deletes prior ingested creatives, preserves AI-generated).
- Never reads `LS DESIGN PRODUCTIONS/` folder.

### 4.4 LangGraph Agent Workflow

```
fetch_assets → analyze_website → gather_requirements ─(missing)→ intake → END
                                                      └(ready)──→ build_prompt
                                                                   → generate
                                                                   → persist → END
```

**Nodes:**

1. **fetch_assets** — Resolve brand (by ID or name), load up to 200 creatives,
   select/sign **all** brand logos + style references (Step 1).
2. **analyze_website** — Resolve and crawl the brand's website, then compile a
   structured **Brand Style Profile** (color palette, typography, visual style,
   tone, target audience, personality). Cached per brand (Step 2).
3. **gather_requirements** — Extract the user's **creative type** and **purpose**
   from the conversation (Step 3).
4. **intake** — If creative type / purpose is missing, ask the user and return
   an `intake` result (the profile is already prepared).
5. **build_prompt** — LLM synthesizes the image prompt from the Brand Style
   Profile + requirements + kit context. Deterministic fallback if LLM fails (Step 4).
6. **generate** — Dual-asset strategy (Step 5):
   - **Variation A (with_logo):** Generate scene with reserved logo zone, then
     **composite the real brand logo** (pixel-perfect, not AI-drawn).
   - **Variation B (with_placeholder):** Same scene with blank placeholder for Canva.
7. **persist** — Upload to GCS (or inline data URLs if unconfigured), assemble API result.

### 4.5 Agent Response Types

| `type` | Meaning |
|--------|---------|
| `assets` | Generated creatives + master prompt + brand profile + Canva info |
| `intake` | Asks for creative type / purpose (brand profile already built) |
| `message` | Plain text (e.g. no brand selected) |

---

## 5. API Endpoints

All routes are mounted under `/api` unless noted.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/` | – | Service info + docs link |
| GET | `/api/health` | – | Liveness (`status`, `env`) |
| POST | `/api/auth/google` | – | Google ID token → app JWT |
| POST | `/api/agent` | Bearer | Run LangGraph agent (multipart: message, brand_id, files, logo) |
| GET | `/api/brands` | Bearer | List all brands |
| GET | `/api/brands/{id}` | Bearer | Brand detail + creatives |
| GET | `/api/brands/{id}/kit` | Bearer | Compact kit (colors, fonts, tone, logo URL) |
| GET | `/api/library` | Bearer | All brands + curated brand-kit gallery |
| POST | `/api/upload-reference` | Bearer | Upload reference file (25 MB max) |
| GET | `/api/references` | Bearer | User's reference uploads |
| GET | `/api/conversations` | Bearer | User's conversation list |
| GET | `/api/conversations/{id}` | Bearer | Full conversation (re-signs expired URLs) |
| DELETE | `/api/conversations/{id}` | Bearer | Delete conversation |
| GET | `/api/admin/users` | Admin | User directory |
| GET | `/api/admin/analytics` | Admin | Creative-request analytics (monthly, by brand/category) |
| GET | `/api/canva/authorize` | – | Redirect to Canva OAuth |
| GET | `/api/canva/callback` | – | OAuth callback → redirect to frontend |
| POST | `/api/canva/import` | – | Import image into connected Canva workspace |

**Note:** README lists some endpoints as unauthenticated; current code requires
`get_current_user` (Bearer JWT) for agent, brands, library, references, and
conversations. Canva authorize/callback/import and health/auth remain open.

---

## 6. Roles & Access Control

### 6.1 User Roles

| Role | How assigned | Capabilities |
|------|--------------|--------------|
| **Authenticated user** | Google sign-in → JWT | Agent, brands, library, references, conversations |
| **Super Admin** | Email in `ADMIN_EMAILS` env var | Above + `/api/admin/users`, `/api/admin/analytics` |

Admin status is embedded in the JWT payload (`admin: true/false`) and enforced via
`require_admin` dependency.

### 6.2 User Lifecycle

1. Frontend obtains Google ID token (Google Identity Services).
2. `POST /api/auth/google` verifies token against `GOOGLE_CLIENT_ID`.
3. User upserted in Firestore (`get_or_create_google_user`).
4. App issues HS256 JWT (`sub`, `email`, `admin`, `iat`, `exp`).
5. Frontend sends `Authorization: Bearer <token>` on protected routes.

### 6.3 Resource Ownership

- **Conversations:** `user_id` must match on read/delete.
- **References:** Scoped to `user_id` on upload and list.
- **Brand kits / library:** Shared across all authenticated users (read-only).

---

## 7. Security Model

### 7.1 Authentication

- **Google-only login** — No password registration. `verify_google_id_token()` uses
  `google.oauth2.id_token` with audience = `GOOGLE_CLIENT_ID`.
- Requires verified email (`email_verified: true`).
- **JWT** — HS256, secret from `JWT_SECRET`, default expiry 7 days
  (`JWT_EXPIRES_MINUTES=10080`).

### 7.2 Authorization

- `HTTPBearer` scheme; missing/invalid token → 401.
- Admin routes → 403 if `is_admin` is false.
- Conversation access → 404 if not owner (no information leak).

### 7.3 Input Limits & Validation

| Resource | Limit |
|----------|-------|
| Agent attachments | 15 MB per file; PDF, DOCX, images only |
| Agent logo upload | 15 MB; PNG/JPG/SVG |
| Reference uploads | 25 MB; PNG/JPEG/WebP/SVG/PDF/DOCX |
| Extracted attachment text | 8,000 chars max |

### 7.4 Data Handling

- **Agent attachments** — Processed in memory only; never stored in Firestore/GCS.
- **Uploaded logos** — Stored under `generated/` prefix only.
- **Signed URLs** — 1-hour expiry; `rehydrate_result()` re-signs on conversation load.
- **CORS** — Configurable via `CORS_ORIGINS`; credentials allowed.
- **Error handler** — Global 500 handler echoes CORS headers so frontend sees real errors.

### 7.5 Secrets & Config

Required for full operation (each service raises on first use if missing):

- `JWT_SECRET`, `GOOGLE_CLIENT_ID`
- `OPENROUTER_API_KEY`
- `GCP_PROJECT_ID`, `GCS_BUCKET_NAME` (optional for demo — falls back to data URLs)
- `CANVA_CLIENT_ID`, `CANVA_CLIENT_SECRET` (optional — Canva features disabled)

Local dev: `GOOGLE_APPLICATION_CREDENTIALS` points to service account JSON.
Cloud Run: attached service account (no JSON file).

### 7.6 Known Limitations (Single-Instance)

- **Canva OAuth token** — Stored in module-level `_active_token` (one token per
  server instance; not per-user or persisted).
- **Canva PKCE state** — In-memory `_pkce_store` with 10-minute TTL.

For multi-tenant production, these should move to a persisted per-user token store.

---

## 8. External Integrations

### 8.1 OpenRouter

**Purpose:** LLM reasoning + image generation + vision OCR.

| Setting | Default | Use |
|---------|---------|-----|
| `OPENROUTER_MODEL` | `anthropic/claude-sonnet-4.5` | Website analysis, brand style profile, master prompt |
| `OPENROUTER_IMAGE_MODEL` | `google/gemini-3-pro-image-preview` | Creative image generation |
| `OPENROUTER_IMAGE_MODEL_HERO` | `black-forest-labs/flux.2-max` | Optional alt/background model |
| `OPENROUTER_VISION_MODEL` | `openai/gpt-4o-mini` | OCR on uploaded images |

- LLM via LangChain `ChatOpenAI` (OpenAI-compatible).
- Images via `/chat/completions` with `modalities: ["image"]` or `["image","text"]`.
- Attribution headers: `HTTP-Referer`, `X-Title`.

### 8.2 Google Cloud Firestore

- Lazy client initialization.
- Custom database ID via `FIRESTORE_DATABASE` (default: `(default)`).
- Collections listed in §4.1.

### 8.3 Google Cloud Storage

- Lazy client; `is_configured()` checks `GCP_PROJECT_ID` + `GCS_BUCKET_NAME`.
- V4 signed URLs; Cloud Run uses IAM `signBlob` (needs `serviceAccountTokenCreator`).
- `to_gallery()` filters browser-renderable images for UI thumbnails.

### 8.4 Canva Connect

- OAuth 2.0 with PKCE (S256).
- Scopes: `asset:read asset:write design:content:read design:content:write`.
- Flow: `/canva/authorize` → Canva → `/canva/callback` → redirect to
  `APP_PUBLIC_URL?canva=connected`.
- `/canva/import` uploads image bytes to user's Canva workspace.

### 8.5 Google Identity (OAuth)

- Web Client ID shared with frontend (`NEXT_PUBLIC_GOOGLE_CLIENT_ID`).
- Backend verifies ID tokens; no direct Google OAuth redirect on backend.

### 8.6 Brand Website Analysis

- The agent resolves the brand's official website (from `brand_metadata` or web
  search) and crawls the homepage.
- Extracts colors, fonts, hero image, copy → LLM compiles a **Brand Style
  Profile** (colors, typography, visual style, tone, audience, personality).
- Profiles are cached per brand for the process lifetime so follow-up turns are fast.

---

## 9. Deployment & Operations

### 9.1 Local Development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --port 8080
```

API docs: `http://localhost:8080/docs`

### 9.2 Docker / Cloud Run

- **Image:** Python 3.12-slim + cairo dev libs (for SVG rasterization).
- **Port:** `8080` (Cloud Run injects `$PORT`).
- **Resources:** 2 GiB RAM, 2 CPU, 600s timeout (CI/CD config).
- **Deploy:** `gcloud run deploy agentos-backend --source . --allow-unauthenticated`
- **CI/CD:** Push to `main` → GitHub Actions → Cloud Run (env vars set once on service).

### 9.3 Graceful Degradation

| Missing config | Behavior |
|----------------|----------|
| GCS | Generated images returned as inline `data:` URLs |
| Firestore | Brand lookup skipped; agent may return "brand not found" |
| OpenRouter | Agent nodes fall back to keyword/deterministic logic where possible |
| Canva | `canva.configured: false` in agent response; import endpoints return 503/401 |

---

## 10. Frontend Contract Summary

- **CORS origins:** `CORS_ORIGINS` (comma-separated, e.g. Vercel URL + localhost:3000).
- **Auth header:** `Authorization: Bearer <jwt>` on protected routes.
- **Agent request:** `multipart/form-data` with `message`, optional `brand_id`,
  `conversation_id`, `files[]`, `logo`.
- **Agent response:** `{ conversation_id, type, ... }` where `type` is
  `assets` | `intake` | `message`.
- **Canva:** Frontend polls `?canva=connected` or `?canva=error` after OAuth redirect.

---

## 11. File Index (Quick Reference)

| File | Responsibility |
|------|----------------|
| `app/main.py` | App factory, CORS, routers, global error handler |
| `app/config.py` | All env-based settings |
| `app/security.py` | Google verify, JWT, `get_current_user`, `require_admin` |
| `app/models.py` | Pydantic API schemas |
| `app/services/firestore_repo.py` | All Firestore operations |
| `app/services/storage.py` | GCS upload/download/sign/gallery |
| `app/services/openrouter.py` | LLM, image gen, vision OCR |
| `app/services/canva.py` | Canva OAuth + import |
| `app/services/imaging.py` | Logo PNG conversion + compositing |
| `app/services/extract.py` | PDF/DOCX/image text extraction |
| `app/ingest.py` | Brand Kits ingestion CLI |
| `app/routers/*.py` | HTTP endpoints per domain |

---

*This document reflects the codebase as analyzed. For credential setup, see
`credentials.md` in the parent project. For endpoint examples, see `README.md`.*
