"""Encrypted prompt storage: provider facade over MemoryStore + one-time disk migration.

The system/character/persona prompt texts live INSIDE the passphrase-encrypted
memory DB (a ``prompts`` table - same AES-256 file, same key, ``hexrekey`` covers
passphrase changes for free). This module provides:

  * ``StorePromptProvider`` - the read/write facade ``prompts.py`` uses once the
    vault is unlocked (bound via ``prompts.set_prompt_provider``). Every access
    goes through the app-wide store RLock (one apsw connection, one lock).
  * ``migrate_prompts_if_needed`` - a one-time import of the plaintext prompt
    files at project ROOT into the encrypted table, deleting the originals ONLY
    after an in-transaction read-back verification (verify-then-delete).
  * ``_cleanup_leftovers`` - runs on every unlock after migration: any ``.txt``/
    ``.bak`` found in the old prompt dirs is absorbed into the vault and deleted.

INTENDED BEHAVIOR (user's explicit choice): after migration, no plaintext prompt
exists on disk. Any ``.txt`` manually dropped into the old dirs later is CONSUMED
(imported + deleted) on the next unlock - that's the offline way to add a new
character/persona from a file. In-app creation/editing is the normal path.
``.bak`` revisions are imported too (as hidden ``*_bak`` kinds) so nothing is lost.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

# Kinds shown in the UI; *_bak kinds exist in the table but stay hidden.
KINDS = ("system", "character", "persona")

# Meta keys for the active selection (encrypted with everything else).
ACTIVE_META = {"character": "active_character", "persona": "active_persona"}


def _read_file(path: Path) -> str:
    """BOM-tolerant read (mirrors prompts._read); never raises."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
    except Exception:
        return ""


class StorePromptProvider:
    """Read/write prompts from the encrypted store under the shared store lock."""

    def __init__(self, store, lock: threading.RLock) -> None:
        self.store = store
        self.lock = lock

    def get(self, kind: str, name: str) -> str | None:
        with self.lock:
            return self.store.get_prompt(kind, name)

    def list(self, kind: str) -> list[str]:
        with self.lock:
            return [n for (_k, n) in self.store.list_prompts(kind)]

    def save(self, kind: str, name: str, text: str) -> None:
        with self.lock:
            self.store.set_prompt(kind, name, text, int(time.time()))

    def get_active(self, kind: str) -> str | None:
        """Active character/persona name: meta first, else alphabetical first."""
        meta_key = ACTIVE_META.get(kind)
        with self.lock:
            if meta_key:
                name = self.store.get_meta(meta_key, "")
                if name and self.store.get_prompt(kind, name) is not None:
                    return name
            names = [n for (_k, n) in self.store.list_prompts(kind)]
        return sorted(names)[0] if names else None

    def set_active(self, kind: str, name: str) -> None:
        meta_key = ACTIVE_META.get(kind)
        if not meta_key:
            return
        with self.lock:
            self.store.set_meta(meta_key, name)

    def rename(self, kind: str, old: str, new: str) -> None:
        """Rename a prompt in ONE transaction; active meta follows the new name."""
        with self.lock:
            with self.store.con:
                text = self.store.get_prompt(kind, old)
                if text is None:
                    raise KeyError(old)
                self.store.set_prompt(kind, new, text, int(time.time()))
                self.store.delete_prompt(kind, old)
                meta_key = ACTIVE_META.get(kind)
                if meta_key and self.store.get_meta(meta_key, "") == old:
                    self.store.set_meta(meta_key, new)

    def delete(self, kind: str, name: str) -> None:
        """Delete a prompt in ONE transaction; if it was active, hand the meta to
        the alphabetically-first survivor (mirrors get_active's fallback)."""
        with self.lock:
            with self.store.con:
                self.store.delete_prompt(kind, name)
                meta_key = ACTIVE_META.get(kind)
                if meta_key and self.store.get_meta(meta_key, "") == name:
                    remaining = sorted(n for (_k, n) in self.store.list_prompts(kind))
                    self.store.set_meta(meta_key, remaining[0] if remaining else "")


# --------------------------------------------------------------------- migration

def _collect_sources(system_file: Path, characters_dir: Path, personas_dir: Path):
    """List every plaintext prompt file with its (kind, name) mapping."""
    sources: list[tuple[str, str, Path]] = []
    if system_file.exists():
        sources.append(("system", system_file.stem, system_file))
    sys_bak = system_file.with_suffix(".bak")
    if sys_bak.exists():
        sources.append(("system_bak", system_file.stem, sys_bak))
    for d, kind in ((characters_dir, "character"), (personas_dir, "persona")):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.txt")):
            sources.append((kind, p.stem, p))
        for p in sorted(d.glob("*.bak")):
            sources.append((kind + "_bak", p.stem, p))
    return sources


def _import_and_delete(store, lock, sources) -> tuple[int, int]:
    """Write+verify all sources in ONE transaction, then (and only then) delete them.

    Returns (imported, deleted). Raises on verification failure - the caller
    treats that as "leave everything on disk, retry next unlock".
    """
    entries = [(kind, name, _read_file(path), path) for (kind, name, path) in sources]
    with lock:
        with store.con:  # one transaction: all rows + verification, or nothing
            now = int(time.time())
            for kind, name, text, _path in entries:
                store.set_prompt(kind, name, text, now)
            for kind, name, text, _path in entries:  # read-back byte-compare
                if store.get_prompt(kind, name) != text:
                    raise RuntimeError(f"verify failed for {kind}/{name}")
    deleted = 0
    for _kind, _name, _text, path in entries:  # strictly after commit
        try:
            path.unlink()
            deleted += 1
        except OSError:
            pass  # locked file -> retried by _cleanup_leftovers on next unlock
    return len(entries), deleted


def _remove_empty_dirs(*dirs: Path) -> None:
    for d in dirs:
        try:
            d.rmdir()  # only succeeds if empty
        except OSError:
            pass


def migrate_prompts_if_needed(store, lock: threading.RLock, *, system_file: Path,
                              characters_dir: Path, personas_dir: Path) -> dict:
    """One-time import of the plaintext prompt files into the encrypted DB.

    Safety contract: originals are deleted ONLY after an in-transaction read-back
    verification and a committed ``prompts_migrated`` meta flag. Never raises -
    this runs on the unlock path, which must survive any failure here.
    """
    result = {"migrated": False, "imported": 0, "deleted": 0}
    try:
        with lock:
            done = store.get_meta("prompts_migrated") == "1"
        if done:
            # retry leftover deletions + absorb any newly dropped files
            result.update(_cleanup_leftovers(
                store, lock, system_file=system_file,
                characters_dir=characters_dir, personas_dir=personas_dir))
            return result

        sources = _collect_sources(system_file, characters_dir, personas_dir)
        if not sources:  # fresh machine: nothing to import, just mark done
            with lock:
                store.set_meta("prompts_migrated", "1")
            result["migrated"] = True
            return result

        imported, deleted = _import_and_delete(store, lock, sources)
        with lock:
            store.set_meta("prompts_migrated", "1")
        _remove_empty_dirs(system_file.parent, characters_dir, personas_dir)
        result.update({"migrated": True, "imported": imported, "deleted": deleted})
    except Exception:
        pass  # plaintext untouched (or partially deleted post-commit); retried next unlock
    return result


def _cleanup_leftovers(store, lock: threading.RLock, *, system_file: Path,
                       characters_dir: Path, personas_dir: Path) -> dict:
    """Post-migration sweep: absorb-then-delete any prompt files still on disk."""
    result = {"imported": 0, "deleted": 0}
    try:
        sources = _collect_sources(system_file, characters_dir, personas_dir)
        if not sources:
            return result
        imported, deleted = _import_and_delete(store, lock, sources)
        _remove_empty_dirs(system_file.parent, characters_dir, personas_dir)
        result.update({"imported": imported, "deleted": deleted})
    except Exception:
        pass
    return result
