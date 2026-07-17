"""Tercihler mixin'i"""

from __future__ import annotations

import base64
import math
import re
from pathlib import Path

from ..config import app_dir, load_settings, update_settings


def _finite(v, default: float) -> float:
    """float'a cevir; NaN/Infinity/gecersiz -> default (min/max NaN'i GECIRIR,
    o yuzden clamp'ten ONCE elenmeli)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


class PrefsApiMixin:
    def ui_text_get(self) -> dict:
        s = load_settings()
        fp = _finite(s.get("ui_msg_font_px", 15.5), 15.5)
        lh = _finite(s.get("ui_msg_line_height", 1.62), 1.62)
        return {"ok": True,
                "font_px": min(19.0, max(13.0, fp)),
                "line_height": min(1.95, max(1.30, lh))}

    def ui_text_set(self, font_px, line_height) -> dict:
        try:
            fp = float(font_px)
            lh = float(line_height)
        except (TypeError, ValueError):
            return {"ok": False, "error": "value"}
        if not (math.isfinite(fp) and math.isfinite(lh)):
            return {"ok": False, "error": "value"}
        fp = min(19.0, max(13.0, fp))
        lh = min(1.95, max(1.30, lh))
        update_settings({"ui_msg_font_px": fp, "ui_msg_line_height": lh})
        return {"ok": True, "font_px": fp, "line_height": lh}

    # ----- sohbet arka plani (kullanicinin kirptigi jpg, userdata/ altinda) -----
    # Kopruden data URI gecer; dosya web/ disinda durur ki rebuild'ler silmesin.
    # Not: gorsel duz dosyadir (kasa DISI) - duvar kagidi hassas veri sayilmaz.
    _BG_MAX_B64 = 9_000_000  # ~6.5MB ham veri; kirpilmis 1280w jpg tipik 200-500KB

    def _bg_path(self) -> Path:
        return app_dir() / "userdata" / "chat_bg.jpg"

    def ui_bg_get(self) -> dict:
        s = load_settings()
        contrast = min(0.85, max(0.0, _finite(s.get("ui_bg_contrast", 0.35), 0.35)))
        lum = min(1.0, max(0.0, _finite(s.get("ui_bg_lum", 0.5), 0.5)))
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
            # NaN tum karsilastirmalardan False ile siyrilir - once sonlu mu bak
            if len(r) != 4 or not all(math.isfinite(x) for x in r) \
                    or r[2] <= 0.02 or r[3] <= 0.02 or r[0] < 0 or r[1] < 0 \
                    or r[0] + r[2] > 1.001 or r[1] + r[3] > 1.001:
                return {"ok": False, "error": "rect"}
        p = self._bg_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        except Exception as exc:
            return {"ok": False, "error": f"write:{exc}"}
        l = min(1.0, max(0.0, _finite(lum, 0.5)))
        update_settings({"ui_bg_lum": l, "ui_bg_rect": r})
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
            c = float(contrast)
        except (TypeError, ValueError):
            return {"ok": False, "error": "value"}
        if not math.isfinite(c):
            return {"ok": False, "error": "value"}
        c = min(0.85, max(0.0, c))
        t = str(tint or "auto").strip().lower()
        if t != "auto" and not re.fullmatch(r"#[0-9a-f]{6}", t):
            return {"ok": False, "error": "tint"}
        update_settings({"ui_bg_contrast": c, "ui_bg_tint": t})
        return {"ok": True, "contrast": c, "tint": t}

