"""Shared HTTP plumbing: polite rate limiting, retries, on-disk response cache."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

CACHE_DIR = Path(os.environ.get("SCRAPER_CACHE", "data/.cache"))


class RateLimiter:
    """Minimum delay between requests, with jitter, thread-safe."""

    def __init__(self, min_interval: float = 1.0, jitter: float = 0.4):
        self.min_interval = min_interval
        self.jitter = jitter
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            delay = self.min_interval - gap
            if delay > 0:
                time.sleep(delay + random.uniform(0, self.jitter))
            self._last = time.monotonic()


class TransientError(RuntimeError):
    pass


class HttpClient:
    def __init__(
        self,
        rate: float = 1.0,
        cache: bool = True,
        timeout: int = 30,
        headers: dict[str, str] | None = None,
    ):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        if headers:
            self.session.headers.update(headers)
        self.limiter = RateLimiter(rate)
        self.timeout = timeout
        self.cache = cache
        if cache:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ---- cache -----------------------------------------------------------
    def _cache_path(self, method: str, url: str, body: Any) -> Path:
        h = hashlib.sha1(f"{method}{url}{json.dumps(body, sort_keys=True)}".encode()).hexdigest()
        return CACHE_DIR / f"{h}.txt"

    def _cached(self, path: Path, max_age: int) -> str | None:
        if not self.cache or not path.exists():
            return None
        if max_age and time.time() - path.stat().st_mtime > max_age:
            return None
        return path.read_text(encoding="utf-8")

    # ---- requests --------------------------------------------------------
    @retry(
        retry=retry_if_exception_type((TransientError, requests.RequestException)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def _request(self, method: str, url: str, **kw) -> requests.Response:
        self.limiter.wait()
        r = self.session.request(method, url, timeout=self.timeout, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"{r.status_code} on {url}")
        return r

    def get_text(self, url: str, max_age: int = 86400 * 7, **kw) -> str:
        p = self._cache_path("GET", url, kw.get("params"))
        hit = self._cached(p, max_age)
        if hit is not None:
            return hit
        r = self._request("GET", url, **kw)
        r.raise_for_status()
        if self.cache:
            p.write_text(r.text, encoding="utf-8")
        return r.text

    def get_json(self, url: str, max_age: int = 86400 * 7, **kw) -> Any:
        return json.loads(self.get_text(url, max_age=max_age, **kw))

    def post_json(self, url: str, payload: dict, max_age: int = 86400 * 7, **kw) -> Any:
        p = self._cache_path("POST", url, payload)
        hit = self._cached(p, max_age)
        if hit is not None:
            return json.loads(hit)
        r = self._request("POST", url, json=payload, **kw)
        r.raise_for_status()
        if self.cache:
            p.write_text(r.text, encoding="utf-8")
        return r.json()


class BaseScraper:
    """Every source scraper implements `hackathons()` and `projects()`."""

    name = "base"
    #: False when the platform publishes no project submissions to scrape,
    #: so callers can skip the project pass instead of reporting an empty run.
    has_project_submissions = True

    def __init__(self, client: HttpClient | None = None, **kw):
        self.client = client or HttpClient(**kw)

    def hackathons(self, limit: int | None = None):
        raise NotImplementedError

    def projects(self, hackathon=None, winners_only: bool = True, limit: int | None = None):
        raise NotImplementedError
