"""prompts_rename / prompts_delete: tum kenar durumlari (gercek kasa, TEMP dizin)."""
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pathlib
APP = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, APP)

# tek basina kosumda da gercek settings.json'a dokunma + repo icinde artik birakma
os.environ.setdefault("WISTERIA_SETTINGS_DIR", tempfile.mkdtemp(prefix="wisteria-test-rd-set-"))
SP = Path(tempfile.mkdtemp(prefix="wisteria-test-rd-"))
if SP.exists():
    shutil.rmtree(SP)
(SP / "system_prompts").mkdir(parents=True)
(SP / "character_prompts").mkdir()
(SP / "personas").mkdir()
(SP / "system_prompts" / "system_prompt.txt").write_text("SYS Lavanta kurallari", encoding="utf-8")
(SP / "character_prompts" / "lavanta.txt").write_text("LAVANTA METNI", encoding="utf-8")
(SP / "personas" / "persona1.txt").write_text("PERSONA METNI", encoding="utf-8")

from backend.config import CONFIG
CONFIG.memory_dir = SP / "memory"
CONFIG.system_prompt = SP / "system_prompts" / "system_prompt.txt"
CONFIG.characters_dir = SP / "character_prompts"
CONFIG.personas_dir = SP / "personas"
CONFIG.embed_cache_dir = SP / "embed"

from backend.api import JsApi

api = JsApi(server=SimpleNamespace())
CONFIG.base_character_name = "Lavanta"
assert api.memory_unlock("rd-test-1", remember=False)["ok"]

# hazirlik: beta ve gamma karakterleri
assert api.prompts_create("character", "beta")["ok"]
assert api.prompts_save("character", "beta", "BETA METNI")["ok"]
assert api.prompts_create("character", "gamma")["ok"]
assert api.prompts_save("character", "gamma", "GAMMA METNI")["ok"]

# --- rename: bekci kurallari ---
assert api.prompts_rename("system", "system_prompt", "x")["error"] == "kind"
assert api.prompts_rename("character", "ghost", "yeni")["error"] == "not_found"
assert api.prompts_rename("character", "beta", "!!!")["error"] == "name"
assert api.prompts_rename("character", "beta", "gamma")["error"] == "exists"
assert api.prompts_rename("character", "beta", "beta")["ok"]  # no-op
print("1) rename bekcileri OK", flush=True)

# aktif OLMAYAN rename: metin korunur, liste guncellenir
r = api.prompts_rename("character", "beta", "Beta Two!")
assert r == {"ok": True, "name": "beta_two"}, r
assert api.prompts_get("character", "beta_two")["text"] == "BETA METNI"
assert api.prompts_get("character", "beta")["text"] == ""  # eski ad yok
assert "beta" not in api.prompts_list()["kinds"]["character"]
assert api._character == "lavanta"  # aktif etkilenmedi
print("2) aktif olmayan rename OK", flush=True)

# AKTIF karakteri rename: _character + meta + sistem mesaji izler
assert api.prompts_set_active("character", "beta_two")["ok"]
r = api.prompts_rename("character", "beta_two", "delta")
assert r["ok"] and api._character == "delta"
assert api.prompts_list()["active"]["character"] == "delta"
assert "BETA METNI" in api._messages[0]["content"]          # metin ayni
assert api.status()["character"] == "Delta"                 # gorunen ad yeni
print("3) aktif rename: isim/meta/sistem-mesaji izliyor OK", flush=True)

# --- delete ---
assert api.prompts_delete("system", "system_prompt")["error"] == "kind"
assert api.prompts_delete("character", "ghost")["error"] == "not_found"
# aktif olmayani sil
assert api.prompts_delete("character", "gamma")["ok"]
assert "gamma" not in api.prompts_list()["kinds"]["character"]
# AKTIFI sil -> devir alfabetik ilk hayatta kalana (delta silinince lavanta kalir)
r = api.prompts_delete("character", "delta")
assert r["ok"] and r["active"] == "lavanta", r
assert api._character == "lavanta"
assert "LAVANTA METNI" in api._messages[0]["content"]      # devralanin metni modelde
# sonuncu silinemez
assert api.prompts_delete("character", "lavanta")["error"] == "last"
print("4) delete: devir + sonuncu korumasi OK", flush=True)

# persona tarafi: rename + last korumasi
r = api.prompts_rename("persona", "persona1", "deniz")
assert r["ok"] and api.prompts_list()["active"]["persona"] == "deniz"
assert "PERSONA METNI" in api._messages[0]["content"]
assert api.prompts_delete("persona", "deniz")["error"] == "last"
print("5) persona rename + koruma OK", flush=True)

# restart kaliciligi
api.close_memory()
api2 = JsApi(server=SimpleNamespace())
assert api2.memory_unlock("rd-test-1")["ok"]
pl = api2.prompts_list()
assert pl["kinds"]["character"] == ["lavanta"] and pl["kinds"]["persona"] == ["deniz"]
assert pl["active"]["character"] == "lavanta" and pl["active"]["persona"] == "deniz"
api2.close_memory()
shutil.rmtree(SP, ignore_errors=True)
print("RENAME/DELETE BACKEND TAMAMEN OK", flush=True)
