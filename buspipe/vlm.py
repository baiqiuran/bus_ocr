"""VLM review adapter and cache.

The VLM is an advisor, never an automatic overwrite.  It receives a target crop
plus neighbouring context and returns a structured opinion.  The batch layer
decides whether that opinion confirms OCR, conflicts with it, or becomes a
VLM-only candidate that still needs a human.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from buspipe.cache import FileCache, key_for
from buspipe.ocr import OcrError

PROMPT_VERSION = "vlm-1"

SYSTEM_PROMPT = (
    "你是公交站牌竖排文字的复核员。只判断给出的目标裁片，不要猜测被裁掉的字。"
    "输出必须是 JSON，字段为 text（目标裁片上的完整站名，不含括号备注）、"
    "note（括号备注，没有则为空字符串）、agree（是否同意 OCR 候选）、"
    "confidence（0 到 1）、reason（简短理由）。只输出 JSON。"
)


class VlmError(RuntimeError):
    pass


@dataclass
class VlmRequest:
    target_png: bytes
    context_pngs: list[bytes]
    route: str
    direction: str
    header_next_stop: str
    destination: str
    ocr_text: str
    prompt_version: str = PROMPT_VERSION


@dataclass
class VlmOpinion:
    text: str
    note: str
    agree: bool
    confidence: float
    reason: str
    model_version: str = ""


@dataclass
class VlmStats:
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    calls: int = 0
    cache_hits: int = 0
    ignored: int = 0
    confirmed: int = 0
    conflicts: int = 0
    vlm_only: int = 0
    errors: int = 0

    def inc(self, name: str, amount: int = 1) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + amount)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "calls": self.calls,
                "cache_hits": self.cache_hits,
                "ignored": self.ignored,
                "confirmed": self.confirmed,
                "conflicts": self.conflicts,
                "vlm_only": self.vlm_only,
                "errors": self.errors,
            }


@dataclass
class VlmConfig:
    url: str
    model: str = "vlm-review"
    api_key: str | None = field(default=None, repr=False)
    api_key_env: str = "BUS_VLM_API_KEY"
    timeout: float = 60.0
    max_retries: int = 1
    backoff: float = 1.0
    disable_proxy: bool = False
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def resolved_key(self) -> str:
        if self.api_key:
            return self.api_key
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


def parse_opinion(content: str, model_version: str = "") -> VlmOpinion:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise VlmError(f"VLM returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise VlmError("VLM JSON must be an object")
    text = payload.get("text")
    note = payload.get("note", "")
    agree = payload.get("agree")
    confidence = payload.get("confidence")
    reason = payload.get("reason", "")
    if not isinstance(text, str) or not text.strip():
        raise VlmError("VLM response is missing a non-empty text")
    if not isinstance(note, str):
        raise VlmError("VLM note must be a string")
    if not isinstance(agree, bool):
        raise VlmError("VLM agree must be a boolean")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise VlmError("VLM confidence must be between 0 and 1")
    if not isinstance(reason, str):
        raise VlmError("VLM reason must be a string")
    return VlmOpinion(
        text=text.strip(),
        note=note.strip(),
        agree=agree,
        confidence=float(confidence),
        reason=reason,
        model_version=model_version,
    )


class HttpVlmReviewer:
    def __init__(self, config: VlmConfig, *, sleep=time.sleep) -> None:
        self.config = config
        self.model_version = config.model
        self._sleep = sleep
        self._opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if config.disable_proxy
            else None
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **dict(self.config.headers)}
        key = self.config.resolved_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _payload(self, request: VlmRequest) -> dict[str, Any]:
        context = (
            f"线路 {request.route}，方向 {request.direction}，"
            f"表头下站 {request.header_next_stop or '无'}，"
            f"开往 {request.destination or '无'}，OCR 候选 {request.ocr_text or '（空）'}。"
            "第一张是目标裁片，其余是相邻站点上下文。"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": context}]
        for png in [request.target_png, *request.context_pngs]:
            encoded = base64.b64encode(png).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                }
            )
        return {
            "model": self.config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "prompt_version": request.prompt_version,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        }

    def review(self, request: VlmRequest) -> VlmOpinion:
        payload = json.dumps(self._payload(request), ensure_ascii=False).encode("utf-8")
        attempt = 0
        while True:
            http_request = urllib.request.Request(
                self.config.url,
                data=payload,
                headers=self._headers(),
                method="POST",
            )
            try:
                opener = self._opener
                context = (
                    opener.open(http_request, timeout=self.config.timeout)
                    if opener is not None
                    else urllib.request.urlopen(http_request, timeout=self.config.timeout)
                )
                with context as response:
                    raw = response.read()
                parsed = json.loads(raw.decode("utf-8"))
                choices = parsed.get("choices") if isinstance(parsed, dict) else None
                if not choices:
                    raise VlmError("VLM response is missing choices")
                message = choices[0].get("message", {})
                content = message.get("content")
                if isinstance(content, dict):
                    content = json.dumps(content, ensure_ascii=False)
                if not isinstance(content, str):
                    raise VlmError("VLM message content must be a string")
                model_version = str(parsed.get("model_version") or self.config.model)
                return parse_opinion(content, model_version)
            except urllib.error.HTTPError as exc:
                if exc.code >= 500 or exc.code == 429:
                    if attempt < self.config.max_retries:
                        self._sleep(self.config.backoff * (2**attempt))
                        attempt += 1
                        continue
                raise VlmError(f"VLM HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                if attempt < self.config.max_retries:
                    self._sleep(self.config.backoff * (2**attempt))
                    attempt += 1
                    continue
                raise VlmError(f"VLM network failure: {exc}") from exc


class VlmCache:
    def __init__(self, root, stats: VlmStats | None = None) -> None:
        self.cache = FileCache(root, "vlm", stats=None)
        self.stats = stats or VlmStats()

    def key(self, request: VlmRequest, model_version: str) -> str:
        return key_for(
            {
                "kind": "vlm",
                "prompt_version": request.prompt_version,
                "model_version": model_version,
                "target": hashlib.sha256(request.target_png).hexdigest(),
                "context": [hashlib.sha256(png).hexdigest() for png in request.context_pngs],
                "route": request.route,
                "direction": request.direction,
                "ocr": request.ocr_text,
            }
        )

    def get(self, request: VlmRequest, model_version: str) -> VlmOpinion | None:
        data = self.cache.get(self.key(request, model_version))
        if not isinstance(data, dict):
            return None
        try:
            opinion = VlmOpinion(**data)
        except TypeError:
            return None
        self.stats.inc("cache_hits")
        return opinion

    def put(self, request: VlmRequest, model_version: str, opinion: VlmOpinion) -> None:
        self.cache.put(self.key(request, model_version), opinion.__dict__)
