"""memory_add_fact / memory_update_fact / export_chat: kenar durumlar (TEMP kasa)."""
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import pathlib
APP = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, APP)

# tek basina kosumda JsApi.__init__ -> ensure_api_key GERCEK settings.json'a yazmasin
os.environ.setdefault("WISTERIA_SETTINGS_DIR", tempfile.mkdtemp(prefix="wisteria-test-nf-set-"))

# gecici alan SISTEM TEMP'inde: repo icinde test artigi birakma (gitignore disi kirlilik)
SP = Path(tempfile.mkdtemp(prefix="wisteria-test-nf-"))
if SP.exists():
    shutil.rmtree(SP)
(SP / "system_prompts").mkdir(parents=True)
(SP / "character_prompts").mkdir()
(SP / "personas").mkdir()
(SP / "system_prompts" / "system_prompt.txt").write_text("GIZLI SISTEM PROMPTU", encoding="utf-8")
(SP / "character_prompts" / "lavanta.txt").write_text("KARAKTER", encoding="utf-8")
(SP / "personas" / "persona1.txt").write_text("", encoding="utf-8")

from backend.config import CONFIG
CONFIG.memory_dir = SP / "memory"
CONFIG.system_prompt = SP / "system_prompts" / "system_prompt.txt"
CONFIG.characters_dir = SP / "character_prompts"
CONFIG.personas_dir = SP / "personas"
CONFIG.embed_cache_dir = SP / "embed"

from backend.api import JsApi

api = JsApi(server=SimpleNamespace())
assert api.memory_add_fact("bilgi", "x", 5)["error"] == "locked"
assert api.memory_unlock("nf-test-1", remember=False)["ok"]

# --- elle ekleme: dogrulama + sinirlar ---
r = api.memory_add_fact("preference", "Kahveyi sade sever", 8)
assert r["ok"] and isinstance(r["id"], int), r
fid = r["id"]
assert api.memory_add_fact("bilgi", "", 5)["error"] == "text"
assert api.memory_add_fact("bilgi", "y" * 2001, 5)["error"] == "text"
r2 = api.memory_add_fact("sacma_tur", "Tur beyaz liste testi", 99)
assert r2["ok"]
ov = api.memory_overview()
byid = {f["id"]: f for f in ov["facts"]}
assert byid[fid]["type"] == "preference" and byid[fid]["importance"] == 8
assert byid[r2["id"]]["type"] == "bilgi" and byid[r2["id"]]["importance"] == 10  # kirpildi
print("1) elle ekleme + sinirlar OK", flush=True)

# --- duzenleme ---
assert api.memory_update_fact(fid, "Kahveyi sutlu sever artik", 4)["ok"]
ov = api.memory_overview(); byid = {f["id"]: f for f in ov["facts"]}
assert byid[fid]["text"] == "Kahveyi sutlu sever artik" and byid[fid]["importance"] == 4
assert api.memory_update_fact(fid, "", 4)["error"] == "text"
assert api.memory_update_fact(fid, "ok", "abc")["error"] == "importance"
print("2) duzenleme + dogrulama OK", flush=True)

# --- sohbet disa aktarma: sentetik onek SIZMAZ, format dogru ---
api._messages.append({"role": "user", "content": "selam"})
api._messages.append({"role": "assistant", "content": "hosgeldin canim"})
out = SP / "disari.txt"

class FakeWindow:
    def create_file_dialog(self, *a, **k):
        return (str(out),)

api._window = FakeWindow()
r = api.export_chat()
assert r["ok"], r
body = out.read_text(encoding="utf-8")
assert "GIZLI SISTEM PROMPTU" not in body, "sistem promptu SIZDI!"
assert "KARAKTER" not in body, "karakter promptu SIZDI!"
assert "Sen:\nselam" in body and "Lavanta:\nhosgeldin canim" in body, body
print("3) disa aktarma: onek yok, format dogru OK", flush=True)

# bos sohbet
api2_messages = api._messages
api._messages = api._messages[:1]  # yalniz sistem mesaji
assert api.export_chat()["error"] == "empty"
api._messages = api2_messages
print("4) bos sohbet korumasi OK", flush=True)

api.close_memory()
shutil.rmtree(SP, ignore_errors=True)
print("YENI OZELLIK BACKEND'I TAMAMEN OK", flush=True)
