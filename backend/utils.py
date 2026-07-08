"""
Parsy Backend — Utils
Consolidated utilities: structured logging, resource guard, and job store.

Merges: logging_config.py + resource_guard.py + job_store.py
"""
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Section 1 — Structured Logging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import json
import logging
import logging.config
import os
import sys
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

# ── Thread-local job context ────────────────────────────────────────────────
_ctx = threading.local()


def set_job_context(job_id: str = "", filename: str = "") -> None:
    """Store per-request context injected into every log record."""
    _ctx.job_id  = job_id
    _ctx.filename = filename


def clear_job_context() -> None:
    _ctx.job_id  = ""
    _ctx.filename = ""


# ── Filters ─────────────────────────────────────────────────────────────────
class JobContextFilter(logging.Filter):
    """Injects job_id and filename from thread-local storage into every record."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.job_id       = getattr(_ctx, "job_id", "")
        record.filename_ctx = getattr(_ctx, "filename", "")
        return True


# ── Formatters ──────────────────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    """Emits one JSON object per log line."""
    _EXCLUDED = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
        | {"message", "asctime", "exc_text"}
    )

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.message,
        }
        if job_id := getattr(record, "job_id", ""):
            payload["job_id"] = job_id
        if filename := getattr(record, "filename_ctx", ""):
            payload["filename"] = filename
        for key, value in record.__dict__.items():
            if key not in self._EXCLUDED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Coloured, human-readable console output for local development."""
    _COLORS = {
        "DEBUG":    "\033[36m",
        "INFO":     "\033[32m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[35m",
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self._COLORS.get(record.levelname, "")
        ts    = time.strftime("%H:%M:%S")
        job   = f"[{record.job_id}] " if getattr(record, "job_id", "") else ""
        base  = (
            f"{color}{ts} {record.levelname:8s}{self._RESET} "
            f"{record.name:<30s} {job}{record.getMessage()}"
        )
        _std = logging.LogRecord("", 0, "", 0, "", (), None).__dict__
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _std and not k.startswith("_")
            and k not in ("job_id", "filename_ctx")
        }
        if extras:
            base += "  " + " ".join(f"{k}={v!r}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(
    level: str | None = None,
    fmt:   str | None = None,
    force: bool = False,
) -> None:
    """
    Configure structured logging for the entire Parsy process.
    Call once at startup. Uses LOG_LEVEL / LOG_FORMAT env vars.
    """
    root = logging.getLogger()
    if root.handlers and not force:
        return

    level_str  = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    format_str = (fmt   or os.getenv("LOG_FORMAT", "console")).lower()
    numeric    = getattr(logging, level_str, logging.INFO)

    formatter: logging.Formatter = (
        JSONFormatter() if format_str == "json" else ConsoleFormatter()
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(JobContextFilter())

    logging.basicConfig(level=numeric, handlers=[handler], force=True)

    for noisy in ("urllib3", "httpx", "PIL", "fitz", "celery.utils.functional"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger("parsy").info(
        "Logging configured",
        extra={"level": level_str, "format": format_str},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Section 2 — Resource Guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_HAS_RESOURCE = sys.platform != "win32"
if _HAS_RESOURCE:
    import resource  # noqa: F401 — imported for side-effects below

log = logging.getLogger("parsy.utils")


@dataclass
class ResourceLimits:
    max_memory_mb:    int = 512
    max_cpu_seconds:  int = 180
    max_file_size_mb: int = 200


class ResourceGuard:
    """Wraps a code block with memory monitoring and CPU timeout."""

    def __init__(self, limits: ResourceLimits | None = None):
        self.limits = limits or ResourceLimits()

    @contextmanager
    def guard(self, job_id: str = "unknown"):
        """Context manager — raises RuntimeError on limit breach."""
        stop_event = threading.Event()
        breach: list[str] = []

        def watchdog():
            deadline = time.monotonic() + self.limits.max_cpu_seconds
            while not stop_event.is_set():
                if _HAS_RESOURCE:
                    import resource as _res
                    usage  = _res.getrusage(_res.RUSAGE_SELF)
                    rss_mb = usage.ru_maxrss / 1024
                    if sys.platform == "darwin":
                        rss_mb = usage.ru_maxrss / (1024 * 1024)
                    if rss_mb > self.limits.max_memory_mb:
                        breach.append(f"OOM: {rss_mb:.0f}MB > {self.limits.max_memory_mb}MB limit")
                        stop_event.set()
                        return
                if time.monotonic() > deadline:
                    breach.append(f"Timeout: exceeded {self.limits.max_cpu_seconds}s")
                    stop_event.set()
                    return
                time.sleep(0.5)

        thread = threading.Thread(target=watchdog, daemon=True, name=f"guard-{job_id}")
        thread.start()
        try:
            yield stop_event
        finally:
            stop_event.set()
            thread.join(timeout=1.0)
            if breach:
                log.warning("Resource limit breached", extra={"job": job_id, "reason": breach[0]})
                raise RuntimeError(f"Resource limit: {breach[0]}")

    def check_file_size(self, data: bytes, filename: str = "file") -> None:
        size_mb = len(data) / (1024 * 1024)
        if size_mb > self.limits.max_file_size_mb:
            raise ValueError(
                f"File '{filename}' is {size_mb:.1f}MB — "
                f"exceeds {self.limits.max_file_size_mb}MB limit"
            )

    @staticmethod
    def set_process_limits() -> None:
        """Call once per worker process to cap address space (Linux only)."""
        if not _HAS_RESOURCE:
            return
        try:
            import resource as _res
            cap = 3 * 1024 * 1024 * 1024
            _res.setrlimit(_res.RLIMIT_AS, (cap, cap))
            log.info(f"Process memory limit set: {cap // (1024**3)}GB")
        except Exception as e:
            log.warning(f"Could not set RLIMIT_AS: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Section 3 — Job Store (Redis with in-memory fallback)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _TrackingDict(dict):
    """Dict subclass that calls a callback on every mutation."""
    def __init__(self, *args, **kwargs):
        self._on_update = kwargs.pop("_on_update", None)
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if self._on_update:
            self._on_update(self)

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        if self._on_update:
            self._on_update(self)


class RedisJobStore:
    """Job metadata store with Redis primary and in-memory fallback."""

    def __init__(self, redis_url: str):
        self.redis_url   = redis_url
        self.redis       = None
        self._local: dict[str, dict] = {}
        try:
            import redis as _redis
            self.redis = _redis.from_url(redis_url, socket_timeout=2.0, decode_responses=True)
            self.redis.ping()
            log.info("Redis job store connected", extra={"url": redis_url})
        except Exception as exc:
            self.redis = None
            log.warning("Redis unavailable — using in-memory job store", extra={"error": str(exc)})

    # ── Low-level helpers ──────────────────────────────────────────────────
    def get(self, job_id: str, default=None) -> dict | None:
        if self.redis:
            try:
                data = self.redis.get(f"parsy:job:{job_id}")
                if data:
                    val = json.loads(data)
                    return _TrackingDict(val, _on_update=lambda d: self.set(job_id, d))
            except Exception as exc:
                log.error("Redis get failed", extra={"job_id": job_id, "error": str(exc)})
        val = self._local.get(job_id)
        if val is not None:
            return _TrackingDict(val, _on_update=lambda d: self.set(job_id, d))
        return default

    def set(self, job_id: str, job_data: dict) -> None:
        if self.redis:
            try:
                self.redis.setex(f"parsy:job:{job_id}", 86400, json.dumps(job_data))
                self.redis.lpush("parsy:recent_jobs", job_id)
                self.redis.ltrim("parsy:recent_jobs", 0, 100)
                return
            except Exception as exc:
                log.error("Redis set failed", extra={"job_id": job_id, "error": str(exc)})
        self._local[job_id] = job_data

    # ── Dict-like interface ────────────────────────────────────────────────
    def __getitem__(self, job_id: str) -> dict:
        val = self.get(job_id)
        if val is None:
            raise KeyError(job_id)
        return val

    def __setitem__(self, job_id: str, job_data: dict) -> None:
        self.set(job_id, job_data)

    def __contains__(self, job_id: str) -> bool:
        if self.redis:
            try:
                return bool(self.redis.exists(f"parsy:job:{job_id}"))
            except Exception as exc:
                log.error("Redis exists check failed", extra={"job_id": job_id, "error": str(exc)})
        return job_id in self._local

    def items(self) -> list[tuple[str, dict]]:
        if self.redis:
            try:
                job_ids = self.redis.lrange("parsy:recent_jobs", 0, 99)
                return [(jid, d) for jid in job_ids if (d := self.get(jid))]
            except Exception as exc:
                log.error("Redis items failed", extra={"error": str(exc)})
        return list(self._local.items())
