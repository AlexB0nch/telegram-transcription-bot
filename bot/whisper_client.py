"""
whisper_client.py – Async HTTP client for the Whisper-compatible transcription API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from bot.config import Config

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    """Holds the result of a transcription call.

    Attributes:
        text: The plain-text transcription returned by the API.
        raw:  The full JSON response dict, preserved for future use
              (e.g. adding speaker_labels / diarization when the API supports it).
    """

    text: str
    raw: dict[str, Any] | None = field(default=None)


class WhisperClient:
    """Async client that wraps the OpenAI-compatible Whisper REST API."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._headers = {"Authorization": f"Bearer {config.WHISPER_API_KEY}"}

    async def transcribe(
        self,
        audio_path: Path,
        *,
        prompt: str | None = None,
        temperature: float | None = None,
    ) -> TranscriptionResult:
        """Send *audio_path* to the Whisper API and return the transcription.

        Args:
            audio_path:  Local path to the audio file (mp3 preferred).
            prompt:      Optional Whisper prompt to guide transcription style.
            temperature: Optional sampling temperature (0.0–1.0).

        Returns:
            A :class:`TranscriptionResult` with ``text`` and the full ``raw`` response.

        Raises:
            httpx.HTTPStatusError: If the API returns a non-2xx status.
            httpx.RequestError:    On network / connection failures.
        """
        timeout = httpx.Timeout(self._config.WHISPER_TIMEOUT)

        async with httpx.AsyncClient(headers=self._headers, timeout=timeout) as client:
            with audio_path.open("rb") as fh:
                files: dict[str, Any] = {
                    "file": (audio_path.name, fh, _mime_for(audio_path)),
                    "model": (None, self._config.WHISPER_MODEL),
                    "language": (None, self._config.WHISPER_LANGUAGE),
                }
                if prompt is not None:
                    files["prompt"] = (None, prompt)
                if temperature is not None:
                    files["temperature"] = (None, str(temperature))

                logger.info(
                    "Sending %s (%d bytes) to Whisper API at %s",
                    audio_path.name,
                    audio_path.stat().st_size,
                    self._config.WHISPER_API_URL,
                )

                response = await client.post(
                    self._config.WHISPER_API_URL,
                    files=files,
                )

        if response.status_code != 200:
            logger.error(
                "Whisper API returned HTTP %d: %s",
                response.status_code,
                response.text[:500],
            )
            response.raise_for_status()

        raw: dict[str, Any] = response.json()
        text: str = raw.get("text", "")
        logger.info("Transcription received (%d chars).", len(text))
        return TranscriptionResult(text=text, raw=raw)


def _mime_for(path: Path) -> str:
    """Return a reasonable MIME type for common audio extensions."""
    ext = path.suffix.lower()
    mapping = {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
    }
    return mapping.get(ext, "application/octet-stream")
