"""AgentOS backend — FastAPI application entry point.

Deployed on Google Cloud Run; serves the LangGraph agent + supporting APIs to
the Next.js frontend (hosted on Vercel) over CORS.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers import (
    admin,
    agent,
    auth,
    brands,
    canva,
    conversations,
    creative_agent,
    graphics_designer,
    health,
    library,
    reference_library,
    references,
)

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AgentOS API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    headers: dict[str, str] = {}
    origin = request.headers.get("origin")
    if origin and origin in settings.cors_origin_list:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
        headers["Vary"] = "Origin"
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {exc}"},
        headers=headers,
    )


for router in (
    health,
    auth,
    agent,
    brands,
    library,
    reference_library,
    references,
    conversations,
    admin,
    canva,
    graphics_designer,
    creative_agent,
):
    app.include_router(router.router, prefix="/api")


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "AgentOS API", "docs": "/docs", "health": "/api/health"}
