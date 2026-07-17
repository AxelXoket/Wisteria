"""Sohbet akisi mixin'i"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .. import images, research, vision
from ..config import CONFIG
from ..llm import LlamaError
from ..logutil import err_brief, log_for
from ..prompts import OBSERVATION_ASK, OBSERVATION_SYSTEM, build_vision_inject, character_display_name
from ..tts import TTS

_log = log_for("chat")


class ChatApiMixin:
    def new_chat(self) -> dict:
        # Akis surerken sifirlama YASAK (denetim Y1): _reset_history listeyi
        # degistirir, akis bitiminde eski tur YENI sohbetin gecmisine eklenirdi -
        # bos gorunen arayuzun arkasinda model gorunmez bir turu bilirdi.
        with self._busy_guard:
            if self._busy:
                return {"ok": False, "error": "busy"}
            self._reset_history()
        return {"ok": True}

    def cancel_gen(self) -> dict:
        self._cancel.set()
        TTS.barge_in()
        return {"ok": True}

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

    def _run(self, text: str, image_data_url: str | None) -> None:
        # (self._busy was set atomically in send(); cleared in the finally below.)
        # busy_cleared: _busy'yi emit'ten ONCE birakinca (sira sozlesmesi O1),
        # emit suresince yeni bir send busy'yi sahiplenebilir - finally'nin
        # kosulsuz False yazmasi o YENI turun kilidini ezerdi (cift _run kapisi).
        busy_cleared = False
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
                except Exception as e:
                    # hafizasiz devam etmek dogru; ama artik izsiz degil
                    _log.warning("hafiza blogu kurulamadi err=%s", err_brief(e))
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
                    except Exception as e:
                        _log.warning("record_turn basarisiz err=%s", err_brief(e))
            # SIRA SOZLESMESI (denetim O1): _busy, appStreamEnd YAYINLANMADAN once
            # temizlenir. JS composer'i StreamEnd'de acar; eski sirada aradaki
            # milisaniye-saniye penceresinde atilan mesaj "busy" ile reddedilir,
            # baloncugu ekranda kalir ama sohbete hic girmezdi (sessiz kayip).
            self._busy = False
            busy_cleared = True
            self._emit("appStreamEnd", {"final": final})
        except Exception as exc:  # backstop: never let a turn die silently mid-UI
            _log.error("tur backstop istisnasi err=%s", err_brief(exc))
            if not busy_cleared:
                self._busy = False
                busy_cleared = True
            try:
                self._emit("appStreamEnd", {"final": f"[Hata] {type(exc).__name__}"})
            except Exception:
                pass
        finally:
            if not busy_cleared:  # erken cikis emniyeti; sahiplik ezmesi YOK
                self._busy = False
