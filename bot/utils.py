"""
utils.py – Temp-file helpers and audio/video conversion utilities.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

# Audio extensions that the bot accepts
SUPPORTED_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".aac", ".flac", ".opus", ".wma"}
)

# Video extensions (Zoom recordings, MP4 videos, etc.) from which audio is extracted
SUPPORTED_VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
)

# Combined set of supported document extensions
SUPPORTED_MEDIA_EXTENSIONS: frozenset[str] = (
    SUPPORTED_AUDIO_EXTENSIONS | SUPPORTED_VIDEO_EXTENSIONS
)


@asynccontextmanager
async def temp_audio_dir() -> AsyncGenerator[Path, None]:
    """Async context manager that creates a temporary directory and removes it on exit."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="whisper_bot_"))
    logger.debug("Created temp dir: %s", tmp_dir)
    try:
        yield tmp_dir
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.debug("Removed temp dir: %s", tmp_dir)


async def convert_to_mp3(source: Path, dest: Path) -> Path:
    """Convert *source* audio or video file to mp3 at *dest* using ffmpeg.

    Extracts the audio track (stripping any video stream via ``-vn``) and
    encodes to 16 kHz mono 32 kbps MP3 in a background thread executor.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _ffmpeg_convert, source, dest)
    return dest


def _ffmpeg_convert(source: Path, dest: Path) -> None:
    """Blocking ffmpeg conversion – run inside an executor."""
    import subprocess  # noqa: PLC0415

    cmd = [
        "ffmpeg",
        "-y",                # overwrite output without asking
        "-i", str(source),
        "-vn",               # strip video stream (extract audio only from .mp4 / Zoom)
        "-ar", "16000",      # 16 kHz sample rate (ideal for speech recognition)
        "-ac", "1",          # mono
        "-b:a", "32k",       # 32 kbps (~14 MB per hour of speech)
        str(dest),
    ]
    logger.debug("ffmpeg command: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        logger.error("ffmpeg stderr: %s", result.stderr[-1000:])
        if "Output file does not contain any stream" in result.stderr or "matches no streams" in result.stderr:
            raise RuntimeError("В видеофайле не найдена звуковая дорожка.")
        raise RuntimeError(
            f"ffmpeg exited with code {result.returncode}: {result.stderr[-200:]}"
        )


# Threshold above which audio files are compressed before uploading (25 MB)
WHISPER_API_MAX_BYTES: int = 25 * 1024 * 1024


def is_video_file(path: Path) -> bool:
    """Return True if *path* has a video extension (e.g. .mp4 Zoom recording)."""
    return path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def needs_conversion(path: Path) -> bool:
    """Return True if the file must be converted/extracted to MP3 before uploading.

    Always converts when:
    - File is a video (.mp4, .mov, .mkv, .webm, .avi, .m4v) so we extract audio only
    - Extension is .oga/.ogg/.opus/.wma
    - File size exceeds 25 MB
    """
    ext = path.suffix.lower()
    if ext in SUPPORTED_VIDEO_EXTENSIONS:
        return True
    if ext in {".oga", ".ogg", ".opus", ".wma"}:
        return True
    if path.stat().st_size > WHISPER_API_MAX_BYTES:
        return True
    return False


def file_size_mb(size_bytes: int) -> float:
    """Convert bytes to megabytes."""
    return size_bytes / (1024 * 1024)
