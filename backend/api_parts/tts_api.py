"""Ses mixin'i"""

from __future__ import annotations

from ..config import CONFIG, update_settings
from ..tts import TTS


class TtsApiMixin:
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
    def _persist_tts_settings(self) -> None:
        update_settings({
            "tts_auto": bool(CONFIG.tts_enabled),
            "tts_speed": float(CONFIG.tts_speed),
            "tts_denoise_prop": float(CONFIG.tts_denoise_prop),
            "tts_exaggeration": float(CONFIG.tts_exaggeration),
        })

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

