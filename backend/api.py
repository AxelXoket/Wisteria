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

from .config import CONFIG, app_dir, load_settings, save_settings, apply_settings_to_config, ensure_api_key
from .llm import LlamaClient, LlamaError
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


class JsApi:
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

    def mark_ready(self, base_url: str) -> None:
        self._client = LlamaClient(base_url)
        self._state = "ready"
        # (TTS boots in main._boot, in parallel with the model - not here.)
        if self._mem_store is not None:  # memory unlocked before the model finished loading
            self._activate_memory()

    # ------------------------------------------------------------ memory setup

    def _try_remembered_unlock(self) -> None:
        try:
            key = self._vault.unlock_remembered()
        except Exception:
            key = None
        if key is not None:
            self._open_memory(key)

    def _open_memory(self, key: bytes) -> bool:
        try:
            CONFIG.memory_dir.mkdir(parents=True, exist_ok=True)
            self._mem_store = MemoryStore.open(CONFIG.memory_dir / "mem.db", key)
        except Exception:
            self._mem_store = None
            return False
        # Prompts live in the same encrypted DB. One-time migration pulls the old
        # plaintext files into the vault (verify-then-delete); the provider then
        # becomes the source of truth for build_system_prompt.
        try:
            migrate_prompts_if_needed(
                self._mem_store, self._store_lock,
                system_file=CONFIG.system_prompt,
                characters_dir=CONFIG.characters_dir,
                personas_dir=CONFIG.personas_dir)
        except Exception:
            pass  # migration failure must never block the unlock
        self._prompts = StorePromptProvider(self._mem_store, self._store_lock)
        set_prompt_provider(self._prompts)
        active_char = self._prompts.get_active("character")
        if active_char:
            self._character = active_char
        self._reset_history()  # rebuild with the REAL system prompt (init used a placeholder)
        self._activate_memory()
        return True

    def _activate_memory(self) -> None:
        """Create the manager once BOTH the store (unlocked) and the client exist."""
        if self._mem_store is None:
            return
        if self._mem is not None:
            if self._client is not None:
                self._mem.set_client(self._client)
            return
        if self._embedder is None:
            try:
                CONFIG.embed_cache_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            self._embedder = Embedder(cache_dir=str(CONFIG.embed_cache_dir))
        self._mem = MemoryManager(
            self._mem_store, self._client, embedder=self._embedder,
            keep_recent=CONFIG.mem_keep_recent, consolidate_every=CONFIG.mem_consolidate_every,
            max_facts=CONFIG.mem_max_facts, recall_k=CONFIG.mem_recall_k,
            recall_max_dist=CONFIG.mem_recall_max_dist, reflect_every=CONFIG.mem_reflect_every,
            lock=self._store_lock,  # one lock for consolidation + prompts + viewer
        )
        threading.Thread(target=self._mem.warmup, daemon=True).start()  # preload embedder off the hot path

    def close_memory(self) -> None:
        # Deliberately NO mem.flush_pending() here: it would run an LLM consolidation
        # pass and stall window close by seconds. Unconsolidated turns stay flagged in
        # the encrypted DB and fold in on the next session's consolidation trigger.
        # The store lock is held so an in-flight consolidation write finishes first
        # (closing mid-write was a race); a post-close write attempt then fails
        # inside the worker's own try/except, which is harmless at exit.
        try:
            with self._store_lock:
                set_prompt_provider(None)  # stale provider must not outlive the connection
                self._prompts = None
                if self._mem_store is not None:
                    self._mem_store.close()
                    self._mem_store = None
        except Exception:
            pass

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

    def new_chat(self) -> dict:
        self._reset_history()
        return {"ok": True}

    def cancel_gen(self) -> dict:
        self._cancel.set()
        TTS.barge_in()
        return {"ok": True}

    def tts_status(self) -> dict:
        return TTS.status()

    def set_tts_enabled(self, on: bool) -> dict:
        CONFIG.tts_enabled = bool(on)
        self._refresh_system_prompt()  # add/remove the [SPOKEN DELIVERY] block live
        st = TTS.set_enabled(bool(on))
        self._persist_tts_settings()
        return st

    # ----- yazi ayarlari (yalnizca sohbet mesajlarinin punto/satir araligi) -----
    # Kilit bekcisi yok: hassas veri degil, salt gorsel tercih. Degerler sinirlanir
    # ki bozuk bir settings.json arayuzu asla kiramasin.
    def ui_text_get(self) -> dict:
        s = load_settings()
        try:
            fp = float(s.get("ui_msg_font_px", 15.5))
            lh = float(s.get("ui_msg_line_height", 1.62))
        except (TypeError, ValueError):
            fp, lh = 15.5, 1.62
        return {"ok": True,
                "font_px": min(19.0, max(13.0, fp)),
                "line_height": min(1.95, max(1.30, lh))}

    def ui_text_set(self, font_px, line_height) -> dict:
        try:
            fp = min(19.0, max(13.0, float(font_px)))
            lh = min(1.95, max(1.30, float(line_height)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "value"}
        s = load_settings()  # MERGE - diger anahtarlara dokunma
        s.update({"ui_msg_font_px": fp, "ui_msg_line_height": lh})
        save_settings(s)
        return {"ok": True, "font_px": fp, "line_height": lh}

    # ----- sohbet arka plani (kullanicinin kirptigi jpg, userdata/ altinda) -----
    # Kopruden data URI gecer; dosya web/ disinda durur ki rebuild'ler silmesin.
    # Not: gorsel duz dosyadir (kasa DISI) - duvar kagidi hassas veri sayilmaz.
    _BG_MAX_B64 = 9_000_000  # ~6.5MB ham veri; kirpilmis 1280w jpg tipik 200-500KB

    def _bg_path(self) -> Path:
        return app_dir() / "userdata" / "chat_bg.jpg"

    def ui_bg_get(self) -> dict:
        s = load_settings()
        try:
            contrast = min(0.85, max(0.0, float(s.get("ui_bg_contrast", 0.35))))
        except (TypeError, ValueError):
            contrast = 0.35
        try:
            lum = min(1.0, max(0.0, float(s.get("ui_bg_lum", 0.5))))
        except (TypeError, ValueError):
            lum = 0.5
        tint = str(s.get("ui_bg_tint", "auto"))
        rect = s.get("ui_bg_rect")
        if not (isinstance(rect, list) and len(rect) == 4):
            rect = [0.0, 0.0, 1.0, 1.0]  # eski kayitlar: tum gorsel odak
        prefs = {"contrast": contrast, "tint": tint, "lum": lum, "rect": rect}
        p = self._bg_path()
        if not p.exists():
            return {"ok": True, "has": False, **prefs}
        try:
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        except Exception:
            return {"ok": True, "has": False, **prefs}
        return {"ok": True, "has": True, "dataurl": "data:image/jpeg;base64," + b64, **prefs}

    def ui_bg_set(self, dataurl: str, lum, rect=None) -> dict:
        """Gorselin ORIJINALI + kullanicinin odak dikdortgeni (normalize 0..1).

        Sabit-oranli on-kirpma YOK: pencere orani degisince JS, odak cercevesine
        capalanip orijinalin gercek cevre pikselleriyle genisler - kadraj kaymaz."""
        if not isinstance(dataurl, str) or not dataurl.startswith("data:image/jpeg;base64,"):
            return {"ok": False, "error": "format"}
        b64 = dataurl.split(",", 1)[1]
        if len(b64) > self._BG_MAX_B64:
            return {"ok": False, "error": "too_big"}
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return {"ok": False, "error": "decode"}
        if not raw.startswith(b"\xff\xd8"):
            return {"ok": False, "error": "format"}  # jpg imzasi sart
        r = [0.0, 0.0, 1.0, 1.0]
        if rect is not None:
            try:
                r = [float(x) for x in rect]
            except (TypeError, ValueError):
                return {"ok": False, "error": "rect"}
            if len(r) != 4 or r[2] <= 0.02 or r[3] <= 0.02 or r[0] < 0 or r[1] < 0 \
                    or r[0] + r[2] > 1.001 or r[1] + r[3] > 1.001:
                return {"ok": False, "error": "rect"}
        p = self._bg_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        except Exception as exc:
            return {"ok": False, "error": f"write:{exc}"}
        try:
            l = min(1.0, max(0.0, float(lum)))
        except (TypeError, ValueError):
            l = 0.5
        s = load_settings()  # MERGE
        s.update({"ui_bg_lum": l, "ui_bg_rect": r})
        save_settings(s)
        return {"ok": True}

    def ui_bg_clear(self) -> dict:
        try:
            p = self._bg_path()
            if p.exists():
                p.unlink()
        except Exception as exc:
            return {"ok": False, "error": f"delete:{exc}"}
        return {"ok": True}

    def ui_bg_prefs(self, contrast, tint) -> dict:
        try:
            c = min(0.85, max(0.0, float(contrast)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "value"}
        t = str(tint or "auto").strip().lower()
        if t != "auto" and not re.fullmatch(r"#[0-9a-f]{6}", t):
            return {"ok": False, "error": "tint"}
        s = load_settings()
        s.update({"ui_bg_contrast": c, "ui_bg_tint": t})
        save_settings(s)
        return {"ok": True, "contrast": c, "tint": t}

    def _persist_tts_settings(self) -> None:
        s = load_settings()  # MERGE - never clobber the window w/h keys
        s.update({
            "tts_auto": bool(CONFIG.tts_enabled),
            "tts_speed": float(CONFIG.tts_speed),
            "tts_denoise_prop": float(CONFIG.tts_denoise_prop),
            "tts_exaggeration": float(CONFIG.tts_exaggeration),
        })
        save_settings(s)

    def speak_message(self, text: str) -> dict:
        """Per-message playback from the ▸ button (works with auto-speak off)."""
        return {"ok": TTS.speak_text(text or "")}

    def stop_speaking(self) -> dict:
        TTS.barge_in()
        return {"ok": True}

    def tts_get_params(self) -> dict:
        st = TTS.status()
        return {
            "ok": True,
            "auto": st["auto"],
            "state": st["state"],
            "speed": float(CONFIG.tts_speed),
            "denoise_prop": float(CONFIG.tts_denoise_prop),
            "exaggeration": float(CONFIG.tts_exaggeration),
        }

    def tts_set_params(self, params: dict) -> dict:
        params = params or {}

        def clamp(v, lo, hi):
            return max(lo, min(hi, float(v)))

        changed = {}
        try:
            if "speed" in params:
                CONFIG.tts_speed = changed["speed"] = clamp(params["speed"], 0.9, 1.3)
            if "denoise_prop" in params:
                CONFIG.tts_denoise_prop = changed["denoise_prop"] = clamp(params["denoise_prop"], 0.0, 0.95)
            if "exaggeration" in params:
                CONFIG.tts_exaggeration = changed["exaggeration"] = clamp(params["exaggeration"], 0.25, 1.2)
        except (TypeError, ValueError):
            return {"ok": False, "error": "value"}
        if changed:
            TTS.update_params(changed)  # live, no worker reload
        if "auto" in params:
            self.set_tts_enabled(bool(params["auto"]))  # handles voice block + persist
        else:
            self._persist_tts_settings()
        return self.tts_get_params()

    def _refresh_system_prompt(self) -> None:
        """Rebuild the system prompt in place (keeps the conversation history)."""
        if not self._messages:
            return
        first = self._messages[0]
        if first.get("role") in ("system", "user"):
            first["content"] = build_system_prompt(self._character)

    # ----- memory unlock (called by the lock screen) -----
    def memory_state(self) -> dict:
        if self._vault is None:
            return {"enabled": False, "initialized": False, "unlocked": False}
        return {
            "enabled": True,
            "initialized": self._vault.is_initialized(),
            "unlocked": self._mem_store is not None,
        }

    def memory_unlock(self, passphrase: str, remember: bool = False) -> dict:
        if self._vault is None:
            return {"ok": False, "error": "disabled"}
        passphrase = (passphrase or "").strip()
        if not passphrase:
            return {"ok": False, "error": "empty"}
        with self._mem_lock:
            if self._mem_store is not None:
                return {"ok": True}
            try:
                if self._vault.is_initialized():
                    key = self._vault.unlock(passphrase)
                    if key is None:
                        return {"ok": False, "error": "wrong"}
                else:
                    key = self._vault.initialize(passphrase)  # first run: create
            except Exception:
                return {"ok": False, "error": "vault"}
            if not self._open_memory(key):
                return {"ok": False, "error": "open"}
            if remember:
                try:
                    self._vault.remember(key)
                except Exception:
                    pass
        return {"ok": True}

    # ----- memory viewer (three-dot menu > Hafiza) -----
    def memory_overview(self) -> dict:
        if self._mem_store is None:
            return {"ok": False, "error": "locked"}
        if self._mem is not None:
            return {"ok": True, **self._mem.overview()}
        # unlocked but the model is still loading (manager not built yet)
        with self._store_lock:
            recap = self._mem_store.get_recap()
            facts = self._mem_store.list_facts()
            episodes = self._mem_store.list_episodes(100)
            count = self._mem_store.message_count()
        return {
            "ok": True,
            "recap": recap,
            "facts": [
                {"id": i, "type": t, "text": x, "importance": imp}
                for (i, t, x, imp) in facts
            ],
            "episodes": [{"text": t, "ts": ts} for (t, ts) in episodes],
            "message_count": count,
        }

    # Elle bilgi ekleme/duzenleme: store add_fact/update_fact zaten mevcut; manager
    # da ayni store kilidiyle calisir (onbellek yok) - dogrudan store yolu guvenli.
    _FACT_TYPES = ("bilgi", "identity", "preference", "milestone")

    def memory_add_fact(self, type_: str, text: str, importance) -> dict:
        if self._mem_store is None:
            return {"ok": False, "error": "locked"}
        text = (text or "").strip()
        if not text or len(text) > 2000:
            return {"ok": False, "error": "text"}
        t = str(type_ or "bilgi").strip().lower()
        if t not in self._FACT_TYPES:
            t = "bilgi"
        try:
            imp = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            imp = 7
        try:
            with self._store_lock:
                fid = self._mem_store.add_fact(t, text, imp, int(time.time()))
        except Exception:
            return {"ok": False, "error": "add"}
        return {"ok": True, "id": fid}

    def memory_update_fact(self, fid, text: str, importance) -> dict:
        if self._mem_store is None:
            return {"ok": False, "error": "locked"}
        text = (text or "").strip()
        if not text or len(text) > 2000:
            return {"ok": False, "error": "text"}
        try:
            imp = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "importance"}
        try:
            with self._store_lock:
                self._mem_store.update_fact(int(fid), text, imp, int(time.time()))
        except Exception:
            return {"ok": False, "error": "update"}
        return {"ok": True}

    def memory_delete_fact(self, fid: int) -> dict:
        if self._mem_store is None:
            return {"ok": False, "error": "locked"}
        try:
            if self._mem is not None:
                self._mem.delete_fact(fid)
            else:
                with self._store_lock:
                    self._mem_store.deactivate_fact(int(fid), int(time.time()))
        except Exception:
            return {"ok": False, "error": "delete"}
        return {"ok": True}

    # ----- encrypted prompts (three-dot menu > Promptlar) -----
    _SLUG_RE = re.compile(r"^[a-z0-9_-]{1,40}$")

    def prompts_list(self) -> dict:
        if self._prompts is None:
            return {"ok": False, "error": "locked"}
        kinds = {k: sorted(self._prompts.list(k)) for k in KINDS}
        active_persona = self._prompts.get_active("persona")
        return {
            "ok": True,
            "kinds": kinds,
            "active": {
                "system": CONFIG.system_prompt.stem,
                "character": self._character,
                "persona": active_persona or "",
            },
        }

    def prompts_get(self, kind: str, name: str) -> dict:
        if self._prompts is None:
            return {"ok": False, "error": "locked"}
        if kind not in KINDS:
            return {"ok": False, "error": "kind"}
        text = self._prompts.get(kind, str(name or ""))
        return {"ok": True, "text": text if text is not None else ""}

    def prompts_save(self, kind: str, name: str, text: str) -> dict:
        if self._prompts is None:
            return {"ok": False, "error": "locked"}
        if kind not in KINDS:
            return {"ok": False, "error": "kind"}
        text = text if isinstance(text, str) else ""
        if len(text) > 512_000:
            return {"ok": False, "error": "too_big"}
        self._prompts.save(kind, str(name or ""), text)
        self._refresh_system_prompt()  # applies live to the ongoing conversation
        return {"ok": True}

    def prompts_create(self, kind: str, name: str) -> dict:
        if self._prompts is None:
            return {"ok": False, "error": "locked"}
        if kind not in ("character", "persona"):
            return {"ok": False, "error": "kind"}
        slug = (name or "").strip().lower().replace(" ", "_")
        slug = re.sub(r"[^a-z0-9_-]", "", slug)  # mirror the UI's input filter
        if not self._SLUG_RE.match(slug):
            return {"ok": False, "error": "name"}
        if self._prompts.get(kind, slug) is not None:
            return {"ok": False, "error": "exists"}
        self._prompts.save(kind, slug, "")  # born encrypted, like everything else
        return {"ok": True, "name": slug}

    def prompts_set_active(self, kind: str, name: str) -> dict:
        if self._prompts is None:
            return {"ok": False, "error": "locked"}
        if kind not in ("character", "persona"):
            return {"ok": False, "error": "kind"}
        name = str(name or "")
        if self._prompts.get(kind, name) is None:
            return {"ok": False, "error": "not_found"}
        self._prompts.set_active(kind, name)
        if kind == "character":
            self._character = name
        self._refresh_system_prompt()
        return {"ok": True}

    def prompts_rename(self, kind: str, old: str, new: str) -> dict:
        if self._prompts is None:
            return {"ok": False, "error": "locked"}
        if kind not in ("character", "persona"):
            return {"ok": False, "error": "kind"}  # sistem promptunun adi sabit (lookup anahtari)
        old = str(old or "")
        slug = (new or "").strip().lower().replace(" ", "_")
        slug = re.sub(r"[^a-z0-9_-]", "", slug)  # mirror the UI's input filter
        if not self._SLUG_RE.match(slug):
            return {"ok": False, "error": "name"}
        if slug == old:
            return {"ok": True, "name": slug}
        if self._prompts.get(kind, old) is None:
            return {"ok": False, "error": "not_found"}
        if self._prompts.get(kind, slug) is not None:
            return {"ok": False, "error": "exists"}
        self._prompts.rename(kind, old, slug)
        if kind == "character" and self._character == old:
            self._character = slug
            self._refresh_system_prompt()  # gorunen ad degisti - name swap guncellensin
        return {"ok": True, "name": slug}

    def prompts_delete(self, kind: str, name: str) -> dict:
        if self._prompts is None:
            return {"ok": False, "error": "locked"}
        if kind not in ("character", "persona"):
            return {"ok": False, "error": "kind"}
        name = str(name or "")
        if self._prompts.get(kind, name) is None:
            return {"ok": False, "error": "not_found"}
        if len(self._prompts.list(kind)) <= 1:
            return {"ok": False, "error": "last"}  # turun sonuncusu silinemez
        self._prompts.delete(kind, name)
        active = self._prompts.get_active(kind) or ""
        if kind == "character" and self._character == name:
            self._character = active  # devir: alfabetik ilk hayatta kalan
            self._refresh_system_prompt()
        return {"ok": True, "active": active}

    def prompts_export(self, kind: str, name: str) -> dict:
        if self._prompts is None:
            return {"ok": False, "error": "locked"}
        if kind not in KINDS:
            return {"ok": False, "error": "kind"}
        text = self._prompts.get(kind, str(name or ""))
        if text is None:
            return {"ok": False, "error": "not_found"}
        if self._window is None:
            return {"ok": False, "error": "no_window"}
        try:
            import webview
            res = self._window.create_file_dialog(
                webview.FileDialog.SAVE,
                save_filename=f"{name}.txt",
                file_types=("Metin dosyasi (*.txt)",),
            )
        except Exception as exc:
            return {"ok": False, "error": f"dialog:{exc}"}
        if not res:  # None or empty tuple = user cancelled
            return {"ok": False, "error": "cancelled"}
        path = res[0] if isinstance(res, (tuple, list)) else res
        try:
            Path(path).write_text(text, encoding="utf-8")
        except Exception as exc:
            return {"ok": False, "error": f"write:{exc}"}
        return {"ok": True, "path": str(path)}

    def export_chat(self) -> dict:
        """Gorunen sohbeti .txt olarak kaydet (prompts_export'un diyalog deseni).

        Sentetik onek disari SIZMAZ: use_system_role True iken messages[0] sistem
        promptudur, False iken [0..1] user(sistem)+assistant("Understood.") ciftidir."""
        skip = 1 if CONFIG.use_system_role else 2
        msgs = list(self._messages[skip:])  # akisla yarismasin diye kopya
        parts = []
        who_ai = character_display_name(self._character)
        for m in msgs:
            text = (m.get("content") or "").strip()
            if not text:
                continue
            who = "Sen" if m.get("role") == "user" else who_ai
            parts.append(f"{who}:\n{text}")
        if not parts:
            return {"ok": False, "error": "empty"}
        body = "\n\n".join(parts) + "\n"
        if self._window is None:
            return {"ok": False, "error": "no_window"}
        try:
            import webview
            res = self._window.create_file_dialog(
                webview.FileDialog.SAVE,
                save_filename=time.strftime("sohbet-%Y%m%d-%H%M.txt"),
                file_types=("Metin dosyasi (*.txt)",),
            )
        except Exception as exc:
            return {"ok": False, "error": f"dialog:{exc}"}
        if not res:  # None or empty tuple = user cancelled
            return {"ok": False, "error": "cancelled"}
        path = res[0] if isinstance(res, (tuple, list)) else res
        try:
            Path(path).write_text(body, encoding="utf-8")
        except Exception as exc:
            return {"ok": False, "error": f"write:{exc}"}
        return {"ok": True, "path": str(path)}

    def send(self, text: str, image_data_url: str | None = None) -> dict:
        if self._state != "ready" or self._client is None:
            return {"ok": False, "error": "not_ready"}
        if CONFIG.memory_enabled and self._mem_store is None:
            return {"ok": False, "error": "locked"}
        with self._busy_guard:  # atomic check-and-set: two rapid sends can't both pass
            if self._busy:
                return {"ok": False, "error": "busy"}
            self._busy = True
        threading.Thread(target=self._run, args=(text or "", image_data_url), daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------- internals

    def _js(self, code: str) -> None:
        if self._window is None:
            return
        try:
            self._window.evaluate_js(code)
        except Exception:
            pass

    def _emit(self, fn: str, payload) -> None:
        self._js(f"window.{fn}({json.dumps(payload, ensure_ascii=True)})")

    def _run(self, text: str, image_data_url: str | None) -> None:
        # (self._busy was set atomically in send(); cleared in the finally below.)
        self._cancel.clear()
        TTS.barge_in()  # stop any speech still draining from a previous turn
        user_text = text.strip()

        # ---- build the LLM turn + a short history placeholder ----
        pending_content = user_text
        hist_user = user_text
        note = None
        try:
            if user_text.lower().startswith("/ara "):
                query = user_text[5:].strip()
                self._emit("appNote", f"Araştırılıyor: {query}")
                res = research.gather_evidence(query)
                ev = res.get("evidence") or []
                if res.get("error") and not ev:
                    note = {"searxng_unreachable": "SearXNG çalışmıyor.",
                            "uv_missing": "Araştırma modülü bulunamadı.",
                            "research_dir_missing": "Araştırma klasörü yok.",
                            "timeout": "Araştırma zaman aşımına uğradı."}.get(res["error"], "Kaynak bulunamadı.")
                    pending_content = (
                        "[The user asked you to look something up, but no reliable web source was found. "
                        "Do not invent facts; stay in character and be honest if unsure.] " + query)
                else:
                    self._emit("appSources", [{"id": e.get("id"), "domain": e.get("domain"),
                                               "url": e.get("url")} for e in ev])
                    pending_content = research.build_inject(ev, query)
                hist_user = f"[The user asked you to look up: {query}]"

            elif image_data_url:
                try:
                    data_uri = images.from_data_url(image_data_url)
                except Exception:
                    data_uri = None
                if data_uri and CONFIG.use_vision_observation:
                    self._emit("appNote", "Görsel inceleniyor...")
                    try:
                        obs = vision.observe_image(self._client, data_uri)
                    except LlamaError:
                        obs = ""
                    q = user_text or "React naturally to this image, in character."
                    pending_content = build_vision_inject(obs, q) if obs else (
                        "[The user shared an image but it could not be analyzed. React in character, "
                        "without inventing what it shows.] " + q)
                    hist_user = f"[User sent an image.] {user_text}".strip()
                else:
                    pending_content = user_text or "React naturally to this image."
                    hist_user = f"[User sent an image.] {user_text}".strip()

            if note:
                self._emit("appNote", note)

            # inject long-term memory for this turn (facts + recap + query-relevant recall)
            mem_block = ""
            if self._mem is not None:
                try:
                    mem_block = self._mem.build_block(user_text)
                except Exception:
                    mem_block = ""
            if mem_block and CONFIG.use_system_role:
                llm_messages = self._messages + [{"role": "system", "content": mem_block},
                                                {"role": "user", "content": pending_content}]
            elif mem_block:
                llm_messages = self._messages + [
                    {"role": "user", "content": mem_block + "\n\n" + pending_content}]
            else:
                llm_messages = self._messages + [{"role": "user", "content": pending_content}]
            self._emit("appStreamStart", {})
            TTS.begin_turn()  # start the speech pipeline for this reply
            buf: list[str] = []
            lock = threading.Lock()
            stop = threading.Event()

            def flush() -> None:
                with lock:
                    chunk = "".join(buf); buf.clear()
                if chunk:
                    self._emit("appStream", chunk)

            def flusher() -> None:
                while not stop.is_set():
                    time.sleep(0.04)
                    flush()

            ft = threading.Thread(target=flusher, daemon=True)
            ft.start()

            def on_token(d: str) -> None:
                with lock:
                    buf.append(d)
                TTS.feed(d)  # speak as it streams (no-op if TTS is off)

            try:
                final = self._client.stream_chat(llm_messages, CONFIG.chat_preset, on_token, self._cancel)
            except LlamaError as exc:
                final = f"[Hata] {exc}"
            finally:
                stop.set()
                ft.join(timeout=0.3)
                flush()

            was_cancelled = self._cancel.is_set()
            is_error = not final.strip() or final.startswith("[Hata]")
            if not final.strip():
                final = "[Boş yanıt.]"
            self._emit("appStreamEnd", {"final": final})
            if is_error or was_cancelled:
                TTS.barge_in()  # never voice an error placeholder / cancelled tail
            else:
                TTS.end_turn()  # flush the last sentence to the speaker
            # Failed turns stay OUT of the LLM context; cancelled partials stay in the
            # session (she "said" them) but are never folded into long-term memory -
            # otherwise "[Hata] ..." or half-sentences pollute the recap/fact ledger.
            if not is_error:
                self._messages.append({"role": "user", "content": hist_user})
                self._messages.append({"role": "assistant", "content": final})
                if self._mem is not None and not was_cancelled:
                    try:
                        self._mem.record_turn(hist_user, final)  # persist + async-consolidate
                    except Exception:
                        pass
        except Exception as exc:  # backstop: never let a turn die silently mid-UI
            try:
                self._emit("appStreamEnd", {"final": f"[Hata] {type(exc).__name__}"})
            except Exception:
                pass
        finally:
            self._busy = False
