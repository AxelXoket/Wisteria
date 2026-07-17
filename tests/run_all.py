"""Tum test suitlerini AYRI SURECLERDE kosar (suitler CONFIG global'ini mutasyona
ugratir - izolasyon sart) ve ozet basar.

Kullanim (repo kokunden):  uv run python tests/run_all.py
"""
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SUITES = [
    "test_settings.py",
    "test_vault_recovery.py",
    "test_server_lifecycle.py",
    "test_chat_state.py",
    "test_tts_gate.py",
    "test_sanitize2.py",
    "test_api_surface.py",
    "test_memory_reliability.py",
    "test_memory_ops_guard.py",
    "test_prompt_flow.py",
    "test_prompt_manage.py",
    "test_features.py",
    "test_background.py",
]

failed = []
for name in SUITES:
    print(f"\n=== {name} ===", flush=True)
    # Ayar izolasyonu: her suite kendi gecici settings dizinini gorur - JsApi
    # kuran testler gercek settings.json'i ne okur ne de yazabilir.
    env = os.environ.copy()
    env["WISTERIA_SETTINGS_DIR"] = tempfile.mkdtemp(prefix="wisteria-test-settings-")
    try:
        r = subprocess.run([sys.executable, str(HERE / name)],
                           cwd=str(HERE.parent), timeout=600, env=env)
        code = r.returncode
    except subprocess.TimeoutExpired:
        print(f"ZAMAN ASIMI (600s): {name}", flush=True)
        code = 1
    if code != 0:
        failed.append(name)

print("\n" + "=" * 46)
if failed:
    print("BASARISIZ:", ", ".join(failed))
    sys.exit(1)
print(f"TUM SUITLER GECTI ({len(SUITES)}/{len(SUITES)})")
