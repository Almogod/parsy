"""
Parsy Backend — FastAPI Entry Point
Endpoints:
  POST /parse          → streaming SSE job  (multi-level orchestration)
  POST /parse/batch    → queue multiple files
  GET  /health         → liveness probe
  GET  /metrics        → Prometheus metrics
  GET  /workers        → worker pool status
"""
import asyncio, json, os, time, uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, File, Form, UploadFile, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import structlog

from router import DocumentRouter, Route
from fast_parser import FastStructuralParser
from ocr_pipeline import VisionOCRPipeline
from structured_parser import StructuredParser
from normalizer import normalize

# ── Logging ────────────────────────────────────────────────────────────────
structlog.configure(processors=[
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.stdlib.add_log_level,
    structlog.dev.ConsoleRenderer()
])
log = structlog.get_logger()

# ── Prometheus metrics ─────────────────────────────────────────────────────
PARSE_COUNT    = Counter("parsy_parses_total", "Total parse requests", ["route", "format"])
PARSE_DURATION = Histogram("parsy_parse_duration_seconds", "Parse duration", ["route"])
PARSE_ERRORS   = Counter("parsy_errors_total", "Parse errors", ["route"])

# ── Worker pool ────────────────────────────────────────────────────────────
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_JOBS", 8))
_semaphore: asyncio.Semaphore

# ── In-memory job tracker (production: use Redis) ─────────────────────────
_jobs: dict[str, dict] = {}

# ── Lifespan ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _semaphore
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    log.info("Parsy backend started", workers=MAX_CONCURRENT)
    yield
    log.info("Parsy backend shutting down")

# ── App ────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Parsy API",
    description="Superior document intelligence — multi-level orchestration engine",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Singletons ─────────────────────────────────────────────────────────────
_router     = DocumentRouter()
_fast       = FastStructuralParser(max_workers=int(os.getenv("FAST_WORKERS", 4)))
_ocr        = VisionOCRPipeline(max_workers=int(os.getenv("OCR_WORKERS", 4)))
_structured = StructuredParser()


# ──────────────────────────────────────────────────────────────────────────
#  SSE event generator
# ──────────────────────────────────────────────────────────────────────────
async def parse_stream(
    job_id: str,
    filename: str,
    data: bytes,
    output_format: str,
    options: dict,
) -> AsyncGenerator[dict, None]:

    def event(kind: str, payload: dict) -> dict:
        return {"event": kind, "data": json.dumps({"jobId": job_id, **payload})}

    yield event("status", {"step": "routing", "message": "Inspecting document…", "pct": 5})

    # ── Level 1: Route ────────────────────────────────────────────────
    decision = _router.route(filename, data)
    yield event("route", {
        "route":       decision.route.value,
        "confidence":  decision.confidence,
        "reasons":     decision.reasons,
        "pageCount":   decision.page_count,
        "complexity":  decision.estimated_complexity,
        "workers":     decision.recommended_workers,
        "pct": 15,
    })

    # ── Level 2: Parse ────────────────────────────────────────────────
    yield event("status", {"step": "parsing", "message": f"Running {decision.route.value} pipeline…", "pct": 20})
    t0 = time.perf_counter()

    try:
        async with asyncio.timeout(300):  # 5-min hard limit per file
            if decision.route == Route.FAST_TEXT or decision.route == Route.MARKDOWN:
                result = await _fast.parse(filename, data)
            elif decision.route == Route.VISION_OCR:
                yield event("status", {"step": "ocr", "message": "Rasterising pages…", "pct": 30})
                result = await _ocr.parse(filename, data)
            else:  # STRUCTURED
                result = await _structured.parse(filename, data)
    except asyncio.TimeoutError:
        yield event("error", {"message": "Parse timeout (5 min limit)", "pct": 0})
        return
    except Exception as ex:
        PARSE_ERRORS.labels(route=decision.route.value).inc()
        yield event("error", {"message": str(ex), "pct": 0})
        return

    parse_dur = time.perf_counter() - t0
    PARSE_DURATION.labels(route=decision.route.value).observe(parse_dur)
    yield event("status", {"step": "normalizing", "message": "Normalizing output…", "pct": 80})

    # ── Level 3: Normalize ────────────────────────────────────────────
    normalized = normalize(result, output_format=output_format)

    # Select requested format
    if output_format == "markdown":
        output_text = normalized.markdown
    elif output_format == "plaintext":
        output_text = normalized.plaintext
    elif output_format == "json":
        output_text = json.dumps(normalized.json_data, indent=2)
    elif output_format == "html":
        output_text = normalized.html
    elif output_format == "csv":
        output_text = "\n\n".join(normalized.csv_tables) or "No tables found."
    else:
        output_text = normalized.markdown

    PARSE_COUNT.labels(route=decision.route.value, format=output_format).inc()

    # Stream output in chunks (for large files)
    chunk_size = 8192
    total_len  = len(output_text)
    chunks_sent = 0
    for start in range(0, total_len, chunk_size):
        chunk = output_text[start:start + chunk_size]
        pct   = 80 + int(20 * (start / max(total_len, 1)))
        yield event("chunk", {
            "chunk":   chunk,
            "offset":  start,
            "total":   total_len,
            "pct":     min(pct, 99),
        })
        chunks_sent += 1
        await asyncio.sleep(0)  # yield to event loop

    yield event("done", {
        "metrics":    normalized.metrics,
        "tables":     len(normalized.tables),
        "parseDuration": f"{parse_dur:.2f}s",
        "chunks":     chunks_sent,
        "pct": 100,
    })
    log.info("parse complete", job=job_id, file=filename,
             route=decision.route.value, dur=parse_dur)


# ──────────────────────────────────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────────────────────────────────

@app.post("/parse")
async def parse_document(
    file:   UploadFile = File(...),
    format: str        = Form("markdown"),
    tables: bool       = Form(True),
    meta:   bool       = Form(True),
    clean:  bool       = Form(True),
):
    """Parse a document with streaming SSE progress."""
    if file.size and file.size > 200 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 200 MB)")

    job_id  = str(uuid.uuid4())[:8]
    data    = await file.read()
    options = {"tables": tables, "meta": meta, "clean": clean}

    _jobs[job_id] = {"status": "running", "file": file.filename}

    async def guarded_stream():
        async with _semaphore:        # worker pool throttle
            async for event in parse_stream(job_id, file.filename, data, format, options):
                yield f"event: {event['event']}\ndata: {event['data']}\n\n"
        _jobs[job_id]["status"] = "done"

    return StreamingResponse(guarded_stream(), media_type="text/event-stream",
                             headers={"X-Job-ID": job_id,
                                      "Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/parse/batch")
async def parse_batch(files: list[UploadFile] = File(...),
                      format: str = Form("markdown")):
    """Submit multiple files — returns job IDs."""
    if len(files) > 20:
        raise HTTPException(400, "Max 20 files per batch")
    job_ids = []
    for f in files:
        jid = str(uuid.uuid4())[:8]
        _jobs[jid] = {"status": "queued", "file": f.filename}
        job_ids.append(jid)
    return {"jobs": job_ids, "message": "Use /parse per-file for SSE streaming"}


@app.get("/health")
async def health():
    return {"status": "ok", "workers": MAX_CONCURRENT,
            "active": MAX_CONCURRENT - _semaphore._value}


@app.get("/workers")
async def workers():
    return {
        "maxConcurrent": MAX_CONCURRENT,
        "available":     _semaphore._value,
        "active":        MAX_CONCURRENT - _semaphore._value,
        "jobs":          list(_jobs.items())[-20:],
    }


@app.get("/metrics")
async def metrics():
    return StreamingResponse(
        iter([generate_latest()]),
        media_type=CONTENT_TYPE_LATEST
    )


@app.get("/")
async def root():
    return {"name": "Parsy API", "version": "2.0.0",
            "docs": "/docs", "health": "/health"}
