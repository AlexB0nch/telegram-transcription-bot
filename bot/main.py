"""
main.py – Entrypoint for the Telegram transcription bot (Pyrogram + AssemblyAI).
"""

from __future__ import annotations

import logging
import sys

from pyrogram import Client

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

    logger.info(
        "Bot initialised. language=%s  diarize=%s  max_file_mb=%d",
        config.ASSEMBLYAI_LANGUAGE,
        config.DIARIZE,
        config.MAX_FILE_MB,
    )

    aai = AssemblyAIClient(config)
    init(config, aai)

    app = Client(
        name="whisper_bot",
        api_id=config.TELEGRAM_API_ID,
        api_hash=config.TELEGRAM_API_HASH,
        bot_token=config.TELEGRAM_BOT_TOKEN,
        workdir="/app/sessions",
    )

    register_handlers(app)

    logger.info("Starting Pyrogram bot...")
    app.run()


if __name__ == "__main__":
    main()
