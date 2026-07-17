"""Kullanicinin sorusunun kaniti: 'Aktif yap' denen prompt GERCEKTEN modele mi gidiyor?
Gercek api.py + gercek sifreli kasa (TEMP dizinde), model yok - mesaj listesi incelenir.
llm_messages = self._messages + [...] oldugu icin messages[0] kaniti = modele giden kanit."""
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pathlib
APP = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, APP)

# tek basina kosumda JsApi.__init__ -> ensure_api_key GERCEK settings.json'a yazmasin
os.environ.setdefault("WISTERIA_SETTINGS_DIR", tempfile.mkdtemp(prefix="wisteria-test-flow-set-"))

# gecici alan SISTEM TEMP'inde: repo icinde test artigi birakma
SP = Path(tempfile.mkdtemp(prefix="wisteria-test-flow-"))
if SP.exists():
    shutil.rmtree(SP)
(SP / "system_prompts").mkdir(parents=True)
(SP / "character_prompts").mkdir()
(SP / "personas").mkdir()
(SP / "system_prompts" / "system_prompt.txt").write_text("SYS Lavanta kurallari", encoding="utf-8")
(SP / "character_prompts" / "lavanta.txt").write_text("LAVANTA KARAKTER METNI", encoding="utf-8")
(SP / "personas" / "persona1.txt").write_text("PERSONA: adi Deniz", encoding="utf-8")

from backend.config import CONFIG
CONFIG.memory_dir = SP / "memory"
CONFIG.system_prompt = SP / "system_prompts" / "system_prompt.txt"
CONFIG.characters_dir = SP / "character_prompts"
CONFIG.personas_dir = SP / "personas"
CONFIG.embed_cache_dir = SP / "embed"

from backend.api import JsApi

api = JsApi(server=SimpleNamespace())
CONFIG.base_character_name = "Lavanta"   # deterministik: gercek settings'ten bagimsiz

assert api.memory_unlock("flow-test-1", remember=False)["ok"]
m0 = api._messages[0]["content"]
assert "LAVANTA KARAKTER METNI" in m0, "aktif karakterin metni sistem mesajinda degil!"
assert "PERSONA: adi Deniz" in m0, "aktif persona metni sistem mesajinda degil!"
assert api.status()["character"] == "Lavanta"
print("1) kilit acildi: aktif karakter + persona metinleri modele giden mesajda OK", flush=True)

# ikinci karakter olustur, farkli metin ver, AKTIF YAP -> mesaj[0] degismeli
assert api.prompts_create("character", "vulgar test")["name"] == "vulgar_test"
assert api.prompts_save("character", "vulgar_test", "VULGAR TEST METNI")["ok"]
assert api.prompts_set_active("character", "vulgar_test")["ok"]
m1 = api._messages[0]["content"]
assert "VULGAR TEST METNI" in m1, "yeni aktif karakterin metni gitmiyor!"
assert "LAVANTA KARAKTER METNI" not in m1, "eski karakterin metni hala duruyor!"
assert api.status()["character"] == "Vulgar Test"
print("2) aktiflestirme: yeni metin girdi, eski metin cikti, gorunen isim degisti OK", flush=True)

# geri don + kayit aninda canli guncelleme
assert api.prompts_set_active("character", "lavanta")["ok"]
assert "LAVANTA KARAKTER METNI" in api._messages[0]["content"]
assert api.prompts_save("character", "lavanta", "LAVANTA GUNCEL METIN")["ok"]
assert "LAVANTA GUNCEL METIN" in api._messages[0]["content"]
assert api.status()["character"] == "Lavanta"
print("3) geri gecis + kaydette canli guncelleme OK", flush=True)

# sohbet gecmisi korunuyor mu (yalnizca messages[0] degisiyor)
api._messages.append({"role": "user", "content": "selam"})
api._messages.append({"role": "assistant", "content": "hosgeldin"})
assert api.prompts_set_active("character", "vulgar_test")["ok"]
assert api._messages[1]["content"] == "selam" and api._messages[2]["content"] == "hosgeldin"
assert "VULGAR TEST METNI" in api._messages[0]["content"]
print("4) gecis sirasinda sohbet gecmisi korunuyor OK", flush=True)

api.close_memory()
shutil.rmtree(SP, ignore_errors=True)
print("PROMPT AKISI KANITLANDI: aktif prompt = modele giden prompt", flush=True)
