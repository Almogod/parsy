"""
Parsy — Resource Guard
Enforces per-job CPU time and memory limits using Python's resource module.
Falls back gracefully on Windows (no resource module).
"""
import os, sys, time, threading, logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Any

log = logging.getLogger("parsy.resource_guard")

# ── Platform check ─────────────────────────────────────────────────────────
_HAS_RESOURCE = sys.platform != "win32"
if _HAS_RESOURCE:
    import resource


@dataclass
class ResourceLimits:
    max_memory_mb:  int   = 512    # per-job RSS cap
    max_cpu_seconds: int  = 180    # wall-clock timeout (seconds)
    max_file_size_mb: int = 200    # input file size cap


class ResourceGuard:
    """
    Wraps a callable with memory monitoring and CPU timeout.
    Uses a watchdog thread for cross-platform compatibility.
    """

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
                # Memory check (Linux/Mac only)
                if _HAS_RESOURCE:
                    usage = resource.getrusage(resource.RUSAGE_SELF)
                    rss_mb = usage.ru_maxrss / 1024  # Linux: KB → MB
                    if sys.platform == "darwin":
                        rss_mb = usage.ru_maxrss / (1024 * 1024)  # Mac: bytes
                    if rss_mb > self.limits.max_memory_mb:
                        breach.append(f"OOM: {rss_mb:.0f}MB > {self.limits.max_memory_mb}MB limit")
                        stop_event.set()
                        return
                # Timeout check
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
                log.warning("Resource limit breached", job=job_id, reason=breach[0])
                raise RuntimeError(f"Resource limit: {breach[0]}")

    def check_file_size(self, data: bytes, filename: str = "file"):
        size_mb = len(data) / (1024 * 1024)
        if size_mb > self.limits.max_file_size_mb:
            raise ValueError(
                f"File '{filename}' is {size_mb:.1f}MB — "
                f"exceeds {self.limits.max_file_size_mb}MB limit"
            )

    @staticmethod
    def set_process_limits():
        """Call once per worker process to cap address space (Linux only)."""
        if not _HAS_RESOURCE:
            return
        try:
            # Cap virtual address space to 3 GB
            cap = 3 * 1024 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
            log.info(f"Process memory limit set: {cap // (1024**3)}GB")
        except Exception as e:
            log.warning(f"Could not set RLIMIT_AS: {e}")
