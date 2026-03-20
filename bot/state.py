"""
state.py – In-memory conversation state between messages.

Stores pending audio info while the bot waits for user to choose
language and number of speakers via inline buttons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingJob:
    """Everything we need to remember about an audio file before transcribing."""
    file_id: str
    file_size: int | None
    default_name: str
    media: Any                        # Pyrogram media object (Voice/Audio/Document)
    language: str | None = None       # set after user picks
    speakers: int | None = None       # set after user picks (None = auto)
    # which step we're waiting for: "language" | "speakers" | "custom_language"
    step: str = "language"


# chat_id → PendingJob
_store: dict[int, PendingJob] = {}


def save(chat_id: int, job: PendingJob) -> None:
    _store[chat_id] = job


def get(chat_id: int) -> PendingJob | None:
    return _store.get(chat_id)


def delete(chat_id: int) -> None:
    _store.pop(chat_id, None)
