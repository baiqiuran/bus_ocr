"""Remote OCR adapter.

The remote service is stateless: it receives PNG crops/canvases and returns
text boxes.  Layout, column mapping and all business parsing stay local.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Sequence

import numpy as np
from PIL import Image

from buspipe.ocr import OcrError, OcrParams, OcrStats, OcrUnavailable, normalize_items

logger = logging.getLogger(__name__)


@dataclass
class RemoteOcrConfig:
    url: str
    model: str = "rapidocr-remote"
    api_key: str | None = field(default=None, repr=False)
    api_key_env: str = "BUS_OCR_API_KEY"
    timeout: float = 30.0
    max_retries: int = 2
    backoff: float = 0.5
    batch_size: int = 6
    max_payload_bytes: int = 1_000_000
    breaker_threshold: int = 3
    breaker_cooldown: float = 30.0
    disable_proxy: bool = False
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def resolved_key(self) -> str:
        if self.api_key:
            return self.api_key
        env_name = self.api_key_env
        return os.environ.get(env_name, "") if env_name else ""


def _png_bytes(image: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.asarray(image)).save(buffer, format="PNG")
    return buffer.getvalue()


def _parse_box(item: Mapping[str, Any]) -> tuple[float, float, float, float]:
    if all(key in item for key in ("x0", "y0", "x1", "y1")):
        return float(item["x0"]), float(item["y0"]), float(item["x1"]), float(item["y1"])
    box = item.get("box")
    if box is None:
        raise OcrError("remote OCR item is missing box coordinates")
    array = np.asarray(box, dtype=float)
    if array.shape == (4, 4) or array.shape == (4, 2):
        return (
            float(array[:, 0].min()),
            float(array[:, 1].min()),
            float(array[:, 0].max()),
            float(array[:, 1].max()),
        )
    if array.size == 4:
        x0, y0, x1, y1 = array.reshape(-1).tolist()
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    raise OcrError(f"unsupported remote box shape: {array.shape}")


def _parse_results(
    payload: Mapping[str, Any], ids: Sequence[str]
) -> tuple[list[list[dict[str, Any]]], str]:
    results = payload.get("results")
    if not isinstance(results, list):
        raise OcrError("remote OCR response is missing results")
    model_version = payload.get("model_version")
    if not isinstance(model_version, str) or not model_version:
        raise OcrError("remote OCR response is missing model_version")
    by_id: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        if not isinstance(result, dict) or "id" not in result:
            raise OcrError("remote OCR result is missing an id")
        items = []
        for item in result.get("items", []):
            if not isinstance(item, dict) or "text" not in item:
                raise OcrError("remote OCR item is missing text")
            if "score" not in item:
                raise OcrError("remote OCR item is missing a confidence score")
            x0, y0, x1, y1 = _parse_box(item)
            items.append(
                {
                    "text": item["text"],
                    "score": float(item["score"]),
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                }
            )
        by_id[str(result["id"])] = normalize_items(items)
    missing = [identifier for identifier in ids if identifier not in by_id]
    if missing:
        raise OcrError(f"remote OCR response is missing ids: {missing[:3]}")
    return [by_id[identifier] for identifier in ids], model_version


class RemoteOcrBackend:
    def __init__(
        self,
        config: RemoteOcrConfig,
        *,
        stats: OcrStats | None = None,
        sleep=time.sleep,
    ) -> None:
        self.config = config
        self.model_version = config.model
        self.stats = stats or OcrStats()
        self._sleep = sleep
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._request_counter = 0
        self._opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if config.disable_proxy
            else None
        )

    def _check_breaker(self) -> None:
        with self._lock:
            if self._open_until and time.monotonic() < self._open_until:
                raise OcrUnavailable("remote OCR circuit breaker is open")

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = 0.0

    def _record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.config.breaker_threshold:
                self._open_until = time.monotonic() + self.config.breaker_cooldown

    def _headers(self, idempotency_key: str = "") -> dict[str, str]:
        headers = {"Content-Type": "application/json", **dict(self.config.headers)}
        key = self.config.resolved_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _post_once(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        idempotency_key = hashlib.sha256(body).hexdigest()[:32]
        request = urllib.request.Request(
            self.config.url,
            data=body,
            headers=self._headers(idempotency_key),
            method="POST",
        )
        opener = self._opener
        if opener is None:
            response_context = urllib.request.urlopen(request, timeout=self.config.timeout)
        else:
            response_context = opener.open(request, timeout=self.config.timeout)
        with response_context as response:
            raw = response.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise OcrError(f"remote OCR returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise OcrError("remote OCR response must be a JSON object")
        return parsed

    def _retry_delay(self, attempt: int, error: urllib.error.HTTPError | None) -> float:
        if error is not None and error.code == 429:
            header = error.headers.get("Retry-After") if error.headers else None
            if header:
                try:
                    return max(0.0, float(header))
                except ValueError:
                    try:
                        target = parsedate_to_datetime(header)
                        return max(0.0, target.timestamp() - time.time())
                    except Exception:
                        pass
        return self.config.backoff * (2**attempt)

    def _encode_batches(self, images: Sequence[np.ndarray]):
        encoded: list[tuple[str, bytes]] = []
        for index, image in enumerate(images):
            png = _png_bytes(image)
            digest = hashlib.sha256(png).hexdigest()[:10]
            encoded.append((f"img-{index}-{digest}", png))
        batches: list[list[tuple[str, bytes]]] = []
        current: list[tuple[str, bytes]] = []
        current_size = 0
        for identifier, png in encoded:
            estimated = len(png) * 4 // 3 + 200
            if current and (
                len(current) >= self.config.batch_size
                or current_size + estimated > self.config.max_payload_bytes
            ):
                batches.append(current)
                current, current_size = [], 0
            current.append((identifier, png))
            current_size += estimated
        if current:
            batches.append(current)
        return batches

    def _send_batch(self, batch: Sequence[tuple[str, bytes]], params: OcrParams) -> list[list[dict[str, Any]]]:
        payload = {
            "model": self.config.model,
            "params": params.as_dict(),
            "images": [
                {
                    "id": identifier,
                    "format": "png",
                    "data": base64.b64encode(png).decode("ascii"),
                }
                for identifier, png in batch
            ],
        }
        ids = [identifier for identifier, _ in batch]
        attempt = 0
        while True:
            self._check_breaker()
            try:
                with self._lock:
                    self._request_counter += 1
                self.stats.inc("remote_calls")
                response = self._post_once(payload)
                parsed, reported_model = _parse_results(response, ids)
                if self.config.model and reported_model != self.config.model:
                    raise OcrError(
                        f"remote model version mismatch: configured {self.config.model}, "
                        f"service reported {reported_model}"
                    )
                self._record_success()
                return parsed
            except urllib.error.HTTPError as exc:
                self.stats.inc("remote_failures")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= self.config.max_retries:
                    self._record_failure()
                    raise OcrUnavailable(f"remote OCR HTTP {exc.code}") from exc
                self.stats.inc("remote_retries")
                self._sleep(self._retry_delay(attempt, exc))
                attempt += 1
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                self.stats.inc("remote_failures")
                if attempt >= self.config.max_retries:
                    self._record_failure()
                    raise OcrUnavailable(f"remote OCR network failure: {exc}") from exc
                self.stats.inc("remote_retries")
                self._sleep(self._retry_delay(attempt, None))
                attempt += 1
            except OcrError:
                self.stats.inc("remote_failures")
                self._record_failure()
                raise

    def recognize(self, images: Sequence[np.ndarray], params: OcrParams) -> list[list[dict[str, Any]]]:
        if not images:
            return []
        batches = self._encode_batches(images)
        results: list[list[dict[str, Any]]] = []
        for batch in batches:
            logger.debug("remote OCR request: %d images, url=%s", len(batch), self.config.url)
            results.extend(self._send_batch(batch, params))
        return results
