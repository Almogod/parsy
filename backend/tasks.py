"""
Parsy — Celery Task Definitions
Offloads heavy parse jobs to isolated worker processes.
Each task runs inside a ResourceGuard to prevent OOM crashes.

Usage:
    celery -A tasks worker --loglevel=info --concurrency=4 \
           --max-memory-per-child=512000 -Q fast,ocr,structured
"""
import os, json, time, uuid
from celery import Celery
from celery.utils.log import get_task_logger

from resource_guard import ResourceGuard, ResourceLimits
from router import DocumentRouter, Route
from fast_parser import FastStructuralParser
from ocr_pipeline import VisionOCRPipeline
from structured_parser import StructuredParser
from normalizer import normalize

log = get_task_logger(__name__)

# ── Celery app ─────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "parsy",
    broker=REDIS_URL,
    backend=REDIS_URL,
)
celery_app.conf.update(
    task_serializer         = "json",
    result_serializer       = "json",
    accept_content          = ["json"],
    task_track_started      = True,
    task_acks_late          = True,           # re-queue on worker crash
    worker_prefetch_multiplier = 1,           # one task at a time per worker
    task_routes             = {
        "tasks.parse_fast":       {"queue": "fast"},
        "tasks.parse_ocr":        {"queue": "ocr"},
        "tasks.parse_structured": {"queue": "structured"},
    },
    result_expires          = 3600,           # keep results 1 hour
)

# ── Singletons (initialised per worker process) ────────────────────────────
_router     = DocumentRouter()
_fast       = FastStructuralParser(max_workers=int(os.getenv("FAST_WORKERS", 4)))
_ocr        = VisionOCRPipeline(max_workers=int(os.getenv("OCR_WORKERS", 2)))
_structured = StructuredParser()
_guard      = ResourceGuard(ResourceLimits(
    max_memory_mb   = int(os.getenv("MAX_MEM_MB", 512)),
    max_cpu_seconds = int(os.getenv("MAX_CPU_SEC", 180)),
))


# ── Helpers ────────────────────────────────────────────────────────────────
def _serialize_result(norm) -> dict:
    """Convert Normalizedef _run_parse(parser_fn, filename: str, data_hex: str = None,
               output_format: str = "markdown", options: dict = None, file_path: str = None) -> dict:
    """Core parse runner used by all task variants."""
    t0 = time.perf_counter()
    data = None
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "rb") as f:
                data = f.read()
        except Exception as read_err:
            log.error(f"Failed to read file from shared path: {file_path}. Error: {read_err}")
    
    if data is None:
        if data_hex:
            data = bytes.fromhex(data_hex)
        else:
            raise ValueError("No data provided (both file_path and data_hex are empty/invalid)")

    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(parser_fn(filename, data))
    finally:
        loop.close()

    # Clean up the shared file immediately to reclaim RAM disk memory
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as del_err:
            log.warning(f"Could not delete shared file {file_path}: {del_err}")

    normalized = normalize(result, output_format=output_format)
    dur = time.perf_counter() - t0
    out = _serialize_result(normalized)
    out["parseDuration"] = f"{dur:.2f}s"
    out["filename"]      = filename
    return out


# ── Task: Fast path (digital PDFs, DOCX, TXT, HTML) ───────────────────────
@celery_app.task(
    bind=True,
    name="tasks.parse_fast",
    max_retries=2,
    soft_time_limit=120,
    time_limit=180,
    queue="fast",
)
def parse_fast(self, filename: str, data_hex: str = None,
               output_format: str = "markdown", options: dict = None, file_path: str = None):
    log.info(f"[FAST] Starting: {filename}")
    self.update_state(state="STARTED", meta={"step": "fast_parse", "pct": 20})
    try:
        with _guard.guard(self.request.id or "fast"):
            result = _run_parse(_fast.parse, filename, data_hex, output_format, options or {}, file_path)
        self.update_state(state="SUCCESS", meta={"pct": 100})
        return result
    except Exception as exc:
        log.error(f"[FAST] Failed: {filename} — {exc}")
        if file_path and os.path.exists(file_path):
            try: os.remove(file_path)
            except Exception: pass
        raise self.retry(exc=exc, countdown=5)


# ── Task: OCR / Vision path (scanned, rotated, image-heavy PDFs) ───────────
@celery_app.task(
    bind=True,
    name="tasks.parse_ocr",
    max_retries=1,
    soft_time_limit=240,
    time_limit=300,
    queue="ocr",
)
def parse_ocr(self, filename: str, data_hex: str = None,
              output_format: str = "markdown", options: dict = None, file_path: str = None):
    log.info(f"[OCR] Starting: {filename}")
    self.update_state(state="STARTED", meta={"step": "ocr_rasterise", "pct": 15})
    try:
        with _guard.guard(self.request.id or "ocr"):
            result = _run_parse(_ocr.parse, filename, data_hex, output_format, options or {}, file_path)
        self.update_state(state="SUCCESS", meta={"pct": 100})
        return result
    except Exception as exc:
        log.error(f"[OCR] Failed: {filename} — {exc}")
        if file_path and os.path.exists(file_path):
            try: os.remove(file_path)
            except Exception: pass
        raise self.retry(exc=exc, countdown=10)


# ── Task: Structured data (CSV, JSON, XML, XLSX) ───────────────────────────
@celery_app.task(
    bind=True,
    name="tasks.parse_structured",
    max_retries=2,
    soft_time_limit=60,
    time_limit=90,
    queue="structured",
)
def parse_structured(self, filename: str, data_hex: str = None,
                     output_format: str = "markdown", options: dict = None, file_path: str = None):
    log.info(f"[STRUCTURED] Starting: {filename}")
    self.update_state(state="STARTED", meta={"step": "structured_parse", "pct": 20})
    try:
        with _guard.guard(self.request.id or "struct"):
            result = _run_parse(_structured.parse, filename, data_hex, output_format, options or {}, file_path)
        self.update_state(state="SUCCESS", meta={"pct": 100})
        return result
    except Exception as exc:
        log.error(f"[STRUCTURED] Failed: {filename} — {exc}")
        if file_path and os.path.exists(file_path):
            try: os.remove(file_path)
            except Exception: pass
        raise self.retry(exc=exc, countdown=3)


# ── Task: Route + dispatch (main orchestrator task) ────────────────────────
@celery_app.task(
    bind=True,
    name="tasks.route_and_parse",
    max_retries=1,
    time_limit=320,
    queue="fast",
)
def route_and_parse(self, filename: str, data_hex: str = None,
                    output_format: str = "markdown", options: dict = None, file_path: str = None):
    """
    Full orchestration: router → select queue → parse → normalize.
    Returns the routing decision alongside the result.
    """
    data = None
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "rb") as f:
                data = f.read()
        except Exception as read_err:
            log.error(f"[ROUTER] Failed to read from shared file {file_path}: {read_err}")

    if data is None:
        if data_hex:
            data = bytes.fromhex(data_hex)
        else:
            raise ValueError("No data provided (both file_path and data_hex are empty/invalid)")

    decision = _router.route(filename, data)
    self.update_state(state="STARTED", meta={
        "step":       "routing",
        "route":      decision.route.value,
        "confidence": decision.confidence,
        "pct":        10,
    })

    route = decision.route
    if route == Route.VISION_OCR:
        task_fn = parse_ocr
    elif route == Route.STRUCTURED:
        task_fn = parse_structured
    else:
        task_fn = parse_fast

    result = task_fn(filename, data_hex, output_format, options or {}, file_path)
    result["routingDecision"] = {
        "route":      decision.route.value,
        "confidence": decision.confidence,
        "reasons":    decision.reasons,
        "pageCount":  decision.page_count,
        "complexity": decision.estimated_complexity,
        "workers":    decision.recommended_workers,
    }
    return result
