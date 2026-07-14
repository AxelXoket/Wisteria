"""Encrypted memory store: AES-256 SQLite (SQLite3MC via apsw) + sqlite-vec.

One encrypted file holds everything — chat messages, the rolling recap (Tier 1),
the durable fact ledger (Tier 2), and episodic embeddings (Tier 3). The whole file
is ciphertext at rest (schema included); the engine sees plaintext only in RAM.
"""

from __future__ import annotations

import apsw
import sqlite_vec

EMBED_DIM = 384  # paraphrase-multilingual-MiniLM-L12-v2 (TR<->EN recall; see embedder.py)

_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS messages("
    " id INTEGER PRIMARY KEY, role TEXT, content TEXT, ts INTEGER, consolidated INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS recap("
    " id INTEGER PRIMARY KEY CHECK(id=1), text TEXT, updated_ts INTEGER)",
    "CREATE TABLE IF NOT EXISTS facts("
    " id INTEGER PRIMARY KEY, type TEXT, text TEXT, importance INTEGER,"
    " active INTEGER DEFAULT 1, created_ts INTEGER, updated_ts INTEGER, last_seen_ts INTEGER)",
    "CREATE TABLE IF NOT EXISTS episodes(id INTEGER PRIMARY KEY, text TEXT, ts INTEGER)",
    "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)",
    # System/character/persona prompt texts — encrypted at rest with everything else.
    "CREATE TABLE IF NOT EXISTS prompts("
    " kind TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL,"
    " updated_ts INTEGER, PRIMARY KEY(kind, name))",
    f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_episodes USING vec0(embedding float[{EMBED_DIM}])",
]


def open_db(path, key: bytes) -> "apsw.Connection":
    """Open (or create) the encrypted DB with a raw 256-bit key and load sqlite-vec."""
    con = apsw.Connection(str(path))
    con.pragma("hexkey", key.hex())              # AES-256 raw key, no KDF at the engine
    # Touch the DB so a wrong key fails HERE (NotADBError) rather than later.
    con.execute("PRAGMA user_version")
    con.enableloadextension(True)
    con.loadextension(sqlite_vec.loadable_path())
    con.enableloadextension(False)
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


class MemoryStore:
    def __init__(self, con: "apsw.Connection") -> None:
        self.con = con

    @classmethod
    def open(cls, path, key: bytes) -> "MemoryStore":
        return cls(open_db(path, key))

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:
            pass

    def rekey(self, new_key: bytes) -> None:
        """Re-encrypt the whole DB under a new RAW key (used on passphrase change).

        Must be ``hexrekey`` (raw key) to match ``hexkey``; plain ``rekey`` would
        treat the hex string as a passphrase and derive a different key.
        """
        self.con.pragma("hexrekey", new_key.hex())

    # ---- Tier 0: messages --------------------------------------------------
    def add_message(self, role: str, content: str, ts: int) -> int:
        self.con.execute(
            "INSERT INTO messages(role, content, ts) VALUES(?,?,?)", (role, content, ts))
        return self.con.last_insert_rowid()

    def unconsolidated(self, keep_recent: int) -> list[tuple[int, str, str]]:
        """Messages that have scrolled out of the raw buffer and not yet folded into memory."""
        return self.con.execute(
            "SELECT id, role, content FROM messages WHERE consolidated=0 AND id NOT IN "
            "(SELECT id FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id",
            (keep_recent,)).fetchall()

    def mark_consolidated(self, ids: list[int]) -> None:
        self.con.executemany("UPDATE messages SET consolidated=1 WHERE id=?", [(i,) for i in ids])

    def message_count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    # ---- Tier 1: rolling recap --------------------------------------------
    def get_recap(self) -> str:
        r = self.con.execute("SELECT text FROM recap WHERE id=1").fetchone()
        return r[0] if r else ""

    def set_recap(self, text: str, ts: int) -> None:
        self.con.execute(
            "INSERT INTO recap(id, text, updated_ts) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET text=excluded.text, updated_ts=excluded.updated_ts",
            (text, ts))

    # ---- Tier 2: durable fact ledger --------------------------------------
    def list_facts(self, limit: int | None = None) -> list[tuple]:
        q = ("SELECT id, type, text, importance FROM facts WHERE active=1 "
             "ORDER BY importance DESC, updated_ts DESC")
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.con.execute(q).fetchall()

    def add_fact(self, type_: str, text: str, importance: int, ts: int) -> int:
        self.con.execute(
            "INSERT INTO facts(type, text, importance, active, created_ts, updated_ts, last_seen_ts)"
            " VALUES(?,?,?,1,?,?,?)", (type_, text, importance, ts, ts, ts))
        return self.con.last_insert_rowid()

    def update_fact(self, fid: int, text: str, importance: int, ts: int) -> None:
        self.con.execute(
            "UPDATE facts SET text=?, importance=?, updated_ts=?, last_seen_ts=? WHERE id=?",
            (text, importance, ts, ts, fid))

    def deactivate_fact(self, fid: int, ts: int) -> None:
        self.con.execute("UPDATE facts SET active=0, updated_ts=? WHERE id=?", (ts, fid))

    # ---- Tier 3: episodic vector memory -----------------------------------
    def add_episode(self, text: str, embedding, ts: int) -> int:
        self.con.execute("INSERT INTO episodes(text, ts) VALUES(?,?)", (text, ts))
        rid = self.con.last_insert_rowid()
        self.con.execute(
            "INSERT INTO vec_episodes(rowid, embedding) VALUES(?,?)",
            (rid, sqlite_vec.serialize_float32(embedding)))
        return rid

    def search_episodes(self, embedding, k: int = 3) -> list[tuple[str, float]]:
        return self.con.execute(
            "SELECT e.text, v.distance FROM vec_episodes v JOIN episodes e ON e.id = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (sqlite_vec.serialize_float32(embedding), k)).fetchall()

    def list_episodes(self, limit: int = 100) -> list[tuple[str, int]]:
        """Plain browse (newest first) for the memory-viewer panel — no vector needed."""
        return self.con.execute(
            "SELECT text, ts FROM episodes ORDER BY ts DESC, id DESC LIMIT ?",
            (int(limit),)).fetchall()

    # ---- prompts (encrypted system/character/persona texts) ----------------
    def get_prompt(self, kind: str, name: str) -> str | None:
        r = self.con.execute(
            "SELECT text FROM prompts WHERE kind=? AND name=?", (kind, name)).fetchone()
        return r[0] if r else None

    def set_prompt(self, kind: str, name: str, text: str, ts: int) -> None:
        self.con.execute(
            "INSERT INTO prompts(kind, name, text, updated_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(kind, name) DO UPDATE SET text=excluded.text, updated_ts=excluded.updated_ts",
            (kind, name, text, ts))

    def list_prompts(self, kind: str | None = None) -> list[tuple[str, str]]:
        if kind is None:
            return self.con.execute(
                "SELECT kind, name FROM prompts ORDER BY kind, name").fetchall()
        return self.con.execute(
            "SELECT kind, name FROM prompts WHERE kind=? ORDER BY name", (kind,)).fetchall()

    def delete_prompt(self, kind: str, name: str) -> None:
        self.con.execute("DELETE FROM prompts WHERE kind=? AND name=?", (kind, name))

    # ---- meta --------------------------------------------------------------
    def get_meta(self, key: str, default: str = "") -> str:
        r = self.con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO meta(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value))
