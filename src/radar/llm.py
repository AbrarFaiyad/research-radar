"""Minimal Ollama client with JSON-schema structured output."""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx


class ChatModel(Protocol):
    def json_chat(self, system: str, user: str, schema: dict) -> dict: ...


class OllamaModel:
    def __init__(self, host: str, model: str, num_ctx: int = 16384, timeout_s: float = 120,
                 think: bool = False):
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout_s = timeout_s
        self.think = think

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
        r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout_s)
        r.raise_for_status()
        content = r.json()["message"]["content"]
        return json.loads(content)

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=5)
            names = {m["name"].split(":")[0] for m in r.json().get("models", [])}
            return self.model.split(":")[0] in names
        except (httpx.HTTPError, ValueError, KeyError):
            return False
