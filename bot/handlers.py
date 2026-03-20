"""
handlers.py – Pyrogram handlers with interactive language/speakers selection.

Flow:
  1. User sends audio → bot asks language via inline buttons
  2. User picks language (or types custom) → bot asks number of speakers
  3. User picks speakers → bot transcribes and returns .md file
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.assemblyai_client import AssemblyAIClient, TranscriptionResult
from bot.config import Config
from bot import state
from bot.state import PendingJob
from bot.utils import (
    SUPPORTED_AUDIO_EXTENSIONS,
    convert_to_mp3,
    file_size_mb,
    needs_conversion,
    temp_audio_dir,
)

logger = logging.getLogger(__name__)

_config: Config | None = None
_aai: AssemblyAIClient | None = None

# Language display names → AssemblyAI language codes
LANGUAGES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "es": "🇪🇸 Español",
    "de": "🇩🇪 Deutsch",
    "fr": "🇫🇷 Français",
}


def init(config: Config, aai_client: AssemblyAIClient) -> None:
    global _config, _aai
    _config = config
    _aai = aai_client


def register_handlers(app: Client) -> None:

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    @app.on_message(filters.command("start"))
    async def cmd_start(client: Client, message: Message) -> None:
        await message.reply_text(
            "Привет! Я бот для расшифровки аудио.\n\n"
            "Отправь мне:\n"
            "• голосовое сообщение\n"
            "• аудиофайл (.mp3, .m4a, .wav, .ogg)\n"
            "• документ с аудио\n\n"
            "Я спрошу язык и количество спикеров, затем пришлю "
            "готовый .md файл с разбивкой по спикерам."
        )

    @app.on_message(filters.command("help"))
    async def cmd_help(client: Client, message: Message) -> None:
        assert _config is not None
        await message.reply_text(
            "Как пользоваться ботом:\n\n"
            "1. Отправь аудиофайл или голосовое сообщение.\n"
            "2. Выбери язык записи.\n"
            "3. Укажи количество спикеров.\n"
            "4. Получи .md файл с расшифровкой.\n\n"
            f"Максимальный размер файла: {_config.MAX_FILE_MB} МБ.\n"
            "Транскрипция через AssemblyAI (русский — высокая точность)."
        )

    # ------------------------------------------------------------------ #
    # Audio intake                                                         #
    # ------------------------------------------------------------------ #

    @app.on_message(filters.voice)
    async def handle_voice(client: Client, message: Message) -> None:
        await _intake(message, "voice.oga", message.voice.file_size, message.voice)

    @app.on_message(filters.audio)
    async def handle_audio(client: Client, message: Message) -> None:
        audio = message.audio
        await _intake(message, audio.file_name or "audio.mp3", audio.file_size, audio)

    @app.on_message(filters.document)
    async def handle_document(client: Client, message: Message) -> None:
        doc = message.document
        filename = doc.file_name or ""
        if Path(filename).suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            await message.reply_text(
                "Формат не поддерживается.\n"
                "Принимаю: .mp3, .m4a, .wav, .ogg, .oga\n"
                "/help — подробности."
            )
            return
        await _intake(message, filename, doc.file_size, doc)

    # ------------------------------------------------------------------ #
    # Inline button callbacks                                              #
    # ------------------------------------------------------------------ #

    @app.on_callback_query(filters.regex(r"^lang:(.+)$"))
    async def cb_language(client: Client, cq: CallbackQuery) -> None:
        lang_code = cq.matches[0].group(1)
        chat_id = cq.message.chat.id
        job = state.get(chat_id)
        if not job:
            await cq.answer("Сессия истекла, отправь аудио заново.", show_alert=True)
            return

        if lang_code == "other":
            job.step = "custom_language"
            state.save(chat_id, job)
            await cq.message.edit_text(
                "Напиши код языка (например: `uk`, `pl`, `tr`, `it`, `zh`):",
                parse_mode="markdown",
            )
            await cq.answer()
            return

        job.language = lang_code
        job.step = "speakers"
        state.save(chat_id, job)
        await cq.message.edit_text(
            f"Язык: *{LANGUAGES.get(lang_code, lang_code)}*\n\n"
            "Сколько спикеров в записи?",
            reply_markup=_speakers_keyboard(),
            parse_mode="markdown",
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^spk:(.+)$"))
    async def cb_speakers(client: Client, cq: CallbackQuery) -> None:
        value = cq.matches[0].group(1)
        chat_id = cq.message.chat.id
        job = state.get(chat_id)
        if not job:
            await cq.answer("Сессия истекла, отправь аудио заново.", show_alert=True)
            return

        job.speakers = None if value == "auto" else int(value)
        state.delete(chat_id)
        await cq.answer("Начинаю расшифровку…")
        await cq.message.edit_text("Принял, расшифровываю…")
        await _transcribe(client, cq.message, job)

    # ------------------------------------------------------------------ #
    # Custom language text input                                           #
    # ------------------------------------------------------------------ #

    @app.on_message(filters.text & ~filters.command(["start", "help"]))
    async def handle_text(client: Client, message: Message) -> None:
        chat_id = message.chat.id
        job = state.get(chat_id)

        if job and job.step == "custom_language":
            lang_code = message.text.strip().lower()[:10]
            job.language = lang_code
            job.step = "speakers"
            state.save(chat_id, job)
            await message.reply_text(
                f"Язык: *{lang_code}*\n\nСколько спикеров в записи?",
                reply_markup=_speakers_keyboard(),
                parse_mode="markdown",
            )
            return

        await message.reply_text(
            "Я понимаю только аудиофайлы и голосовые сообщения.\n"
            "/help — подробности."
        )


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

async def _intake(
    message: Message,
    default_name: str,
    file_size: int | None,
    media,
) -> None:
    """Check size, save state, ask for language."""
    assert _config is not None

    if file_size is not None:
        sz = file_size_mb(file_size)
        if sz > _config.MAX_FILE_MB:
            await message.reply_text(
                f"Файл слишком большой ({sz:.1f} МБ).\n"
                f"Максимум: {_config.MAX_FILE_MB} МБ."
            )
            return

    job = PendingJob(
        file_id=media.file_id,
        file_size=file_size,
        default_name=default_name,
        media=media,
    )
    state.save(message.chat.id, job)

    await message.reply_text(
        "Аудио получено. Выбери язык записи:",
        reply_markup=_language_keyboard(),
    )


def _language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇷🇺 Русский",  callback_data="lang:ru"),
            InlineKeyboardButton("🇬🇧 English",  callback_data="lang:en"),
        ],
        [
            InlineKeyboardButton("🇪🇸 Español",  callback_data="lang:es"),
            InlineKeyboardButton("🇩🇪 Deutsch",  callback_data="lang:de"),
        ],
        [
            InlineKeyboardButton("🇫🇷 Français", callback_data="lang:fr"),
            InlineKeyboardButton("✏️ Другой",    callback_data="lang:other"),
        ],
    ])


def _speakers_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 спикер",       callback_data="spk:1"),
            InlineKeyboardButton("2 спикера",       callback_data="spk:2"),
        ],
        [
            InlineKeyboardButton("3 спикера",       callback_data="spk:3"),
            InlineKeyboardButton("Не указывать",    callback_data="spk:auto"),
        ],
    ])


async def _transcribe(client: Client, status_msg: Message, job: PendingJob) -> None:
    """Download → compress → transcribe → send .md"""
    assert _config is not None
    assert _aai is not None

    async with temp_audio_dir() as tmp_dir:
        try:
            # Download via MTProto
            download_path = tmp_dir / job.default_name
            logger.info("Downloading %s", job.default_name)
            await client.download_media(job.media, file_name=str(download_path))
            logger.info("Downloaded %.1f MB", download_path.stat().st_size / 1024 / 1024)

            # Compress if needed
            if needs_conversion(download_path):
                mp3_path = tmp_dir / (download_path.stem + "_c.mp3")
                logger.info("Compressing → %s", mp3_path.name)
                audio_path = await convert_to_mp3(download_path, mp3_path)
                logger.info("After compression: %.1f MB", audio_path.stat().st_size / 1024 / 1024)
            else:
                audio_path = download_path

            await status_msg.edit_text("Файл загружен, отправляю на распознавание…")

            # Transcribe
            result: TranscriptionResult = await _aai.transcribe(
                audio_path,
                language=job.language or _config.ASSEMBLYAI_LANGUAGE,
                speakers_expected=job.speakers,
            )

            if not result.text.strip():
                await status_msg.edit_text(
                    "Расшифровка пустая — возможно, в файле нет речи."
                )
                return

            # Build MD
            original_name = Path(job.default_name).stem
            md_path = tmp_dir / f"{_safe_filename(original_name)}_transcript.md"
            _write_md(md_path, result, original_name, job.language or _config.ASSEMBLYAI_LANGUAGE)

            await status_msg.edit_text("Готово, отправляю файл…")
            await status_msg.reply_document(
                document=str(md_path),
                caption=(
                    f"📄 {original_name}\n"
                    f"🌐 Язык: {job.language or _config.ASSEMBLYAI_LANGUAGE}\n"
                    f"👥 Спикеров: {_count_speakers(result)}"
                    if result.utterances else
                    f"📄 {original_name}"
                ),
            )
            await status_msg.delete()

        except (httpx.HTTPStatusError, httpx.RequestError):
            logger.exception("AssemblyAI HTTP error")
            await status_msg.edit_text(
                "Ошибка сети при обращении к AssemblyAI. Попробуй ещё раз."
            )
        except RuntimeError as exc:
            logger.exception("Transcription error: %s", exc)
            await status_msg.edit_text(f"Ошибка транскрипции: {exc}")
        except Exception:
            logger.exception("Unexpected error")
            await status_msg.edit_text(
                "Не получилось расшифровать аудио, попробуй ещё раз."
            )


# --------------------------------------------------------------------------- #
# Markdown builder                                                             #
# --------------------------------------------------------------------------- #

def _write_md(
    path: Path,
    result: TranscriptionResult,
    original_name: str,
    language: str,
) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = [
        f"# Расшифровка: {original_name}",
        f"*Дата: {today} | Язык: {language}*",
        "",
        "---",
        "",
    ]

    if result.utterances:
        current_speaker: str | None = None
        buffer: list[str] = []

        def flush() -> None:
            if current_speaker is not None and buffer:
                lines.append(f"## Спикер {current_speaker}")
                lines.append("")
                lines.append(" ".join(buffer))
                lines.append("")

        for utt in result.utterances:
            if utt.speaker != current_speaker:
                flush()
                current_speaker = utt.speaker
                buffer = [utt.text]
            else:
                buffer.append(utt.text)
        flush()
    else:
        lines.append(result.text)
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _count_speakers(result: TranscriptionResult) -> int:
    return len({u.speaker for u in result.utterances})


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:80]
