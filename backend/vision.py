"""Two-call decoupled vision: a low-temp neutral observation, then the in-character
reply is grounded on it (image not resent to the roleplay turn)."""

from __future__ import annotations

from .config import CONFIG
from .llm import LlamaClient
from .prompts import OBSERVATION_ASK, OBSERVATION_SYSTEM


def observe_image(client: LlamaClient, image_data_uri: str) -> str:
    """Return an accurate, neutral description of the image (low temperature)."""
    messages = [
        {"role": "system", "content": OBSERVATION_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_data_uri}},
                {"type": "text", "text": OBSERVATION_ASK},
            ],
        },
    ]
    return client.complete(messages, CONFIG.vision_obs_preset)
