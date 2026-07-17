"""Yerel, gizlilik-guvenli gunluk altyapisi.

POLITIKA (bu uygulamada her sey sifreli kasada yasar - gunluk o ilkeyi DELEMEZ):
  * Kullanici ICERIGI asla loglanmaz: mesaj metni, prompt metni, hafiza kaydi,
    ozet, persona - hicbiri. Gunluge yalnizca OLAY adlari, istisna TURU +
    kirpilmis ilk satiri ve sayisal baglam (id, adet, sure) yazilir.
  * Dosya userdata/logs/ altindadir: git-ignored, yalnizca bu makinede.
  * Gunluk altyapisi hicbir kosulda uygulamayi dusuremez (windowed exe'de
    stderr yoktur; logging.raiseExceptions kapatilir, kurulum hatasi sessizce
    NullHandler'a duser).

Kullanim:
    from .logutil import log_for, err_brief
    log = log_for("memory")
    log.warning("consolidate_extract_failed err=%s", err_brief(e))
"""

from __future__ import annotations

import logging
import logging.handlers

from .config import app_dir

_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True
    logging.raiseExceptions = False  # log yazimi asla istisna yukseltmesin
    root = logging.getLogger("wisteria")
    root.setLevel(logging.INFO)
    root.propagate = False
    try:
        d = app_dir() / "userdata" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        # Arastirma notlari: Windows'ta encoding verilmezse yerel ANSI kod sayfasi
        # kullanilir ve kodlanamayan karakter SATIRI SESSIZCE DUSURUR ->
        # utf-8 + backslashreplace. delay=True: dosya ilk kayitta acilir
        # (rotasyon kilit penceresini kucultur). TEK handler kurali: ayni dosyaya
        # ikinci handler Windows'ta doRollover'i PermissionError'a bogar.
        h = logging.handlers.RotatingFileHandler(
            d / "wisteria.log", maxBytes=1_000_000, backupCount=3,
            encoding="utf-8", errors="backslashreplace", delay=True)
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
        root.addHandler(h)
    except Exception:
        root.addHandler(logging.NullHandler())


def log_for(name: str) -> logging.Logger:
    """'wisteria.<name>' alt gunlukcusu (kok yapilandirmayi garantiler)."""
    _configure()
    return logging.getLogger(f"wisteria.{name}")


def err_brief(e: BaseException, cap: int = 200) -> str:
    """Icerik sizdirmadan istisna ozeti: TUR + kirpilmis ILK satir.

    Istisna mesajlari (ozellikle JSON/LLM hatalari) kullanici icerigi
    barindirabilir - tek satir + sinir, sizinti yuzeyini kucultur."""
    s = str(e)
    first = s.splitlines()[0] if s else ""
    return f"{type(e).__name__}: {first[:cap]}"
