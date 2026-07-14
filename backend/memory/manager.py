"""3-tier memory orchestration.

Builds the compact memory block injected into the system prompt every turn (Tier 2
facts + Tier 1 recap + Tier 3 on-demand vector recall), and folds evicted raw turns
into memory ASYNCHRONOUSLY so a turn is never blocked by summarization.

Thread-safety: one RLock guards the (single) encrypted connection. The lock is held
ONLY for fast DB ops — never during the slow LLM extract/recap calls or embedding —
so the async consolidation worker never stalls the per-turn build_block().
"""

from __future__ import annotations

import threading
import time

from .embedder import Embedder
from .summarizer import Summarizer

_BLOCK_HEADER = (
    "[MEMORY — what you genuinely remember about this person from before; "
    "weave it in naturally when relevant, never recite or list it]"
)
_TAG_TYPES = ("boundary", "commitment", "milestone")


class MemoryManager:
    def __init__(
        self,
        store,
        client,
        embedder: Embedder | None = None,
        *,
        keep_recent: int = 10,
        consolidate_every: int = 2,
        max_facts: int = 25,
        recall_k: int = 3,
        recall_max_dist: float = 1.2,  # calibrated: relevant <=1.13, unrelated ~1.33 (L2 on unit vecs)
        recap_max_chars: int = 1400,
        reflect_every: int = 15,       # consolidations between reflection passes (0 = off)
        max_active_facts: int = 50,    # hard cap on the ledger; overflow is decayed
        lock: threading.RLock | None = None,  # share the app-wide store lock (one apsw con)
    ) -> None:
        self.store = store
        self.client = client
        self.embedder = embedder or Embedder()
        self.summarizer = Summarizer(client)
        self.keep_recent = keep_recent
        self.consolidate_every = consolidate_every
        self.max_facts = max_facts
        self.recall_k = recall_k
        self.recall_max_dist = recall_max_dist
        self.recap_max_chars = recap_max_chars
        self.reflect_every = reflect_every
        self.max_active_facts = max_active_facts
        self._lock = lock or threading.RLock()
        self._consolidating = False
        self._consol_count = 0

    def set_client(self, client) -> None:
        """Attach the LLM client once the model is ready (memory may unlock first)."""
        self.client = client
        self.summarizer = Summarizer(client)

    # ------------------------------------------------------- injected every turn
    def build_block(self, query_text: str = "") -> str:
        """The memory text to splice into the system prompt for this turn."""
        with self._lock:
            facts = self.store.list_facts(limit=self.max_facts)
            recap = self.store.get_recap()
        lines: list[str] = []
        if facts:
            lines.append("Known facts about the user and your relationship:")
            for (_id, typ, text, _imp) in facts:
                tag = f" [{typ}]" if typ in _TAG_TYPES else ""
                lines.append(f"- {text}{tag}")
        if recap:
            lines.append("")
            lines.append(f"Story so far: {recap}")
        recalled = self._recall(query_text)
        if recalled:
            lines.append("")
            lines.append("You also recall from earlier:")
            lines.extend(f"- {t}" for t in recalled)
        return (_BLOCK_HEADER + "\n" + "\n".join(lines)) if lines else ""

    # ------------------------------------------------------- memory-viewer reads
    def overview(self, episodes_limit: int = 100) -> dict:
        """Everything the 'what she remembers' panel shows, read under the lock."""
        with self._lock:
            recap = self.store.get_recap()
            facts = self.store.list_facts()  # active only, importance desc
            episodes = self.store.list_episodes(episodes_limit)
            count = self.store.message_count()
        return {
            "recap": recap,
            "facts": [
                {"id": i, "type": t, "text": x, "importance": imp}
                for (i, t, x, imp) in facts
            ],
            "episodes": [{"text": t, "ts": ts} for (t, ts) in episodes],
            "message_count": count,
        }

    def delete_fact(self, fid: int) -> None:
        with self._lock:
            self.store.deactivate_fact(int(fid), int(time.time()))

    def _recall(self, query_text: str) -> list[str]:
        if not query_text.strip():
            return []
        try:
            emb = self.embedder.encode_one(query_text)  # no lock: CPU embed
        except Exception:
            return []
        with self._lock:
            try:
                hits = self.store.search_episodes(emb, k=self.recall_k)
            except Exception:
                return []
        return [t for (t, dist) in hits if dist <= self.recall_max_dist]

    # ------------------------------------------------------- recording + consolidation
    def record_turn(self, user_text: str, assistant_text: str, ts: int | None = None) -> None:
        ts = ts or int(time.time())
        with self._lock:
            if user_text and user_text.strip():
                self.store.add_message("user", user_text, ts)
            if assistant_text and assistant_text.strip():
                self.store.add_message("assistant", assistant_text, ts)
        self._maybe_consolidate()

    def _maybe_consolidate(self) -> None:
        with self._lock:
            if self._consolidating:
                return
            if len(self.store.unconsolidated(self.keep_recent)) < self.consolidate_every:
                return
            self._consolidating = True
        threading.Thread(target=self._consolidate_worker, daemon=True).start()

    def _consolidate_worker(self) -> None:
        try:
            with self._lock:
                pending = self.store.unconsolidated(self.keep_recent)
                ledger = self.store.list_facts()
                recap = self.store.get_recap()
            if not pending:
                return
            ids = [pid for (pid, _role, _content) in pending]
            turns_text = "\n".join(f"{role}: {content}" for (_id, role, content) in pending)

            # ---- slow work, NO lock held ----
            ops = self.summarizer.extract_ops(ledger, turns_text)
            recap_add = self.summarizer.update_recap(recap, turns_text)
            # Store the clean English summary as the episode (better recall + cleaner
            # injection than the raw "user:/assistant:" blob); fall back to the turns.
            episode_text = recap_add.strip() if recap_add.strip() else turns_text[:1200]
            try:
                episode_emb = self.embedder.encode_one(episode_text)
            except Exception:
                episode_emb = None

            # ---- fast apply, lock held ----
            now = int(time.time())
            with self._lock:
                self._apply_ops(ops, now)
                if recap_add:
                    merged = f"{recap} {recap_add}".strip() if recap else recap_add
                    self.store.set_recap(self._trim_recap(merged), now)
                if episode_emb is not None:
                    self.store.add_episode(episode_text, episode_emb, now)
                self.store.mark_consolidated(ids)
            self._maybe_reflect()  # periodic ledger cleanup (off the per-turn path)
        except Exception:
            pass
        finally:
            with self._lock:
                self._consolidating = False
            # something may have piled up while we worked
            self._maybe_consolidate()

    def _apply_ops(self, ops: list[dict], now: int) -> None:
        for op in ops:
            kind = op.get("op")
            try:
                if kind == "ADD":
                    self.store.add_fact(op.get("type", "fact"), op["text"],
                                        int(op.get("importance", 5)), now)
                elif kind == "UPDATE":
                    self.store.update_fact(int(op["id"]), op["text"],
                                           int(op.get("importance", 5)), now)
                elif kind == "DELETE":
                    self.store.deactivate_fact(int(op["id"]), now)
            except Exception:
                continue

    # ---- reflection: periodic ledger hygiene (dedup / decay / insights) -----
    def _maybe_reflect(self) -> None:
        self._consol_count += 1
        if self.reflect_every > 0 and self._consol_count % self.reflect_every == 0:
            self._reflect()

    def _reflect(self) -> None:
        with self._lock:
            ledger = self.store.list_facts()
        if not ledger:
            return
        ops = self.summarizer.reflect(ledger)   # slow LLM pass, NO lock held
        now = int(time.time())
        with self._lock:
            self._apply_ops(ops, now)            # ADD insights + DELETE dupes/contradictions
            self._decay(now)

    def _decay(self, now: int) -> None:
        """Bound ledger growth: deactivate lowest-importance NON-protected facts past the cap."""
        facts = self.store.list_facts()          # active, importance desc
        if len(facts) <= self.max_active_facts:
            return
        protected = {"identity", "commitment", "boundary", "milestone"}
        droppable = [f for f in facts if f[1] not in protected]
        overflow = len(facts) - self.max_active_facts
        for f in sorted(droppable, key=lambda x: x[3])[:overflow]:  # x[3] = importance
            self.store.deactivate_fact(f[0], now)

    def _trim_recap(self, text: str) -> str:
        """Hard cap so the recap can't grow unbounded (drops oldest at a sentence break).
        Durable detail is preserved in the fact ledger, so trimming prose is safe."""
        if len(text) <= self.recap_max_chars:
            return text
        cut = text[len(text) - self.recap_max_chars:]
        i = cut.find(". ")
        return cut[i + 2:] if i != -1 else cut

    def warmup(self) -> None:
        try:
            self.embedder.warmup()
        except Exception:
            pass

    def flush_pending(self) -> None:
        """Synchronously consolidate anything pending.

        Intentionally NOT wired into app close (it would run LLM calls and stall the
        window for seconds — see JsApi.close_memory). Kept for a future maintenance
        action; pending rows persist and consolidate next session regardless.
        """
        with self._lock:
            if self._consolidating:
                return
            if not self.store.unconsolidated(self.keep_recent):
                return
            self._consolidating = True
        self._consolidate_worker()
