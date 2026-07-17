"""Output sanitization + hidden-block hiding (ported from chat-image-local.ps1).

- Streaming: StreamFilter, <think>/<observe>/<observation>/<channel>thought
  bloklarini ARTIMLI bir durum makinesiyle gizler. Emit edilen onek MONOTONDUR
  (asla geri alinmaz) ve her parca yalniz bekleyen kuyrugu tarar (toplam O(n)).
  Eski surum her parcada TUM birikimi yeniden tariyordu (O(n^2)) ve tutucu
  regex'i '< think>' gibi bosluklu acilislari kacirip parcayi SIZDIRIYORDU:
  ekrana gizli etiket kirintisi cikar, akis metni nihai metinden ayrisirdi.
- Final: strip hidden blocks defensively, collapse decorative junk, compress a
  runaway repeated tail (yalniz son 4KB - near-miss kuyrukta kuadratikti).
"""

from __future__ import annotations

import re

_TAG = r"(?:think(?:ing)?|observe|observation)"
_BLOCK_RE = re.compile(rf"(?is)<\s*{_TAG}\s*>.*?<\s*/\s*{_TAG}\s*>")
_CHANNEL_RE = re.compile(r"(?is)<\|channel\|?>\s*thought.*?(?:<\s*channel\s*\|?>|<\|)")
_OPEN_RE = re.compile(rf"(?is)<\s*{_TAG}\s*>|<\|channel\|?>\s*thought")
_STRAY_TOKEN_RE = re.compile(r"(?i)<\|?/?(channel|think|tool[_a-z]*)\|?>")

# --- artimli filtre kaliplari ---------------------------------------------
# pend'in BASINDA tam acilis var mi
_OPEN_TAG_HEAD = re.compile(rf"(?is)^<\s*{_TAG}\s*>")
_OPEN_CHAN_HEAD = re.compile(r"(?is)^<\|channel\|?>\s*thought")
# gizli blok kapanislari (acilis turune gore)
_CLOSE_TAG_RE = re.compile(rf"(?is)<\s*/\s*{_TAG}\s*>")
_CHAN_END_RE = re.compile(r"(?is)<\s*channel\s*\|?>|<\|")
# '<' ile baslayan kuyruk hala BIZIM etiketlerimizden birine buyuyebilir mi?
# Onek zincirleri KODLA uretilir (el yapimi ic ice regex dengesizlik hatasina
# acikti): _prefix_re("think") -> (?:t(?:h(?:i(?:n(?:k)?)?)?)?)? - kurulus
# geregi dengeli. Rastgele '<div' gibi metin BEKLETILMEZ, aninda akar.


def _prefix_re(word: str) -> str:
    r = ""
    for ch in reversed(word):
        r = f"(?:{re.escape(ch)}{r})?"
    return r


_WORD_PREFIXES = "|".join(_prefix_re(w) for w in ("think", "thinking", "observe", "observation"))
_CHAN_PREFIXES = "|".join(
    p + r"(?:\s*" + _prefix_re("thought") + r")?"
    for p in (_prefix_re("|channel|>"), _prefix_re("|channel>")))
_GROW_RE = re.compile(
    r"(?is)^<(?:\s*/?\s*(?:" + _WORD_PREFIXES + r")\s*>?|" + _CHAN_PREFIXES + r")$")
_MAX_HOLD = 64  # olasi bir acilis kalibinin sigabilecegi en genis pencere


def _visible(s: str) -> str:
    """Tam metin uzerinde gorunurluk (test/parite icin korunur; akista kullanilmaz)."""
    s = _BLOCK_RE.sub("", s)
    s = _CHANNEL_RE.sub("", s)
    m = _OPEN_RE.search(s)          # unclosed hidden block -> cut from its start
    if m:
        s = s[: m.start()]
    return s


class StreamFilter:
    """Feed streamed chunks; get back only the currently-safe visible delta."""

    def __init__(self) -> None:
        self.raw = ""
        self._pend = ""          # karari verilmemis kuyruk
        self._close_re = None    # None = gorunur mod; degilse aktif gizli blogun kapanisi

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self.raw += chunk
        self._pend += chunk
        return self._drain(final=False)

    def flush_tail(self) -> str:
        """Emit anything held back once the stream is complete."""
        return self._drain(final=True)

    def _drain(self, final: bool) -> str:
        out: list[str] = []
        while True:
            if self._close_re is not None:
                m = self._close_re.search(self._pend)
                if m:
                    self._pend = self._pend[m.end():]
                    self._close_re = None
                    continue
                # kapanis henuz yok: gizli icerigin yalniz kuyruk penceresi
                # tutulur (kapanis etiketi parca sinirini asabilir)
                if len(self._pend) > _MAX_HOLD:
                    self._pend = self._pend[-_MAX_HOLD:]
                if final:
                    self._pend = ""  # kapanmamis gizli blok sonsuza dek gizli
                    self._close_re = None
                return "".join(out)
            i = self._pend.find("<")
            if i == -1:
                out.append(self._pend)
                self._pend = ""
                return "".join(out)
            out.append(self._pend[:i])
            self._pend = self._pend[i:]
            m = _OPEN_TAG_HEAD.match(self._pend)
            if m:
                self._close_re = _CLOSE_TAG_RE
                self._pend = self._pend[m.end():]
                continue
            m = _OPEN_CHAN_HEAD.match(self._pend)
            if m:
                self._close_re = _CHAN_END_RE
                self._pend = self._pend[m.end():]
                continue
            if not final and len(self._pend) <= _MAX_HOLD and _GROW_RE.match(self._pend):
                return "".join(out)  # hala etikete buyuyebilir: beklet
            out.append(self._pend[0])  # etiket degil: '<' gorunur metindir
            self._pend = self._pend[1:]


def final_clean(text: str) -> str:
    """Whole-message cleanup for the stored/committed reply."""
    if not text:
        return ""
    t = _BLOCK_RE.sub("", text)
    t = _CHANNEL_RE.sub("", t)
    t = _STRAY_TOKEN_RE.sub("", t)
    # collapse decorative symbol runs
    t = re.sub(r"([@#%&*+=_~-])\1{3,}", r"\1\1", t)
    t = re.sub(r"([!?.])\1{4,}", r"\1\1\1", t)
    # compress a runaway repeated word/phrase tail - YALNIZ son 4KB'de:
    # near-miss kuyrukta backtracking kuadratik (60KB'de 6.6sn CPU olculmustu)
    _rep = r"(?is)(\b[\w'-]{2,}\b)(?:\s+\1){4,}\s*$"
    if len(t) > 4096:
        t = t[:-4096] + re.sub(_rep, r"\1", t[-4096:])
    else:
        t = re.sub(_rep, r"\1", t)
    return t.strip()
