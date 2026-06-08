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
    health,
    library,
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
async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Log full context server-side; return a sanitized message to the client."""
    logging.getLogger("agentos").exception("unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


for router in (
    health,
    auth,
    agent,
    brands,
    library,
    references,
    conversations,
    admin,
    canva,
):
    app.include_router(router.router, prefix="/api")


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "AgentOS API", "docs": "/docs", "health": "/api/health"}
