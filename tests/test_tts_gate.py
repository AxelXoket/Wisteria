"""TTS ebeveyn tarafi: yukleme kapisi (stdin'e yazim YOK, ayar coalesce),
ready'de tek flush, not-ready kapanista nazik-yazim atlama, takili gosterge kemeri.

Denetim O9 regresyonlari. Gercek worker GEREKMEZ: proc/borular sahtelenir.
"""
import os
import pathlib
import sys
import tempfile
import time

os.environ.setdefault("WISTERIA_SETTINGS_DIR",
                      tempfile.mkdtemp(prefix="wisteria-test-tts-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import tts as ttsmod
from backend.tts import TtsEngine


class FakePipe:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass


class FakeProc:
    def __init__(self, out_lines=()):
        self.stdin = FakePipe()
        self.stdout = iter(out_lines)
        self.pid = 777
        self.waited = False

    def wait(self, timeout=None):
        self.waited = True
        return 0


def test_1_loading_gate_coalesces_params():
    eng = TtsEngine()
    proc = FakeProc(out_lines=['{"ev":"ready","cloned":true}\n'])
    eng._proc = proc
    eng._state = "loading"
    for i in range(50):  # slider suruklemesi: yuklenirken 50 guncelleme
        eng.update_params({"speed": 0.9 + i * 0.001})
    eng.barge_in()       # yuklenirken stop da anlamsiz
    assert proc.stdin.lines == [], \
        "yukleme sirasinda stdin'e TEK BAYT yazilmamali (boru dolumu = donma)"
    assert abs(eng._pending_params["speed"] - 0.949) < 1e-9, "son deger kazanmali"
    eng._closing = True            # sync reader'in EOF'u hayalet error basmasin
    eng._reader(proc)              # 'ready' olayini isler -> flush
    assert eng._got_ready is True
    cfg_lines = [l for l in proc.stdin.lines if '"config"' in l]
    assert len(cfg_lines) == 1 and '"speed": 0.949' in cfg_lines[0], \
        f"ready'de TEK coalesced config gitmeli: {proc.stdin.lines}"
    assert eng._pending_params == {}
    print("1) yukleme kapisi: 0 yazim + tek coalesced flush OK")


def test_2_speak_denied_until_loaded():
    eng = TtsEngine()
    eng._proc = FakeProc()
    eng._state = "loading"
    assert eng.speak_text("merhaba") is False, "yuklenmeden konusma reddedilir"
    assert eng._proc.stdin.lines == []
    print("2) yuklenmeden speak reddi OK")


def test_3_kill_not_ready_skips_graceful():
    calls = []
    real_run = ttsmod.subprocess.run
    ttsmod.subprocess.run = lambda args, **kw: calls.append(args)
    try:
        eng = TtsEngine()
        proc = FakeProc()
        eng._proc = proc
        eng._got_ready = False   # worker hala CUDA yuklemesinde (stdin okunmuyor)
        eng._kill()
        assert proc.stdin.lines == [], \
            "not-ready kapanista nazik shutdown YAZILMAZ (suresiz bloklanirdi)"
        assert proc.waited is False, "wait(1.5) de atlanir - dogrudan agac oldurme"
        assert calls and calls[0][:4] == ["taskkill", "/F", "/T", "/PID"]
    finally:
        ttsmod.subprocess.run = real_run
    print("3) not-ready kapanis: dogrudan taskkill OK")


def test_4_kill_ready_graceful_first():
    calls = []
    real_run = ttsmod.subprocess.run
    ttsmod.subprocess.run = lambda args, **kw: calls.append(args)
    try:
        eng = TtsEngine()
        proc = FakeProc()
        eng._proc = proc
        eng._got_ready = True
        eng._kill()
        assert any('"shutdown"' in l for l in proc.stdin.lines), "nazik yol once denenir"
        assert proc.waited is True
        assert calls == [], "temiz cikista taskkill gerekmez"
    finally:
        ttsmod.subprocess.run = real_run
    print("4) ready kapanis: nazik yol + taskkill'siz OK")


def test_5_stuck_speaking_belt():
    eng = TtsEngine()
    eng._speaking = True
    eng._speak_ts = time.time() - 121
    st = eng.status()
    assert st["speaking"] is False, "120sn olaysiz 'konusuyor' gostergesi birakilmali"
    print("5) takili gosterge kemeri (120sn) OK")


test_1_loading_gate_coalesces_params()
test_2_speak_denied_until_loaded()
test_3_kill_not_ready_skips_graceful()
test_4_kill_ready_graceful_first()
test_5_stuck_speaking_belt()
print("TTS KAPISI TAMAM")
