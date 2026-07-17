"""JsApi - the object exposed to the WebView UI (window.pywebview.api.*).

The frontend calls send()/cancel()/status(); the backend streams tokens back into
the page via time-batched window.evaluate_js. All generation runs on worker threads
so the GUI loop never blocks.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from . import images, research, vision
import base64

from .config import CONFIG, app_dir, load_settings, apply_settings_to_config, ensure_api_key
from .llm import LlamaClient, LlamaError
from .logutil import err_brief, log_for
from .memory.constants import MANUAL_FACT_TYPES
from .memory.crypto import KeyVault
from .memory.embedder import Embedder
from .memory.manager import MemoryManager
from .memory.store import MemoryStore
from .prompt_store import KINDS, StorePromptProvider, migrate_prompts_if_needed
from .tts import TTS
from .prompts import (
    build_system_prompt,
    build_vision_inject,
    character_display_name,
    list_characters,
    set_prompt_provider,
)
from .server import ServerManager


from .api_parts.chat_api import ChatApiMixin
from .api_parts.gen_api import GenApiMixin
from .api_parts.memory_api import MemoryApiMixin
from .api_parts.prefs_api import PrefsApiMixin
from .api_parts.prompts_api import PromptsApiMixin
from .api_parts.tts_api import TtsApiMixin

_log = log_for("api")


class JsApi(ChatApiMixin, MemoryApiMixin, PromptsApiMixin,
            TtsApiMixin, PrefsApiMixin, GenApiMixin):
    def __init__(self, server: ServerManager) -> None:
        self._server = server
        self._window = None
        self._client: LlamaClient | None = None
        self._character = CONFIG.default_character
        self._messages: list[dict] = []
        self._cancel = threading.Event()
        self._state = "loading"
        self._detail = ""
        self._busy = False
        self._busy_guard = threading.Lock()  # send() check-and-set is atomic
        self._boot_hook = None   # main.py baglar: hata ekranindaki "Yeniden dene"
        self._js_fails = 0       # evaluate_js hata sayaci (log kirpma)
        # Persisted user settings (voice tuning + auto-speak) overlay CONFIG before
        # the first system-prompt build so the [SPOKEN DELIVERY] block is consistent.
        apply_settings_to_config(load_settings(), CONFIG)
        ensure_api_key(CONFIG)  # sidecar auth: settings-based, generated on fresh installs
        self._reset_history()

        # --- long-term memory (encrypted, passphrase-locked) ---
        self._mem_lock = threading.Lock()
        self._store_lock = threading.RLock()  # guards the ONE apsw connection app-wide
        self._vault: KeyVault | None = KeyVault(CONFIG.memory_dir) if CONFIG.memory_enabled else None
        self._mem_store: MemoryStore | None = None
        self._embedder: Embedder | None = None
        self._mem: MemoryManager | None = None
        self._prompts: StorePromptProvider | None = None
        if self._vault is not None:
            self._try_remembered_unlock()  # "remember on this device" -> auto-unlock, no lock screen

    # ------------------------------------------------------------- lifecycle

    def set_window(self, window) -> None:
        self._window = window

    def set_boot_hook(self, fn) -> None:
        self._boot_hook = fn

    def retry_boot(self) -> dict:
        """Hata ekranindaki "Yeniden dene": boot dizisini yeniden kosar.

        Eskiden hata durumu TERMINALDI - yavas bir soguk acilis yanlis siniflanirsa
        tek care uygulamayi kapatip acmakti. Yalniz state=='error' iken calisir."""
        if self._state != "error" or self._boot_hook is None:
            return {"ok": False, "error": "state"}
        with self._busy_guard:
            if self._busy:
                return {"ok": False, "error": "busy"}
            self._busy = True
        self._state = "loading"
        self._detail = ""

        def _run_boot() -> None:
            try:
                self._boot_hook()
            except Exception as e:
                _log.error("retry_boot istisna err=%s", err_brief(e))
                self.mark_error("retry_failed")
            finally:
                with self._busy_guard:
                    self._busy = False

        threading.Thread(target=_run_boot, daemon=True).start()
        return {"ok": True}

    def mark_ready(self, base_url: str) -> None:
        self._client = LlamaClient(base_url)
        self._state = "ready"
        # (TTS boots in main._boot, in parallel with the model - not here.)
        if self._mem_store is not None:  # memory unlocked before the model finished loading
            self._activate_memory()

    # ------------------------------------------------------------ memory setup

    def mark_error(self, detail: str) -> None:
        self._state = "error"
        self._detail = detail

    def _reset_history(self) -> None:
        sys_prompt = build_system_prompt(self._character)
        if CONFIG.use_system_role:
            self._messages = [{"role": "system", "content": sys_prompt}]
        else:
            self._messages = [
                {"role": "user", "content": sys_prompt},
                {"role": "assistant", "content": "Understood."},
            ]

    # ------------------------------------------------------- exposed to JS

    def status(self) -> dict:
        return {
            "state": self._state,
            "detail": self._detail,
            "character": character_display_name(self._character),
            "characters": [character_display_name(c) for c in list_characters()],
        }

    def _refresh_system_prompt(self) -> None:
        """Rebuild the system prompt in place (keeps the conversation history)."""
        if not self._messages:
            return
        first = self._messages[0]
        if first.get("role") in ("system", "user"):
            first["content"] = build_system_prompt(self._character)

    # ----- memory unlock (called by the lock screen) -----
    def _js(self, code: str) -> None:
        if self._window is None:
            return
        try:
            self._window.evaluate_js(code)
        except Exception as e:
            # olmus kopru artik izsiz degil; kapanista dogal hatalar spam
            # yapmasin diye ilk 5 + her 100. kayit yazilir
            self._js_fails += 1
            if self._js_fails <= 5 or self._js_fails % 100 == 0:
                _log.warning("evaluate_js hatasi #%d err=%s", self._js_fails, err_brief(e))

    def _emit(self, fn: str, payload) -> None:
        self._js(f"window.{fn}({json.dumps(payload, ensure_ascii=True)})")

