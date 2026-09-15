"""Polite HTTP client.

Constraints enforced here, not left to callers:
  * robots.txt is fetched once per host and every request path is checked
    against it. A disallowed path raises before any request is made.
  * a minimum delay between requests to the same host (default 2s)
  * exponential backoff on 429/503, honouring Retry-After when present
  * a single connection per host, requests issued serially
  * a descriptive, honest User-Agent

There is deliberately no retry on 4xx other than 429: a 404 or 500 is data
about the endpoint, not a transient to paper over.
"""
from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids the path. Never caught internally."""


class FetchError(Exception):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{status or 'ERR'} {url}: {message}")
        self.url = url
        self.status = status
        self.message = message


@dataclass
class RateLimit:
    min_interval_s: float = 2.0
    max_retries: int = 4
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 60.0
    jitter_s: float = 0.25
    timeout_s: float = 60.0


@dataclass
class PoliteClient:
    user_agent: str
    rate: RateLimit = field(default_factory=RateLimit)
    obey_robots: bool = True
    _last_request_at: dict[str, float] = field(default_factory=dict, init=False)
    _robots: dict[str, urllib.robotparser.RobotFileParser | None] = field(
        default_factory=dict, init=False)

    # -- robots -----------------------------------------------------------
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{origin}/robots.txt")
        try:
            req = urllib.request.Request(
                f"{origin}/robots.txt",
                headers={"User-Agent": self.user_agent, "Accept": "text/plain"},
            )
            with urllib.request.urlopen(req, timeout=self.rate.timeout_s) as resp:
                rp.parse(resp.read().decode("utf-8", "replace").splitlines())
        except Exception:
            # Unreachable robots.txt is treated as "do not proceed" when we are
            # obeying robots -- failing closed is the conservative choice.
            self._robots[origin] = None
            return None
        self._robots[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.obey_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return False
        return rp.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        rp = self._robots_for(url)
        if rp is None:
            return None
        try:
            d = rp.crawl_delay(self.user_agent)
            return float(d) if d is not None else None
        except Exception:
            return None

    # -- fetching ---------------------------------------------------------
    def _throttle(self, host: str, url: str) -> None:
        interval = self.rate.min_interval_s
        declared = self.crawl_delay(url)
        if declared is not None:
            interval = max(interval, declared)
        last = self._last_request_at.get(host)
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait + random.uniform(0, self.rate.jitter_s))

    def get(self, url: str, accept: str = "application/json,text/plain,*/*") -> str:
        if self.obey_robots and not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        host = urllib.parse.urlsplit(url).netloc
        attempt = 0
        while True:
            self._throttle(host, url)
            req = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent,
                "Accept": accept,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip",
                "Connection": "close",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.rate.timeout_s) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if status in (429, 503) and attempt < self.rate.max_retries:
                    self._sleep_backoff(attempt, retry_after)
                    attempt += 1
                    continue
                raise FetchError(url, status, exc.reason or "http error") from exc
            except urllib.error.URLError as exc:
                if attempt < self.rate.max_retries:
                    self._sleep_backoff(attempt, None)
                    attempt += 1
                    continue
                raise FetchError(url, None, str(exc.reason)) from exc
            finally:
                self._last_request_at[host] = time.monotonic()

    def get_json(self, url: str) -> Any:
        body = self.get(url)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise FetchError(url, None, f"response was not JSON: {exc}") from exc

    def get_json_with_body(self, url: str) -> tuple[Any, str]:
        """Return (parsed, raw_text) so the caller can archive the exact bytes."""
        body = self.get(url)
        try:
            return json.loads(body), body
        except json.JSONDecodeError as exc:
            raise FetchError(url, None, f"response was not JSON: {exc}") from exc

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), self.rate.backoff_cap_s))
                return
            except (TypeError, ValueError):
                pass
        delay = min(self.rate.backoff_base_s * (2 ** attempt), self.rate.backoff_cap_s)
        time.sleep(delay + random.uniform(0, self.rate.jitter_s))
