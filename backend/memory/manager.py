"""3-tier memory orchestration.

Builds the compact memory block injected into the system prompt every turn (Tier 2
facts + Tier 1 recap + Tier 3 on-demand vector recall), and folds evicted raw turns
into memory ASYNCHRONOUSLY so a turn is never blocked by summarization.

Thread-safety: one RLock guards the (single) encrypted connection. The lock is held
ONLY for fast DB ops — never during the slow LLM extract/recap calls or embedding —
so the async consolidation worker never stalls the per-turn build_block().
"""

from __future__ import annotations

import random
import re
import threading
import time

import apsw

from ..logutil import err_brief, log_for
from .constants import FACT_TYPES, PROTECTED_FACT_TYPES
from .embedder import Embedder
from .summarizer import LLMUnavailable, Summarizer

_log = log_for("memory.manager")


def _flat(s: str) -> str:
    """Prompta giren kayit metinlerinde satir sonu birakma: saklanan bir metnin
    'NEW MESSAGES:' gibi bolum basliklarini taklit etmesi YAPISAL olarak
    imkansizlasir (UI/DB'deki orijinal metin degismez, yalnizca enjeksiyon ani)."""
    return re.sub(r"\s*\n\s*", " / ", s or "").strip()

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
        batch_turns: int = 40,         # bir konsolidasyon gecisinin azami tur sayisi
        lock: threading.RLock | None = None,  # share the app-wide store lock (one apsw con)
        on_warn=None,                  # UI'a tek seferlik uyari kanali (icerik tasimaz)
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
        self.batch_turns = batch_turns
        self._lock = lock or threading.RLock()
        self._consolidating = False
        self._consol_count = 0
        self._on_warn = on_warn
        # Zaman-kapili devre kesici (timer YOK - cikis asma tuzaklarina girmeyiz):
        # basarisizlikta bir sonraki deneme zamani ileri atilir; denemeler yeni
        # turlarla dogal tetiklenir. 5 ardisik basarisizlik = 5 dk sogutma + uyari;
        # 20'de OTURUM BOYU durdurma (deterministik hata sonsuza dek denenmez,
        # turlar imzalanmadan birikir ve sonraki oturumda islenir).
        self._fail_count = 0
        self._next_attempt_ts = 0.0
        self._warned_suspend = False
        self._warned_giveup = False
        self._record_fails = 0
        self._warned_record = False

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
            for f in facts:
                tag = f" [{f.type}]" if f.type in _TAG_TYPES else ""
                lines.append(f"- {_flat(f.text)}{tag}")
        if recap:
            lines.append("")
            lines.append(f"Story so far: {_flat(recap)}")
        recalled = self._recall(query_text)
        if recalled:
            lines.append("")
            lines.append("You also recall from earlier:")
            lines.extend(f"- {_flat(t)}" for t in recalled)
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
                {"id": f.id, "type": f.type, "text": f.text,
                 "importance": f.importance, "source": f.source}
                for f in facts
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
        except Exception as e:
            _log.warning("recall embed hatasi err=%s", err_brief(e))
            return []
        with self._lock:
            try:
                hits = self.store.search_episodes(emb, k=self.recall_k)
            except Exception as e:
                _log.warning("recall arama hatasi err=%s", err_brief(e))
                return []
        return [t for (t, dist) in hits if dist <= self.recall_max_dist]

    # ------------------------------------------------------- recording + consolidation
    def record_turn(self, user_text: str, assistant_text: str, ts: int | None = None) -> None:
        ts = ts or int(time.time())
        try:
            with self._lock:
                if user_text and user_text.strip():
                    self.store.add_message("user", user_text, ts)
                if assistant_text and assistant_text.strip():
                    self.store.add_message("assistant", assistant_text, ts)
        except Exception as e:
            _log.error("record_turn DB yazma hatasi err=%s", err_brief(e))
            # kalici yazim sorunu arayuz yesilken sessiz kalamaz: 5 ardisikta TEK uyari
            self._record_fails += 1
            if self._record_fails >= 5 and not self._warned_record and self._on_warn:
                self._warned_record = True
                try:
                    self._on_warn("Hafıza kaydı yazılamıyor; konuşma geçici olarak "
                                  "hafızaya işlenmiyor. Ayrıntı: userdata/logs")
                except Exception:
                    pass
            return  # tur kaydedilemedi - konsolidasyonu tetiklemenin anlami yok
        self._record_fails = 0
        self._maybe_consolidate()

    def _maybe_consolidate(self) -> None:
        if time.time() < self._next_attempt_ts:
            return  # devre acik/geri cekilme: siradaki dogal tetiklemede tekrar bakilir
        with self._lock:
            if self._consolidating:
                return
            if len(self.store.unconsolidated(self.keep_recent)) < self.consolidate_every:
                return
            self._consolidating = True
        try:
            threading.Thread(target=self._consolidate_worker, daemon=True).start()
        except Exception as e:
            # start() patlarsa bayrak takili kalir ve konsolidasyon OTURUM BOYU
            # olurdu - bayragi geri al + logla
            with self._lock:
                self._consolidating = False
            _log.error("consolidate thread baslatilamadi err=%s", err_brief(e))

    def _note_failure(self, stage: str, e: BaseException) -> None:
        """Basarisizlik muhasebesi: ustel geri cekilme -> 5'te sogutma + TEK uyari ->
        20'de OTURUM BOYU durdurma. Kalici 4xx (408/429 haric) 5'lik adimla sayilir:
        deterministik hata (or. desteklenmeyen json_schema) sonsuza dek denenmez."""
        status = getattr(e, "status", None)
        permanent = status is not None and 400 <= int(status) < 500 and int(status) not in (408, 429)
        self._fail_count += 5 if permanent else 1
        _log.warning("consolidate %s basarisiz (sayac %d%s) err=%s",
                     stage, self._fail_count, " kalici-4xx" if permanent else "",
                     err_brief(e))
        if self._fail_count >= 20:
            self._next_attempt_ts = float("inf")  # bu oturumda bir daha denenmez
            if not self._warned_giveup:
                self._warned_giveup = True
                _log.error("consolidate bu oturum icin DURDURULDU (sayac %d); "
                           "turlar imzalanmadan birikiyor, sonraki oturum dener", self._fail_count)
                if self._on_warn:
                    try:
                        self._on_warn("Hafıza özetleme bu oturum için durduruldu; "
                                      "konuşma kaydediliyor, sonraki açılışta işlenecek. "
                                      "Ayrıntı: userdata/logs")
                    except Exception:
                        pass
            return
        if self._fail_count >= 5:
            self._next_attempt_ts = time.time() + 300  # 5 dk sogutma (devre acik)
            if not self._warned_suspend:
                self._warned_suspend = True
                _log.error("consolidate askiya alindi (5+ hata) - 5 dk sonra yeniden denenecek")
                if self._on_warn:
                    try:
                        self._on_warn("Hafıza güncelleyici arka arkaya hata aldı; "
                                      "bir süreliğine duraklatıldı. Ayrıntı: userdata/logs")
                    except Exception:
                        pass
        else:
            delay = min(120.0, 5.0 * (2 ** (self._fail_count - 1)))
            self._next_attempt_ts = time.time() + delay * random.uniform(0.8, 1.2)

    def _note_success(self) -> None:
        if self._fail_count:
            _log.info("consolidate toparlandi (%d basarisiz denemeden sonra)", self._fail_count)
        self._fail_count = 0
        self._next_attempt_ts = 0.0
        self._warned_suspend = False
        self._warned_giveup = False

    def _consolidate_worker(self) -> None:
        """Uc adim, her adimin KENDI hata sinifi var (tek dev try yok):
        toplama/DB -> yeniden dene; LLM ulasamiyor -> yeniden dene (turlar
        imzalanmaz, hafiza kaybolmaz); icerik/parse tuhafligi -> logla + degrade."""
        ok = False
        try:
            if time.time() < self._next_attempt_ts:
                return  # gate'i gectikten sonra baska thread'in hatasi devreyi acmis olabilir
            # ---- 1) topla (kilit altinda, hizli) ----
            try:
                with self._lock:
                    # batch cap: kesinti birikintisi parca parca islenir (tek dev
                    # istek 700 token'da kesilip sessiz toplu kayba donusuyordu);
                    # ledger de prompt icin sinirlanir (korumali tip enflasyonu
                    # extract istegini sinirsiz buyutemez).
                    # ARDISIK HATADA KUCULEN BATCH: ayni 40-turluk paket her
                    # seferinde ayni sekilde patliyorsa (or. ops ciktisi 700
                    # token'a sigmiyor) sabit boy sonsuz kama olurdu - her
                    # basarisizlik boyu yariya indirir (40->20->10->5), basari
                    # sayaci sifirlayinca boy da geri buyur.
                    eff_batch = max(5, self.batch_turns // (2 ** min(self._fail_count, 3)))
                    pending = self.store.unconsolidated(self.keep_recent, limit=eff_batch)
                    ledger = self.store.list_facts(limit=200)
                    recap = self.store.get_recap()
            except Exception as e:
                self._note_failure("toplama/DB", e)
                return
            if not pending:
                ok = True
                return
            ids = [pid for (pid, _role, _content) in pending]
            turns_text = "\n".join(f"{role}: {content}" for (_id, role, content) in pending)

            # ---- 2) yavas is, kilit YOK ----
            try:
                ops = self.summarizer.extract_ops(ledger, turns_text)
                recap_add = self.summarizer.update_recap(recap, turns_text)
            except LLMUnavailable as e:
                self._note_failure("llm", e)   # turlar IMZALANMADI - sonra tekrar
                return
            # Episode = temiz Ingilizce ozet. Ozet uretilemediyse episode ATLANIR:
            # eski fallback ham "user:/assistant:" dokumunu saklayip sonraki
            # turlara geri enjekte ediyordu (rol onekli ham diyalog kirliligi).
            episode_text = recap_add.strip()
            episode_emb = None
            if episode_text:
                try:
                    episode_emb = self.embedder.encode_one(episode_text)
                except Exception as e:
                    _log.warning("episode embed atlandi err=%s", err_brief(e))
            else:
                _log.info("episode atlandi (ozet bos) - turlar yine de imzalanacak")

            # ---- 3) uygula + imzala (kilit altinda, TEK transaction) ----
            # apsw autocommit: transaction'siz bu blok cokme aninda YARIM kalirdi
            # (fact yazildi, tur imzalanmadi -> sonraki oturum ayni turlari yeniden
            # isler, kayitlar/ozet ciftlenir). Simdi ya hepsi ya hicbiri.
            try:
                now = int(time.time())
                with self._lock:
                    with self.store.transaction():
                        self._apply_ops(ops, now)
                        if recap_add:
                            merged = f"{recap} {recap_add}".strip() if recap else recap_add
                            self.store.set_recap(self._trim_recap(merged), now)
                        if episode_emb is not None:
                            self.store.add_episode(episode_text, episode_emb, now)
                        self.store.mark_consolidated(ids)
            except Exception as e:
                self._note_failure("uygulama/DB", e)
                return
            ok = True
            self._note_success()
            self._maybe_reflect()  # periodic ledger cleanup (off the per-turn path)
        except Exception as e:
            # beklenmedik - logla, geri cekilme uygula, ASLA sessiz gecme
            self._note_failure("beklenmedik", e)
        finally:
            with self._lock:
                self._consolidating = False
            if ok:
                self._maybe_consolidate()  # biz calisirken birikmis olabilir

    def _apply_ops(self, ops: list[dict], now: int) -> None:
        """LLM operasyonlari, decay ile AYNI korumalardan gecer (denetim Y4):
        kullanicinin elle ekledigi kayitlara (source='user') hicbir op dokunamaz;
        korumali tipler LLM tarafindan SILINEMEZ; ADD tipi whitelist'ten gecer.
        Tek halusinasyonlu DELETE, kullanicinin sabitledigi kaydi dusurebiliyordu."""
        for op in ops:
            kind = op.get("op")
            try:
                if kind == "ADD":
                    ftype = op.get("type", "fact")
                    if ftype not in FACT_TYPES:
                        ftype = "fact"
                    self.store.add_fact(ftype, op["text"],
                                        int(op.get("importance", 5)), now)
                elif kind in ("UPDATE", "DELETE"):
                    fid = int(op["id"])
                    cur = self.store.get_fact(fid)
                    if cur is None:
                        continue  # zaten pasif/yok
                    if cur.source == "user":
                        _log.info("apply_op korundu (source=user) op=%s id=%d", kind, fid)
                        continue
                    if kind == "DELETE" and cur.type in PROTECTED_FACT_TYPES:
                        _log.info("apply_op korundu (tip korumasi) id=%d", fid)
                        continue
                    if kind == "UPDATE":
                        self.store.update_fact(fid, op["text"],
                                               int(op.get("importance", 5)), now)
                    else:
                        self.store.deactivate_fact(fid, now)
            except apsw.Error:
                # DB-seviyesi hata ICERIK hatasi degildir: yutulursa transaction,
                # kaydi DUSMUS ama turlari IMZALANMIS halde commit edebilirdi
                # ("ya hepsi ya hicbiri" delinirdi). Yukari cikar -> rollback + retry.
                raise
            except Exception as e:
                # icerik LOGLANMAZ: yalniz op turu + id + hata ozeti
                _log.warning("apply_op atlandi op=%s id=%s err=%s",
                             kind, op.get("id"), err_brief(e))
                continue

    # ---- reflection: periodic ledger hygiene (dedup / decay / insights) -----
    def _maybe_reflect(self) -> None:
        self._consol_count += 1
        if self.reflect_every > 0 and self._consol_count % self.reflect_every == 0:
            self._reflect()

    def _reflect(self) -> None:
        """Istege bagli hijyen gecisi: hatasi konsolidasyonu ASLA geri sarmaz."""
        try:
            with self._lock:
                ledger = self.store.list_facts()
            if not ledger:
                return
            try:
                ops = self.summarizer.reflect(ledger)   # slow LLM pass, NO lock held
            except LLMUnavailable as e:
                _log.warning("reflect atlandi (llm ulasilamaz) err=%s", err_brief(e))
                return
            now = int(time.time())
            with self._lock:
                with self.store.transaction():       # yarim hijyen gecisi kalmasin
                    self._apply_ops(ops, now)        # ADD insights + DELETE dupes/contradictions
                    self._decay(now)
        except Exception as e:
            _log.warning("reflect hatasi err=%s", err_brief(e))

    def _decay(self, now: int) -> None:
        """Bound ledger growth: deactivate lowest-importance NON-protected facts past the cap.

        Muafiyet iki katmanli: korumali TIPLER (tek kaynak: constants) VE kullanicinin
        panelden elle ekledigi HER kayit (source='user') - kullanici 'hatirla' dediyse
        otomatik temizlik ona dokunamaz."""
        facts = self.store.list_facts()          # active, importance desc
        if len(facts) <= self.max_active_facts:
            return
        droppable = [f for f in facts
                     if f.type not in PROTECTED_FACT_TYPES and f.source != "user"]
        overflow = len(facts) - self.max_active_facts
        dropped = 0
        for f in sorted(droppable, key=lambda x: x.importance)[:overflow]:
            self.store.deactivate_fact(f.id, now)
            dropped += 1
        if dropped:
            _log.info("decay: %d kayit pasife alindi (tavan %d)", dropped, self.max_active_facts)

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
        try:
            self._reembed_missing()
        except Exception as e:
            _log.warning("reembed gecisi hatasi err=%s", err_brief(e))

    def _reembed_missing(self) -> None:
        """Vektoru eksik episode'lari tamamla: embed boyutu gocunden (vec tablosu
        yeniden kuruldu) ya da eski yarim yazimlardan kalan satirlar yeniden
        aranabilir olur. Sicak yoldan uzak (warmup thread'i), kilit kisa tutulur."""
        with self._lock:
            have = self.store.vec_rowids()
            eps = self.store.episodes_all()
        missing = [(rid, txt) for (rid, txt) in eps if rid not in have]
        if not missing:
            return
        _log.info("reembed: %d episode vektoru tamamlaniyor", len(missing))
        done = 0
        for rid, txt in missing:
            try:
                emb = self.embedder.encode_one(txt)  # kilitsiz: CPU embed
                with self._lock:
                    self.store.add_episode_vector(rid, emb)
                done += 1
            except Exception as e:
                _log.warning("reembed atlandi id=%s err=%s", rid, err_brief(e))
        if done:
            _log.info("reembed tamam: %d/%d", done, len(missing))

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
