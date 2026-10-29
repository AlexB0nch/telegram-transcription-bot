"""
config.py – Load and validate configuration from environment variables.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.error("Required environment variable %s is not set. Exiting.", name)
        sys.exit(1)
    return value


def _optional(name: str, default: str) -> str:
    return os.getenv(name, default)


def _default_session_dir() -> str:
    if Path("/app/sessions").exists():
        return "/app/sessions"
    return "sessions"


class Config:
    """Application-wide configuration loaded from environment variables."""

    # --- Telegram ---
    TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
    TELEGRAM_API_ID: int    = int(_require("TELEGRAM_API_ID"))
    TELEGRAM_API_HASH: str  = _require("TELEGRAM_API_HASH")

    # --- Access / Admin ---
    # Comma-separated list of admin IDs or @usernames (optional: if empty, first user becomes admin)
    ADMIN_IDS: list[str] = [
        x.strip()
        for x in (_optional("ADMIN_IDS", "") or _optional("ADMIN_ID", "")).split(",")
        if x.strip()
    ]
    SESSION_DIR: str = _optional("SESSION_DIR", _default_session_dir())

    # --- AssemblyAI ---
    ASSEMBLYAI_API_KEY: str  = _require("ASSEMBLYAI_API_KEY")
    ASSEMBLYAI_LANGUAGE: str = _optional("ASSEMBLYAI_LANGUAGE", "ru")
    ASSEMBLYAI_TIMEOUT: int  = int(_optional("ASSEMBLYAI_TIMEOUT", "900"))

    # --- Diarization ---
    DIARIZE: bool             = _optional("DIARIZE", "true").lower() == "true"
    SPEAKERS_EXPECTED: int    = int(_optional("SPEAKERS_EXPECTED", "0"))  # 0 = auto

    # --- File limits ---
    MAX_FILE_MB: int = int(_optional("MAX_FILE_MB", "500"))
