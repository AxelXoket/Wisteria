"""Hafiza alt sisteminin TEK-KAYNAK sabitleri.

Bu degerler daha once uc ayri dosyada bagimsiz kopyalarla yasiyordu (fact tip
listeleri summarizer/api'de farkli, EMBED_DIM store/embedder'da elle esitlenmis).
Kopyalar birbirinden habersiz degisebildigi icin gercek veri riskiydi - artik
herkes buradan okur.
"""

from __future__ import annotations

from typing import NamedTuple

# LLM'in konsolidasyonda uretebildigi tipler (ops semasinin enum'u).
FACT_TYPES = ("identity", "preference", "milestone", "commitment", "boundary",
              "fact", "insight")

# Kullanicinin hafiza panelinden ELLE ekleyebildigi tipler.
MANUAL_FACT_TYPES = ("bilgi", "identity", "preference", "milestone")

# Otomatik temizligin (decay) ASLA dokunamayacagi tipler. 'bilgi' burada cunku
# panelin varsayilan tipi o: kullanicinin bilerek ekledigi kayit korunmali.
# (Ayrica source='user' kayitlar tipten bagimsiz muaftir - bkz. manager._decay.)
PROTECTED_FACT_TYPES = frozenset({"identity", "commitment", "boundary",
                                  "milestone", "bilgi"})

# Embedding boyutu: paraphrase-multilingual-MiniLM-L12-v2 (bkz. embedder.py).
# vec_episodes tablosu bu boyutla olusur; embedder ilk kodlamada dogrular.
EMBED_DIM = 384


class FactRow(NamedTuple):
    """facts tablosunun isimli satiri - pozisyonla (f[3]) erisim yasak bolge:
    sorgu sutun sirasi degisirse pozisyon SESSIZCE yanlis alani okur, isimli
    erisim ya dogru calisir ya acikca patlar."""
    id: int
    type: str
    text: str
    importance: int
    source: str  # 'auto' (LLM cikarimi) | 'user' (panelden elle eklendi)
