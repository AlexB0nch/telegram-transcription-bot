"""
assemblyai_client.py – Async client for AssemblyAI transcription + speaker diarization.

Flow:
  1. Upload local audio file → get upload_url
  2. Submit transcription job with speaker_labels=True, language_code=ru
  3. Poll until status == "completed" or "error"
  4. Return TranscriptionResult with utterances (speaker + text) or plain text fallback
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from bot.config import Config

logger = logging.getLogger(__name__)

_BASE = "https://api.assemblyai.com/v2"
_POLL_INTERVAL = 5   # seconds between status checks
_POLL_TIMEOUT  = 900 # max seconds to wait (15 min)


@dataclass
class Utterance:
    """A single speaker turn."""
    speaker: str   # "A", "B", "C", …
    text: str
    start_ms: int = 0
    end_ms: int   = 0


@dataclass
class TranscriptionResult:
    """Result returned by AssemblyAI.

    Attributes:
        text:       Full plain-text transcript (always present).
        utterances: Per-speaker segments when diarization was requested.
        raw:        Full API response dict for future use.
    """
    text: str
    utterances: list[Utterance] = field(default_factory=list)
    raw: dict[str, Any] | None  = field(default=None)


class AssemblyAIClient:
    """Async AssemblyAI client — upload, transcribe, poll, return."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._headers = {
            "authorization": config.ASSEMBLYAI_API_KEY,
            "content-type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speakers_expected: int | None = None,
    ) -> TranscriptionResult:
        """Upload *audio_path*, submit a transcription job, wait, return result.

        Args:
            audio_path:        Local path to the (compressed) audio file.
            language:          Language code override (e.g. 'ru', 'en').
                               Falls back to config.ASSEMBLYAI_LANGUAGE.
            speakers_expected: Number of speakers override.
                               Falls back to config.SPEAKERS_EXPECTED.

        Returns:
            :class:`TranscriptionResult` with ``text`` and ``utterances``.

        Raises:
            RuntimeError: If AssemblyAI returns an error status.
            httpx.HTTPStatusError / httpx.RequestError: On HTTP failures.
        """
        timeout = httpx.Timeout(self._config.ASSEMBLYAI_TIMEOUT)
        lang = language or self._config.ASSEMBLYAI_LANGUAGE
        spk  = speakers_expected if speakers_expected is not None else (
            self._config.SPEAKERS_EXPECTED or None
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            upload_url = await self._upload(client, audio_path)
            logger.info("Uploaded to AssemblyAI: %s", upload_url)

            job_id = await self._submit(client, upload_url, lang, spk)
            logger.info("AssemblyAI job submitted: %s", job_id)

            raw = await self._poll(client, job_id)

        return self._parse(raw)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _upload(self, client: httpx.AsyncClient, path: Path) -> str:
        """Upload binary audio and return the upload_url."""
        upload_headers = {
            "authorization": self._config.ASSEMBLYAI_API_KEY,
        }
        # Read file into memory synchronously before entering the async context
        data = path.read_bytes()
        resp = await client.post(
            f"{_BASE}/upload",
            content=data,
            headers=upload_headers,
        )
        resp.raise_for_status()
        return resp.json()["upload_url"]

    async def _submit(
        self,
        client: httpx.AsyncClient,
        upload_url: str,
        language: str,
        speakers_expected: int | None,
    ) -> str:
        """Submit transcription job and return the transcript id."""
        payload: dict[str, Any] = {
            "audio_url": upload_url,
            "speech_models": ["universal-2"],
            "language_code": language,
            "speaker_labels": self._config.DIARIZE,
        }
        if self._config.DIARIZE and speakers_expected:
            payload["speakers_expected"] = speakers_expected

        resp = await client.post(
            f"{_BASE}/transcript",
            json=payload,
            headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()["id"]

    async def _poll(self, client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
        """Poll until job is completed or error; return full response dict."""
        url = f"{_BASE}/transcript/{job_id}"
        elapsed = 0

        while elapsed < _POLL_TIMEOUT:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            resp = await client.get(url, headers=self._headers)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            status = data.get("status")

            logger.info("AssemblyAI job %s status: %s (elapsed %ds)", job_id, status, elapsed)

            if status == "completed":
                return data
            if status == "error":
                error_msg = data.get("error", "unknown error")
                raise RuntimeError(f"AssemblyAI transcription failed: {error_msg}")

        raise RuntimeError(f"AssemblyAI job {job_id} timed out after {_POLL_TIMEOUT}s")

    @staticmethod
    def _parse(raw: dict[str, Any]) -> TranscriptionResult:
        """Convert raw API response into :class:`TranscriptionResult`."""
        text = raw.get("text") or ""
        utterances: list[Utterance] = []

        for u in raw.get("utterances") or []:
            utterances.append(Utterance(
                speaker=u.get("speaker", "?"),
                text=u.get("text", ""),
                start_ms=u.get("start", 0),
                end_ms=u.get("end", 0),
            ))

        return TranscriptionResult(text=text, utterances=utterances, raw=raw)
