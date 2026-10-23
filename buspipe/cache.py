"""Versioned, atomic, file-based caches for blocks and whole posters."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

CACHE_SCHEMA = 1
_REPLACE_LOCK = threading.Lock()


def key_for(parts: dict[str, Any]) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _replace_with_retry(temp_name: str, path: Path) -> None:
    """os.replace can hit a transient sharing violation on Windows.

    Serialize replacements inside the process and retry a few times so two
    workers writing the same cache key cannot produce a partial file.
    """
    last_error: OSError | None = None
    with _REPLACE_LOCK:
        for attempt in range(8):
            try:
                os.replace(temp_name, path)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.02 * (attempt + 1))
    assert last_error is not None
    raise last_error


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


@dataclass
class CacheStats:
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    block_hits: int = 0
    block_misses: int = 0
    poster_hits: int = 0
    poster_misses: int = 0
    invalid: int = 0
    writes: int = 0

    def inc(self, name: str, amount: int = 1) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + amount)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "block_hits": self.block_hits,
                "block_misses": self.block_misses,
                "poster_hits": self.poster_hits,
                "poster_misses": self.poster_misses,
                "invalid": self.invalid,
                "writes": self.writes,
            }


class FileCache:
    def __init__(self, root: Path | str, namespace: str, stats: CacheStats | None = None) -> None:
        self.root = Path(root) / namespace
        self.namespace = namespace
        self.stats = stats or CacheStats()

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str, validator: Callable[[Any], bool] | None = None) -> Any | None:
        path = self.path_for(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            self.stats.inc("invalid")
            return None
        if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA or payload.get("key") != key:
            self.stats.inc("invalid")
            return None
        data = payload.get("data")
        if validator is not None:
            try:
                if not validator(data):
                    self.stats.inc("invalid")
                    return None
            except Exception:
                self.stats.inc("invalid")
                return None
        return data

    def put(self, key: str, data: Any) -> Path:
        path = self.path_for(key)
        payload = {
            "schema": CACHE_SCHEMA,
            "key": key,
            "namespace": self.namespace,
            "data": data,
        }
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True))
        self.stats.inc("writes")
        return path


def _valid_block(block_index: int) -> Callable[[Any], bool]:
    def validate(data: Any) -> bool:
        return (
            isinstance(data, dict)
            and data.get("block_index") == block_index
            and isinstance(data.get("endpoints") if data.get("extract_mode") == "endpoints" else data.get("stops"), list)
            and isinstance(data.get("route"), str)
        )

    return validate


def _valid_poster(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("entries"), list)
        and isinstance(data.get("route_blocks"), list)
        and isinstance(data.get("poster"), str)
    )


class BlockCache:
    """Checkpoint adapter handed to :class:`PosterExtractor`."""

    def __init__(self, cache: FileCache, image_hash: str, base_key: str) -> None:
        self.cache = cache
        self.image_hash = image_hash
        self.base_key = base_key

    def _key(self, block_index: int) -> str:
        return key_for({"kind": "block", "image_hash": self.image_hash, "base": self.base_key, "block": block_index})

    def load(self, block_index: int) -> dict[str, Any] | None:
        data = self.cache.get(self._key(block_index), _valid_block(block_index))
        if data is None:
            self.cache.stats.inc("block_misses")
        else:
            self.cache.stats.inc("block_hits")
        return data

    def store(self, block_index: int, entry: dict[str, Any]) -> None:
        self.cache.put(self._key(block_index), entry)


class PosterCache:
    def __init__(self, cache: FileCache, key: str) -> None:
        self.cache = cache
        self.key = key

    def load(self) -> dict[str, Any] | None:
        data = self.cache.get(self.key, _valid_poster)
        if data is None:
            self.cache.stats.inc("poster_misses")
        else:
            self.cache.stats.inc("poster_hits")
        return data

    def store(self, result: dict[str, Any]) -> None:
        self.cache.put(self.key, result)

