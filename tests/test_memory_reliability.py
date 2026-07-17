"""Hafiza guvenilirligi: devre kesici, sessiz-kayip yasagi, migrasyon, decay korumalari.

Gercek MemoryStore (TEMP'te sifreli) + sahte LLM/embedder ile kosar; model gerekmez.
"""
import pathlib
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.memory.constants import PROTECTED_FACT_TYPES, FactRow
from backend.memory.manager import MemoryManager
from backend.memory.store import MemoryStore
from backend.memory.summarizer import LLMUnavailable

TMP = pathlib.Path(tempfile.mkdtemp())
KEY = b"k" * 32


class FakeStore:
    def __init__(self):
        self.marked = []
    def unconsolidated(self, keep, limit=None):
        return [(1, "user", "selam"), (2, "assistant", "merhaba")]
    def list_facts(self, limit=None):
        return []
    def get_recap(self):
        return ""
    def mark_consolidated(self, ids):
        self.marked.append(ids)
    def set_recap(self, *a):
        pass
    def add_episode(self, *a):
        pass
    def get_fact(self, fid):
        return None
    def vec_rowids(self):
        return set()
    def episodes_all(self):
        return []
    def transaction(self):
        from contextlib import nullcontext
        return nullcontext()


class DownSummarizer:
    def extract_ops(self, ledger, turns):
        raise LLMUnavailable("ConnectError: down")
    def update_recap(self, r, t):
        raise LLMUnavailable("ConnectError: down")


class OkSummarizer:
    def extract_ops(self, l, t):
        return []
    def update_recap(self, r, t):
        return "Two friends chatted."


class FakeEmb:
    def encode_one(self, t):
        return [0.0]
    def warmup(self):
        pass


def test_circuit_breaker():
    warns = []
    m = MemoryManager(FakeStore(), client=None, embedder=FakeEmb(),
                      lock=threading.RLock(), consolidate_every=1,
                      on_warn=lambda s: warns.append(s))
    m.summarizer = DownSummarizer()
    for _ in range(5):
        m._next_attempt_ts = 0
        m._consolidating = True
        m._consolidate_worker()
    assert m._fail_count == 5
    assert m._next_attempt_ts > time.time() + 200, "sogutma kurulmadi"
    assert len(warns) == 1 and "duraklat" in warns[0]
    assert m.store.marked == [], "LLM kapaliyken turlar IMZALANMAMALI (hafiza kaybi)"
    m._consolidating = False
    m._maybe_consolidate()
    assert not m._consolidating, "sogutmada yeni is baslamamali"
    # toparlanma
    m.summarizer = OkSummarizer()
    m._next_attempt_ts = 0
    m._consolidating = True
    m._consolidate_worker()
    assert m._fail_count == 0 and m.store.marked, "toparlanmada imzalanmali"
    print("1) devre kesici + sessiz-kayip yasagi + toparlanma OK")


def test_migration_and_decay():
    # eski semali (source kolonsuz) kasa -> acilista migrasyon
    import apsw
    con = apsw.Connection(str(TMP / "old.db"))
    con.pragma("hexkey", KEY.hex())
    con.execute("PRAGMA user_version")
    con.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, type TEXT, text TEXT,"
                " importance INTEGER, active INTEGER DEFAULT 1, created_ts INTEGER,"
                " updated_ts INTEGER, last_seen_ts INTEGER)")
    con.execute("INSERT INTO facts(type,text,importance,active,created_ts,updated_ts,last_seen_ts)"
                " VALUES('preference','eski kayit',6,1,1,1,1)")
    con.close()
    st = MemoryStore.open(TMP / "old.db", KEY)
    rows = st.list_facts()
    assert isinstance(rows[0], FactRow) and rows[0].source == "auto"
    print("2) eski kasa migrasyonu (source kolonu) OK")

    now = int(time.time())
    st.add_fact("bilgi", "kullanici notu", 1, now, source="user")
    st.add_fact("fact", "onemsiz oto", 1, now)
    st.add_fact("identity", "korunan tip", 1, now)
    m = MemoryManager(st, client=None, embedder=FakeEmb(),
                      lock=threading.RLock(), max_active_facts=2)
    m._decay(now)
    kalan = {f.text for f in st.list_facts()}
    assert "kullanici notu" in kalan, "USER kaydi decay'den MUAF olmali"
    assert "korunan tip" in kalan
    assert "onemsiz oto" not in kalan
    assert "bilgi" in PROTECTED_FACT_TYPES
    st.close()
    print("3) decay: user-muaf + tip korumasi OK")


test_circuit_breaker()
test_migration_and_decay()
print("HAFIZA GUVENILIRLIGI TAMAM")
