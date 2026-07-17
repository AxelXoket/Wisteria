"""Memory summarizer — turns evicted raw turns into durable facts + a rolling recap.

Anti-drift rules (from the research), enforced here:
  * grow the ledger + recap ONLY from raw evicted turns — never re-summarize a summary;
  * every ADD/UPDATE fact must quote VERBATIM evidence from those turns; facts whose
    quote isn't found in the source are DROPPED (ProMem-style "quote-or-drop");
  * memory is written in ENGLISH (character's language + the English system prompt),
    which the local model produces from the source turns regardless of their language.

All model calls go through llama.cpp with a json_schema-constrained grammar +
low temperature, so the JSON is always valid.
"""

from __future__ import annotations

import re

from ..config import GenPreset
from ..llm import LlamaError
from ..logutil import err_brief, log_for
from .constants import FACT_TYPES

_log = log_for("memory.summarizer")

_FACT_TYPES = list(FACT_TYPES)  # tek kaynak: constants.py (sema enum'u buradan)


class LLMUnavailable(RuntimeError):
    """Model/tasima katmani hatasi: girdi SUCLU DEGIL, sonra yeniden denenebilir.

    Ayrimin sebebi veri guvenligi: bu istisna yukari cikar ve turlar 'islendi'
    olarak IMZALANMAZ - eski davranis sessizce bos liste dondurup turlarin
    hafizaya hic islenmeden imzalanmasina yol aciyordu (sessiz hafiza kaybi).

    status: varsa HTTP kodu - devre kesici kalici 4xx'i (or. desteklenmeyen
    json_schema) hizli askiya almak icin kullanir."""

    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status

# Flat, grammar-friendly schema — a mid-size local model follows this reliably.
OPS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "op": {"type": "string", "enum": ["ADD", "UPDATE", "DELETE"]},
                    "id": {"type": "integer"},        # existing fact id for UPDATE/DELETE; 0 for ADD
                    "type": {"type": "string", "enum": _FACT_TYPES},
                    "text": {"type": "string"},
                    "importance": {"type": "integer"},
                    "evidence": {"type": "string"},   # verbatim quote from the NEW MESSAGES
                },
                "required": ["op", "id", "type", "text", "importance", "evidence"],
            },
        }
    },
    "required": ["ops"],
}

_EXTRACT_SYS = (
    "You maintain a durable long-term memory ledger for a companion character. "
    "You are given the CURRENT LEDGER and some NEW MESSAGES from the conversation. "
    "Extract only stable facts worth remembering for months about the user and the "
    "relationship: identity, preferences, commitments/plans, relationship milestones, "
    "boundaries, and important concrete facts. Ignore small talk and transient mood.\n"
    "Write every fact in ENGLISH, concise (<= 20 words), preserving exact names, numbers, "
    "dates and places. Do NOT infer or embellish.\n"
    "For each candidate choose an operation against the CURRENT LEDGER:\n"
    "  ADD    -> a genuinely new fact (set id = 0)\n"
    "  UPDATE -> same subject as an existing fact, new/corrected value (set id = that fact id)\n"
    "  DELETE -> the new messages contradict an existing fact (set id = that fact id)\n"
    "RULE: every ADD/UPDATE MUST include 'evidence' = a short quote copied VERBATIM from the "
    "NEW MESSAGES that proves the fact. If you cannot quote it, do not output it. "
    "importance is 1-10 (1 mundane, 10 major). If nothing is worth remembering, return an "
    "empty ops list. Output only the JSON."
)

_RECAP_SYS = (
    "You keep a short 'story so far' recap of a companion conversation, in ENGLISH, past "
    "tense, factual. You are given the existing RECAP and some NEW MESSAGES. Write 1-2 new "
    "sentences summarizing ONLY what happened in the NEW MESSAGES (events, feelings, "
    "decisions). Preserve exact names/places. Do NOT rewrite or repeat the existing recap, "
    "and do not invent. Output only the new sentence(s), nothing else."
)

_REFLECT_SYS = (
    "You maintain a durable memory ledger for a companion character. You are given the CURRENT "
    "LEDGER (facts with ids). Clean it up:\n"
    "- DELETE (by id) any fact that is a duplicate or near-duplicate of another (keep the single "
    "best-worded one), that is contradicted by a newer fact (keep the newer), or that is trivial "
    "and no longer worth remembering.\n"
    "- ADD up to 3 higher-level 'insight' facts about the person or the relationship, each "
    "grounded in at least TWO existing facts (type = 'insight', importance 6-9). Do not restate "
    "an existing fact.\n"
    "NEVER delete facts of type identity, commitment, boundary, or milestone. For a DELETE, "
    "'evidence' may be empty; for an ADD insight, put a one-line rationale in 'evidence'. "
    "Output only the JSON ops."
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _evidence_supported(evidence: str, source_norm: str) -> bool:
    """Quote-or-drop: the evidence must appear in (or strongly overlap) the source turns.

    source_norm rol oneklerinden ARINDIRILMIS verilmelidir (bkz. extract_ops):
    'user:'/'assistant:' her kaynakta gectiginden uydurma kanitlara bedava puan
    veriyordu; kisa dolgu sozcukleri de (the/and/she) minimum uzunlukla elenir."""
    ev = _norm(evidence)
    if len(ev) < 6:
        return False
    if ev in source_norm:
        return True
    toks = [t for t in ev.split() if len(t) > 3]  # lenient fallback for a lightly-paraphrased quote
    if not toks:
        return False
    return sum(1 for t in toks if t in source_norm) / len(toks) >= 0.8


class Summarizer:
    def __init__(self, client) -> None:
        self.client = client
        self._p_extract = GenPreset(temperature=0.2, top_p=0.9, top_k=40,
                                    min_p=0.0, repeat_penalty=1.0, max_tokens=700)
        self._p_recap = GenPreset(temperature=0.3, top_p=0.9, top_k=40,
                                  min_p=0.0, repeat_penalty=1.05, max_tokens=200)

    # ---- ortak yardimcilar (extract_ops + reflect ayni iskeleti paylasir) ----
    @staticmethod
    def _ledger_text(ledger: list) -> str:
        # kayit metinleri TEK satira duzlestirilir: cok satirli bir kayit,
        # 'NEW MESSAGES:' benzeri bolum basligi taklidi yapamaz (enjeksiyon)
        return "\n".join(
            f"[{f.id}] ({f.type}, imp {f.importance}) "
            + re.sub(r"\s*\n\s*", " / ", f.text or "").strip()
            for f in ledger)

    def _ops_request(self, system: str, user: str, what: str) -> list:
        """Sema-kisitli ops istegi. HER basarisizlik LLMUnavailable'dir: gramer
        kisiti gecerli {"ops":[...]} garantiler; "ops" anahtari YOKSA yanit
        kesilmis/curumus demektir ve turlar imzalanmamalidir. (Eski davranis:
        kesik JSON -> {} -> [] -> TUM birikinti "islenmis" imzalanirdi = sessiz
        toplu hafiza kaybi. Gecerli bos sonuc zaten {"ops":[]} olarak gelir.)"""
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        try:
            data = self.client.complete_json(messages, self._p_extract, OPS_SCHEMA)
        except LlamaError as e:
            _log.warning("%s: llm_unavailable err=%s", what, err_brief(e))
            raise LLMUnavailable(err_brief(e), status=getattr(e, "status", None)) from e
        except Exception as e:
            _log.warning("%s: beklenmedik istek hatasi err=%s", what, err_brief(e))
            raise LLMUnavailable(err_brief(e)) from e
        ops = data.get("ops") if isinstance(data, dict) else None
        if not isinstance(ops, list):
            _log.warning("%s: cevap kesik/bicimsiz (ops yok) - turlar imzalanmayacak", what)
            raise LLMUnavailable("bad_json: ops missing")
        return ops

    def extract_ops(self, ledger: list, new_turns: str) -> list[dict]:
        """Return validated, evidence-checked ops. ledger = list[FactRow]
        (isimli erisim: f.id/f.type/f.text/f.importance - tuple DEGIL)."""
        ledger_txt = self._ledger_text(ledger) or "(empty)"
        user = (f"CURRENT LEDGER:\n{ledger_txt}\n\nNEW MESSAGES:\n{new_turns}\n\n"
                "Return the JSON ops.")
        ops = self._ops_request(_EXTRACT_SYS, user, "extract_ops")
        # rol onekleri kanit kaynagından cikarilir: her satirda gectiklerinden
        # kanit ortusmesine bedava katki yapiyorlardi (quote-or-drop gevsemesi)
        source_norm = _norm(re.sub(r"(?mi)^(user|assistant):\s*", "", new_turns))
        valid_ids = {f.id for f in ledger}
        out: list[dict] = []
        for op in ops:
            if not isinstance(op, dict):
                continue
            kind = op.get("op")
            text = (op.get("text") or "").strip()
            if kind in ("ADD", "UPDATE"):
                if not text or not _evidence_supported(op.get("evidence", ""), source_norm):
                    continue  # quote-or-drop
            if kind in ("UPDATE", "DELETE") and op.get("id") not in valid_ids:
                if kind == "UPDATE" and text:      # points at a missing fact -> treat as ADD
                    op = {**op, "op": "ADD", "id": 0}
                else:
                    continue
            if kind not in ("ADD", "UPDATE", "DELETE"):
                continue
            out.append(op)
        return out

    def update_recap(self, current_recap: str, new_turns: str) -> str:
        """Return 1-2 NEW sentences to append to the recap (grounded in new_turns)."""
        user = f"RECAP:\n{current_recap or '(none yet)'}\n\nNEW MESSAGES:\n{new_turns}"
        messages = [{"role": "system", "content": _RECAP_SYS},
                    {"role": "user", "content": user}]
        try:
            return self.client.complete(messages, self._p_recap).strip()
        except LlamaError as e:
            _log.warning("update_recap: llm_unavailable err=%s", err_brief(e))
            raise LLMUnavailable(err_brief(e), status=getattr(e, "status", None)) from e
        except Exception as e:
            _log.warning("update_recap: hata err=%s", err_brief(e))
            return ""

    def reflect(self, ledger: list) -> list[dict]:  # ledger = list[FactRow]
        """Periodic ledger cleanup: merge/dedup, resolve contradictions, add insights.

        Operates over the LEDGER (not raw turns), so ops here are NOT quote-checked:
        DELETE removes duplicates/contradicted/trivial facts; ADD adds synthesized
        'insight' facts. UPDATE is ignored (reflection shouldn't paraphrase facts).
        """
        if not ledger:
            return []
        user = (f"CURRENT LEDGER:\n{self._ledger_text(ledger)}\n\n"
                "Clean it up and return the JSON ops.")
        ops = self._ops_request(_REFLECT_SYS, user, "reflect")
        valid_ids = {f.id for f in ledger}
        out: list[dict] = []
        for op in ops:
            if not isinstance(op, dict):
                continue
            kind = op.get("op")
            if kind == "DELETE" and op.get("id") in valid_ids:
                out.append(op)
            elif kind == "ADD" and (op.get("text") or "").strip():
                out.append({**op, "op": "ADD", "id": 0, "type": op.get("type") or "insight"})
        return out
