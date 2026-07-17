"""Settings cekirdegi: kilitli merge-yazim, atomiklik, bozuk-dosya kurtarma,
ortam-degiskeni izolasyonu.

Denetim bulgusu Y5'in regresyon suiti: 6 kilitsiz yazar kayip guncellemeye,
bozuk JSON ise tum ayarlarin sessizce silinmesine yol aciyordu.
"""
import json
import os
import pathlib
import sys
import tempfile
import threading

TMP = tempfile.mkdtemp(prefix="wisteria-test-settings-suite-")
os.environ["WISTERIA_SETTINGS_DIR"] = TMP  # import ONCESI: modul yolu buna gore

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import config as cfg

SPATH = pathlib.Path(TMP) / "settings.json"


def test_env_isolation():
    assert cfg.settings_path() == SPATH, "env override calismiyor"
    real = pathlib.Path(__file__).resolve().parents[1] / "settings.json"
    assert cfg.settings_path() != real
    print("1) WISTERIA_SETTINGS_DIR izolasyonu OK")


def test_concurrent_updates_no_lost_writes():
    if SPATH.exists():
        SPATH.unlink()
    N = 50

    def writer(prefix: str):
        for i in range(N):
            cfg.update_settings({f"{prefix}{i}": i})

    threads = [threading.Thread(target=writer, args=(p,)) for p in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads(SPATH.read_text(encoding="utf-8"))
    for p in ("a", "b", "c"):
        missing = [i for i in range(N) if f"{p}{i}" not in data]
        assert not missing, f"kayip guncelleme: {p} -> {missing[:5]}"
    print(f"2) 3 thread x {N} anahtar, kayip guncelleme yok OK")


def test_corrupt_backup_and_recovery():
    SPATH.write_text('{"model_file": "onemli.gguf", BOZUK', encoding="utf-8")
    out = cfg.load_settings()
    assert out == {}, "bozuk dosya {} donmeli"
    baks = list(pathlib.Path(TMP).glob("settings.json.corrupt-*"))
    assert baks, "bozuk dosya kenara alinmali (kurtarilabilir yedek)"
    assert "onemli.gguf" in baks[0].read_text(encoding="utf-8"), "yedek icerigi korunmali"
    assert not SPATH.exists(), "bozuk orijinal yerinde kalmamali (tekrar tekrar loglamasin)"
    # sonraki merge-yazim yedegi ezmez, taze dosya olusturur
    cfg.update_settings({"api_key": "local-test"})
    assert json.loads(SPATH.read_text(encoding="utf-8"))["api_key"] == "local-test"
    assert baks[0].exists(), "yeni yazim yedege dokunmamali"
    for b in baks:
        b.unlink()
    print("3) bozuk dosya: yedek + kurtarma + taze yazim OK")


def test_atomic_tmp_cleanup_and_uniqueness():
    cfg.update_settings({"x": 1})
    leftovers = list(pathlib.Path(TMP).glob("settings.tmp-*"))
    assert not leftovers, f"tmp artigi kalmamali: {leftovers}"
    # tmp adi surec+thread'e ozgu: iki es zamanli yazar ayni tmp'yi ezemez
    names = set()

    def grab():
        names.add(f"settings.tmp-{os.getpid()}-{threading.get_ident()}")

    t1, t2 = threading.Thread(target=grab), threading.Thread(target=grab)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert len(names) == 2, "tmp adlari thread'e ozgu olmali"
    print("4) atomik tmp: artik yok + ad benzersiz OK")


def test_nan_infinity_rejected():
    SPATH.write_text(json.dumps({"tts_speed": float("nan"), "tts_exaggeration": 99,
                                 "gen_temperature": float("inf"), "n_ctx": 16384}),
                     encoding="utf-8")
    # json.dumps NaN/Infinity uretir (python ozelligi) - load da kabul eder;
    # savunma apply_settings_to_config'te olmali.
    c = cfg.Config()
    base_speed = c.tts_speed
    base_temp = c.chat_preset.temperature
    cfg.apply_settings_to_config(cfg.load_settings(), c)
    assert c.tts_speed == base_speed, "NaN tts_speed CONFIG'e tasinmamali"
    assert c.tts_exaggeration == 1.2, "asiri deger clamp'lenmeli (0.25..1.2)"
    assert c.chat_preset.temperature == base_temp, "Infinity gen degeri tasinmamali"
    SPATH.unlink()
    print("5) NaN/Infinity/clamp savunmasi OK")


test_env_isolation()
test_concurrent_updates_no_lost_writes()
test_corrupt_backup_and_recovery()
test_atomic_tmp_cleanup_and_uniqueness()
test_nan_infinity_rejected()
print("SETTINGS CEKIRDEGI TAMAM")
