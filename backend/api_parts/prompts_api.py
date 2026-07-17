"""Prompt mixin'i"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import CONFIG
from ..prompt_store import KINDS


class PromptsApiMixin:
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

