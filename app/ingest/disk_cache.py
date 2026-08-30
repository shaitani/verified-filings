"""Disk cache for SEC HTTP responses, keyed by request URL.

Every response ever fetched from a SEC URL is written here. Before any
future request to that same URL, callers check this cache first — a hit
never touches the network or the rate limiter.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CachedResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes


class DiskCache:
    """Content-addressed, on-disk cache keyed by URL.

    Each cached URL is stored as two files, named by the sha256 hash of
    the URL (URLs themselves aren't safe/short enough to use as
    filenames directly):

      {hash}.body       -- the raw response bytes, unmodified
      {hash}.meta.json  -- {"url", "status_code", "headers"}

    Entries never expire or get invalidated automatically: once a URL has
    been cached, it is served from disk forever (until the cache
    directory is cleared out by hand).
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _body_path(self, url: str) -> Path:
        return self._cache_dir / f"{self._key(url)}.body"

    def _meta_path(self, url: str) -> Path:
        return self._cache_dir / f"{self._key(url)}.meta.json"

    def has(self, url: str) -> bool:
        return self._body_path(url).exists() and self._meta_path(url).exists()

    def get(self, url: str) -> CachedResponse | None:
        body_path = self._body_path(url)
        meta_path = self._meta_path(url)
        if not (body_path.exists() and meta_path.exists()):
            return None

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        content = body_path.read_bytes()
        return CachedResponse(
            status_code=meta["status_code"],
            headers=meta["headers"],
            content=content,
        )

    def set(self, url: str, response: CachedResponse) -> None:
        self._body_path(url).write_bytes(response.content)
        meta = {
            "url": url,
            "status_code": response.status_code,
            "headers": response.headers,
        }
        self._meta_path(url).write_text(json.dumps(meta, indent=2), encoding="utf-8")
