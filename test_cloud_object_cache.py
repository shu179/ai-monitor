import hashlib
import json
from pathlib import Path

import pytest

from core.cloud_object_cache import CloudObjectCache, CloudObjectCacheError, normalize_object_ref


def _ref(data: bytes, **extra):
    payload = {
        "object_id": "object-1",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "content_type": "text/plain",
        "compression": "none",
        "storage_key": "../../unsafe",
    }
    payload.update(extra)
    return payload


def test_cloud_object_cache_writes_verified_object_and_metadata(tmp_path: Path):
    data = b"cached answer body"
    cache = CloudObjectCache(tmp_path / "cache")

    result = cache.cache_bytes(_ref(data), [data[:6], data[6:]], trace_id="trace-1")
    cached = cache.cached_object(_ref(data))
    metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert Path(result["path"]).read_bytes() == data
    assert cached["exists"] is True
    assert cached["valid"] is True
    assert metadata["sha256"] == hashlib.sha256(data).hexdigest()
    assert metadata["size_bytes"] == len(data)
    assert metadata["trace_id"] == "trace-1"


def test_cloud_object_cache_uses_sha_path_not_storage_key(tmp_path: Path):
    data = b"safe path data"
    cache = CloudObjectCache(tmp_path / "cache")

    result = cache.cache_bytes(_ref(data), [data])
    path = Path(result["path"])

    assert path.parent == tmp_path / "cache" / hashlib.sha256(data).hexdigest()[:2] / hashlib.sha256(data).hexdigest()[2:4]
    assert ".." not in path.relative_to(tmp_path / "cache").parts


def test_cloud_object_cache_rejects_sha_mismatch_and_removes_temp(tmp_path: Path):
    cache = CloudObjectCache(tmp_path / "cache")
    bad_ref = _ref(b"expected")

    with pytest.raises(CloudObjectCacheError, match="sha256 mismatch"):
        cache.cache_bytes(bad_ref, [b"actual"])

    assert not list((tmp_path / "cache").glob("*/*/.tmp*"))
    assert not cache.cached_object(bad_ref)["exists"]


def test_cloud_object_cache_rejects_oversized_object(tmp_path: Path):
    cache = CloudObjectCache(tmp_path / "cache", max_object_bytes=4)
    data = b"12345"

    with pytest.raises(CloudObjectCacheError, match="size limit"):
        cache.cache_bytes(_ref(data), [data])


def test_normalize_object_ref_rejects_invalid_sha256():
    with pytest.raises(CloudObjectCacheError, match="64 hex"):
        normalize_object_ref({"sha256": "../not-a-sha"})
