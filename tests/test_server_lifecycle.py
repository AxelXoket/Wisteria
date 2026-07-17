"""Surec yasam dongusu: server kilidi/_closing/bayat-proc/TOCTOU retry,
llm cerceve hatasi + sinirli okuma, tek-ornek mutex'i, reuse'da ctx reddi.

Denetim K2/K3/Y6/O4/O5/O7 regresyonlari. llama-server GEREKMEZ: Popen/_healthy
sahtelenir; mutex canli probu gercek iki python sureciyle kosar.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import types

os.environ.setdefault("WISTERIA_SETTINGS_DIR",
                      tempfile.mkdtemp(prefix="wisteria-test-srv-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import llm as llmmod
from backend import server as srv
from backend.api_parts.gen_api import GenApiMixin
from backend.config import CONFIG


class FakeProc:
    def __init__(self, alive=True, pid=4242):
        self._alive = alive
        self.pid = pid

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self._alive = False


def test_1_stale_proc_cleared_on_reuse():
    sm = srv.ServerManager()
    sm._owned = True
    sm.proc = FakeProc(alive=False)   # stop() sonrasi kalan olu referans senaryosu
    real_h = srv._healthy
    srv._healthy = lambda url: True   # 8080'de saglikli sunucu var
    try:
        ok, msg = sm.ensure()
        assert msg == "reused" and sm.proc is None, (msg, sm.proc)
        assert sm.wait_ready(timeout=3) is True, \
            "bayat olu proc, saglikli sunucuya ragmen wait_ready'yi dusuruyordu"
    finally:
        srv._healthy = real_h
    print("1) reuse: bayat proc temizligi + wait_ready OK")


def test_2_closing_blocks_ensure():
    sm = srv.ServerManager()
    real_h = srv._healthy
    srv._healthy = lambda url: False
    try:
        sm.shutdown()
        assert sm.ensure() == (False, "closing"), "kapanis sonrasi spawn YASAK"
    finally:
        srv._healthy = real_h
    print("2) shutdown sonrasi ensure reddi OK")


def test_3_toctou_retry_and_already_running():
    sm = srv.ServerManager()
    calls = []
    real_h, real_popen, real_sleep = srv._healthy, srv.subprocess.Popen, srv.time.sleep
    srv._healthy = lambda url: False
    srv.subprocess.Popen = lambda args, **kw: (calls.append(args) or
                                               FakeProc(alive=(len(calls) >= 2)))
    srv.time.sleep = lambda s: None
    old_ls, old_mp = CONFIG.llama_server, CONFIG.model_path
    CONFIG.llama_server = pathlib.Path(sys.executable)  # var olan dosyalar: kontrol gecsin
    CONFIG.model_path = pathlib.Path(sys.executable)
    try:
        ok, msg = sm.ensure()
        assert ok and msg == "spawned" and len(calls) == 2, \
            f"calinan port bir kez taze portla denenmeli: {(ok, msg, len(calls))}"
        assert sm.ensure() == (False, "already_running"), \
            "canli surec varken ikinci ensure yeni spawn ACMAMALI (cift-boot kemeri)"
        assert len(calls) == 2
    finally:
        srv._healthy, srv.subprocess.Popen, srv.time.sleep = real_h, real_popen, real_sleep
        CONFIG.llama_server, CONFIG.model_path = old_ls, old_mp
    print("3) TOCTOU tek retry + already_running kemeri OK")


def test_4_llm_frame_error_and_timeout():
    assert llmmod._frame_error({"error": {"message": "context dolu"}}) == "context dolu"
    assert llmmod._frame_error({"error": "duz metin"}) == "duz metin"
    assert llmmod._frame_error({"choices": [{"delta": {"content": "x"}}]}) is None
    assert llmmod._frame_error({"timings": {}}) is None
    assert llmmod._frame_error("saçma") is None
    t = llmmod._STREAM_TIMEOUT
    assert t.read == 180.0 and t.connect == 10.0, "okuma siniri kayboldu (denetim K2)"
    assert llmmod.LlamaError("x", status=400).status == 400
    assert llmmod.LlamaError("y").status is None
    # DAVRANISSAL kanit (sabit-var testi yetmez): stream_chat, Client'i GERCEKTEN
    # _STREAM_TIMEOUT ile kuruyor mu? timeout= kaldirilirsa bu test KIRILIR.
    seen = {}
    real_client = llmmod.httpx.Client

    class ProbeClient:
        def __init__(self, **kw):
            seen.update(kw)
            raise llmmod.httpx.ConnectError("probe")
    llmmod.httpx.Client = ProbeClient
    try:
        try:
            llmmod.LlamaClient("http://127.0.0.1:9").stream_chat(
                [{"role": "user", "content": "x"}], __import__("backend.config", fromlist=["GenPreset"]).GenPreset(), lambda d: None, None)
            raise AssertionError("ConnectError LlamaError'a donmeliydi")
        except llmmod.LlamaError:
            pass
    finally:
        llmmod.httpx.Client = real_client
    assert seen.get("timeout") is llmmod._STREAM_TIMEOUT, \
        "stream_chat Client'a _STREAM_TIMEOUT gecmiyor (K2 fixi kalkmis olabilir)"
    print("4) llm: SSE hata cercevesi + zaman asimi (davranissal) + status OK")


def test_5_ctx_apply_rejects_reuse():
    ns = types.SimpleNamespace(_state="ready", _model_probe=None,
                               _server=types.SimpleNamespace(owned=False))
    r = GenApiMixin.gen_ctx_apply(ns, 8192)
    assert r == {"ok": False, "error": "reuse"}, r
    print("5) reuse modunda ctx degisimi durustce reddedilir OK")


def test_6_single_instance_mutex_live():
    app = pathlib.Path(__file__).resolve().parents[1]
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(app)!r})
        from main import _single_instance
        print("FIRST" if _single_instance() else "SECOND", flush=True)
        time.sleep(12)
    """)
    env = dict(os.environ)
    a = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, cwd=str(app), env=env)
    try:
        line_a = a.stdout.readline().strip()
        assert line_a == "FIRST", f"ilk ornek mutexi almali: {line_a!r}"
        b = subprocess.run([sys.executable, "-c", script], capture_output=True,
                           text=True, timeout=90, cwd=str(app), env=env)
        assert "SECOND" in (b.stdout or ""), (
            "ikinci ornek ERROR_ALREADY_EXISTS gormedi - tutamac yasamiyor "
            f"(denetim K3): out={b.stdout!r} err={b.stderr[-200:]!r}")
    finally:
        try:
            a.kill()
        except Exception:
            pass
    print("6) tek-ornek mutex canli probe (iki gercek surec) OK")


test_1_stale_proc_cleared_on_reuse()
test_2_closing_blocks_ensure()
test_3_toctou_retry_and_already_running()
test_4_llm_frame_error_and_timeout()
test_5_ctx_apply_rejects_reuse()
test_6_single_instance_mutex_live()
print("SUREC YASAM DONGUSU TAMAM")
