"""
Parsy Backend — FastAPI Entry Point v3
Phase 1: Resource-guarded streaming with Celery worker dispatch
Phase 2: ML pipeline integration (LayoutLM, MiniLM, TATR)
Phase 3: Multi-level orchestration with confidence signals

Endpoints:
  POST /parse          → streaming SSE  (direct, small files)
  POST /parse/async    → Celery dispatch (large/OCR files)
  GET  /jobs/{id}      → poll async job status
  POST /parse/batch    → multi-file queue
  GET  /health         → liveness probe
  GET  /workers        → worker pool status
  GET  /metrics        → Prometheus metrics
"""
import asyncio, json, os, time, uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import structlog

from router import DocumentRouter, Route
from fast_parser import FastStructuralParser
from ocr_pipeline import VisionOCRPipeline
from structured_parser import StructuredParser
from normalizer import normalize
from utils import ResourceGuard, ResourceLimits, configure_logging, RedisJobStore
from input_validator import validate_upload, ValidationError as InputValidationError

# Bootstrap stdlib logging before structlog
configure_logging()

# ── Logging ────────────────────────────────────────────────────────────────
structlog.configure(processors=[
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.stdlib.add_log_level,
    structlog.dev.ConsoleRenderer(),
])
log = structlog.get_logger()

# ── Prometheus ─────────────────────────────────────────────────────────────
PARSE_COUNT    = Counter("parsy_parses_total", "Total parses", ["route", "format"])
PARSE_DURATION = Histogram("parsy_parse_duration_seconds", "Parse duration", ["route"])
PARSE_ERRORS   = Counter("parsy_errors_total", "Parse errors", ["route"])
ML_ANNOTATIONS = Counter("parsy_ml_annotations_total", "ML annotations run", ["model"])

# ── Worker pool ────────────────────────────────────────────────────────────
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_JOBS", 8))
_semaphore: asyncio.Semaphore

# ── Redis/In-memory job tracker (production) ──────────────────────────────
_jobs = RedisJobStore(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

# ── Resource guard ─────────────────────────────────────────────────────────
_guard = ResourceGuard(ResourceLimits(
    max_memory_mb    = int(os.getenv("MAX_MEM_MB", 1024)),
    max_cpu_seconds  = int(os.getenv("MAX_CPU_SEC", 300)),
    max_file_size_mb = int(os.getenv("MAX_FILE_MB", 200)),
))

# ── Optional Celery integration ────────────────────────────────────────────
_celery_available = False
try:
    from tasks import celery_app, route_and_parse as _celery_route
    _celery_available = True
    log.info("Celery integration active")
except ImportError:
    log.warning("Celery not available — async dispatch disabled")

# ── Shared memory directory for zero-copy IPC task passing ────────────────
SHARED_UPLOAD_DIR = os.getenv("SHARED_UPLOAD_DIR", "/dev/shm/parsy_uploads")
_shared_dir_active = False
try:
    os.makedirs(SHARED_UPLOAD_DIR, exist_ok=True)
    _shared_dir_active = True
    log.info("Shared memory IPC directory active", path=SHARED_UPLOAD_DIR)
except Exception as e:
    SHARED_UPLOAD_DIR = os.path.join(os.getcwd(), "tmp_uploads")
    try:
        os.makedirs(SHARED_UPLOAD_DIR, exist_ok=True)
        _shared_dir_active = True
        log.info("Falling back to local temp directory for IPC", path=SHARED_UPLOAD_DIR)
    except Exception as inner_err:
        log.warning("Could not set up shared IPC directory; using standard serialization", error=str(inner_err))
        _shared_dir_active = False

# ── Optional ML pipeline ───────────────────────────────────────────────────
_ml_available = os.getenv("ML_ENABLED", "true").lower() == "true"
if _ml_available:
    try:
        import ml_pipeline
        log.info("ML pipeline available")
    except ImportError:
        _ml_available = False
        log.warning("ML pipeline unavailable — install transformers + sentence-transformers")

# ── Lifespan ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _semaphore
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    log.info("Parsy backend started", workers=MAX_CONCURRENT,
             celery=_celery_available, ml=_ml_available)
    yield
    log.info("Parsy backend shutting down")

# ── App ────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Parsy API",
    description="Superior document intelligence — multi-level orchestration + ML",
    version="3.0.0",
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
_ocr        = VisionOCRPipeline(max_workers=int(os.getenv("OCR_WORKERS", 2)))
_structured = StructuredParser()


# ── SSE helper ────────────────────────────────────────────────────────────
def _evt(kind: str, job_id: str, payload: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps({'jobId': job_id, **payload})}\n\n"


# ── Core parse stream ──────────────────────────────────────────────────────
async def parse_stream(job_id: str, filename: str, data: bytes,
                       output_format: str, options: dict):
    # Validate file size
    try:
        _guard.check_file_size(data, filename)
    except ValueError as e:
        yield _evt("error", job_id, {"message": str(e), "pct": 0})
        return

    yield _evt("status", job_id, {"step": "routing", "message": "Inspecting document…", "pct": 5})

    # ── Level 1: Route ─────────────────────────────────────────────────
    decision = _router.route(filename, data)
    yield _evt("route", job_id, {
        "route":      decision.route.value,
        "confidence": decision.confidence,
        "reasons":    decision.reasons,
        "pageCount":  decision.page_count,
        "complexity": decision.estimated_complexity,
        "workers":    decision.recommended_workers,
        "pct": 15,
    })

    # ── Level 2: Parse ─────────────────────────────────────────────────
    yield _evt("status", job_id, {
        "step": "parsing",
        "message": f"Running {decision.route.value} pipeline…",
        "pct": 20,
    })
    t0 = time.perf_counter()

    try:
        async with asyncio.timeout(300):
            if decision.route in (Route.FAST_TEXT, Route.MARKDOWN):
                result = await _fast.parse(filename, data)
            elif decision.route == Route.VISION_OCR:
                yield _evt("status", job_id, {"step": "ocr", "message": "Rasterising pages…", "pct": 30})
                result = await _ocr.parse(filename, data)
            else:
                result = await _structured.parse(filename, data)
    except asyncio.TimeoutError:
        yield _evt("error", job_id, {"message": "Parse timeout (5 min limit)", "pct": 0})
        PARSE_ERRORS.labels(route=decision.route.value).inc()
        return
    except Exception as ex:
        yield _evt("error", job_id, {"message": str(ex), "pct": 0})
        PARSE_ERRORS.labels(route=decision.route.value).inc()
        return

    parse_dur = time.perf_counter() - t0
    PARSE_DURATION.labels(route=decision.route.value).observe(parse_dur)

    # ── Phase 2: ML annotation (optional) ──────────────────────────────
    ml_annotation = None
    if _ml_available and decision.route != Route.STRUCTURED:
        yield _evt("status", job_id, {"step": "ml_analysis", "message": "ML structural analysis…", "pct": 72})
        try:
            headings = [b.content for b in result.blocks if b.block_type == "heading"]
            page_texts = []
            # Aggregate text per page
            page_map: dict[int, list[str]] = {}
            for b in result.blocks:
                page_map.setdefault(b.page, []).append(b.content)
            page_texts = ["\n".join(page_map.get(i, [])) for i in sorted(page_map)]

            loop = asyncio.get_running_loop()
            ml_annotation = await loop.run_in_executor(
                None, ml_pipeline.annotate, page_texts, headings, None, None
            )
            if ml_annotation:
                ML_ANNOTATIONS.labels(model="layout").inc()
                yield _evt("ml_result", job_id, {
                    "regions":      len(ml_annotation.regions),
                    "clusters":     len(ml_annotation.heading_clusters),
                    "tableStructs": len(ml_annotation.table_structures),
                    "models":       ml_annotation.model_versions,
                    "inferenceMs":  ml_annotation.inference_ms,
                    "pct": 78,
                })
        except Exception as e:
            log.warning(f"ML annotation skipped: {e}")

    # ── Level 3: Normalize ─────────────────────────────────────────────
    yield _evt("status", job_id, {"step": "normalizing", "message": "Normalizing output…", "pct": 80})
    normalized = normalize(result, output_format=output_format)

    # Format selection
    if output_format == "markdown":    output_text = normalized.markdown
    elif output_format == "plaintext": output_text = normalized.plaintext
    elif output_format == "json":      output_text = json.dumps(normalized.json_data, indent=2)
    elif output_format == "html":      output_text = normalized.html
    elif output_format == "csv":       output_text = "\n\n".join(normalized.csv_tables) or "No tables found."
    else:                              output_text = normalized.markdown

    PARSE_COUNT.labels(route=decision.route.value, format=output_format).inc()

    # ── Chunked streaming output ────────────────────────────────────────
    chunk_size  = 8192
    total_len   = len(output_text)
    chunks_sent = 0
    for start in range(0, total_len, chunk_size):
        chunk = output_text[start:start + chunk_size]
        pct   = 80 + int(19 * (start / max(total_len, 1)))
        yield _evt("chunk", job_id, {
            "chunk":  chunk,
            "offset": start,
            "total":  total_len,
            "pct":    min(pct, 99),
        })
        chunks_sent += 1
        await asyncio.sleep(0)

    metrics = {**normalized.metrics}
    if ml_annotation:
        metrics["mlInferenceMs"]  = ml_annotation.inference_ms
        metrics["mlModels"]       = ml_annotation.model_versions
        metrics["headingClusters"] = len(ml_annotation.heading_clusters)

    yield _evt("done", job_id, {
        "metrics":      metrics,
        "tables":       len(normalized.tables),
        "parseDuration": f"{parse_dur:.2f}s",
        "chunks":       chunks_sent,
        "pct": 100,
    })
    log.info("parse complete", job=job_id, file=filename,
             route=decision.route.value, dur=parse_dur)


# ── Routes ─────────────────────────────────────────────────────────────────

@app.post("/parse")
async def parse_document(
    file:   UploadFile = File(...),
    format: str        = Form("markdown"),
    tables: bool       = Form(True),
    meta:   bool       = Form(True),
    clean:  bool       = Form(True),
    use_ml: bool       = Form(True),
):
    """Parse a document with streaming SSE progress (direct, synchronous)."""
    job_id  = str(uuid.uuid4())[:8]
    data    = await file.read()

    # ── Input validation (security gate) ────────────────────────────────────
    try:
        validate_upload(file.filename or "upload", data)
    except InputValidationError as e:
        log.warning("Upload rejected by input validator",
                    job=job_id, file=file.filename, reason=str(e), code=e.code)
        raise HTTPException(status_code=422, detail={"error": str(e), "code": e.code})

    options = {"tables": tables, "meta": meta, "clean": clean, "use_ml": use_ml}
    _jobs[job_id] = {"status": "running", "file": file.filename, "startedAt": time.time()}

    async def guarded_stream():
        async with _semaphore:
            async for chunk in parse_stream(job_id, file.filename, data, format, options):
                yield chunk
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["completedAt"] = time.time()

    return StreamingResponse(
        guarded_stream(),
        media_type="text/event-stream",
        headers={
            "X-Job-ID":          job_id,
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/parse/async")
async def parse_async(
    file:         UploadFile = File(...),
    format:       str        = Form("markdown"),
    tables:       bool       = Form(True),
    meta:         bool       = Form(True),
    clean:        bool       = Form(True),
    background_tasks: BackgroundTasks = None,
):
    """
    Dispatch to Celery worker pool for large/OCR files.
    Returns a job ID immediately; poll /jobs/{id} for status.
    """
    if not _celery_available:
        raise HTTPException(503, "Celery workers not running. Use /parse for synchronous processing.")

    data   = await file.read()
    job_id = str(uuid.uuid4())[:8]

    # ── Input validation (security gate) ────────────────────────────────────
    try:
        validate_upload(file.filename or "upload", data)
    except InputValidationError as e:
        log.warning("Async upload rejected",
                    job=job_id, file=file.filename, reason=str(e), code=e.code)
        raise HTTPException(status_code=422, detail={"error": str(e), "code": e.code})

    file_path = None
    data_hex = None
    if _shared_dir_active:
        try:
            temp_filename = f"{job_id}_{file.filename}"
            file_path = os.path.join(SHARED_UPLOAD_DIR, temp_filename)
            with open(file_path, "wb") as f:
                f.write(data)
        except Exception as write_err:
            log.warning("Shared directory write failed; falling back to hex serialization",
                        job=job_id, error=str(write_err))
            file_path = None

    if not file_path:
        data_hex = data.hex()

    task = _celery_route.delay(
        filename      = file.filename,
        data_hex      = data_hex,
        output_format = format,
        options       = {"tables": tables, "meta": meta, "clean": clean},
        file_path     = file_path,
    )
    _jobs[job_id] = {
        "status":   "queued",
        "file":     file.filename,
        "celeryId": task.id,
        "queuedAt": time.time(),
    }
    return {"jobId": job_id, "celeryId": task.id, "status": "queued",
            "pollUrl": f"/jobs/{job_id}"}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Poll async job status. Returns result when ready."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")

    if not _celery_available or "celeryId" not in job:
        return job

    from celery.result import AsyncResult
    res = AsyncResult(job["celeryId"], app=celery_app)
    return {
        "jobId":    job_id,
        "celeryId": job["celeryId"],
        "status":   res.status,
        "progress": res.info if res.status == "STARTED" else None,
        "result":   res.result if res.ready() else None,
        "file":     job.get("file"),
    }


@app.post("/parse/batch")
async def parse_batch(files: list[UploadFile] = File(...), format: str = Form("markdown")):
    """Submit multiple files — returns job IDs for async polling."""
    if len(files) > 20:
        raise HTTPException(400, "Max 20 files per batch")
    job_ids = []
    for f in files:
        jid = str(uuid.uuid4())[:8]
        _jobs[jid] = {"status": "queued", "file": f.filename}
        job_ids.append({"jobId": jid, "file": f.filename, "pollUrl": f"/jobs/{jid}"})
    return {"jobs": job_ids, "message": "Use /parse/async for per-file Celery dispatch"}


@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "version": "3.0.0",
        "workers": MAX_CONCURRENT,
        "active":  MAX_CONCURRENT - _semaphore._value,
        "celery":  _celery_available,
        "ml":      _ml_available,
    }


@app.get("/workers")
async def workers():
    return {
        "maxConcurrent": MAX_CONCURRENT,
        "available":     _semaphore._value,
        "active":        MAX_CONCURRENT - _semaphore._value,
        "celery":        _celery_available,
        "mlEnabled":     _ml_available,
        "jobs":          list(_jobs.items())[-20:],
    }


@app.get("/metrics")
async def metrics():
    return StreamingResponse(iter([generate_latest()]), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
async def root():
    return {
        "name":    "Parsy API",
        "version": "3.0.0",
        "docs":    "/docs",
        "health":  "/health",
        "phases":  ["resource_guard", "celery_workers", "ml_pipeline", "sse_streaming"],
    }
