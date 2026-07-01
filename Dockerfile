# ── Stage 1: Build dependencies ────────────────────────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /build

# System deps for OCR + PDF processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev libssl-dev \
    libpoppler-cpp-dev poppler-utils \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-spa \
    libtesseract-dev libleptonica-dev \
    libmupdf-dev \
    fonts-dejavu fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── Stage 2: Runtime image ─────────────────────────────────────────────────
FROM python:3.12-slim AS runtime
WORKDIR /app

# Copy system libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-spa \
    libmupdf-dev \
    fonts-dejavu fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Copy source
COPY backend/ ./

# Resource limits (CPU + memory set by Docker/K8s, but enforce at app level)
ENV MAX_CONCURRENT_JOBS=8 \
    FAST_WORKERS=4 \
    OCR_WORKERS=4 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user for security
RUN useradd -r -u 1001 parsy && chown -R parsy /app
USER parsy

EXPOSE 8000

# Health check for K8s readiness probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--loop", "uvloop", \
     "--http", "h11", \
     "--timeout-keep-alive", "120"]
