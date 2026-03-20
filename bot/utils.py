"""
utils.py – Temp-file helpers and audio conversion utilities.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

# Audio extensions that the bot accepts from document messages
SUPPORTED_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".wav", ".ogg", ".oga"}
)


@asynccontextmanager
async def temp_audio_dir() -> AsyncGenerator[Path, None]:
    """Async context manager that creates a temporary directory and removes it on exit.

    Yields:
        Path to the newly created temp directory.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="whisper_bot_"))
    logger.debug("Created temp dir: %s", tmp_dir)
    try:
        yield tmp_dir
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.debug("Removed temp dir: %s", tmp_dir)


async def convert_to_mp3(source: Path, dest: Path) -> Path:
    """Convert *source* audio file to mp3 at *dest* using ffmpeg.

    This is done in a thread executor so it does not block the event loop.

    Args:
        source: Path to the original audio file.
        dest:   Destination path for the output mp3 file (must end with .mp3).

    Returns:
        *dest* on success.

    Raises:
        RuntimeError: If ffmpeg exits with a non-zero code.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _ffmpeg_convert, source, dest)
    return dest


def _ffmpeg_convert(source: Path, dest: Path) -> None:
    """Blocking ffmpeg conversion – run inside an executor."""
    import subprocess  # noqa: PLC0415  (lazy import, only used here)

    cmd = [
        "ffmpeg",
        "-y",                # overwrite output without asking
        "-i", str(source),
        "-ar", "16000",      # 16 kHz sample rate (ideal for Whisper)
        "-ac", "1",          # mono
        "-b:a", "32k",       # 32 kbps is enough for speech; keeps file well under 25 MB
        str(dest),
    ]
    logger.debug("ffmpeg command: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        logger.error("ffmpeg stderr: %s", result.stderr[-1000:])
        raise RuntimeError(
            f"ffmpeg exited with code {result.returncode}: {result.stderr[-200:]}"
        )


# OpenAI Whisper API hard limit: 25 MB
WHISPER_API_MAX_BYTES: int = 25 * 1024 * 1024


def needs_conversion(path: Path) -> bool:
    """Return True if the file must be converted/compressed before sending to Whisper.

    We always convert if:
    - Extension is .oga/.ogg (opus codec, some API endpoints reject it)
    - File size exceeds the Whisper API 25 MB limit

    Args:
        path: Path to the audio file.

    Returns:
        True if conversion/compression is needed.
    """
    if path.suffix.lower() in {".oga", ".ogg"}:
        return True
    if path.stat().st_size > WHISPER_API_MAX_BYTES:
        return True
    return False


def file_size_mb(size_bytes: int) -> float:
    """Convert bytes to megabytes."""
    return size_bytes / (1024 * 1024)
