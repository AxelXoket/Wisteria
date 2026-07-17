"""Sohbet durum makinesi: busy/emit SIRA sozlesmesi, new_chat busy reddi,
hata yolunun gecmis disi kalmasi.

Denetim O1/Y1 regresyonlari. Gercek JsApi + sahte LlamaClient; llama-server,
kasa ve ses GEREKMEZ (memory_enabled kapatilir).
"""
import os
import pathlib
import sys
import tempfile
import threading
import time

os.environ.setdefault("WISTERIA_SETTINGS_DIR",
                      tempfile.mkdtemp(prefix="wisteria-test-chat-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.config import CONFIG
CONFIG.memory_enabled = False  # kasa/embedder bu suitin konusu degil

from backend.api import JsApi
from backend.llm import LlamaError


class FakeClient:
    def __init__(self, chunks, final=None, gate: threading.Event | None = None,
                 raise_error=False):
        self.chunks = chunks
        self.final = "".join(chunks) if final is None else final
        self.gate = gate
        self.raise_error = raise_error

    def stream_chat(self, messages, preset, on_token, cancel):
        for c in self.chunks:
            on_token(c)
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if self.raise_error:
            raise LlamaError("llama-server stream error: test")
        return self.final


def make_api(client):
    api = JsApi(None)
    api._state = "ready"
    api._client = client
    return api


def wait_for(pred, timeout=8.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_1_busy_cleared_before_stream_end():
    api = make_api(FakeClient(["merhaba ", "dunya"]))
    events = []
    busy_at_end = []
    api._emit = lambda fn, payload: (
        events.append(fn),
        busy_at_end.append(api._busy) if fn == "appStreamEnd" else None)
    r = api.send("selam")
    assert r["ok"], r
    assert wait_for(lambda: "appStreamEnd" in events), f"akis bitmedi: {events}"
    assert busy_at_end == [False], (
        "SIRA SOZLESMESI: appStreamEnd yayinlanirken _busy COKTAN False olmali "
        "(JS composer'i o anda acar; True kalsa aradaki mesaj sessizce duserdi)")
    assert wait_for(lambda: not api._busy)
    assert api._messages[-1]["content"] == "merhaba dunya"
    assert api._messages[-2] == {"role": "user", "content": "selam"}
    print("1) emit sirasi: busy=False -> appStreamEnd + gecmis dogru OK")


def test_2_new_chat_rejected_mid_stream():
    gate = threading.Event()
    api = make_api(FakeClient(["yarim "], gate=gate))
    events = []
    api._emit = lambda fn, payload: events.append(fn)
    assert api.send("uzun soru")["ok"]
    assert wait_for(lambda: "appStreamStart" in events)
    r = api.new_chat()
    assert r == {"ok": False, "error": "busy"}, (
        "akis surerken new_chat REDDEDILMELI (hayalet tur bulasmasi - denetim Y1): "
        f"{r}")
    gate.set()
    assert wait_for(lambda: not api._busy)
    # tur, SIFIRLANMAMIS gecmise normal eklendi
    assert api._messages[-2]["content"] == "uzun soru"
    r2 = api.new_chat()
    assert r2["ok"] and len(api._messages) == 1, "bosta new_chat normal calisir"
    print("2) akis sirasinda new_chat reddi + bosta calisiyor OK")


def test_3_error_turn_stays_out_of_history():
    api = make_api(FakeClient(["kismi "], raise_error=True))
    events = {}
    api._emit = lambda fn, payload: events.setdefault(fn, payload)
    assert api.send("soru")["ok"]
    assert wait_for(lambda: "appStreamEnd" in events)
    assert events["appStreamEnd"]["final"].startswith("[Hata]")
    assert wait_for(lambda: not api._busy)
    assert len(api._messages) == 1, "hatali tur LLM baglamina GIRMEMELI"
    r = api.send("tekrar")
    assert r["ok"], "hata sonrasi composer/busy kilitli kalmamali"
    assert wait_for(lambda: not api._busy)
    print("3) hata yolu: gecmis disi + busy toparlanmasi OK")


test_1_busy_cleared_before_stream_end()
test_2_new_chat_rejected_mid_stream()
test_3_error_turn_stays_out_of_history()
print("SOHBET DURUM MAKINESI TAMAM")
