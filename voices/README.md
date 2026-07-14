# Voices (Chatterbox voice cloning)

Place a reference clip here to give the companion a custom cloned voice:

    voices/wisteria.wav      (or .mp3 / .flac / .ogg / .m4a - all decoded)

**A good reference clip:**
- **6-20 seconds**, single speaker, clean (no music / noise / reverb).
- **Expressive and warm** (not monotone) - the clone inherits its emotional
  base color from this clip. Cleanliness matters more than length.

**Ethics - non-negotiable:** only use **your own voice**, the voice of someone
who has **explicitly consented**, or audio that is licensed / public-domain for
this use. Never clone a real person's voice without their consent.

**Notes:**
- The voice is conditioned once at worker startup (`prepare_conditionals`);
  restart the app after changing the clip.
- Every utterance goes through the cleanup chain (spectral denoise →
  high-pass → trim → edge fades → normalize). Tunables live in
  `backend/config.py` (`tts_denoise_prop`, `tts_highpass_hz`,
  `tts_normalize_peak`) and in-app under **⋮ → Ses ayarları**.
- Reference clips in this folder are **git-ignored by design** - they never
  leave your machine.
