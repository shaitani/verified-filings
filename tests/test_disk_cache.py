from pathlib import Path

from app.ingest.disk_cache import CachedResponse, DiskCache


def test_miss_then_hit(tmp_path: Path):
    cache = DiskCache(tmp_path)
    url = "https://data.sec.gov/submissions/CIK0000320193.json"

    assert cache.get(url) is None
    assert not cache.has(url)

    cache.set(
        url,
        CachedResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            content=b'{"ok": true}',
        ),
    )

    assert cache.has(url)
    cached = cache.get(url)
    assert cached is not None
    assert cached.content == b'{"ok": true}'
    assert cached.status_code == 200


def test_different_urls_do_not_collide(tmp_path: Path):
    cache = DiskCache(tmp_path)
    url_a = "https://data.sec.gov/submissions/CIK0000320193.json"
    url_b = "https://data.sec.gov/submissions/CIK0000789019.json"

    cache.set(url_a, CachedResponse(status_code=200, headers={}, content=b"AAPL"))
    cache.set(url_b, CachedResponse(status_code=200, headers={}, content=b"MSFT"))

    assert cache.get(url_a).content == b"AAPL"
    assert cache.get(url_b).content == b"MSFT"
