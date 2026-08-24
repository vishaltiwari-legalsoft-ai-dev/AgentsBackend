"""AgentOS backend — FastAPI application entry point.

Deployed on Google Cloud Run; serves the agent APIs to the Next.js frontend
(hosted on Vercel) over CORS.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers import (
    admin,
    auth,
    blog_writer,
    brands,
    canva,
    creative_agent,
    geo,
    graphics_designer,
    health,
    library,
    marketing_research,
    reference_library,
    seo_geo,
)
from app.services.gd_brand_source import firestore_spec_source
from graphics_designer_agent import registry as gd_registry
# Imported after the routers so the agent roots app/__init__ registers are
# already on sys.path in exactly the order they were before.
from marketing_research_agent import runs as mr_runs
from marketing_research_agent import snapshots as mr_snapshots

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AgentOS API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _cors_headers(request: Request) -> dict[str, str]:
    """Echo the allowed Origin so the browser can read the real error body.

    Error responses are produced in Starlette's outer error middleware, outside
    CORSMiddleware — without this the frontend sees "Failed to fetch" instead of
    the message."""
    headers: dict[str, str] = {}
    origin = request.headers.get("origin")
    if origin and origin in settings.cors_origin_list:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
        headers["Vary"] = "Origin"
    return headers


@app.exception_handler(mr_runs.RunStoreError)
@app.exception_handler(mr_snapshots.SnapshotStoreError)
async def datastore_unreadable_handler(request: Request, exc: Exception) -> JSONResponse:
    """A read against the durable store FAILED — answer 502, never an empty page.

    These two stores used to swallow their own failures and return ``[]``, so a
    Firestore outage (or a query whose composite index does not exist) rendered
    as "this workspace has no data". 502 is the honest answer: the gateway could
    not reach its data, the client knows not to trust an empty screen, and Cloud
    Scheduler retries instead of recording a clean run.
    """
    logging.getLogger("agentos").error("datastore unreadable: %s", exc)
    return JSONResponse(
        status_code=502,
        content={"detail": f"Could not read the data store: {exc}. "
                           "Nothing is shown rather than showing you an empty page."},
        headers=_cors_headers(request),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a 500 that still carries CORS headers.

    The catch-all handler runs in Starlette's outer error middleware (outside
    CORSMiddleware), so without this the browser sees a CORS failure ("Failed to
    fetch") instead of the actual error. We echo the allowed Origin so the
    frontend can read the real message, and include the detail to aid debugging.
    """
    logging.getLogger("agentos").exception("unhandled error: %s", exc)
    headers = _cors_headers(request)
    # Exception text can carry file paths / doc ids / model names — keep it in
    # the server log only outside local dev.
    detail = (
        f"Internal server error: {exc}"
        if settings.app_env == "development"
        else "Internal server error"
    )
    return JSONResponse(status_code=500, content={"detail": detail}, headers=headers)


for router in (
    health,
    auth,
    blog_writer,
    brands,
    library,
    reference_library,
    admin,
    canva,
    graphics_designer,
    creative_agent,
    marketing_research,
    seo_geo,
    geo,
):
    app.include_router(router.router, prefix="/api")

# Firestore-backed brands become available to the Graphics Designer registry
# once GD_DYNAMIC_BRANDS=1 (see graphics_designer_agent.registry._registry).
# Unconditional and harmless when the flag is unset: firestore_spec_source is
# simply never invoked.
gd_registry.register_dynamic_source(firestore_spec_source)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "AgentOS API", "docs": "/docs", "health": "/api/health"}
