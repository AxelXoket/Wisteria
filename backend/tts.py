"""Chatterbox text-to-speech - client side (runs in the MAIN app venv).

This module is a THIN client. It owns no heavy deps: it segments the reply into
speakable units and drives a Chatterbox worker running in its own venv/process
(see ``tts_worker.py``). That isolation keeps torch/transformers/numpy out of the
app venv (no clash with the memory stack).

Lifecycle: the worker is RESIDENT - spawned at app boot (parallel with the LLM
load; measured 2.2 GB VRAM margin with both models resident) and killed only at
app close (``shutdown``). The speaker toggle is just the AUTO-SPEAK flag: with it
off the worker stays warm, so per-message playback and re-enabling are instant.

Flow per reply (auto mode, non-streaming - she speaks once the text is done):

    token deltas ─▶ accumulate ─▶ (end of turn) segment into *narration*/dialogue
                 ─▶ send one "speak" command to the worker
                 ─▶ worker: Chatterbox synth ─▶ BALANCED cleanup ─▶ speakers

Per-message playback (works with auto OFF): ``speak_text(text)`` barge-ins any
current speech and voices that one message. Live tuning: ``update_params`` pushes
speed/denoise/emotion to the worker without a reload.

Public interface: ``configure / status / set_enabled / ensure_loaded / begin_turn
/ feed / end_turn / barge_in / speak_text / update_params / shutdown``.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from .logutil import err_brief, log_for

_log = log_for("tts")

try:  # Windows job object: worker dies WITH the app (never orphans holding VRAM)
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32job  # type: ignore
    _HAS_WIN32 = True
except Exception:  # pragma: no cover
    _HAS_WIN32 = False

# Chatterbox emits mono float32 at 24 kHz.
SAMPLE_RATE = 24000

_CREATE_NO_WINDOW = 0x08000000  # Windows: don't pop a console for the child

# Characters that end a speakable sentence.
_SENTENCE_END = ".!?…\n"
# Softer boundaries used only to bound the size of a run-on span.
_CLAUSE_BREAK = ",;:--"
# Split a run-on span at a clause break once it grows past this many chars.
_CLAUSE_MIN = 60


def _extract_units(buf: str, narration_mode: str, in_ast: bool = False):
    """Split ``buf`` into speakable ``(voice, text)`` units.

    ``voice`` is ``"narration"`` for ``*...*`` stage directions and ``"dialogue"``
    for everything else. Returns ``(units, remainder, in_ast)``. Boundaries are
    emitted only when definite (a sentence-ender followed by a real char, a clause
    break on a long span, or an asterisk mode switch). Asterisks are consumed, and
    the remainder resumes in the returned ``in_ast`` mode. Since synthesis now
    happens once at end-of-turn, this is called on the whole reply in one pass
    (with a trailing "\\n " so the final sentence flushes).
    """
    units: list[tuple[str, str]] = []
    flushed = 0
    cur_start = 0
    i = 0
    n = len(buf)

    def emit(mode: str, start: int, end: int) -> None:
        nonlocal flushed
        flushed = end
        text = buf[start:end].strip().strip('"').strip()
        if not text:
            return
        if mode == "narration":
            if narration_mode == "skip":
                return
            voice = "narration" if narration_mode == "soft" else "dialogue"
        else:
            voice = "dialogue"
        units.append((voice, text))

    while i < n:
        ch = buf[i]
        if ch == "*":
            emit("narration" if in_ast else "dialogue", cur_start, i)
            in_ast = not in_ast
            i += 1
            cur_start = i
            flushed = i
            continue
        if ch in _SENTENCE_END:
            nxt = buf[i + 1] if i + 1 < n else ""
            if nxt == "":
                break
            if nxt in " \t\n\"'" or nxt == "*":
                emit("narration" if in_ast else "dialogue", cur_start, i + 1)
                i += 1
                cur_start = i
                continue
        elif ch in _CLAUSE_BREAK and (i - cur_start) >= _CLAUSE_MIN:
            nxt = buf[i + 1] if i + 1 < n else ""
            if nxt in " \t":
                emit("narration" if in_ast else "dialogue", cur_start, i + 1)
                i += 1
                cur_start = i
                continue
        i += 1

    return units, buf[flushed:], in_ast


def _merge_units(units, max_chars: int = 180):
    """Combine adjacent same-voice fragments into fuller chunks (up to ``max_chars``).

    Chatterbox stutters / repeats the opening on very short inputs ("Sit.", "come
    here."), so we glue consecutive same-voice sentences together until they reach a
    natural length. This steadies prosody and cuts the per-utterance overhead, while
    still keeping chunks short enough that the first audio lands quickly.
    """
    merged: list[tuple[str, str]] = []
    for voice, text in units:
        if merged and merged[-1][0] == voice and len(merged[-1][1]) + 1 + len(text) <= max_chars:
            merged[-1] = (voice, merged[-1][1] + " " + text)
        else:
            merged.append((voice, text))
    return merged


class TtsEngine:
    """Manages the resident Chatterbox worker subprocess and feeds it replies."""

    def __init__(self) -> None:
        self._cfg = None
        self._auto = False       # auto-speak replies (the speaker toggle)
        self._speaking = False   # worker is currently playing audio (for the UI)

        self._state = "off"     # off | loading | ready | error | unavailable
        self._detail = ""
        self._lock = threading.Lock()

        self._proc: subprocess.Popen | None = None
        self._job = None  # Windows job object handle (worker dies with the app)
        self._seq = 0
        self._buf = ""
        # Yukleme kapisi (denetim O9): worker stdin'i ANCAK model yuklendikten
        # sonra okur; o pencerede boruya yazmak 4KB tamponu doldurup bridge
        # thread'ini suresiz bloklayabilir. Ayar guncellemeleri hazir olana dek
        # burada COALESCE edilir (son deger kazanir), ready'de tek seferde gider.
        self._got_ready = False
        self._pending_params: dict = {}
        self._closing = False    # shutdown ile yarisan reader hayalet "error" basmasin
        self._speak_ts = 0.0     # takili "konusuyor" gostergesi icin nabiz
        # stdin'e artik iki thread yazabilir (bridge cagrilari + reader'in
        # ready-flush'i): kilitsiz es zamanli write satirlari karistirabilirdi
        self._wlock = threading.Lock()

    # ------------------------------------------------------------- configuration

    def configure(self, cfg) -> None:
        self._cfg = cfg

    def status(self) -> dict:
        # kemer: done/error olayi kacarsa (olu akis) gosterge sonsuza dek donmesin
        if self._speaking and self._speak_ts and time.time() - self._speak_ts > 120:
            _log.warning("speaking gostergesi 120sn olaysiz kaldi - birakiliyor")
            self._speaking = False
        return {
            "enabled": self._auto,  # back-compat alias for the frontend
            "auto": self._auto,
            "loaded": self.loaded,
            "state": self._state,
            "detail": self._detail,
            "speaking": self._speaking,
        }

    def set_enabled(self, on: bool) -> dict:
        """Toggle AUTO-SPEAK only. The worker stays resident either way - turning
        the voice off must not unload the model (user requirement: re-enable and
        per-message playback are instant)."""
        self._auto = bool(on)
        if self._auto and self._state in ("off", "error", "unavailable"):
            self._spawn()  # also the retry path after a worker error
        return self.status()

    def ensure_loaded(self) -> dict:
        """Spawn the resident worker (called at app boot, regardless of auto)."""
        if self._cfg is not None and self._proc is None \
                and self._state in ("off", "error", "unavailable"):
            self._spawn()
        return self.status()

    @property
    def loaded(self) -> bool:
        return self._state == "ready" and self._proc is not None

    @property
    def ready(self) -> bool:
        """Auto-speak gate used by begin_turn/feed/end_turn."""
        return self._auto and self.loaded

    # ------------------------------------------------------------- worker launch

    def _worker_script(self) -> Path:
        override = getattr(self._cfg, "tts_worker_script", None)
        if override:
            return Path(override)
        return Path(__file__).resolve().parent / "tts_worker.py"

    def _worker_python(self) -> Path | None:
        py = getattr(self._cfg, "tts_python", None)
        return Path(py) if py else None

    def _resolve_ref(self) -> str:
        """Locate the cloned-voice reference clip (defaults to voices/wisteria.wav)."""
        ref = getattr(self._cfg, "tts_voice_wav", None)
        ref = Path(ref) if ref else None
        if ref and ref.exists():
            return str(ref)
        if ref and ref.parent.is_dir():
            for cand in sorted(ref.parent.glob(ref.stem + ".*")):
                if cand.suffix.lower() in (".wav", ".flac", ".mp3", ".ogg", ".m4a"):
                    return str(cand)
        return ""

    def _worker_cfg(self) -> dict:
        c = self._cfg
        return {
            "device": getattr(c, "tts_device", "cuda"),
            "ref_wav": self._resolve_ref(),
            "exaggeration": getattr(c, "tts_exaggeration", 0.5),
            "cfg_weight": getattr(c, "tts_cfg_weight", 0.5),
            "temperature": getattr(c, "tts_temperature", 0.7),
            "repetition_penalty": getattr(c, "tts_repetition_penalty", 1.3),
            "speed": getattr(c, "tts_speed", 1.1),
            "denoise_prop": getattr(c, "tts_denoise_prop", 0.75),
            "highpass_hz": getattr(c, "tts_highpass_hz", 85.0),
            "normalize_peak": getattr(c, "tts_normalize_peak", 0.95),
            "gap_ms": getattr(c, "tts_gap_ms", 180.0),
            "narration_mode": getattr(c, "tts_narration_mode", "soft"),
            "narration_gain": getattr(c, "tts_narration_gain", 0.6),
        }

    def _spawn(self) -> None:
        with self._lock:
            if self._state == "loading" or self._proc is not None:
                return
            py = self._worker_python()
            if not py or not py.exists():
                self._state = "unavailable"
                self._detail = "TTS ortami yok - install-tts.bat calistir (tts_env)."
                return
            script = self._worker_script()
            if not script.exists():
                self._state = "unavailable"
                self._detail = f"worker bulunamadi: {script.name}"
                return
            self._state = "loading"
            self._detail = ""
            self._got_ready = False
            self._closing = False
            try:
                self._proc = subprocess.Popen(
                    [str(py), str(script), json.dumps(self._worker_cfg())],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", bufsize=1,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except Exception as exc:
                self._proc = None
                self._state = "error"
                self._detail = f"worker baslatilamadi: {exc}"
                _log.error("tts worker spawn hatasi err=%s", err_brief(exc))
                return
            self._make_job()  # tie the worker's lifetime to the app (no VRAM orphans)
            proc = self._proc  # kilit ICINDE yakala: kilitsiz yeniden okuma,
        # shutdown'la yarisip thread'e None gecirebiliyordu (hayalet error)
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()

    def _make_job(self) -> None:
        """Windows Job Object (KILL_ON_JOB_CLOSE): if the app dies for ANY reason,
        the worker tree (venv shim + real interpreter) dies with it. Without this a
        hard-killed app left an orphaned tts_env python holding ~3.8 GB VRAM."""
        if not _HAS_WIN32 or self._proc is None:
            return
        try:
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            h = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, self._proc.pid)
            win32job.AssignProcessToJobObject(job, h)
            self._job = job  # keep the handle alive for the app's lifetime
        except Exception:
            self._job = None

    def _reader(self, proc: subprocess.Popen) -> None:
        """Consume worker events from stdout and reflect them into state."""
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                kind = ev.get("ev")
                if kind == "ready":
                    # gecis + bekleyen-ayar devri AYNI kilit altinda: kilitsiz
                    # halde "bayragi gordum-stashladim" ile "flip ettim-flush
                    # ettim" araya girisebiliyor, tam flip aninda gelen ayar
                    # sonsuza dek beklemede kaliyordu (dogrulama turu bulgusu)
                    with self._wlock:
                        self._got_ready = True
                        self._state = "ready"
                        pend, self._pending_params = self._pending_params, {}
                    cloned = ev.get("cloned")
                    self._detail = "hazir (ses klonu)" if cloned else "hazir"
                    if pend:  # yukleme sirasinda biriken ayarlar tek pakette
                        self._send({"cmd": "config", **pend})
                elif kind == "speaking":
                    self._speaking = True
                    self._speak_ts = time.time()
                elif kind == "done":
                    self._speaking = False
                elif kind == "status":
                    self._detail = str(ev.get("detail", ""))
                elif kind == "error":
                    # a fatal load error arrives before "ready"; per-utterance
                    # warnings arrive after and shouldn't flip us out of ready.
                    if self._state != "ready":
                        self._state = "error"
                    self._detail = str(ev.get("detail", ""))
                    self._speaking = False  # hatali cumle gostergeyi takili birakmasin
        except Exception:
            pass
        # stdout closed -> worker exited (resident worker death is always an error)
        if proc is not None and self._proc is proc and not self._closing:
            self._proc = None
            self._speaking = False
            if self._state != "unavailable":
                self._state = "error"
                if not self._detail:
                    self._detail = "worker beklenmedik sekilde kapandi"
                _log.error("tts worker beklenmedik kapandi detail=%s", self._detail)

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for _ in proc.stderr:  # type: ignore[union-attr]
                pass  # keep the pipe from filling; tracebacks land on stdout as events
        except Exception:
            pass

    def _send(self, msg: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        # karar + eylem TEK kilit altinda: _got_ready kontrolu, stash ve pipe
        # yazimi bolunemez - reader'in ready-flip'i ile yarisamaz
        try:
            with self._wlock:
                if not self._got_ready:
                    # worker yukleme bitmeden stdin OKUMAZ: yazmak boruyu doldurup
                    # bu (bridge) thread'ini suresiz bloklayabilirdi. Ayarlar
                    # coalesce edilir; speak/stop yuklenirken anlamsiz - dusurulur.
                    if msg.get("cmd") == "config":
                        p = dict(msg)
                        p.pop("cmd", None)
                        self._pending_params.update(p)
                    return
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
        except Exception as e:
            _log.warning("tts stdin yazilamadi err=%s", err_brief(e))

    def _kill(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if self._got_ready:
            # nazik yol yalniz worker stdin'i OKURKEN denenir; yuklenirken
            # yazmak kapanisi suresiz asabilirdi (denetim O9) - o durumda
            # dogrudan agac oldurmeye gecilir
            try:
                if proc.stdin:
                    with self._wlock:
                        proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                        proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.5)
                return  # clean exit - tree is gone
            except Exception:
                pass
        # Graceful failed (e.g. worker still mid-load, stdin cmd not yet read).
        # proc is the VENV SHIM; terminate() would kill only the shim and orphan
        # the real interpreter child holding ~3.8 GB VRAM - kill the WHOLE TREE.
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           creationflags=_CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------- turn control

    def begin_turn(self) -> None:
        if not self.ready:
            return
        self._buf = ""
        self._seq += 1

    def feed(self, delta: str) -> None:
        """Accumulate a visible token delta (already stripped of <observe>/<think>)."""
        if not self.ready:
            return
        self._buf += delta

    def end_turn(self) -> None:
        """Segment the whole reply and hand it to the worker to voice."""
        if not self.ready:
            return
        text = self._buf
        self._buf = ""
        if not text.strip():
            return
        mode = getattr(self._cfg, "tts_narration_mode", "soft") or "soft"
        # trailing "\n " gives the last sentence a lookahead char so it flushes
        units, _, _ = _extract_units(text + "\n ", mode, False)
        units = _merge_units(units)  # glue short fragments -> steadier, less-stuttery synth
        if units:
            self._send({"cmd": "speak", "seq": self._seq, "units": units})

    def barge_in(self) -> None:
        """Interrupt any speech immediately."""
        self._buf = ""
        self._speaking = False
        if self._proc is not None:
            self._send({"cmd": "stop"})

    # --------------------------------------------------- per-message + live tuning

    def speak_text(self, text: str) -> bool:
        """Voice ONE message on demand (works with auto-speak off)."""
        if not self.loaded or not (text or "").strip():
            return False
        self.barge_in()  # stop whatever is playing; this message takes over
        mode = getattr(self._cfg, "tts_narration_mode", "soft") or "soft"
        units, _, _ = _extract_units(text + "\n ", mode, False)
        units = _merge_units(units)
        if not units:
            return False
        self._seq += 1
        # Optimistic: synthesis takes seconds before the worker's "speaking" event;
        # without this the UI's status poll sees speaking=False mid-synth and drops
        # its playing indicator. The worker's done/error (or barge_in) clears it;
        # status()'un 120sn nabiz kemeri de kacan olaya karsi son guvence.
        self._speaking = True
        self._speak_ts = time.time()
        self._send({"cmd": "speak", "seq": self._seq, "units": units})
        return True

    def update_params(self, params: dict) -> None:
        """Push live tuning (speed/denoise/emotion...) to the worker, no reload."""
        if self._proc is not None and params:
            self._send({"cmd": "config", **params})

    def shutdown(self) -> None:
        self._closing = True  # reader'in dogal EOF'u hayalet "error" basmasin
        self._auto = False
        self._kill()
        self._state = "off"
        self._speaking = False


# Module-level singleton used by the app.
TTS = TtsEngine()
