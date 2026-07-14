"""Output sanitization + hidden-block hiding (ported from chat-image-local.ps1).

- Streaming: a StreamFilter hides <think>/<observe>/<observation>/<channel>thought
  spans as they arrive, and holds back partial tags across chunk boundaries.
- Final: strip hidden blocks defensively, collapse decorative junk, compress a
  runaway repeated tail.
"""

from __future__ import annotations

import re

_TAG = r"(?:think(?:ing)?|observe|observation)"
_BLOCK_RE = re.compile(rf"(?is)<\s*{_TAG}\s*>.*?<\s*/\s*{_TAG}\s*>")
_CHANNEL_RE = re.compile(r"(?is)<\|channel\|?>\s*thought.*?(?:<\s*channel\s*\|?>|<\|)")
_OPEN_RE = re.compile(rf"(?is)<\s*{_TAG}\s*>|<\|channel\|?>\s*thought")
# a trailing, not-yet-complete '<...tag' that could be the start of a hidden tag
_PARTIAL_TAIL_RE = re.compile(r"<[|/a-zA-Z][\w|]*$")
_STRAY_TOKEN_RE = re.compile(r"(?i)<\|?/?(channel|think|tool[_a-z]*)\|?>")


def _visible(s: str) -> str:
    s = _BLOCK_RE.sub("", s)
    s = _CHANNEL_RE.sub("", s)
    m = _OPEN_RE.search(s)          # unclosed hidden block -> cut from its start
    if m:
        s = s[: m.start()]
    p = _PARTIAL_TAIL_RE.search(s)  # hold back a partial tag at the very end
    if p:
        s = s[: p.start()]
    return s


class StreamFilter:
    """Feed streamed chunks; get back only the currently-safe visible delta."""

    def __init__(self) -> None:
        self.raw = ""
        self._emitted = 0

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self.raw += chunk
        vis = _visible(self.raw)
        out = vis[self._emitted:]
        self._emitted = len(vis)
        return out

    def flush_tail(self) -> str:
        """Emit anything held back once the stream is complete."""
        vis = _BLOCK_RE.sub("", self.raw)
        vis = _CHANNEL_RE.sub("", vis)
        m = _OPEN_RE.search(vis)
        if m:
            vis = vis[: m.start()]
        out = vis[self._emitted:]
        self._emitted = len(vis)
        return out


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
    # compress a runaway repeated word/phrase tail
    t = re.sub(r"(?is)(\b[\w'-]{2,}\b)(?:\s+\1){4,}\s*$", r"\1", t)
    return t.strip()
