# ── Stage 1: Python dependency builder ───────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python deps (cached layer if requirements unchanged)
COPY backend/requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir \
    # CPU-only PyTorch first (much smaller than default)
    torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: Runtime image ────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="Parsy <parsy@local>" \
      version="3.0.0" \
      description="Parsy — Superior Document Intelligence"

# System dependencies: Tesseract OCR, Poppler (pdf2image), fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Tesseract OCR + language packs
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-fra \
    tesseract-ocr-deu \
    tesseract-ocr-spa \
    tesseract-ocr-chi-sim \
    # PDF rasterisation
    poppler-utils \
    # Font rendering
    fonts-liberation \
    fonts-dejavu-core \
    # Networking (healthcheck)
    curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy backend source
COPY backend/ .

# Non-root user for security
RUN useradd -m -u 1001 parsy && \
    mkdir -p /app/model_cache /app/uploads && \
    chown -R parsy:parsy /app
USER parsy

# Environment defaults
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAX_CONCURRENT_JOBS=8 \
    FAST_WORKERS=4 \
    OCR_WORKERS=2 \
    ML_ENABLED=true \
    MODEL_CACHE_DIR=/app/model_cache

EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default: run the API server (workers use different CMD in compose)
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--loop", "uvloop", \
     "--http", "h11", \
     "--timeout-keep-alive", "30"]
