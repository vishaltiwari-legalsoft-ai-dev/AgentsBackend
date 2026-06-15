# Cloud Run image. Python 3.12 pinned for stable Google gRPC/LangChain wheels.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Libs for SVG logo rasterization. pycairo (via svglib/rlPyCairo) has no Linux
# wheel and compiles from source, so we need the C toolchain + cairo dev headers
# at build time and libcairo2 at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libcairo2 \
    libcairo2-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY agents ./agents

# Cloud Run injects $PORT; default to 8080 locally.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
