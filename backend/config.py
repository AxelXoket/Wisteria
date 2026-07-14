"""Configuration + path resolution for the desktop app.

Resolves everything relative to the *project root* (the folder that contains
Models/ and llama_cpp/), so the app works both in dev (the app folder inside the
project) and when packaged (the .exe placed next to Models/ and llama_cpp/).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


def app_dir() -> Path:
    """Directory of the real launched executable (frozen) or the app source (dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]  # app source dir


def bundle_dir() -> Path:
    """Where bundled assets (web/) live: PyInstaller _MEIPASS when frozen, else the source dir."""
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        return Path(mp)
    return Path(__file__).resolve().parents[1]  # app source dir


def project_root() -> Path:
    """Walk up from the app dir to find the folder holding Models/ and llama_cpp/."""
    here = app_dir()
    for cand in [here, *here.parents]:
        if (cand / "Models").is_dir() and (cand / "llama_cpp").is_dir():
            return cand
    return here.parent  # sensible fallback


ROOT = project_root()


@dataclass
class GenPreset:
    temperature: float = 0.8
    top_p: float = 0.95
    top_k: int = 64
    min_p: float = 0.02
    repeat_penalty: float = 1.05
    max_tokens: int = 2048


@dataclass
class Config:
    root: Path = ROOT

    # --- model / server (llama-server sidecar flags) ---
    # Generic placeholders - the ACTUAL file names come from settings.json
    # ("model_file" / "mmproj_file", created locally, git-ignored), so which
    # model you run stays your business. See apply_settings_to_config.
    model_path: Path = field(default_factory=lambda: ROOT / "Models" / "model.gguf")
    mmproj_path: Path = field(default_factory=lambda: ROOT / "Models" / "mmproj.gguf")
    llama_server: Path = field(default_factory=lambda: ROOT / "llama_cpp" / "llama-server.exe")
    # Served model alias. Overridable via settings.json ("model_alias") so tools
    # that address the server by name keep matching without touching source.
    alias: str = "local-model"
    # Loopback auth key for the llama-server sidecar. NOT committed to source:
    # loaded from settings.json; a fresh install generates a random one on first
    # run (see ensure_api_key). Keeps even localhost traffic key-gated.
    api_key: str = ""
    host: str = "127.0.0.1"
    # 16k context is the measured sweet spot: on sliding-window-attention models
    # 24k->16k costs only ~0.4 GB. (Chatterbox voice runs in its own process; when
    # it's on, VRAM headroom is the open question tracked in tts_device.)
    n_ctx: int = 16384
    n_predict: int = 4096
    ngl: int = 999
    threads: int = 8
    ubatch: int = 1280
    image_max_tokens: int = 1120
    image_min_tokens: int = 256
    # VRAM levers proven by live measurement (RTX 5080 16GB):
    #   n_parallel=1  -> +1.0 GB free (llama.cpp auto-picks 4; the app only needs 1)
    #   KV-quant q8_0 -> +0.4 GB free, ~lossless (context-shift is off under vision anyway)
    n_parallel: int = 1
    cache_type_k: str = "q8_0"
    cache_type_v: str = "q8_0"

    # --- prompts (reuse the existing project files) ---
    system_prompt: Path = field(default_factory=lambda: ROOT / "system_prompts" / "system_prompt.txt")
    characters_dir: Path = field(default_factory=lambda: ROOT / "character_prompts")
    personas_dir: Path = field(default_factory=lambda: ROOT / "personas")
    default_character: str = "wisteria"
    # The name the base system prompt is written around; when a different
    # character is active, this token gets swapped for their display name.
    # Overridable via settings.json ("base_character_name").
    base_character_name: str = "Wisteria"

    # --- research (/ara) ---
    research_dir: Path = field(default_factory=lambda: ROOT / "local-research-agent")
    research_depth: str = "quick"
    research_timeout: int = 90

    # --- generation presets ---
    chat_preset: GenPreset = field(default_factory=lambda: GenPreset(temperature=0.8, max_tokens=2048))
    vision_obs_preset: GenPreset = field(default_factory=lambda: GenPreset(temperature=0.3, max_tokens=900))

    # --- behavior toggles ---
    use_system_role: bool = True
    use_vision_observation: bool = True

    # --- TTS (Chatterbox, opt-in; runs in a SEPARATE venv/process - see tts_worker.py) ---
    # The worker is launched with tts_python so its heavy torch/transformers/numpy
    # deps never touch the app venv (which shares fastembed/onnxruntime with memory).
    tts_enabled: bool = False              # off by default; user toggles it in the UI
    tts_python: Path = field(default_factory=lambda: app_dir() / "tts_env" / "Scripts" / "python.exe")
    tts_worker_script: Path = field(default_factory=lambda: app_dir() / "backend" / "tts_worker.py")
    tts_device: str = "cuda"               # "cuda" | "cpu" - the "where do we run it" lever
    # Cloned voice reference (user-provided, consent required). Any audio format resolves.
    tts_voice_wav: Path = field(default_factory=lambda: app_dir() / "voices" / "wisteria.wav")
    tts_exaggeration: float = 0.5          # Chatterbox emotion intensity (0.5 = natural)
    tts_cfg_weight: float = 0.5            # CFG weight (0.5 = default/stable; lower slows pacing)
    tts_temperature: float = 0.7           # a touch lower than 0.8 = steadier, fewer artifacts
    tts_repetition_penalty: float = 1.3    # >1.2 discourages the repeated sentence-start stutter
    tts_speed: float = 1.1                 # pitch-preserving playback speed-up (fixes "slightly slow")
    tts_narration_mode: str = "soft"       # "soft" (quieter *actions*) | "skip" | "all"
    tts_narration_gain: float = 0.6        # gain applied to narration when mode == "soft"
    # Approved BALANCED cleanup chain (spectral denoise -> high-pass -> normalize),
    # applied once per utterance so loudness stays even.
    tts_denoise_prop: float = 0.75
    tts_highpass_hz: float = 85.0
    tts_normalize_peak: float = 0.92       # headroom: 0.95 boosted the residual floor audibly
    tts_gap_ms: float = 180.0              # silence inserted between sentences
    # Tell the character her lines are voiced aloud, so she puts the emotion into the
    # words + punctuation (the voice reads tone from the text itself).
    tts_voice_awareness: bool = True

    # --- long-term memory (AES-256 encrypted, passphrase-locked) ---
    # NOT: prompt metinleri de (sistem/karakter/persona) migrasyondan sonra bu
    # sifreli DB'de yasar. memory_enabled=False yapilirsa kasa hic acilmaz ->
    # uygulama yer-tutucu bir sistem promptuyla calisir. Kapatma.
    memory_enabled: bool = True
    memory_dir: Path = field(default_factory=lambda: app_dir() / "memory")   # mem.db + key files
    embed_cache_dir: Path = field(default_factory=lambda: app_dir() / "models" / "embed")  # persistent fastembed cache
    mem_keep_recent: int = 10              # raw messages kept verbatim before eviction
    mem_consolidate_every: int = 2         # evicted messages that trigger a consolidation
    mem_max_facts: int = 25                # facts injected into the prompt each turn
    mem_recall_k: int = 3                  # episodic recall hits per turn
    mem_recall_max_dist: float = 1.2       # cosine-ish distance floor for recall
    mem_reflect_every: int = 15            # consolidations between reflection passes

    # --- web assets (bundled with the app) ---
    web_dir: Path = field(default_factory=lambda: bundle_dir() / "web")

    def api_base(self, port: int) -> str:
        return f"http://{self.host}:{port}"


# ------------------------------------------------------------------ settings

def settings_path() -> Path:
    """Portable-first: settings.json next to the app (no AppData trail)."""
    return app_dir() / "settings.json"


def load_settings() -> dict:
    p = settings_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_settings(data: dict) -> None:
    p = settings_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def apply_settings_to_config(s: dict, cfg: "Config") -> None:
    """Overlay persisted user settings onto the live CONFIG (bad values ignored).

    settings.json schema (portable, next to the app):
      { "w": 1120, "h": 820,                # window size (read by main.py)
        "tts_auto": false,                   # auto-speak replies
        "tts_speed": 1.1, "tts_denoise_prop": 0.75, "tts_exaggeration": 0.5 }
    """
    try:
        cfg.tts_enabled = bool(s.get("tts_auto", cfg.tts_enabled))
    except Exception:
        pass
    for key, attr in (("tts_speed", "tts_speed"),
                      ("tts_denoise_prop", "tts_denoise_prop"),
                      ("tts_exaggeration", "tts_exaggeration")):
        try:
            setattr(cfg, attr, float(s.get(key, getattr(cfg, attr))))
        except Exception:
            pass
    try:
        k = s.get("api_key")
        if isinstance(k, str) and k:
            cfg.api_key = k
    except Exception:
        pass
    # Model file names live in settings.json (not in source): privacy + per-user.
    for key, attr in (("model_file", "model_path"), ("mmproj_file", "mmproj_path")):
        try:
            name = s.get(key)
            if isinstance(name, str) and name:
                setattr(cfg, attr, ROOT / "Models" / name)
        except Exception:
            pass
    try:
        a = s.get("model_alias")
        if isinstance(a, str) and a:
            cfg.alias = a
    except Exception:
        pass
    try:
        b = s.get("base_character_name")
        if isinstance(b, str) and b:
            cfg.base_character_name = b
    except Exception:
        pass


def ensure_api_key(cfg: "Config") -> None:
    """Guarantee a sidecar auth key exists: settings.json wins; a fresh install
    generates a random one and persists it (merge-write, other keys preserved)."""
    if cfg.api_key:
        return
    import secrets
    cfg.api_key = "local-" + secrets.token_urlsafe(24)
    s = load_settings()
    s["api_key"] = cfg.api_key
    save_settings(s)


CONFIG = Config()
