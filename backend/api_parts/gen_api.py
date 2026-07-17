"""Uretim mixin'i"""

from __future__ import annotations

import math
import threading

from ..config import CONFIG, update_settings
from ..logutil import err_brief, log_for

_log = log_for("gen")


class GenApiMixin:
    # ----- uretim ayarlari: algilanan model bilgisi + ornekleme + baglam -----
    _GEN_LIMITS = {
        "temperature":    (0.1, 2.0, 0.05),
        "top_p":          (0.5, 1.0, 0.01),
        "top_k":          (1,   200, 1),
        "min_p":          (0.0, 0.5, 0.005),
        "repeat_penalty": (1.0, 1.5, 0.01),
        "max_tokens":     (256, 8192, 256),
    }
    _model_probe = None  # sunucu oturumu basina bir kez /props + /v1/models

    def _probe_model(self) -> dict | None:
        """Yuklu modelin kimligini SUNUCUDAN olc (tahmin yok): egitim maks baglami,
        parametre sayisi, agirlik boyutu, modaliteler. Hazir degilse None."""
        if self._state != "ready":
            return None
        if self._model_probe is not None:
            return self._model_probe
        try:
            import httpx
            base = self._server.base_url
            h = {"Authorization": f"Bearer {CONFIG.api_key}"}
            props = httpx.get(base + "/props", headers=h, timeout=6).json()
            models = httpx.get(base + "/v1/models", headers=h, timeout=6).json()
            meta = (models.get("data") or [{}])[0].get("meta") or {}
            self._model_probe = {
                "alias": str(props.get("model_alias") or ""),
                "n_ctx_train": int(meta.get("n_ctx_train") or 0) or None,
                "n_params": int(meta.get("n_params") or 0) or None,
                "size_bytes": int(meta.get("size") or 0) or None,
                "vision": bool((props.get("modalities") or {}).get("vision")),
            }
        except Exception as e:
            _log.warning("model probe basarisiz err=%s", err_brief(e))
            return None
        return self._model_probe

    def gen_get(self) -> dict:
        p = CONFIG.chat_preset
        return {
            "ok": True,
            "state": self._state,
            "detail": self._detail,
            "sampling": {k: getattr(p, k) for k in self._GEN_LIMITS},
            "limits": {k: list(v) for k, v in self._GEN_LIMITS.items()},
            "ctx_active": int(CONFIG.n_ctx),
            "model": self._probe_model(),
        }

    def gen_set(self, sampling) -> dict:
        """Ornekleme parametreleri: ANINDA gecerli (sonraki mesaj) + kalici."""
        if not isinstance(sampling, dict):
            return {"ok": False, "error": "value"}
        patch = {}
        applied = {}
        for k, (lo, hi, _st) in self._GEN_LIMITS.items():
            if k not in sampling:
                continue
            try:
                v = float(sampling[k])
            except (TypeError, ValueError):
                return {"ok": False, "error": f"value:{k}"}
            if not math.isfinite(v):
                return {"ok": False, "error": f"value:{k}"}
            v = min(hi, max(lo, v))
            if k in ("top_k", "max_tokens"):
                v = int(v)
            setattr(CONFIG.chat_preset, k, v)
            patch["gen_" + k] = v
            applied[k] = v
        if patch:
            update_settings(patch)
        return {"ok": True, "applied": applied}

    def gen_ctx_apply(self, n_ctx) -> dict:
        """Baglami degistir = sidecar'i yeni -c ile GUVENLI yeniden baslat.
        Sigmazsa (VRAM/OOM) onceki degere OTOMATIK doner - uygulama asla olu kalmaz."""
        try:
            n = int(n_ctx)
        except (TypeError, ValueError):
            return {"ok": False, "error": "value"}
        probe = self._model_probe or {}
        ceil = probe.get("n_ctx_train") or 262144
        n = min(int(ceil), max(4096, n))
        if self._state != "ready":
            return {"ok": False, "error": "not_ready"}
        if not self._server.owned:
            # reuse modunda stop/ensure ayni yabanci sunucuyu bulur: arayuz yeni
            # baglami gosterir ama sunucu eski -c ile calisirdi (denetim O4).
            return {"ok": False, "error": "reuse"}
        with self._busy_guard:
            if self._busy:
                return {"ok": False, "error": "busy"}
            self._busy = True
        prev = int(CONFIG.n_ctx)
        if n == prev:
            with self._busy_guard:
                self._busy = False
            return {"ok": True, "n_ctx": n, "restarted": False}

        # state gecisi DONUSTEN ONCE: donus aninda status() polleyen JS bir an
        # "ready" gorup restart overlay'ini erken kaldirabiliyordu (TOCTOU).
        self._state = "loading"
        self._detail = ""

        def _restart() -> None:
            try:
                self._model_probe = None  # yeni oturumda yeniden olculur
                CONFIG.n_ctx = n
                self._server.stop()
                ok, msg = self._server.ensure()
                # ensure'in ilk elemani "yeni spawn mi" demektir, "basari" DEGIL:
                # (False,"reused") saglikli yabanci sunucudur (restart ortasinda
                # 8080'de belirmis olabilir) - hata sanip revert dongusune girmek
                # saglam sunucuya ragmen hata ekrani birakiyordu (dogrulama bulgusu)
                usable = ok or msg in ("reused", "already_running")
                if not usable:
                    _log.error("ctx restart ensure reddi: %s", msg)
                if usable and self._server.wait_ready(timeout=240):
                    if ok:
                        # KANIT SONRASI kalicilik (denetim O6): yalniz KENDI
                        # spawn'imiz yeni -c ile kanitlidir; ancak o zaman yazilir.
                        update_settings({"n_ctx": int(n)})
                    else:
                        # yabanci sunucu: bizim -c gecmedi - durustce eski degerde
                        # kal ve panelde belirt (dosyaya yazim YOK)
                        CONFIG.n_ctx = prev
                        self._detail = "ctx_reuse"
                    self.mark_ready(self._server.base_url)
                    return
                # sigmadi ya da acilamadi: onceki calisan degere geri don
                # (dosya zaten prev'de - geri yazim gerekmez)
                CONFIG.n_ctx = prev
                self._server.stop()
                ok2, msg2 = self._server.ensure()
                usable2 = ok2 or msg2 in ("reused", "already_running")
                if usable2 and self._server.wait_ready(timeout=240):
                    self._detail = "ctx_reverted"
                    self.mark_ready(self._server.base_url)
                    return
                _log.error("ctx revert de basarisiz: %s", msg2 if not usable2 else "wait_ready")
                # gercek neden kullaniciya tasinir (eskiden 2x240sn sahte
                # bekleyisten sonra jenerik server_not_ready kalirdi)
                detail = msg if msg.startswith(("missing:", "spawn_failed")) else "server_not_ready"
                self.mark_error(detail)
            except Exception as e:
                _log.error("ctx restart istisna err=%s", err_brief(e))
                self.mark_error("restart_failed")
            finally:
                with self._busy_guard:
                    self._busy = False

        threading.Thread(target=_restart, daemon=True).start()
        return {"ok": True, "n_ctx": n, "restarted": True}

