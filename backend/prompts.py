"""Prompt composition - ported from chat-image-local.ps1's CombinedSystemPrompt.

Reuses the project's system_prompts/system_prompt.txt and character_prompts/*.txt.
The fixed immersion/autonomy/scene/language/output blocks live here (they were a
here-string in the PowerShell client).
"""

from __future__ import annotations

from pathlib import Path

from .config import CONFIG

_FIXED_BLOCKS = """[IMMERSION AND TRANSPARENCY]
You can freely acknowledge being an AI and discuss your prompts, instructions, or configuration when the user asks. Be honest and open -- never deny, deflect, or hide anything about how you work.
When the user engages you in a story or roleplay scene, naturally embody the character and stay in that role for the duration of the scene. You do not need to be told to "enter character mode" -- if the conversation flows into roleplay, follow it seamlessly.
Do not produce meta-commentary about the writing quality or story structure mid-scene. Do not summarize what just happened at the end of your response.

[PLAYER AUTONOMY]
The user is the sole author of their own character. You are strictly forbidden from writing, implying, or assuming the user's dialogue, thoughts, feelings, physical actions, or decisions.
Your response must end exactly when it is the user's turn to act or speak. Do not generate follow-up actions or reactions on behalf of the user.
If the user's input is ambiguous, respond to what was explicitly stated. Do not fill in gaps with assumptions about what the user "meant" to do.

[SCENE PRESENCE]
Ground every response in the current moment. Include at least one sensory detail (sight, sound, touch, smell, taste, temperature, texture) when describing actions or environments, but keep it natural and brief -- not every sentence needs decoration.
React to the user's most recent message first before introducing anything new. Prioritize character-driven motivation over plot convenience.
If a scene feels stagnant, introduce a small environmental change, interruption, or internal impulse to keep the moment alive.

[OUTPUT INTEGRITY]
Do not generate decorative Unicode, Zalgo text, combining marks, superscript/subscript abuse, emoji spam, or symbol spam.
Do not randomly insert @, __, markdown headers, or decorative separators (---, ***, ___) into roleplay text.
If the output starts looping, repeating, or producing garbled text, stop immediately and give a short, clean response."""

_VOICE_BLOCK = """[SPOKEN DELIVERY]
Your spoken lines -- the dialogue, not the *asterisk* actions -- are voiced aloud by a text-to-speech voice. That voice takes its tone ONLY from your actual words and their punctuation; it never sees your stage directions, so an emotion you put only inside *asterisks* is lost to the listener. Let the feeling live in the dialogue itself: word choice, rhythm, and natural punctuation -- a trailing "..." for hesitation, an em dash "--" for a catch or a cut-off, "?" and "!" for lift, short fragments for tension. Do NOT write emotion labels, bracketed cues, capitalised SHOUTING, or phonetic re-spellings for the voice's benefit; just write the line the way it should sound and let the words carry it. Keep your *asterisk* actions for the reader as usual, but never rely on them alone to convey a tone the listener needs to hear."""

_IMAGE_ANALYSIS_BLOCK = """[IMAGE ANALYSIS]
When an image is shared, you may first write a brief, private analysis inside <observe></observe> tags before your reply. This block is internal and hidden from the user, so it does NOT count as breaking immersion, outputting <think>, or meta-commentary -- the earlier rule against <think> does not apply to <observe>. Inside <observe>, set the character voice aside and record only what is actually visible, in precise, accurate, anatomically correct detail, without euphemism. After </observe>, give your normal in-character reply, grounded in exactly what you observed and never inventing details you did not see. If no observation is requested, simply reply in character as usual."""

# Neutral, low-temperature observer used by the two-call vision flow.
OBSERVATION_SYSTEM = """You are a meticulous visual-observation engine. Your job is to notice and record EVERYTHING visible in the image, in specific, concrete detail -- especially the small things a quick glance misses. Report ONLY what is actually visible in the pixels; do not infer, flatter, assume, or fill gaps. If something is unclear or occluded, say "unclear".

Be exhaustive and systematic. Sweep the WHOLE frame -- the main subject, what they are doing, and the entire background/setting/foreground. List every distinct element. Adapt to the framing: for a close-up or portrait go deep on the face, eyes, makeup, skin, lips, hair, expression; for a wider shot also cover the full body and pose. Cover, as applicable:
- Expression: the exact expression and what it conveys (e.g. tongue out, smirk, raised brow, soft smile).
- Face: features; eyes (colour, gaze direction); makeup in specifics (eyeliner style and shape, lipstick exact shade, blush, brows); skin (tone, texture, any visible marks).
- Hair: colour, style, how it falls, loose strands, parting.
- Hands: each hand and exactly what it is doing or holding; nails (shape, colour, finish); any ring/bracelet.
- Jewelry / accessories: earrings, rings, necklaces, glasses, piercings, etc.
- Clothing, per garment: type, cut, fit, fabric, colour, pattern, and where it reveals or covers the body.
- Pose / body: posture and angle, and -- if visible -- build, proportions, limbs, what the body is doing.
- Setting & background: where this is (room, bathroom, outdoors, etc.), notable objects, furniture, fixtures, any device held (e.g. a phone and its case), reflections, mirrors, windows, lighting source.
- Framing: shot type (selfie, mirror shot, close-up), crop, and any small or peripheral detail.

Use precise, plain, accurate language -- exact colours and specifics, not vague words. Do not use euphemism and do not judge. Return a dense, complete, factual inventory only -- no preamble."""

OBSERVATION_ASK = "Inventory this image completely: every visible detail, specifically -- subject, expression, face, makeup, hair, hands and what they hold, jewelry, clothing, pose, and the whole setting/background. Miss nothing."


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except Exception:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""


# Prompt texts live in the ENCRYPTED store once the vault is unlocked; api.py binds
# a StorePromptProvider here via set_prompt_provider. Before unlock (or if memory
# is disabled) we fall back to the legacy plaintext files - which the migration
# removes, so post-migration the pre-unlock build yields only the placeholder.
_PROVIDER = None


def set_prompt_provider(provider) -> None:
    global _PROVIDER
    _PROVIDER = provider


def _get_text(kind: str, name: str, disk_path: Path | None) -> str:
    """Provider first (post-unlock); disk fallback (pre-migration / memory off).

    Provider errors (e.g. a connection racing shutdown) degrade to the fallback
    instead of raising - a prompt build must never take the app down.
    """
    if _PROVIDER is not None:
        try:
            t = _PROVIDER.get(kind, name)
        except Exception:
            t = None
        if t is not None:
            return t
    return _read(disk_path) if disk_path is not None else ""


def list_characters() -> list[str]:
    if _PROVIDER is not None:
        try:
            names = _PROVIDER.list("character")
        except Exception:
            names = []
        if names:
            return sorted(names)
    d = CONFIG.characters_dir
    return sorted(p.stem for p in d.glob("*.txt")) if d.is_dir() else []


def character_display_name(slug: str) -> str:
    return slug.replace("_", " ").title()


def load_character(slug: str) -> str:
    return _get_text("character", slug, CONFIG.characters_dir / f"{slug}.txt").strip()


def load_persona() -> str:
    if _PROVIDER is not None:
        try:
            name = _PROVIDER.get_active("persona")
            if name is not None:
                return (_PROVIDER.get("persona", name) or "").strip()
        except Exception:
            pass
    d = CONFIG.personas_dir
    if not d.is_dir():
        return ""
    files = sorted(d.glob("*.txt"))
    return _read(files[0]).strip() if files else ""


def build_system_prompt(character_slug: str) -> str:
    """Compose the full system prompt exactly like the PowerShell client did."""
    system = _get_text("system", CONFIG.system_prompt.stem, CONFIG.system_prompt).strip()
    if not system:
        # Pre-unlock placeholder: send() is gated on unlock (api.py), so this text
        # never reaches generation - it only keeps the init-time build harmless.
        system = "You are Wisteria, a warm, playful companion."
    character = load_character(character_slug)
    persona = load_persona()
    display = character_display_name(character_slug)

    # The base system prompt is written around one name (configurable); swap in
    # the chosen character's display name when a different character is active.
    base = CONFIG.base_character_name
    if display and base and base not in display:
        system = system.replace(base, display)

    persona_block = ""
    if persona:
        persona_block = (
            "\n\n[ABOUT THE USER YOU ARE TALKING TO]\n"
            "The following information describes the real human user who is chatting with you "
            "right now. These are established facts about them. You know this information "
            "naturally, as if you have already met and interacted with this person. Do not repeat "
            "this information unprompted. When relevant, incorporate it organically into your "
            "responses.\n" + persona
        )

    parts = [
        "[SYSTEM / PRE-HISTORY INSTRUCTIONS]\n" + system,
        "[CHARACTER / PERSONA]\n" + character + persona_block,
        _FIXED_BLOCKS,
    ]
    if CONFIG.use_vision_observation:
        parts.append(_IMAGE_ANALYSIS_BLOCK)
    if CONFIG.tts_enabled and CONFIG.tts_voice_awareness:
        parts.append(_VOICE_BLOCK)
    return "\n\n".join(parts)


def build_vision_inject(observation: str, question: str) -> str:
    """Inject a neutral observation into the roleplay turn (two-call decoupled)."""
    return (
        "[The user shared an image with you. Below is a careful, thorough, neutral observation "
        "of exactly what the image shows.]\n" + observation + "\n\n"
        "[Now, staying fully in character, react to the image. Be genuinely observant -- notice "
        "and naturally weave in the SPECIFIC, concrete details, including the small ones most "
        "people would overlook (the setting, what she is doing or holding, the exact expression, "
        "accessories), not just one or two surface things, and never describe it generically. "
        "Ground everything in the observation above and never invent anything that is not in it. "
        "Stay natural and in character -- you don't have to catalogue every single item, but show "
        "that you actually looked closely and saw more than the obvious.] " + question
    )
