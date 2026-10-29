"""
main.py – Entrypoint for the Telegram transcription bot (Pyrogram + AssemblyAI).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from pyrogram import Client

from bot.access import AccessManager
from bot.assemblyai_client import AssemblyAIClient
from bot.config import Config
from bot.handlers import init, register_handlers

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    config = Config()

    session_dir = Path(config.SESSION_DIR)
    session_dir.mkdir(parents=True, exist_ok=True)

    access = AccessManager(
        storage_path=session_dir / "access.json",
        env_admins=config.ADMIN_IDS,
    )

    logger.info(
        "Bot initialised. language=%s  diarize=%s  max_file_mb=%d  admins=%d",
        config.ASSEMBLYAI_LANGUAGE,
        config.DIARIZE,
        config.MAX_FILE_MB,
        len(access.get_admins()),
    )

    aai = AssemblyAIClient(config)
    init(config, aai, access)

    app = Client(
        name="whisper_bot",
        api_id=config.TELEGRAM_API_ID,
        api_hash=config.TELEGRAM_API_HASH,
        bot_token=config.TELEGRAM_BOT_TOKEN,
        workdir=str(session_dir),
    )

    register_handlers(app)

    logger.info("Starting Pyrogram bot...")
    app.run()


if __name__ == "__main__":
    main()
