"""ui_bg_get/set/clear/prefs: format/boyut bekcileri + kalicilik (izole TEMP)."""
import base64
import io
import json
import sys
import tempfile
from pathlib import Path

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import backend.config as cfg
import backend.api as apimod
import backend.api_parts.prefs_api as prefsmod   # ui_bg/ui_text artik burada yasiyor

TMP = Path(tempfile.mkdtemp())
sfile = TMP / "settings.json"
cfg.load_settings = lambda: json.loads(sfile.read_text()) if sfile.exists() else {}
cfg.save_settings = lambda d: sfile.write_text(json.dumps(d))
for mod in (apimod, prefsmod):                    # yamalar KULLANILAN modullere
    mod.load_settings, mod.save_settings = cfg.load_settings, cfg.save_settings
prefsmod.app_dir = lambda: TMP                    # userdata TEMP'e gitsin

api = apimod.JsApi.__new__(apimod.JsApi)           # yalniz ui_bg_* test edilir

# gercek minik jpg uret
from PIL import Image
buf = io.BytesIO()
Image.new("RGB", (8, 8), (40, 30, 50)).save(buf, "JPEG")
JPG = buf.getvalue()
DU = "data:image/jpeg;base64," + base64.b64encode(JPG).decode()

r = api.ui_bg_get()
assert r["ok"] and r["has"] is False and r["contrast"] == 0.35 and r["tint"] == "auto", r
print("1) bos durum + varsayilanlar OK", flush=True)

assert api.ui_bg_set("data:image/png;base64,xxxx", 0.5)["error"] == "format"
assert api.ui_bg_set("data:image/jpeg;base64," + base64.b64encode(b"PNGdegil").decode(), 0.5)["error"] == "format"
assert api.ui_bg_set("data:image/jpeg;base64," + "A" * 9_000_001, 0.5)["error"] == "too_big"
print("2) format/boyut bekcileri OK", flush=True)

assert api.ui_bg_set(DU, 0.83)["ok"]
assert (TMP / "userdata" / "chat_bg.jpg").read_bytes() == JPG
r = api.ui_bg_get()
assert r["has"] is True and r["dataurl"] == DU and abs(r["lum"] - 0.83) < 1e-9, r
print("3) kaydet + geri oku (dosya birebir) OK", flush=True)

assert api.ui_bg_prefs(0.99, "auto")["contrast"] == 0.85          # kirpildi
assert api.ui_bg_prefs(0.5, "#3B2652")["tint"] == "#3b2652"       # normalize
assert api.ui_bg_prefs(0.5, "mor")["error"] == "tint"
assert api.ui_bg_prefs("abc", "auto")["error"] == "value"
r = api.ui_bg_get(); assert r["contrast"] == 0.5 and r["tint"] == "#3b2652", r
print("4) tercih sinirlari + kalicilik OK", flush=True)

assert api.ui_bg_clear()["ok"]
assert api.ui_bg_get()["has"] is False
assert api.ui_bg_clear()["ok"]                                     # idempotent
print("5) kaldirma OK", flush=True)
print("BG BACKEND TAMAMEN OK", flush=True)
