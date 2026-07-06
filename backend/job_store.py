import os
import json
import logging

log = logging.getLogger("parsy.job_store")

class TrackingDict(dict):
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
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.redis = None
        self._local_jobs: dict[str, dict] = {}
        try:
            import redis
            self.redis = redis.from_url(self.redis_url, socket_timeout=2.0, decode_responses=True)
            self.redis.ping()
            log.info("Redis job store connected", extra={"url": self.redis_url})
        except Exception as exc:
            self.redis = None
            log.warning("Redis job store not available, falling back to in-memory store", extra={"error": str(exc)})

    def get(self, job_id: str, default=None) -> dict | None:
        if self.redis:
            try:
                data = self.redis.get(f"parsy:job:{job_id}")
                if data:
                    val = json.loads(data)
                    return TrackingDict(val, _on_update=lambda d: self.set(job_id, d))
            except Exception as exc:
                log.error("Redis get failed", extra={"job_id": job_id, "error": str(exc)})
        val = self._local_jobs.get(job_id)
        if val is not None:
            return TrackingDict(val, _on_update=lambda d: self.set(job_id, d))
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
        self._local_jobs[job_id] = job_data

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
        return job_id in self._local_jobs

    def items(self) -> list[tuple[str, dict]]:
        if self.redis:
            try:
                job_ids = self.redis.lrange("parsy:recent_jobs", 0, 99)
                results = []
                for jid in job_ids:
                    data = self.get(jid)
                    if data:
                        results.append((jid, data))
                return results
            except Exception as exc:
                log.error("Redis items failed", extra={"error": str(exc)})
        return list(self._local_jobs.items())
