"""Kasa kurtarma: verifier kaybi, salt kaybi, bozuk migrasyon, no-clobber,
yarim parola degisimi, handle sizintisi.

Denetim K1/Y2/Y3 regresyonlari. Ilke: dogrulamanin nihai kaynagi DB'nin
kendisidir - dogru parola/anahtar, kimlik dosyalari kayipken bile kasayi
ACAR ve dosyalari onarir; kasa ASLA sessizce yeniden kurulmaz.

Gercek KeyVault (DPAPI dahil) + gercek sifreli MemoryStore, TEMP altinda kosar.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import threading

TMP = pathlib.Path(tempfile.mkdtemp(prefix="wisteria-test-vault-"))
os.environ.setdefault("WISTERIA_SETTINGS_DIR", str(TMP / "settings"))
(TMP / "settings").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.config import CONFIG

# Prompt yollari TEMP'e: testler gercek prompt dizinlerine asla dokunmaz.
CONFIG.memory_dir = TMP / "memory"
CONFIG.system_prompt = TMP / "sysp" / "system_prompt.txt"
CONFIG.characters_dir = TMP / "chars"
CONFIG.personas_dir = TMP / "pers"
for d in (TMP / "sysp", TMP / "chars", TMP / "pers"):
    d.mkdir(parents=True, exist_ok=True)

import backend.api_parts.memory_api as memapi
from backend import prompt_store
from backend.api import JsApi
from backend.memory.crypto import KeyVault
from backend.memory.store import MemoryStore, key_opens, open_db


class FakeEmb:
    def __init__(self, cache_dir=None):
        pass
    def encode_one(self, t):
        return [0.0]
    def warmup(self):
        pass


memapi.Embedder = FakeEmb  # mixin importu modul adindan cozer - agir model yok

PASS = "dogru parola 123"


def fresh_api() -> JsApi:
    return JsApi(None)


def salt_bytes() -> bytes:
    return (CONFIG.memory_dir / "salt.bin").read_bytes()


def test_1_verifier_loss_heals():
    api = fresh_api()
    r = api.memory_unlock(PASS)  # ilk kurulum (mem.db yok)
    assert r["ok"], r
    with api._store_lock:
        api._mem_store.add_fact("bilgi", "kayip olmamasi gereken kayit", 8, 1, source="user")
    api.close_memory()

    ver = CONFIG.memory_dir / "verifier.bin"
    ver.unlink()  # AV karantinasi / disk hatasi senaryosu
    old_salt = salt_bytes()

    api2 = fresh_api()
    st = api2.memory_state()
    assert st["initialized"] is False  # eski kod burada INITIALIZE edip salt'i eziyordu
    r = api2.memory_unlock("yanlis parola")
    assert not r["ok"] and r["error"] == "wrong", r
    assert salt_bytes() == old_salt, "yanlis parola salt'a DOKUNAMAZ"
    assert not ver.exists(), "yanlis parola verifier uretmemeli"

    r = api2.memory_unlock(PASS)
    assert r["ok"], f"dogru parola verifier'siz ACMALIYDI: {r}"
    assert ver.exists(), "verifier onarilmali (self-heal)"
    assert salt_bytes() == old_salt, "salt degismemeli"
    with api2._store_lock:
        texts = {f.text for f in api2._mem_store.list_facts()}
    assert "kayip olmamasi gereken kayit" in texts, "eski veri okunmali"
    api2.close_memory()
    print("1) verifier kaybi: yanlis parola reddi + dogru parola self-heal OK")


def test_2_salt_loss_devicekey_and_damaged():
    api = fresh_api()
    r = api.memory_unlock(PASS, remember=True)  # device.key yazilir
    assert r["ok"], r
    api.close_memory()

    dk = CONFIG.memory_dir / "device.key"
    dk_bytes = dk.read_bytes()
    (CONFIG.memory_dir / "salt.bin").unlink()
    (CONFIG.memory_dir / "verifier.bin").unlink()

    api2 = fresh_api()  # __init__ icindeki remembered-unlock DB-dogrulamali girmeli
    assert api2.memory_state()["unlocked"], "device.key + DB dogrulamasi acmaliydi"
    with api2._store_lock:
        texts = {f.text for f in api2._mem_store.list_facts()}
    assert "kayip olmamasi gereken kayit" in texts
    api2.close_memory()

    dk.unlink()  # simdi salt YOK, verifier YOK, device.key YOK
    db = CONFIG.memory_dir / "mem.db"
    size_before = db.stat().st_size
    api3 = fresh_api()
    r = api3.memory_unlock(PASS)
    assert not r["ok"] and r["error"] == "damaged", f"acik hasar bildirimi bekleniyordu: {r}"
    assert db.stat().st_size == size_before, "mem.db'ye dokunulmamali"
    assert not (CONFIG.memory_dir / "salt.bin").exists(), "yeniden kurulum YASAK"

    dk.write_bytes(dk_bytes)  # kullanici yedegi geri koydu senaryosu
    api4 = fresh_api()
    assert api4.memory_state()["unlocked"], "device.key geri gelince veri aynen durmali"
    api4.close_memory()
    print("2) salt kaybi: device.key kurtarmasi + damaged durusu + veri butunlugu OK")


def test_3_migration_unreadable_file_kept():
    tmp2 = TMP / "mig"
    (tmp2 / "chars").mkdir(parents=True, exist_ok=True)
    (tmp2 / "pers").mkdir(parents=True, exist_ok=True)
    (tmp2 / "sysp").mkdir(parents=True, exist_ok=True)
    sysf = tmp2 / "sysp" / "system_prompt.txt"
    sysf.write_text("SISTEM METNI", encoding="utf-8")
    charf = tmp2 / "chars" / "mira.txt"
    charf.write_text("KARAKTER METNI", encoding="utf-8")

    st = MemoryStore.open(tmp2 / "mig.db", b"k" * 32)
    lock = threading.RLock()

    real_read = prompt_store._read_file
    prompt_store._read_file = lambda p: None if p.name == "mira.txt" else real_read(p)
    try:
        res = prompt_store.migrate_prompts_if_needed(
            st, lock, system_file=sysf, characters_dir=tmp2 / "chars",
            personas_dir=tmp2 / "pers")
    finally:
        prompt_store._read_file = real_read
    assert res["migrated"] is True
    assert charf.exists(), "okunamayan dosya SILINMEMELI"
    assert st.get_prompt("character", "mira") is None, "okunamayan icerik yazilmamali"
    assert st.get_prompt("system", "system_prompt") == "SISTEM METNI"
    assert not sysf.exists(), "saglam okunan dosya normal tuketilmeli"

    res = prompt_store.migrate_prompts_if_needed(  # sonraki unlock: artik okunuyor
        st, lock, system_file=sysf, characters_dir=tmp2 / "chars",
        personas_dir=tmp2 / "pers")
    assert st.get_prompt("character", "mira") == "KARAKTER METNI"
    assert not charf.exists()
    st.close()
    print("3) migrasyon: okunamayan dosya korunur, sonraki aciliste alinir OK")


def test_4_leftover_noclobber():
    tmp3 = TMP / "noclb"
    (tmp3 / "chars").mkdir(parents=True, exist_ok=True)
    (tmp3 / "pers").mkdir(parents=True, exist_ok=True)
    (tmp3 / "sysp").mkdir(parents=True, exist_ok=True)
    sysf = tmp3 / "sysp" / "system_prompt.txt"

    st = MemoryStore.open(tmp3 / "n.db", b"n" * 32)
    lock = threading.RLock()
    st.set_meta("prompts_migrated", "1")  # migrasyon coktan bitti
    st.set_prompt("character", "mira", "GUNCEL UYGULAMA ICI METIN", 1)

    stale = tmp3 / "chars" / "mira.txt"
    stale.write_text("ESKI DISK KOPYASI", encoding="utf-8")
    prompt_store.migrate_prompts_if_needed(
        st, lock, system_file=sysf, characters_dir=tmp3 / "chars",
        personas_dir=tmp3 / "pers")
    assert st.get_prompt("character", "mira") == "GUNCEL UYGULAMA ICI METIN", \
        "artik dosya kasadaki guncel kaydi EZMEMELI"
    assert stale.exists(), "catisan dosya birakilmali (kullanici karar verir)"

    stale.write_text("GUNCEL UYGULAMA ICI METIN", encoding="utf-8")  # birebir ayni
    prompt_store.migrate_prompts_if_needed(
        st, lock, system_file=sysf, characters_dir=tmp3 / "chars",
        personas_dir=tmp3 / "pers")
    assert st.get_prompt("character", "mira") == "GUNCEL UYGULAMA ICI METIN"
    assert not stale.exists(), "birebir kopya guvenle tuketilmeli"
    st.close()
    print("4) artik dosya: no-clobber + birebir kopya tuketimi OK")


def test_5_failed_open_releases_handle():
    tmp4 = TMP / "handle"
    tmp4.mkdir(parents=True, exist_ok=True)
    dbp = tmp4 / "h.db"
    open_db(dbp, b"a" * 32).close()
    try:
        open_db(dbp, b"b" * 32)
        raise AssertionError("yanlis anahtar acmamali")
    except AssertionError:
        raise
    except Exception:
        pass  # beklenen: NotADBError
    moved = tmp4 / "h2.db"
    dbp.rename(moved)  # handle sizsaydi Windows'ta PermissionError olurdu
    moved.rename(dbp)
    print("5) basarisiz acilis handle birakmaz OK")


def test_6_interrupted_passphrase_change():
    tmp5 = TMP / "chg"
    tmp5.mkdir(parents=True, exist_ok=True)
    vault = KeyVault(tmp5)
    key1 = vault.initialize("eski parola")
    dbp = tmp5 / "mem.db"
    st = MemoryStore.open(dbp, key1)
    st.add_fact("bilgi", "degisimden once", 5, 1)
    # yarim kalmis degisim: .new dosyalari + rekey yapildi, swap YAPILMADI
    from backend.memory.crypto import derive_key, make_verifier, new_salt
    salt2 = new_salt()
    key2 = derive_key("yeni parola", salt2)
    (tmp5 / "salt.bin.new").write_bytes(salt2)
    (tmp5 / "verifier.bin.new").write_bytes(make_verifier(key2))
    st.rekey(key2)
    st.close()

    assert vault.unlock("yeni parola") is None, "asil verifier henuz eski"
    rec = vault.recover_with_db("yeni parola", lambda k: key_opens(dbp, k))
    assert rec == key2, "kurtarma .new salt'ini denemeli"
    assert not (tmp5 / "salt.bin.new").exists(), "swap tamamlanmali"
    assert vault.unlock("yeni parola") == key2, "sonraki girisler normal yoldan"
    st2 = MemoryStore.open(dbp, key2)
    assert any(f.text == "degisimden once" for f in st2.list_facts())
    st2.close()
    print("6) yarim parola degisimi: .new adayi + tamamlama OK")


def test_7_zero_byte_db_cannot_hijack_identity():
    """Dogrulama turu bulgusu: 0-bayt mem.db her anahtara 'acilir' gorunuyordu -
    yanlis parola recover_with_db'den gecip verifier'i EZEBILIYORDU (kimlik gaspi).
    key_opens artik 100 bayttan kucuk dosyaya ve eksik yola False der, dosya da
    YARATMAZ (READONLY)."""
    tmp7 = TMP / "zb"
    tmp7.mkdir(parents=True, exist_ok=True)
    vault = KeyVault(tmp7)
    vault.initialize("gercek parola")
    dbp = tmp7 / "mem.db"
    dbp.write_bytes(b"")  # yarim kalmis ilk kurulum: bos dosya + gecerli kimlik
    ver_before = (tmp7 / "verifier.bin").read_bytes()

    assert key_opens(dbp, b"x" * 32) is False, "bos dosya HICBIR anahtara acilamaz"
    missing = tmp7 / "yok.db"
    assert key_opens(missing, b"x" * 32) is False
    assert not missing.exists(), "key_opens dosya YARATMAMALI (READONLY)"

    rec = vault.recover_with_db("YANLIS parola", lambda k: key_opens(dbp, k))
    assert rec is None, "yanlis parola bos-db uzerinden kurtarma GECEMEZ"
    assert (tmp7 / "verifier.bin").read_bytes() == ver_before, \
        "yanlis parola verifier'a DOKUNAMAZ (kimlik gaspi kapali)"
    assert vault.unlock("gercek parola") is not None, "gercek parola hala gecerli"
    print("7) 0-bayt kasa: kimlik gaspi kapali + key_opens yaratmiyor OK")


test_1_verifier_loss_heals()
test_2_salt_loss_devicekey_and_damaged()
test_3_migration_unreadable_file_kept()
test_4_leftover_noclobber()
test_5_failed_open_releases_handle()
test_6_interrupted_passphrase_change()
test_7_zero_byte_db_cannot_hijack_identity()
shutil.rmtree(TMP, ignore_errors=True)
print("KASA KURTARMA TAMAM")
