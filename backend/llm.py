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
    """LLM tasima/protokol hatasi. status: HTTP kodu (tasima hatasinda None) -
    tuketiciler kalici 4xx ile gecici hatayi ayirt edebilsin (denetim O10)."""

    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


# Akis zaman asimlari (denetim K2): read=None sonsuz bekleme demekti - GPU
# surucusu takilip soket acik kalirsa iptal dahil her sey kilitleniyordu.
# Prefill sirasinda sunucu mesruen sessizdir (buyuk baglam ~10-60sn); 180sn
# "olu okuma" siniri, gercek uretimde token araliklarinin cok ustunde, takili
# sunucuyu ise SONLU surede hataya cevirir.
_STREAM_TIMEOUT = httpx.Timeout(10.0, connect=10.0, read=180.0, write=30.0)


def _frame_error(frame) -> str | None:
    """SSE cercevesindeki sunucu hatasini yakala (llama.cpp: tepe seviye
    {"error": {code,message,...}}). Eskiden bu cerceveler sessizce atlanir,
    kesik yanit basari sanilirdi (denetim O7)."""
    if not isinstance(frame, dict):
        return None
    err = frame.get("error")
    if not err:
        return None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)[:200]
    return str(err)[:200]


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
        was_cancelled = False
        try:
            with httpx.Client(timeout=_STREAM_TIMEOUT) as client:
                with connect_sse(client, "POST", url, json=body, headers=self._headers()) as es:
                    if es.response.status_code >= 400:
                        # Guncel llama.cpp (PR #16486): akis BASLAMADAN olusan hata
                        # (tipik: baglam asimi) stream:true olsa da duz JSON dondurur.
                        # Iterasyona girmeden yakala ki gercek mesaj kullaniciya tasinsin
                        # (iter_sse content-type kontrolu SSEError'a bogar, mesaj kaybolurdu).
                        es.response.read()
                        raise LlamaError(
                            f"llama-server {es.response.status_code}: {es.response.text[:200]}",
                            status=es.response.status_code)
                    saw_done = False
                    for sse in es.iter_sse():
                        if cancel is not None and cancel.is_set():
                            was_cancelled = True
                            break
                        if sse.data == "[DONE]":
                            saw_done = True
                            break
                        try:
                            frame = sse.json()
                        except Exception:
                            continue
                        emsg = _frame_error(frame)
                        if emsg is not None:
                            raise LlamaError(f"llama-server stream error: {emsg}")
                        try:
                            delta = frame["choices"][0]["delta"].get("content", "")
                        except Exception:
                            continue  # choices'siz bilgi cerceveleri (or. timings/usage)
                        if delta:
                            vis = filt.feed(delta)
                            if vis:
                                on_token(vis)
                    if not saw_done and not was_cancelled:
                        # normal biten akis HER ZAMAN [DONE] tasir; tasimayan akis
                        # sessiz kesintidir - basari gibi gecmise islenmemeli
                        raise LlamaError("llama-server stream truncated (no [DONE])")
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
            raise LlamaError(f"llama-server {r.status_code}: {r.text[:200]}",
                             status=r.status_code)
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
            raise LlamaError(f"llama-server {r.status_code}: {r.text[:200]}",
                             status=r.status_code)
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
