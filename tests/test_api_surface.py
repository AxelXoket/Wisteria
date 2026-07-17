"""JsApi yuzey guvencesi: mixin bolmesi metot kaybetmedi + kopru kurali.

Kopru kurali: pywebview, js_api nesnesinin PUBLIC ozniteliklerini ozyineli gezer;
public VERI ozniteligi UI thread'ini dondurabilir (yasanmis olay). Bu test, sinif
hiyerarsisinde public veri ozniteligi olmadigini garantiler.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.api import JsApi

# JS koprusunun cagirdigi zorunlu yuzey (UI bunlara bagli - kaybi build kirar)
REQUIRED = {
    "status", "send", "cancel_gen", "new_chat", "export_chat", "retry_boot",
    "memory_state", "memory_unlock", "memory_overview",
    "memory_add_fact", "memory_update_fact", "memory_delete_fact",
    "prompts_list", "prompts_get", "prompts_save", "prompts_create",
    "prompts_set_active", "prompts_rename", "prompts_delete", "prompts_export",
    "tts_status", "set_tts_enabled", "speak_message", "stop_speaking",
    "tts_get_params", "tts_set_params",
    "ui_text_get", "ui_text_set",
    "ui_bg_get", "ui_bg_set", "ui_bg_clear", "ui_bg_prefs",
    "gen_get", "gen_set", "gen_ctx_apply",
}
have = {m for m in dir(JsApi) if callable(getattr(JsApi, m, None))}
missing = REQUIRED - have
assert not missing, f"KAYIP JS yuzeyi: {sorted(missing)}"
print(f"1) JS yuzeyi tam ({len(REQUIRED)} zorunlu metot) OK")

pub_data = []
for klass in JsApi.__mro__:
    if klass is object:
        continue
    pub_data += [n for n in vars(klass)
                 if not n.startswith("_") and not callable(getattr(klass, n))]
assert not pub_data, f"PUBLIC veri ozniteligi yasak: {pub_data}"
print("2) kopru kurali: public veri ozniteligi yok OK")
print("API YUZEYI TAMAM")
