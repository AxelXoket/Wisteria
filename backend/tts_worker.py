"""Chatterbox TTS subprocess worker - runs in its OWN venv (tts_env), never imported
by the app. Launched by backend/tts.py as a child process.

Why a separate process (not in-process like the old XTTS engine):
  * Dependency isolation. Chatterbox drags in torch/transformers/numpy with tight
    pins that would collide with the memory stack (fastembed/onnxruntime) already
    proven in the main venv. A child process with its own venv = zero collision.
  * VRAM control. The worker can be spawned only when voice is on and killed to
    reclaim its ~5-6 GB the instant it's turned off - the "where do we run it"
    lever. It can also be pointed at the CPU by config with no app changes.
  * Crash isolation. If Chatterbox faults, the app survives; we just report it.

Voice: the companion speaks a user-provided, consent-based cloned voice (voices/wisteria.wav by default),
with the approved BALANCED cleanup applied to every utterance
(spectral denoise -> high-pass -> normalize), matching the sample the user OK'd.

Protocol - newline-delimited JSON, stdin = commands, stdout = events:
  stdin:
    {"cmd":"speak","seq":N,"units":[["dialogue","..."],["narration","..."]]}
    {"cmd":"speak","seq":N,"units":[...],"save":"C:/path/out.wav"}  # debug: write, don't play
    {"cmd":"stop"}       # barge-in: abort current utterance + drop the queue now
    {"cmd":"shutdown"}   # clean exit
  stdout (one JSON object per line):
    {"ev":"ready"}                    model loaded + voice conditioned, can speak
    {"ev":"status","detail":"..."}    informational
    {"ev":"error","detail":"..."}     fatal load error (worker exits) or per-utterance warn
    {"ev":"speaking","seq":N}         began playback of turn N
    {"ev":"done","seq":N}             finished turn N

  stdin (live tuning, no reload):
    {"cmd":"config","speed":1.15,"denoise_prop":0.8,"exaggeration":0.6,...}

Config arrives as one JSON blob in argv[1] so the app owns every setting.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback

os.environ.setdefault("TQDM_DISABLE", "1")           # silence Chatterbox sampling bars
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


def _emit(ev: str, **kw) -> None:
    """Write one event line to stdout and flush (the app reads these)."""
    try:
        sys.stdout.write(json.dumps({"ev": ev, **kw}) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


class Worker:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.sr = 24000  # Chatterbox native sample rate; overwritten from the model

        # tuning (all from the app's config)
        self.device = str(cfg.get("device", "cuda"))
        self.ref = cfg.get("ref_wav") or ""
        self.exaggeration = float(cfg.get("exaggeration", 0.5))
        self.cfg_weight = float(cfg.get("cfg_weight", 0.5))
        self.temperature = float(cfg.get("temperature", 0.7))
        self.repetition_penalty = float(cfg.get("repetition_penalty", 1.3))
        self.speed = float(cfg.get("speed", 1.1))
        self.denoise_prop = float(cfg.get("denoise_prop", 0.75))
        self.highpass_hz = float(cfg.get("highpass_hz", 85.0))
        self.normalize_peak = float(cfg.get("normalize_peak", 0.95))
        self.gap_ms = float(cfg.get("gap_ms", 180.0))
        self.narration_mode = str(cfg.get("narration_mode", "soft"))
        self.narration_gain = float(cfg.get("narration_gain", 0.6))

        # heavy libs (loaded in _load)
        self.np = None
        self.sd = None
        self.torch = None
        self.nr = None
        self.sps = None
        self.model = None

        # generation control (barge-in via a monotonic generation counter)
        self.gen = 0
        self.req_q: "queue.Queue" = queue.Queue()
        self.conds_ready = False

        # gapless player
        self.audio_q: "queue.Queue" = queue.Queue(maxsize=256)
        self.stream = None
        self.residual = None
        self.play_lock = threading.Lock()

    # Live-tunable float params ({"cmd":"config", ...}). Plain attribute writes are
    # GIL-atomic; the synth thread reads them per-sentence - no lock needed.
    _TUNABLE = ("speed", "denoise_prop", "exaggeration", "cfg_weight", "temperature",
                "highpass_hz", "gap_ms", "narration_gain", "normalize_peak",
                "repetition_penalty")

    def apply_config(self, msg: dict) -> None:
        for k in self._TUNABLE:
            if k in msg:
                try:
                    setattr(self, k, float(msg[k]))
                except (TypeError, ValueError):
                    pass
        if "narration_mode" in msg:
            self.narration_mode = str(msg["narration_mode"])
        _emit("status", detail="ayarlar guncellendi")

    # ------------------------------------------------------------------- loading

    def load(self) -> None:
        # sounddevice is imported lazily (only for playback) in _open_stream, so the
        # worker loads fine headless / in save-mode without an audio output device.
        import numpy as np
        import noisereduce as nr
        import scipy.signal as sps
        import torch

        self.np, self.torch, self.nr, self.sps = np, torch, nr, sps

        if self.device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA istendi ama torch GPU gormuyor (cu128 kurulu mu?).")

        # Chatterbox pulls in `perth` (watermarker); on this build the implicit
        # watermarker class resolves to None -> TypeError at init. Neutralise it
        # (we are not distributing generated audio; local playback only).
        try:
            import perth

            class _NoWM:
                def apply_watermark(self, wav, sample_rate=None, watermark=None):
                    return wav

                def get_watermark(self, wav, sample_rate=None):
                    return None

            perth.PerthImplicitWatermarker = _NoWM
        except Exception:
            pass

        from chatterbox.tts import ChatterboxTTS

        self.model = ChatterboxTTS.from_pretrained(device=self.device)
        self.sr = int(getattr(self.model, "sr", 24000))

        # Condition on the cloned voice ONCE (re-encoding the reference for every
        # sentence would waste ~0.3-0.5 s each). generate() then reuses self.conds.
        if self.ref:
            try:
                self.model.prepare_conditionals(self.ref, exaggeration=self.exaggeration)
                self.conds_ready = True
                _emit("status", detail=f"ses klonu hazir: {self.ref}")
            except Exception as exc:
                self.conds_ready = False
                _emit("status", detail=f"referans kosullanamadi ({exc}); her cumlede yuklenecek")

    def _open_stream(self) -> None:
        import sounddevice as sd  # lazy: only needed to actually play audio
        self.sd = sd
        np = self.np
        self.residual = np.zeros(0, dtype=np.float32)

        def _cb(outdata, frames, time_info, status):  # audio thread
            out = self.residual
            while len(out) < frames:
                try:
                    chunk = self.audio_q.get_nowait()
                except queue.Empty:
                    break
                if chunk is None:
                    break
                out = np.concatenate([out, chunk])
            k = min(len(out), frames)
            outdata[:k, 0] = out[:k]
            if k < frames:
                outdata[k:, 0] = 0.0
            self.residual = out[k:]

        self.stream = sd.OutputStream(
            samplerate=self.sr, channels=1, dtype="float32",
            blocksize=1024, callback=_cb,
        )
        self.stream.start()

    # ---------------------------------------------------------------- synthesis

    def _synth_one(self, text: str):
        """Chatterbox -> float32 mono at self.sr for one sentence (no post yet)."""
        torch, np = self.torch, self.np
        with torch.inference_mode():
            if self.conds_ready:
                wav = self.model.generate(
                    text, exaggeration=self.exaggeration, cfg_weight=self.cfg_weight,
                    temperature=self.temperature, repetition_penalty=self.repetition_penalty,
                )
            else:
                wav = self.model.generate(
                    text, audio_prompt_path=self.ref, exaggeration=self.exaggeration,
                    cfg_weight=self.cfg_weight, temperature=self.temperature,
                    repetition_penalty=self.repetition_penalty,
                )
        return wav.squeeze().detach().to(torch.float32).cpu().numpy()

    def _stretch(self, y):
        """Pitch-preserving speed-up via WSOLA (time-domain) - NOT a phase vocoder.
        librosa.time_stretch (STFT PV) smeared transients and made the voice sound
        distant/underwater with a bleeding hum; WSOLA is artifact-free at 1.0-1.3x
        speech rates. If audiotsm is missing we SKIP stretching entirely (a slightly
        slower voice beats a phasey one)."""
        np = self.np
        if not self.speed or abs(self.speed - 1.0) <= 0.01:
            return y
        try:
            from audiotsm import wsola
            from audiotsm.io.array import ArrayReader, ArrayWriter
            reader = ArrayReader(y.reshape(1, -1).astype(np.float32))
            writer = ArrayWriter(1)
            wsola(1, speed=float(self.speed)).run(reader, writer)
            return writer.data.reshape(-1).astype(np.float32)
        except Exception:
            return y

    def _polish(self, y):
        """Cleanup chain per sentence, in artifact-safe order:
        WSOLA stretch -> spectral denoise (STATIONARY: constant floor, no pumping)
        -> high-pass -> tail/head trim (drops Chatterbox's breathy tail that bled a
        fading hum behind speech) -> short edge fades (no hard cut into the silent
        gaps) -> normalize to a shared peak so sentences sit at one loudness.
        Narration gain is applied by the caller, after this."""
        np, nr, sps = self.np, self.nr, self.sps
        if y.size == 0:
            return y
        y = self._stretch(y)
        y = nr.reduce_noise(y=y, sr=self.sr, prop_decrease=self.denoise_prop, stationary=True)
        b, a = sps.butter(2, self.highpass_hz / (self.sr / 2.0), btype="high")
        y = sps.filtfilt(b, a, y).astype(np.float32)
        try:  # trim breathy/noisy edges (~-45 dB); keep a hair of natural air
            import librosa
            y, _ = librosa.effects.trim(y, top_db=45)
        except Exception:
            pass
        n = int(self.sr * 0.025)  # 25 ms edge fades
        if y.size > 2 * n:
            env = np.linspace(0.0, 1.0, n, dtype=np.float32)
            y[:n] *= env
            y[-n:] *= env[::-1]
        peak = float(np.max(np.abs(y))) or 1.0
        return (y / peak * self.normalize_peak).astype(np.float32)

    def _process(self, req: dict) -> None:
        """Synthesize + clean + play each sentence AS IT'S READY so speech starts
        after ~one sentence (not after the whole reply). Each unit is a complete
        sentence, so there's no mid-word artifact. In save mode we instead collect
        everything and write one file (debug/preview)."""
        np = self.np
        my = self.gen
        seq = req.get("seq", 0)
        units = req.get("units", [])
        save = req.get("save")

        gap = np.zeros(int(self.sr * self.gap_ms / 1000.0), dtype=np.float32)
        collected = []      # save mode only
        played_any = False  # play mode: whether we've emitted "speaking"/queued audio

        for voice, text in units:
            if my != self.gen:
                return  # barged out
            text = (text or "").strip()
            if not text:
                continue
            if voice == "narration" and self.narration_mode == "skip":
                continue
            try:
                y = self._polish(self._synth_one(text))
            except Exception as exc:
                _emit("error", detail=f"cumle sentezlenemedi: {exc}", seq=seq)
                continue
            if voice == "narration" and self.narration_mode == "soft":
                y = y * self.narration_gain

            if save:
                if collected:
                    collected.append(gap)
                collected.append(y)
                continue

            # play mode: stream this sentence to the speakers now
            if self.stream is None:
                try:
                    self._open_stream()
                except Exception as exc:
                    _emit("error", detail=f"ses cihazi acilamadi: {exc}", seq=seq)
                    return
            if not played_any:
                _emit("speaking", seq=seq)
                played_any = True
                chunks = [y]
            else:
                chunks = [gap, y]
            for piece in chunks:
                block = 4096
                for i in range(0, len(piece), block):
                    if my != self.gen:
                        return
                    try:
                        self.audio_q.put(piece[i:i + block], timeout=2.0)
                    except queue.Full:
                        return

        if save and collected:
            try:
                import soundfile as sf
                sf.write(save, np.concatenate(collected), self.sr)
                _emit("done", seq=seq, saved=save)
            except Exception as exc:
                _emit("error", detail=f"kaydedilemedi: {exc}", seq=seq)
            return
        if my == self.gen:
            _emit("done", seq=seq)

    def _synth_loop(self) -> None:
        while True:
            req = self.req_q.get()
            if req is None:
                return
            try:
                self._process(req)
            except Exception:
                _emit("error", detail="synth loop: " + traceback.format_exc().splitlines()[-1])

    # ------------------------------------------------------------------- control

    def stop(self) -> None:
        """Barge-in: invalidate in-flight work, drop queues, abort playback. < ~100 ms."""
        self.gen += 1
        _drain(self.req_q)
        _drain(self.audio_q)
        with self.play_lock:
            if self.stream is not None and self.np is not None:
                try:
                    self.stream.abort()
                    self.residual = self.np.zeros(0, dtype=self.np.float32)
                    self.stream.start()
                except Exception:
                    pass

    def shutdown(self) -> None:
        self.stop()
        self.req_q.put(None)
        with self.play_lock:
            if self.stream is not None:
                try:
                    self.stream.stop(); self.stream.close()
                except Exception:
                    pass
                self.stream = None

    def run(self) -> None:
        try:
            self.load()
        except Exception as exc:
            _emit("error", detail=f"yuklenemedi: {type(exc).__name__}: {exc}")
            return
        threading.Thread(target=self._synth_loop, daemon=True).start()
        _emit("ready", sr=self.sr, device=self.device, cloned=self.conds_ready)

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            cmd = msg.get("cmd")
            if cmd == "speak":
                self.req_q.put(msg)
            elif cmd == "stop":
                self.stop()
            elif cmd == "config":
                self.apply_config(msg)
            elif cmd == "shutdown":
                break
        self.shutdown()


def _drain(q: "queue.Queue") -> None:
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def main() -> None:
    cfg = {}
    if len(sys.argv) > 1:
        try:
            cfg = json.loads(sys.argv[1])
        except Exception:
            cfg = {}
    Worker(cfg).run()


if __name__ == "__main__":
    main()
