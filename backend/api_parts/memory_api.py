"""Hafiza mixin'i"""

from __future__ import annotations

import threading
import time

from ..config import CONFIG
from ..logutil import err_brief, log_for
from ..memory.constants import MANUAL_FACT_TYPES
from ..memory.embedder import Embedder
from ..memory.manager import MemoryManager
from ..memory.store import MemoryStore, key_opens
from ..prompt_store import StorePromptProvider, migrate_prompts_if_needed
from ..prompts import set_prompt_provider

_log = log_for("memory_api")


class MemoryApiMixin:
    def _db_path(self):
        return CONFIG.memory_dir / "mem.db"

    def _try_remembered_unlock(self) -> None:
        try:
            key = self._vault.unlock_remembered()
        except Exception as e:
            _log.warning("remembered unlock hatasi err=%s", err_brief(e))
            key = None
        if key is None:
            # verifier kayip/bozuksa dogrulamayi DB'nin kendisi yapsin
            # (denetim K1 ilkesi: DB acilan anahtar dogru anahtardir)
            try:
                db = self._db_path()
                if db.exists():
                    rk = self._vault.unlock_remembered_unverified()
                    if rk is not None and key_opens(db, rk):
                        key = rk
                        self._vault.heal(rk)
                        _log.warning("kasa kurtarildi: remembered anahtar DB-dogrulamasiyla, verifier onarildi")
            except Exception as e:
                _log.warning("remembered kurtarma hatasi err=%s", err_brief(e))
        if key is not None:
            self._open_memory(key)

    def _open_memory(self, key: bytes) -> bool:
        try:
            CONFIG.memory_dir.mkdir(parents=True, exist_ok=True)
            self._mem_store = MemoryStore.open(self._db_path(), key)
        except Exception as e:
            _log.error("kasa acilamadi err=%s", err_brief(e))
            self._mem_store = None
            return False
        # Prompts live in the same encrypted DB. One-time migration pulls the old
        # plaintext files into the vault (verify-then-delete); the provider then
        # becomes the source of truth for build_system_prompt.
        try:
            migrate_prompts_if_needed(
                self._mem_store, self._store_lock,
                system_file=CONFIG.system_prompt,
                characters_dir=CONFIG.characters_dir,
                personas_dir=CONFIG.personas_dir)
        except Exception as e:
            # migration failure must never block the unlock
            _log.error("prompt migrasyonu basarisiz err=%s", err_brief(e))
        self._prompts = StorePromptProvider(self._mem_store, self._store_lock)
        set_prompt_provider(self._prompts)
        active_char = self._prompts.get_active("character")
        if active_char:
            self._character = active_char
        self._reset_history()  # rebuild with the REAL system prompt (init used a placeholder)
        self._activate_memory()
        return True

    def _activate_memory(self) -> None:
        """Create the manager once BOTH the store (unlocked) and the client exist."""
        if self._mem_store is None:
            return
        if self._mem is not None:
            if self._client is not None:
                self._mem.set_client(self._client)
            return
        if self._embedder is None:
            try:
                CONFIG.embed_cache_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            self._embedder = Embedder(cache_dir=str(CONFIG.embed_cache_dir))
        self._mem = MemoryManager(
            self._mem_store, self._client, embedder=self._embedder,
            keep_recent=CONFIG.mem_keep_recent, consolidate_every=CONFIG.mem_consolidate_every,
            max_facts=CONFIG.mem_max_facts, recall_k=CONFIG.mem_recall_k,
            recall_max_dist=CONFIG.mem_recall_max_dist, reflect_every=CONFIG.mem_reflect_every,
            batch_turns=CONFIG.mem_batch_turns,
            lock=self._store_lock,  # one lock for consolidation + prompts + viewer
            on_warn=lambda msg: self._emit("appNote", msg),  # arka plan hatasi kullaniciya gorunur
        )
        threading.Thread(target=self._mem.warmup, daemon=True).start()  # preload embedder off the hot path

    def close_memory(self) -> None:
        # Deliberately NO mem.flush_pending() here: it would run an LLM consolidation
        # pass and stall window close by seconds. Unconsolidated turns stay flagged in
        # the encrypted DB and fold in on the next session's consolidation trigger.
        # The store lock is held so an in-flight consolidation write finishes first
        # (closing mid-write was a race); a post-close write attempt then fails
        # inside the worker's own try/except, which is harmless at exit.
        try:
            with self._store_lock:
                set_prompt_provider(None)  # stale provider must not outlive the connection
                self._prompts = None
                if self._mem_store is not None:
                    self._mem_store.close()
                    self._mem_store = None
        except Exception as e:
            _log.warning("close_memory hatasi err=%s", err_brief(e))

    def memory_state(self) -> dict:
        if self._vault is None:
            return {"enabled": False, "initialized": False, "unlocked": False,
                    "has_data": False}
        return {
            "enabled": True,
            "initialized": self._vault.is_initialized(),
            "unlocked": self._mem_store is not None,
            # kimlik dosyalari (salt/verifier) kayip olsa da kasa DURUYOR olabilir:
            # kilit ekrani bu durumda "yeni parola olustur" DEGIL "mevcut parolanla
            # onar" dili gostermeli (dogrulama turu bulgusu)
            "has_data": self._db_path().exists(),
        }

    def memory_unlock(self, passphrase: str, remember: bool = False) -> dict:
        """Kilidi ac. DORT durum (denetim K1'in koku - dogrulamanin nihai
        kaynagi DB'nin kendisidir, kimlik dosyalari onarilabilir yardimcidir):
          A) salt+verifier saglam -> normal unlock (verifier bozuksa DB dogrular)
          B) kimlik dosyalari eksik AMA mem.db DURUYOR -> asla yeniden kurulum
             yapilmaz; dogru parola DB-dogrulamasiyla girer ve dosyalar onarilir
          C) salt yok + parola caresiz -> device.key varsa DB-dogrulamali giris,
             yoksa acik "damaged" (eski davranis: salt'i EZIP kasayi kalici
             kilitlemekti)
          D) mem.db yok -> gercek ilk kurulum."""
        if self._vault is None:
            return {"ok": False, "error": "disabled"}
        passphrase = (passphrase or "").strip()
        if not passphrase:
            return {"ok": False, "error": "empty"}
        with self._mem_lock:
            if self._mem_store is not None:
                return {"ok": True}
            db = self._db_path()
            db_exists = db.exists()
            try:
                key = None
                if self._vault.is_initialized():
                    key = self._vault.unlock(passphrase)
                    if key is None and db_exists:
                        # verifier bozulmus olabilir: yanlis paroladan DB ayirir
                        key = self._vault.recover_with_db(
                            passphrase, lambda k: key_opens(db, k))
                        if key is not None:
                            _log.warning("kasa kurtarildi: verifier DB-dogrulamasiyla yeniden yazildi")
                    if key is None:
                        return {"ok": False, "error": "wrong"}
                elif db_exists:
                    key = self._vault.recover_with_db(
                        passphrase, lambda k: key_opens(db, k))
                    if key is not None:
                        _log.warning("kasa kurtarildi: eksik kimlik dosyalari onarildi")
                    elif not self._vault.can_derive():
                        rk = self._vault.unlock_remembered_unverified()
                        if rk is not None and key_opens(db, rk):
                            key = rk
                            _log.warning("kasa kurtarildi: device.key DB-dogrulamasiyla (salt eksik)")
                    if key is None:
                        return {"ok": False,
                                "error": "wrong" if self._vault.can_derive() else "damaged"}
                else:
                    key = self._vault.initialize(passphrase)  # gercek ilk kurulum
            except Exception as e:
                _log.error("vault islemi basarisiz err=%s", err_brief(e))
                return {"ok": False, "error": "vault"}
            if not self._open_memory(key):
                # yarim kalmis parola degisimi: verifier eski anahtari onaylar
                # ama DB yeni anahtardadir - .new salt adaylari denenir
                k2 = None
                if db_exists:
                    try:
                        k2 = self._vault.recover_with_db(
                            passphrase, lambda k: key_opens(db, k))
                    except Exception:
                        k2 = None
                if k2 is None or k2 == key or not self._open_memory(k2):
                    return {"ok": False, "error": "open"}
                _log.warning("kasa kurtarildi: yarim parola degisimi tamamlandi")
                key = k2
            if remember:
                try:
                    self._vault.remember(key)
                except Exception as e:
                    _log.warning("remember yazilamadi err=%s", err_brief(e))
        return {"ok": True}

    # ----- memory viewer (three-dot menu > Hafiza) -----
    def memory_overview(self) -> dict:
        if self._mem_store is None:
            return {"ok": False, "error": "locked"}
        if self._mem is not None:
            return {"ok": True, **self._mem.overview()}
        # unlocked but the model is still loading (manager not built yet)
        with self._store_lock:
            recap = self._mem_store.get_recap()
            facts = self._mem_store.list_facts()
            episodes = self._mem_store.list_episodes(100)
            count = self._mem_store.message_count()
        return {
            "ok": True,
            "recap": recap,
            "facts": [
                {"id": f.id, "type": f.type, "text": f.text,
                 "importance": f.importance, "source": f.source}
                for f in facts
            ],
            "episodes": [{"text": t, "ts": ts} for (t, ts) in episodes],
            "message_count": count,
        }

    # Elle bilgi ekleme/duzenleme: store add_fact/update_fact zaten mevcut; manager
    # da ayni store kilidiyle calisir (onbellek yok) - dogrudan store yolu guvenli.
    # Tipler TEK kaynaktan (constants.py); elle eklenenler source='user' damgasi
    # alir ve otomatik temizlikten (decay) tipten bagimsiz MUAF tutulur.
    _FACT_TYPES = MANUAL_FACT_TYPES

    def memory_add_fact(self, type_: str, text: str, importance) -> dict:
        if self._mem_store is None:
            return {"ok": False, "error": "locked"}
        text = (text or "").strip()
        if not text or len(text) > 2000:
            return {"ok": False, "error": "text"}
        t = str(type_ or "bilgi").strip().lower()
        if t not in self._FACT_TYPES:
            t = "bilgi"
        try:
            imp = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            imp = 7
        try:
            with self._store_lock:
                fid = self._mem_store.add_fact(t, text, imp, int(time.time()),
                                               source="user")  # decay'den muaf
        except Exception:
            return {"ok": False, "error": "add"}
        return {"ok": True, "id": fid}

    def memory_update_fact(self, fid, text: str, importance) -> dict:
        if self._mem_store is None:
            return {"ok": False, "error": "locked"}
        text = (text or "").strip()
        if not text or len(text) > 2000:
            return {"ok": False, "error": "text"}
        try:
            imp = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "importance"}
        try:
            with self._store_lock:
                # panelden DUZENLENEN kayit da kullanici iradesidir: source='user'
                # terfisi olmadan, elle duzeltilen oto-kayit LLM DELETE'ine ve
                # decay'e acik kalirdi (dogrulama turu bulgusu)
                self._mem_store.update_fact(int(fid), text, imp, int(time.time()),
                                            source="user")
        except Exception:
            return {"ok": False, "error": "update"}
        return {"ok": True}

    def memory_delete_fact(self, fid: int) -> dict:
        if self._mem_store is None:
            return {"ok": False, "error": "locked"}
        try:
            fid = int(fid)  # JS dataset id'leri string gelir - iki dalda da normalize
        except (TypeError, ValueError):
            return {"ok": False, "error": "id"}
        try:
            if self._mem is not None:
                self._mem.delete_fact(fid)
            else:
                with self._store_lock:
                    self._mem_store.deactivate_fact(fid, int(time.time()))
        except Exception:
            return {"ok": False, "error": "delete"}
        return {"ok": True}

    # ----- encrypted prompts (three-dot menu > Promptlar) -----
