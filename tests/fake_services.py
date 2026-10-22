"""Local HTTP services used to test the remote OCR and VLM contracts."""

from __future__ import annotations

import base64
import io
import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import numpy as np
from PIL import Image

from buspipe.ocr import OcrParams
from tests.synth import SyntheticOcrBackend


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        app: Callable = self.server.app  # type: ignore[attr-defined]
        try:
            status, payload, headers = app(self.headers, body)
        except Exception as exc:  # surface as a 500, like a real service
            status, payload, headers = 500, {"error": str(exc)}, {}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        return


@contextmanager
def serve(app: Callable):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class FakeOcrService:
    def __init__(
        self,
        backend: SyntheticOcrBackend | None = None,
        *,
        status_plan: list[int] | None = None,
        always_status: int | None = None,
        delay: float = 0.0,
        drop_box: bool = False,
        drop_score: bool = False,
        reported_model: str | None = None,
    ) -> None:
        self.backend = backend or SyntheticOcrBackend()
        self.status_plan = deque(status_plan or [])
        self.always_status = always_status
        self.delay = delay
        self.drop_box = drop_box
        self.drop_score = drop_score
        self.reported_model = reported_model
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []

    def __call__(self, headers, body: bytes):
        payload = json.loads(body.decode("utf-8"))
        with self.lock:
            self.requests.append(
                {
                    "authorization": headers.get("Authorization"),
                    "idempotency": headers.get("Idempotency-Key"),
                    "ids": [image["id"] for image in payload.get("images", [])],
                    "model": payload.get("model"),
                }
            )
            status = self.always_status
            if status is None and self.status_plan:
                status = self.status_plan.popleft()
        if self.delay:
            time.sleep(self.delay)
        if status is not None and status != 200:
            extra = {"Retry-After": "0.02"} if status == 429 else {}
            return status, {"error": "planned failure", "status": status}, extra
        images = []
        for record in payload.get("images", []):
            raw = base64.b64decode(record["data"])
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            images.append(np.asarray(image))
        recognized = self.backend.recognize(images, OcrParams())
        results = []
        for record, items in zip(payload.get("images", []), recognized):
            out_items = []
            for index, item in enumerate(items):
                item = dict(item)
                if self.drop_box and index == 0:
                    for key in ("box", "x0", "x1", "y0", "y1"):
                        item.pop(key, None)
                else:
                    item["box"] = [
                        [item["x0"], item["y0"]],
                        [item["x1"], item["y0"]],
                        [item["x1"], item["y1"]],
                        [item["x0"], item["y1"]],
                    ]
                if self.drop_score and index == 0:
                    item.pop("score", None)
                out_items.append(item)
            results.append({"id": record["id"], "items": out_items})
        model_version = self.reported_model or payload.get("model") or self.backend.model_version
        return 200, {"model_version": model_version, "results": results}, {}


class FakeVlmService:
    def __init__(
        self,
        opinion: dict[str, Any] | None = None,
        *,
        invalid_json: bool = False,
        status_plan: list[int] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.opinion = opinion or {
            "text": "测试站",
            "note": "",
            "agree": True,
            "confidence": 0.95,
            "reason": "clear glyphs",
        }
        self.invalid_json = invalid_json
        self.status_plan = deque(status_plan or [])
        self.delay = delay
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []

    def __call__(self, headers, body: bytes):
        payload = json.loads(body.decode("utf-8"))
        with self.lock:
            self.requests.append(payload)
            status = self.status_plan.popleft() if self.status_plan else 200
        if self.delay:
            time.sleep(self.delay)
        if status != 200:
            return status, {"error": "planned failure"}, {}
        content = "not valid json" if self.invalid_json else json.dumps(self.opinion, ensure_ascii=False)
        return 200, {"choices": [{"message": {"content": content}}], "model_version": "fake-vlm-v1"}, {}
