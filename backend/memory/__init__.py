"""Encrypted long-term memory for the companion.

Three tiers injected/retrieved around each turn:
  Tier 0  raw recent messages (verbatim, in chat history)
  Tier 1  rolling narrative recap        (prose, always in prompt)
  Tier 2  durable fact ledger            (structured, always in prompt)
  Tier 3  episodic vector recall         (on-demand, retrieved when relevant)

Everything persists to ONE AES-256 encrypted SQLite file (SQLite3MC via apsw) +
sqlite-vec, with the DB key wrapped by Windows DPAPI (or an optional passphrase).
"""
