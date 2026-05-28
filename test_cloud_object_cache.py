import hashlib
import json
import os
import time
import compression.zstd as zstd
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


def test_cloud_object_cache_decompresses_zstd_before_verifying_and_writing(tmp_path: Path):
    data = b"cached compressed answer body" * 20
    compressed = zstd.compress(data)
    cache = CloudObjectCache(tmp_path / "cache")

    result = cache.cache_bytes(
        _ref(
            data,
            compression="zstd",
            storage_size_bytes=len(compressed),
        ),
        [compressed[:7], compressed[7:]],
        trace_id="trace-zstd",
    )
    metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert Path(result["path"]).read_bytes() == data
    assert metadata["compression"] == "zstd"
    assert metadata["size_bytes"] == len(data)
    assert metadata["storage_size_bytes"] == len(compressed)
    assert metadata["expected_storage_size_bytes"] == len(compressed)


def test_cloud_object_cache_rejects_zstd_storage_size_mismatch(tmp_path: Path):
    data = b"compressed size mismatch" * 20
    compressed = zstd.compress(data)
    cache = CloudObjectCache(tmp_path / "cache")

    with pytest.raises(CloudObjectCacheError, match="storage size mismatch"):
        cache.cache_bytes(_ref(data, compression="zstd", storage_size_bytes=len(compressed) + 1), [compressed])

    assert not cache.cached_object(_ref(data))["exists"]


def test_cloud_object_cache_rejects_unknown_compression(tmp_path: Path):
    data = b"unsupported compression"
    cache = CloudObjectCache(tmp_path / "cache")

    with pytest.raises(CloudObjectCacheError, match="unsupported object compression"):
        cache.cache_bytes(_ref(data, compression="brotli"), [data])


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


def test_cloud_object_cache_rejects_object_larger_than_total_budget(tmp_path: Path):
    cache = CloudObjectCache(tmp_path / "cache", max_cache_bytes=4)
    data = b"12345"

    with pytest.raises(CloudObjectCacheError, match="total size limit"):
        cache.cache_bytes(_ref(data), [data])


def test_cloud_object_cache_auto_prunes_after_write(tmp_path: Path):
    old_data = b"old object"
    new_data = b"new object"
    cache = CloudObjectCache(tmp_path / "cache", max_cache_bytes=len(old_data) + 2)
    old_result = cache.cache_bytes(_ref(old_data, object_id="old"), [old_data])
    time.sleep(0.01)

    new_result = cache.cache_bytes(_ref(new_data, object_id="new"), [new_data])

    assert new_result["pruned"] == 1
    assert not Path(old_result["path"]).exists()
    assert Path(new_result["path"]).exists()
    assert cache.diagnostics()["bytes"] == len(new_data)


def test_cloud_object_cache_prunes_before_writing_incoming_object(tmp_path: Path):
    old_data = b"old cached object"
    new_data = b"new cached object"
    cache = CloudObjectCache(tmp_path / "cache", max_cache_bytes=len(old_data) + len(new_data) - 1)
    old_result = cache.cache_bytes(_ref(old_data, object_id="old"), [old_data])
    time.sleep(0.01)

    new_result = cache.cache_bytes(_ref(new_data, object_id="new"), [new_data])

    assert new_result["pruned"] == 1
    assert new_result["bytes_removed"] == len(old_data)
    assert not Path(old_result["path"]).exists()
    assert Path(new_result["path"]).exists()
    assert cache.diagnostics()["bytes"] == len(new_data)


def test_cloud_object_cache_rejects_single_object_over_cache_budget(tmp_path: Path):
    data = b"too large for remaining cache"
    cache = CloudObjectCache(tmp_path / "cache", max_cache_bytes=len(data) - 1)

    with pytest.raises(CloudObjectCacheError, match="total size limit"):
        cache.cache_bytes(_ref(data), [data])


def test_cloud_object_cache_prunes_lru_objects_when_over_budget(tmp_path: Path):
    old_data = b"old cached object"
    new_data = b"new cached object"
    cache = CloudObjectCache(tmp_path / "cache", max_cache_bytes=10_000)
    old_result = cache.cache_bytes(_ref(old_data, object_id="old"), [old_data])
    time.sleep(0.01)
    new_result = cache.cache_bytes(_ref(new_data, object_id="new"), [new_data])
    cache.max_cache_bytes = len(old_data) + 2

    result = cache.prune(target_bytes=len(new_data))

    assert result["pruned"] == 1
    assert result["bytes_removed"] == len(old_data)
    assert not Path(old_result["path"]).exists()
    assert not Path(old_result["metadata_path"]).exists()
    assert Path(new_result["path"]).exists()


def test_cloud_object_cache_cached_object_refreshes_lru_mtime(tmp_path: Path):
    data = b"touch me"
    cache = CloudObjectCache(tmp_path / "cache")
    result = cache.cache_bytes(_ref(data), [data])
    path = Path(result["path"])
    old_mtime = path.stat().st_mtime - 60
    os.utime(path, (old_mtime, old_mtime))

    cache.cached_object(_ref(data))

    assert path.stat().st_mtime > old_mtime


def test_normalize_object_ref_rejects_invalid_sha256():
    with pytest.raises(CloudObjectCacheError, match="64 hex"):
        normalize_object_ref({"sha256": "../not-a-sha"})
