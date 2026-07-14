"""Wisteria desktop app entry point.

Fully local: spawns/reuses a local llama-server, renders the custom HTML UI in a
native WebView2 window, streams replies. No internet needed at runtime.
"""

from __future__ import annotations

import sys

import webview

from backend.api import JsApi
from backend.config import CONFIG
from backend.server import ServerManager


def _set_dpi_aware() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor v2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _set_window_icon(window) -> None:
    """Taskbar + başlık çubuğu ikonu. WinForms pencereleri exe kaynağındaki ikonu
    OTOMATİK ALMAZ (jenerik .NET ikonu görünür); form Icon'u ico dosyasından elle
    verilir. Dev'de assets/, frozen'da bundle içindeki assets/ çözülür.

    KRİTİK: pywebview 'shown' işleyicileri UI-DIŞI bir thread'de koşar. Form.Icon
    ataması o thread'den SendMessage'a düşer ve UI thread'inin mesaj pompalamasını
    BEKLER; UI thread'i ise shown sırasında WebView2 Focus() içinde çocuk sürecin
    ilk açılışını bekliyor olabilir -> karşılıklı kilitlenme = taze build'in ilk
    soğuk açılışındaki "Yanıt Vermiyor" (py-spy ile kanıtlandı, 2026-07-14).
    Çözüm: Icon'u burada (işleyici thread'inde) KUR, forma BeginInvoke ile
    ASENKRON uygula - kuyruğa atar, asla beklemez."""
    try:
        from backend.config import app_dir, bundle_dir
        for cand in (bundle_dir() / "assets" / "wisteria.ico",
                     app_dir() / "assets" / "wisteria.ico"):
            if cand.exists():
                import clr
                clr.AddReference("System.Drawing")  # garanti: bazen implicit yüklenmez
                from System import Action
                from System.Drawing import Icon
                icon = Icon(str(cand))
                form = window.native
                form.BeginInvoke(Action(lambda: setattr(form, "Icon", icon)))
                break
    except Exception:
        pass


def _ensure_js_bridge(window) -> None:
    """Sayfadaki pywebview koprusunu dogrula, olmamissa yeniden enjekte et.

    pywebview enjeksiyon thread'i 'loaded' olayini sayfa ICINDE script gercekten
    calismis mi diye bakmadan ateşler; soguk ilk aciliste enjeksiyon sayfada
    sessizce kaybolabiliyor ve UI koprusuz kaliyordu. Buradaki probe evaluate_js
    ile gercek sayfa durumunu okur (stub'a bagimli degildir), kopru yoksa
    pywebview'in kendi enjektorunu yeniden kosar. Normal acilista ilk probe
    True doner ve hicbir sey yapilmaz."""
    import time
    for attempt in range(6):
        time.sleep(3.0 if attempt == 0 else 2.0)
        try:
            ok = window.evaluate_js(
                "!!(window.pywebview && window.pywebview.api"
                " && typeof window.pywebview.api.status === 'function')")
        except Exception:
            ok = None
        if ok is True:
            return
        try:
            from webview.util import inject_pywebview
            inject_pywebview('edgechromium', window)
        except Exception:
            pass


def _single_instance() -> bool:
    """Return True if we are the only instance (else False -> caller should exit)."""
    try:
        import win32api
        import win32event
        import winerror
        win32event.CreateMutex(None, False, "Global\\WisteriaApp_singleton")
        return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS
    except Exception:
        return True  # if pywin32 missing, don't block


def _boot(window, api: JsApi, server: ServerManager) -> None:
    """Background: start/reuse llama-server, wait until ready, then unlock the UI.

    Boot order is deliberately SERIAL: the LLM first (the critical path - chat can't
    work without it), the voice worker only after mark_ready. Loading both at the
    same instant made them race for VRAM/disk on a cold morning boot (heavy desktop
    + two models cudaMalloc'ing at once) and llama-server could die mid-load. The
    voice worker just comes up ~7 s later in the background (icon pulses briefly);
    it stays resident from then on.

    This must NOT start before the WebView is up (see _kick_boot in main): the
    multi-GB gguf read saturates a cold disk and starves WebView2 initialization,
    which freezes the UI thread ("Yanıt Vermiyor") on the first launch of a fresh
    build. UI first, heavy I/O second.
    """
    spawned, msg = server.ensure()
    if msg.startswith("missing:") or msg.startswith("spawn_failed"):
        api.mark_error(msg)
        return
    ready = server.wait_ready(timeout=240)
    if ready:
        api.mark_ready(server.base_url)
    else:
        api.mark_error("server_not_ready")
        return
    try:
        from backend.tts import TTS
        TTS.configure(CONFIG)
        TTS.set_enabled(CONFIG.tts_enabled)  # restore the persisted auto flag
        TTS.ensure_loaded()                  # resident worker; killed only at app close
    except Exception:
        pass


def main() -> None:
    _set_dpi_aware()
    if not _single_instance():
        sys.exit(0)

    server = ServerManager()
    api = JsApi(server)

    settings = {}
    try:
        from backend.config import load_settings
        settings = load_settings()
    except Exception:
        settings = {}

    window = webview.create_window(
        "Wisteria",
        url=str(CONFIG.web_dir / "index.html"),
        js_api=api,
        width=int(settings.get("w", 1120)),
        height=int(settings.get("h", 820)),
        min_size=(760, 560),
        background_color="#ECE8E1",
    )
    api.set_window(window)

    import threading

    _boot_once = threading.Event()

    def _kick_boot(*_a) -> None:
        """Backend'i (llama-server + TTS) ancak WebView ayaga kalktiktan sonra baslat.

        Eskiden _boot, webview.start(func) ile pencere cizilmeden firlatiliyordu;
        soguk onbellekte (taze build'in ilk acilisi) modelin multi-GB disk okumasi
        WebView2 baslatmasini acliktan bogup UI thread'ini donduruyordu. 'loaded'
        tetiklemesi sirayi garantiler: once arayuz, sonra agir I/O.
        """
        if _boot_once.is_set():
            return
        _boot_once.set()
        threading.Thread(target=_boot, args=(window, api, server), daemon=True).start()
        threading.Thread(target=_ensure_js_bridge, args=(window,), daemon=True).start()

    def _boot_fallback_timer(*_a) -> None:
        # emniyet: 'loaded' bir sebeple hic gelmezse 10 sn sonra yine de basla
        t = threading.Timer(10.0, _kick_boot)
        t.daemon = True
        t.start()

    def _on_closed() -> None:
        _boot_once.set()  # kapanistan sonra gec kalan fallback timer sunucu acmasin
        try:
            from backend.tts import TTS
            TTS.shutdown()  # kill the voice worker subprocess (frees its VRAM)
        except Exception:
            pass
        try:
            api.close_memory()
        except Exception:
            pass
        try:
            server.stop()
        except Exception:
            pass

    window.events.closed += _on_closed
    # sira onemli: shown isleyicileri AYNI thread'de sirayla kosar - zamanlayici
    # once kaydolur ki sonraki bir isleyici takilirsa bile boot garantide olsun
    window.events.shown += _boot_fallback_timer
    window.events.shown += (lambda *a: _set_window_icon(window))  # taskbar ikonu
    window.events.loaded += _kick_boot

    webview.start()


if __name__ == "__main__":
    main()
