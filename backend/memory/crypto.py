"""Key management for the encrypted memory DB.

PRIMARY mode (user's choice): a passphrase only the user knows. The 256-bit DB key
is derived from it with scrypt (stdlib, memory-hard) and is NEVER stored — so the
DB cannot be opened without the passphrase, not even inside the user's own Windows
session. Forgetting it means the data is unrecoverable (by design).

We persist only:
  - salt.bin      : scrypt salt (not secret)
  - verifier.bin  : DPAPI-independent check so we can tell "wrong passphrase" from
                    "corrupt file" WITHOUT storing the key. It's HMAC(key, "verify");
                    knowing it does not reveal the key.

OPTIONAL convenience ("remember on this device"): the derived key may be wrapped
with Windows DPAPI (user scope) so the app auto-unlocks under this Windows account.
This trades some security (same-session code could unwrap it) for not retyping.
Default is OFF — always ask.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import secrets
import time
from pathlib import Path

# ---------------------------------------------------------------- scrypt KDF

# Memory-hard params: N=2^15 (~32 MB), r=8, p=1 -> ~fast for one login, painful to brute-force.
_SCRYPT = dict(n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def new_salt() -> bytes:
    return secrets.token_bytes(16)


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive the 32-byte DB key from the passphrase (never stored)."""
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, **_SCRYPT)


# v1 protocol domain constant. BYTE-STABLE FOREVER: existing vaults derive their
# verifier from exactly these bytes - changing them would lock users out.
_VERIFY_DOMAIN = b"\x6d\x61\x64\x65\x6c\x69\x6e\x65-verify"


def _verifier(key: bytes) -> bytes:
    return hmac.new(key, _VERIFY_DOMAIN, hashlib.sha256).digest()


def make_verifier(key: bytes) -> bytes:
    return _verifier(key)


def check_verifier(key: bytes, stored: bytes) -> bool:
    return hmac.compare_digest(_verifier(key), stored)


# ---------------------------------------------------------------- Windows DPAPI (optional)

# v1 app-binding entropy (not a secret). BYTE-STABLE: "remember on this device"
# blobs were protected with these exact bytes; changing them invalidates them.
_ENTROPY = b"\x6d\x61\x64\x65\x6c\x69\x6e\x65-memory-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _in(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _out(blob: _BLOB) -> bytes:
    raw = ctypes.string_at(blob.pbData, blob.cbData)
    ctypes.windll.kernel32.LocalFree(blob.pbData)
    return raw


def dpapi_protect(data: bytes) -> bytes:
    out = _BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_in(data)), None, ctypes.byref(_in(_ENTROPY)),
        None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise ctypes.WinError()
    return _out(out)


def dpapi_unprotect(data: bytes) -> bytes:
    out = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_in(data)), None, ctypes.byref(_in(_ENTROPY)),
        None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise ctypes.WinError()
    return _out(out)


# ---------------------------------------------------------------- vault (files beside the DB)

class KeyVault:
    """Manages the passphrase-derived key + its salt/verifier + optional DPAPI remember."""

    def __init__(self, dir_path: Path) -> None:
        self.dir = Path(dir_path)
        self.salt_path = self.dir / "salt.bin"
        self.verifier_path = self.dir / "verifier.bin"
        self.remember_path = self.dir / "device.key"  # DPAPI-wrapped key (only if remembered)

    # -- state ---------------------------------------------------------------
    def is_initialized(self) -> bool:
        """True once a passphrase has been set (salt + verifier exist)."""
        return self.salt_path.exists() and self.verifier_path.exists()

    def can_derive(self) -> bool:
        """Salt duruyor mu - parola anahtari hala turetilebilir mi."""
        return self.salt_path.exists()

    def has_remembered(self) -> bool:
        return self.remember_path.exists()

    # -- first run: set the passphrase --------------------------------------
    def initialize(self, passphrase: str) -> bytes:
        """YALNIZCA gercek ilk kurulum icindir - cagiran taraf (memory_api),
        mem.db varken buraya asla girmemeyi garantiler (denetim K1: salt'in
        ezilmesi = kasanin kalici kaybi). Emniyet agi olarak mevcut kimlik
        dosyalari yine de silinmez, kenara alinir."""
        self.dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        for p in (self.salt_path, self.verifier_path):
            if p.exists():
                try:
                    p.replace(p.with_name(f"{p.name}.bak-{ts}"))
                except OSError:
                    pass
        salt = new_salt()
        key = derive_key(passphrase, salt)
        self.salt_path.write_bytes(salt)
        self.verifier_path.write_bytes(make_verifier(key))
        return key

    # -- later runs: unlock --------------------------------------------------
    def unlock(self, passphrase: str) -> bytes | None:
        """Return the key if the passphrase is correct, else None."""
        if not self.is_initialized():
            return None
        salt = self.salt_path.read_bytes()
        key = derive_key(passphrase, salt)
        if check_verifier(key, self.verifier_path.read_bytes()):
            return key
        return None

    # -- optional "remember on this device" ---------------------------------
    def remember(self, key: bytes) -> None:
        self.remember_path.write_bytes(dpapi_protect(key))

    def forget(self) -> None:
        if self.remember_path.exists():
            self.remember_path.unlink()

    def unlock_remembered(self) -> bytes | None:
        if not (self.is_initialized() and self.remember_path.exists()):
            return None
        try:
            key = dpapi_unprotect(self.remember_path.read_bytes())
        except OSError:
            return None
        if check_verifier(key, self.verifier_path.read_bytes()):
            return key
        return None

    def unlock_remembered_unverified(self) -> bytes | None:
        """DPAPI blob'unu verifier KONTROLSUZ coz. Yalniz DB-dogrulamali
        kurtarma akisi icindir: salt/verifier kayipken dogrulamayi DB yapar."""
        if not self.remember_path.exists():
            return None
        try:
            return dpapi_unprotect(self.remember_path.read_bytes())
        except OSError:
            return None

    # -- DB-dogrulamali kurtarma ---------------------------------------------
    # Ilke (denetim K1'in koku): anahtarin DOGRULUGUNUN nihai kaynagi sifreli
    # DB'nin kendisidir; salt/verifier yalnizca kolayliktir. Verifier kaybi /
    # bozulmasi / yarim kalmis parola degisimi, DB acilabildigi surece onarilir.

    def heal(self, key: bytes) -> None:
        """DB tarafindan dogrulanmis anahtar icin verifier'i yeniden yaz
        (salt'a dokunulmaz; verifier = HMAC(key) salt'tan bagimsizdir)."""
        self.verifier_path.write_bytes(make_verifier(key))

    def recover_with_db(self, passphrase: str, db_check) -> bytes | None:
        """Aday saltlarla (asil + yarim kalmis .new) anahtar turet; db_check(key)
        True donen ilk anahtar icin kimlik dosyalarini tutarli hale getir ve
        anahtari dondur. Hicbiri acmazsa None (dosyalara DOKUNULMAZ)."""
        salt_new = self.salt_path.with_name("salt.bin.new")
        ver_new = self.verifier_path.with_name("verifier.bin.new")
        for sp in (self.salt_path, salt_new):
            if not sp.exists():
                continue
            try:
                salt = sp.read_bytes()
                key = derive_key(passphrase, salt)
            except Exception:
                continue
            if not db_check(key):
                continue
            try:  # iyilestirme: yazim hatasi girisi engellemez (anahtar dogru)
                if sp != self.salt_path:
                    self.salt_path.write_bytes(salt)
                self.verifier_path.write_bytes(make_verifier(key))
                for leftover in (salt_new, ver_new):
                    if leftover.exists():
                        leftover.unlink()
            except OSError:
                pass
            return key
        return None

    # -- change passphrase ----------------------------------------------------
    def change_passphrase(self, new_passphrase: str, rekey_fn) -> bytes:
        """Parola degisimi, DOSYA pencereleri cokme-guvenli olacak sirayla:
          1) yeni salt/verifier .new dosyalarina yazilir (asillar el degmemis)
          2) rekey_fn(new_key) DB'yi yeni anahtara cevirir
          3) .new dosyalari asillarin uzerine tasinir
        1-2 arasi cokme: asil dosyalar + eski DB anahtari gecerli (kayip yok).
        2-3 arasi cokme: recover_with_db .new salt'ini dener ve tamamlar.
        SINIR: adim 2'nin kendisi (PRAGMA hexrekey) motor icidir ve burada
        atomiklestirilemez - tam rekey ORTASINDA cokme iki anahtarin da
        acamadigi bir DB birakabilir; UI baglanmadan once yedek stratejisi
        (mem.db kopyasi) eklenmelidir. 3 sonrasi remember() oncesi cokme:
        bayat device.key auto-unlock'u devre disi birakir (kilit ekrani calisir)."""
        salt = new_salt()
        key = derive_key(new_passphrase, salt)
        salt_new = self.salt_path.with_name("salt.bin.new")
        ver_new = self.verifier_path.with_name("verifier.bin.new")
        salt_new.write_bytes(salt)
        ver_new.write_bytes(make_verifier(key))
        rekey_fn(key)
        salt_new.replace(self.salt_path)
        ver_new.replace(self.verifier_path)
        if self.remember_path.exists():
            self.remember(key)
        return key
