"""Minimal Ollama client with JSON-schema structured output."""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import httpx


def model_tag(name: str) -> str:
    """Normalize an Ollama model name: no tag means ":latest"."""
    name = name.strip()
    return name if ":" in name else f"{name}:latest"


class ChatModel(Protocol):
    def json_chat(self, system: str, user: str, schema: dict) -> dict: ...


class OllamaModel:
    def __init__(
        self,
        host: str,
        model: str,
        num_ctx: int = 16384,
        timeout_s: float = 120,
        think: bool = False,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout_s = timeout_s
        self.think = think
        self.attempts = 2
        self.backoff_s = 2.0

    def json_chat(self, system: str, user: str, schema: dict) -> dict:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "think": self.think,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout_s)
                r.raise_for_status()
                return json.loads(r.json()["message"]["content"])
            except (httpx.TimeoutException, httpx.TransportError, ValueError, KeyError) as e:
                last = e  # transient: model loading, a dropped connection, truncated JSON
                if attempt + 1 < self.attempts:
                    time.sleep(self.backoff_s * (attempt + 1))
        raise RuntimeError(f"local model failed after {self.attempts} attempts: {last}") from last

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=5)
            names = {model_tag(m["name"]) for m in r.json().get("models", [])}
            return model_tag(self.model) in names
        except (httpx.HTTPError, ValueError, KeyError):
            return False
