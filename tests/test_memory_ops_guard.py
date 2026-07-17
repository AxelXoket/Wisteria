"""Hafiza boru hatti korumalari: LLM op'lari user/korumali kayitlara dokunamaz,
faz-3 atomiktir (rollback), kesik JSON tur imzalatmaz, devre kesici oturum-boyu
durdurmayi bilir, birikinti parca parca islenir, embed boyutu gocu onarilir.

Denetim Y4 + O10 regresyonlari. Gercek sifreli MemoryStore (TEMP) + sahte LLM.
"""
import os
import pathlib
import sys
import tempfile
import threading
import time

os.environ.setdefault("WISTERIA_SETTINGS_DIR",
                      tempfile.mkdtemp(prefix="wisteria-test-mem-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.memory.constants import EMBED_DIM
from backend.memory.manager import MemoryManager, _flat
from backend.memory.store import MemoryStore
from backend.memory.summarizer import LLMUnavailable, Summarizer, _evidence_supported, _norm

TMP = pathlib.Path(tempfile.mkdtemp(prefix="wisteria-test-mem-db-"))
KEY = b"g" * 32


class FakeEmb:
    def encode_one(self, t):
        return [0.0] * EMBED_DIM
    def warmup(self):
        pass


def mk(store, **kw):
    kw.setdefault("keep_recent", 0)
    kw.setdefault("consolidate_every", 10**9)  # otomatik tetikleme kapali: testler worker'i elle kosar
    return MemoryManager(store, client=None, embedder=FakeEmb(),
                         lock=threading.RLock(), **kw)


def test_1_apply_ops_protections():
    st = MemoryStore.open(TMP / "p.db", KEY)
    m = mk(st)
    now = int(time.time())
    uid = st.add_fact("bilgi", "kullanicinin sabitledigi", 9, now, source="user")
    pid = st.add_fact("identity", "korunan tip kaydi", 8, now)
    fid = st.add_fact("fact", "siradan oto kayit", 3, now)
    m._apply_ops([
        {"op": "DELETE", "id": uid},                          # user -> korunur
        {"op": "UPDATE", "id": uid, "text": "EZILDI", "importance": 1},
        {"op": "DELETE", "id": pid},                          # korumali tip -> silinemez
        {"op": "UPDATE", "id": pid, "text": "guncellenmis korunan", "importance": 8},
        {"op": "DELETE", "id": fid},                          # serbest -> uygulanir
        {"op": "ADD", "type": "uydurma_tip", "text": "whitelist disi", "importance": 4},
    ], now)
    rows = {f.id: f for f in st.list_facts()}
    assert uid in rows and rows[uid].text == "kullanicinin sabitledigi", \
        "USER kaydina LLM op'u DOKUNAMAZ (denetim Y4)"
    assert pid in rows, "korumali tip LLM tarafindan SILINEMEZ"
    assert rows[pid].text == "guncellenmis korunan", "korumali tipte UPDATE serbest"
    assert fid not in rows, "serbest kayit silinebilir"
    added = [f for f in rows.values() if f.text == "whitelist disi"]
    assert added and added[0].type == "fact", "whitelist disi tip 'fact'e duser"
    st.close()
    print("1) apply_ops korumalari (user/tip/whitelist) OK")


class StubSummarizer:
    def __init__(self, ops=None, recap="yeni ozet cumlesi."):
        self.ops = ops or []
        self.recap = recap
        self.calls = []
    def extract_ops(self, ledger, turns):
        self.calls.append(turns)
        return list(self.ops)
    def update_recap(self, r, t):
        return self.recap


def test_2_phase3_rollback_is_atomic():
    st = MemoryStore.open(TMP / "t.db", KEY)
    m = mk(st)
    m.summarizer = StubSummarizer(ops=[{"op": "ADD", "type": "fact",
                                        "text": "yarim kalmamali", "importance": 5}])
    st.add_message("user", "merhaba", 1)
    st.add_message("assistant", "selam", 1)
    boom = RuntimeError("disk dolu (simulasyon)")
    orig = st.set_recap
    st.set_recap = lambda *a: (_ for _ in ()).throw(boom)
    m._consolidating = True
    m._consolidate_worker()
    st.set_recap = orig
    assert st.list_facts() == [], \
        "faz-3 ATOMIK olmali: recap yazimi patlarsa ADD de geri alinir (rollback)"
    assert len(st.unconsolidated(0)) == 2, "turlar imzalanmamis kalmali"
    assert m._fail_count == 1
    # ayni worker duzeltilmis store ile kosunca her sey tek seferde tamamlanir
    m._next_attempt_ts = 0
    m._consolidating = True
    m._consolidate_worker()
    assert len(st.unconsolidated(0)) == 0 and len(st.list_facts()) == 1
    assert st.get_recap().startswith("yeni ozet")
    st.close()
    print("2) faz-3 transaction: rollback + temiz tekrar OK")


def test_3_bad_json_raises_unavailable():
    class C:
        def __init__(self, data):
            self.data = data
        def complete_json(self, *a):
            return self.data
    s = Summarizer(C({}))                       # kesik/curumus yanit
    try:
        s._ops_request("s", "u", "test")
        raise AssertionError("ops'suz yanit LLMUnavailable firlatmali (sessiz toplu kayip)")
    except LLMUnavailable:
        pass
    s2 = Summarizer(C({"ops": []}))             # gecerli bos sonuc
    assert s2._ops_request("s", "u", "test") == []
    print("3) kesik JSON -> LLMUnavailable, gecerli bos -> [] OK")


def test_4_giveup_ladder():
    class DeadStore:
        def unconsolidated(self, k, limit=None):
            return [(1, "user", "x")]
        def list_facts(self, limit=None):
            return []
        def get_recap(self):
            return ""
    warns = []
    m = mk(DeadStore(), on_warn=lambda s: warns.append(s))
    class Down:
        def extract_ops(self, l, t):
            raise LLMUnavailable("llama-server 400: schema unsupported", status=400)
        def update_recap(self, r, t):
            return ""
    m.summarizer = Down()
    for _ in range(4):                          # kalici 4xx: 5'lik adimlar -> 20'de durdurma
        m._next_attempt_ts = 0
        m._consolidating = True
        m._consolidate_worker()
    assert m._fail_count >= 20 and m._next_attempt_ts == float("inf"), \
        f"kalici 4xx hizla oturum-boyu durdurmaya gitmeli: {m._fail_count}"
    assert any("durduruldu" in w for w in warns), "kullaniciya TEK durdurma uyarisi"
    m._maybe_consolidate()
    assert not m._consolidating, "durdurulmusken yeni is baslamamali"
    m._note_success()
    assert m._next_attempt_ts == 0.0 and m._fail_count == 0, "toparlanma sifirlar"
    print("4) give-up merdiveni: 4xx x5 + inf + tek uyari + reset OK")


def test_5_batch_cap():
    st = MemoryStore.open(TMP / "b.db", KEY)
    m = mk(st, batch_turns=10)
    stub = StubSummarizer()
    m.summarizer = stub
    for i in range(30):
        st.add_message("user", f"mesaj {i}", i + 1)
    m._consolidating = True
    m._consolidate_worker()
    assert len(st.unconsolidated(0)) == 20, "tek geciste yalniz batch kadari islenmeli"
    assert len(stub.calls) >= 1 and stub.calls[0].count("\n") == 9, \
        "LLM istegine 10 tur gitmeli (700-token kesilme riski sinirlanir)"
    st.close()
    print("5) birikinti batch cap (10/gecis) OK")


def test_6_embed_dim_migration_and_reembed():
    dbp = TMP / "e.db"
    st = MemoryStore.open(dbp, KEY)
    st.add_episode("ilk ani", [0.0] * EMBED_DIM, 1)
    st.add_episode("ikinci ani", [0.0] * EMBED_DIM, 2)
    assert len(st.vec_rowids()) == 2
    st.con.execute("UPDATE meta SET v='999' WHERE k='embed_dim'")  # model degisti simulasyonu
    st.close()
    st2 = MemoryStore.open(dbp, KEY)            # acilis: vec tablosu yeniden kurulur
    assert st2.vec_rowids() == set(), "boyut uyusmazligi vec tablosunu sifirlamali"
    assert len(st2.episodes_all()) == 2, "episode METINLERI korunmali"
    m = mk(st2)
    m._reembed_missing()
    assert st2.vec_rowids() == {rid for (rid, _t) in st2.episodes_all()}, \
        "reembed eksik vektorleri tamamlamali"
    st2.close()
    print("6) embed_dim gocu + reembed tamamlama OK")


def test_7_flatten_and_evidence():
    assert _flat("satir1\nsatir2\n\nsatir3") == "satir1 / satir2 / satir3"
    src = _norm("she likes filter coffee and lives in izmir")  # rol onekleri SOYULMUS kaynak
    assert not _evidence_supported("user: assistant: she maybe", src), \
        "rol onekleri artik bedava ortusme puani veremez"
    assert _evidence_supported("she likes filter coffee", src)
    print("7) duzlestirme + kanit sikilastirma OK")


def test_8_batch_shrinks_on_failure():
    """Deterministik icerik hatasi (ayni 40'lik paket hep patliyor) sonsuz kama
    olmasin: her basarisizlik etkin batch'i yariya indirir (40->20->10->5)."""
    st = MemoryStore.open(TMP / "s.db", KEY)
    m = mk(st, batch_turns=40)
    stub = StubSummarizer()
    m.summarizer = stub
    for i in range(60):
        st.add_message("user", f"m{i}", i + 1)
    for fails, expect in ((0, 40), (1, 20), (2, 10), (3, 5), (7, 5)):
        m._fail_count = fails
        stub.calls.clear()
        m._next_attempt_ts = 0
        m._consolidating = True
        m._consolidate_worker()  # basarili gecis: expect kadar tur isler
        got = stub.calls[0].count("\n") + 1
        assert got == min(expect, 60), f"fail={fails} icin batch {expect} olmali, {got} geldi"
        st.con.execute("UPDATE messages SET consolidated=0")  # ayni birikintiyle tekrar
    st.close()
    print("8) ardisik hatada kuculen batch (40/20/10/5) OK")


def test_9_edited_fact_promoted_to_user():
    """Panelden DUZENLENEN oto-kayit source='user' olur: LLM DELETE'i ve decay
    artik dokunamaz (dogrulama turu bulgusu)."""
    st = MemoryStore.open(TMP / "u.db", KEY)
    now = int(time.time())
    fid = st.add_fact("fact", "oto yazilmis bilgi", 4, now)  # source='auto'
    st.update_fact(fid, "kullanicinin duzelttigi hali", 8, now, source="user")
    row = st.get_fact(fid)
    assert row.source == "user" and row.text == "kullanicinin duzelttigi hali"
    m = mk(st)
    m._apply_ops([{"op": "DELETE", "id": fid}], now)
    assert st.get_fact(fid) is not None, "duzenlenmis kayit LLM ile SILINEMEZ"
    st.update_fact(fid, "llm guncellemesi", 5, now)  # source verilmedi: degismez
    assert st.get_fact(fid).source == "user", "source'suz update kokeni EZMEMELI"
    st.close()
    print("9) panel duzenlemesi user-korumasina terfi OK")


test_1_apply_ops_protections()
test_2_phase3_rollback_is_atomic()
test_3_bad_json_raises_unavailable()
test_4_giveup_ladder()
test_5_batch_cap()
test_6_embed_dim_migration_and_reembed()
test_7_flatten_and_evidence()
test_8_batch_shrinks_on_failure()
test_9_edited_fact_promoted_to_user()
print("HAFIZA BORU HATTI TAMAM")
