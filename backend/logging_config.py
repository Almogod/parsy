"""
Parsy Backend — Structured Logging Configuration
Configures Python's stdlib ``logging`` module with:
    * JSON format in production  (LOG_FORMAT=json, the default in Docker)
    * Human-readable console format in development  (LOG_FORMAT=console)
    * Configurable level via the LOG_LEVEL environment variable
    * Automatic correlation of job IDs via a thread-local filter

Import and call ``configure_logging()`` once at application startup
(main.py, tasks.py, and Celery worker bootstrap).

All modules should obtain their logger via:
    import logging
    log = logging.getLogger("parsy.<module>")

and pass structured context as ``extra={...}`` kwargs, e.g.:
    log.info("Parse complete", extra={"file_name": "report.pdf", "elapsed_s": "1.23"})
"""
import json
import logging
import logging.config
import os
import sys
import time
import threading
from typing import Any

# ── Thread-local job context ────────────────────────────────────────────────

_ctx = threading.local()


def set_job_context(job_id: str = "", filename: str = "") -> None:
    """Store per-request context that will be injected into every log record."""
    _ctx.job_id = job_id
    _ctx.filename = filename


def clear_job_context() -> None:
    _ctx.job_id = ""
    _ctx.filename = ""


# ── Filters ─────────────────────────────────────────────────────────────────

class JobContextFilter(logging.Filter):
    """Injects job_id and filename from thread-local storage into every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.job_id = getattr(_ctx, "job_id", "")
        record.filename_ctx = getattr(_ctx, "filename", "")
        return True


# ── Formatters ──────────────────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    """
    Emits one JSON object per log line.
    Standard fields: timestamp, level, logger, message, job_id, filename.
    Additional ``extra`` kwargs are included as top-level keys.
    """

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

        # Thread-local context fields
        if job_id := getattr(record, "job_id", ""):
            payload["job_id"] = job_id
        if filename := getattr(record, "filename_ctx", ""):
            payload["filename"] = filename

        # Any extra={...} kwargs
        for key, value in record.__dict__.items():
            if key not in self._EXCLUDED and not key.startswith("_"):
                payload[key] = value

        # Exception info
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """
    Coloured, human-readable console output for local development.
    """

    _COLORS = {
        "DEBUG":    "\033[36m",    # cyan
        "INFO":     "\033[32m",    # green
        "WARNING":  "\033[33m",    # yellow
        "ERROR":    "\033[31m",    # red
        "CRITICAL": "\033[35m",    # magenta
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self._COLORS.get(record.levelname, "")
        ts = time.strftime("%H:%M:%S")
        job = f"[{record.job_id}] " if getattr(record, "job_id", "") else ""
        base = (
            f"{color}{ts} {record.levelname:8s}{self._RESET} "
            f"{record.name:<30s} {job}{record.getMessage()}"
        )
        # Append extra fields compactly
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__
            and not k.startswith("_")
            and k not in ("job_id", "filename_ctx")
        }
        if extras:
            base += "  " + " ".join(f"{k}={v!r}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


# ── Public configuration entrypoint ─────────────────────────────────────────

def configure_logging(
    level: str | None = None,
    fmt: str | None = None,
    force: bool = False,
) -> None:
    """
    Configure structured logging for the entire Parsy process.

    Parameters
    ----------
    level:
        Log level string (DEBUG/INFO/WARNING/ERROR).
        Defaults to ``LOG_LEVEL`` env var, or ``INFO``.
    fmt:
        ``"json"`` or ``"console"``.
        Defaults to ``LOG_FORMAT`` env var, or ``"console"``.
    force:
        If ``True``, re-configures even if already initialised.
    """
    # Idempotency guard — skip if already configured and not forced
    root = logging.getLogger()
    if root.handlers and not force:
        return

    level_str  = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    format_str = (fmt   or os.getenv("LOG_FORMAT", "console")).lower()

    numeric_level = getattr(logging, level_str, logging.INFO)

    # Choose formatter
    formatter: logging.Formatter
    if format_str == "json":
        formatter = JSONFormatter()
    else:
        formatter = ConsoleFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(JobContextFilter())

    # Apply to root logger — all child loggers inherit
    logging.basicConfig(
        level=numeric_level,
        handlers=[handler],
        force=True,
    )

    # Quiet noisy third-party loggers
    for noisy in ("urllib3", "httpx", "PIL", "fitz", "celery.utils.functional"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger("parsy").info(
        "Logging configured",
        extra={"level": level_str, "format": format_str},
    )
