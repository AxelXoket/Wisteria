"""llama-server client: streaming chat (SSE) + non-streaming completion.

Runs on a worker thread (pywebview GUI loop stays on the main thread). Hidden
<observe>/<think> spans are filtered out of the visible stream as they arrive.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict
from typing import Callable

import httpx
from httpx_sse import connect_sse

from .config import CONFIG, GenPreset
from .sanitize import StreamFilter, final_clean

Message = dict


class LlamaError(RuntimeError):
    pass


class LlamaClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if CONFIG.api_key:
            h["Authorization"] = f"Bearer {CONFIG.api_key}"
        return h

    def _body(self, messages: list[Message], preset: GenPreset, stream: bool) -> dict:
        p = asdict(preset)
        return {
            "model": CONFIG.alias,
            "messages": messages,
            "temperature": p["temperature"],
            "top_p": p["top_p"],
            "top_k": p["top_k"],
            "min_p": p["min_p"],
            "repeat_penalty": p["repeat_penalty"],
            "max_tokens": p["max_tokens"],
            "stream": stream,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def stream_chat(
        self,
        messages: list[Message],
        preset: GenPreset,
        on_token: Callable[[str], None],
        cancel: threading.Event | None = None,
    ) -> str:
        """Stream a reply; call on_token(visible_delta) as it arrives; return final text."""
        filt = StreamFilter()
        body = self._body(messages, preset, stream=True)
        url = f"{self.base_url}/v1/chat/completions"
        try:
            with httpx.Client(timeout=httpx.Timeout(None, connect=10)) as client:
                with connect_sse(client, "POST", url, json=body, headers=self._headers()) as es:
                    for sse in es.iter_sse():
                        if cancel is not None and cancel.is_set():
                            break
                        if sse.data == "[DONE]":
                            break
                        try:
                            delta = sse.json()["choices"][0]["delta"].get("content", "")
                        except Exception:
                            continue
                        if delta:
                            vis = filt.feed(delta)
                            if vis:
                                on_token(vis)
        except httpx.HTTPError as exc:
            raise LlamaError(f"llama-server stream error: {exc.__class__.__name__}") from exc

        tail = filt.flush_tail()
        if tail:
            on_token(tail)
        return final_clean(filt.raw)

    def complete(self, messages: list[Message], preset: GenPreset) -> str:
        """Non-streaming completion (used for the low-temp vision observation)."""
        body = self._body(messages, preset, stream=False)
        url = f"{self.base_url}/v1/chat/completions"
        try:
            with httpx.Client(timeout=180) as client:
                r = client.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise LlamaError(f"llama-server error: {exc.__class__.__name__}") from exc
        if r.status_code >= 400:
            raise LlamaError(f"llama-server {r.status_code}: {r.text[:200]}")
        try:
            content = r.json()["choices"][0]["message"]["content"] or ""
        except Exception:
            content = ""
        return final_clean(content)

    def complete_json(self, messages: list[Message], preset: GenPreset, schema: dict) -> dict:
        """Non-streaming call constrained to a JSON schema (llama.cpp builds a grammar
        from it, so the output is always valid JSON). Used by the memory summarizer."""
        body = self._body(messages, preset, stream=False)
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "memory", "schema": schema}}
        url = f"{self.base_url}/v1/chat/completions"
        try:
            with httpx.Client(timeout=180) as client:
                r = client.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise LlamaError(f"llama-server error: {exc.__class__.__name__}") from exc
        if r.status_code >= 400:
            raise LlamaError(f"llama-server {r.status_code}: {r.text[:200]}")
        try:
            content = r.json()["choices"][0]["message"]["content"] or "{}"
        except Exception:
            content = "{}"
        try:
            return json.loads(content)
        except Exception:
            m = re.search(r"\{.*\}", content, re.S)  # salvage if a stray token slipped in
            try:
                return json.loads(m.group(0)) if m else {}
            except Exception:
                return {}
