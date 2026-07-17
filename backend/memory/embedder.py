"""Local multilingual text embeddings for episodic memory recall.

fastembed (ONNX, CPU) - zero VRAM, never competes with the LLM or the voice engine.
A MULTILINGUAL model is required here: the user may write in one language while the
assistant replies in another, so a query in either language must still match the
memory. Embeddings are L2-normalized so vec0's default L2 distance ranks like
cosine similarity.

Lazy-loaded; the first call downloads the model (~220 MB) once, then it's cached.
"""

from __future__ import annotations

import threading

from ..logutil import log_for
from .constants import EMBED_DIM

_log = log_for("memory.embedder")

# 50+ languages, ~220 MB. Boyut TEK kaynaktan (constants.EMBED_DIM) - eskiden
# burada elle esitlenen ikinci bir kopya vardi; model degisiminde sessiz
# uyusmazlik riskiydi.
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIM = EMBED_DIM


class Embedder:
    def __init__(self, model_name: str = MODEL_NAME, cache_dir: str | None = None) -> None:
        self._name = model_name
        self._cache_dir = cache_dir  # persistent path so the model isn't re-downloaded
        self._model = None
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()  # onnxruntime Run() is not safe to call concurrently

    @property
    def ready(self) -> bool:
        return self._model is not None

    def _ensure(self):
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from fastembed import TextEmbedding
                    kw = {"cache_dir": self._cache_dir} if self._cache_dir else {}
                    self._model = TextEmbedding(self._name, **kw)
        return self._model

    def warmup(self) -> None:
        """Force the (slow, one-time) model load/download off the hot path."""
        self.encode_one("warmup")

    def encode(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        model = self._ensure()
        with self._infer_lock:                      # serialize inference across threads
            vecs = list(model.embed(list(texts)))
        out: list[list[float]] = []
        for vec in vecs:
            a = np.asarray(vec, dtype=np.float32)
            if a.shape[-1] != EMBED_DIM:
                # ACIK hata: model degisti ve boyut artik tabloyla uyusmuyor.
                # Eski davranis bunu yukarida sessizce yutar, Tier-3 habersiz olurdu.
                _log.error("embedding boyutu %d != beklenen %d (model: %s)",
                           a.shape[-1], EMBED_DIM, self._name)
                raise RuntimeError(
                    f"embedding dim {a.shape[-1]} != {EMBED_DIM}; "
                    "model degistiyse constants.EMBED_DIM guncellenmeli")
            norm = float(np.linalg.norm(a)) or 1.0
            out.append((a / norm).tolist())
        return out

    def encode_one(self, text: str) -> list[float]:
        return self.encode([text])[0]
