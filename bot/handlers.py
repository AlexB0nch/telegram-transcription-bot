"""
handlers.py – Pyrogram handlers with access control, video/Zoom extraction,
and interactive language/speakers selection.

Flow:
  1. Check user access (admin & whitelisted accounts only)
  2. User sends audio / voice / video (.mp4, Zoom recording, etc.) → bot asks language
  3. User picks language (or types custom) → bot asks number of speakers
  4. User picks speakers → bot extracts audio (if video), transcribes, returns .md file
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
    User,
)

from bot import state
from bot.access import AccessManager, UserEntry
from bot.assemblyai_client import AssemblyAIClient, TranscriptionResult
from bot.config import Config
from bot.state import PendingJob
from bot.utils import (
    SUPPORTED_MEDIA_EXTENSIONS,
    convert_to_mp3,
    file_size_mb,
    is_video_file,
    needs_conversion,
    temp_audio_dir,
)

logger = logging.getLogger(__name__)

_config: Config | None = None
_aai: AssemblyAIClient | None = None
_access: AccessManager | None = None

# Language display names → AssemblyAI language codes
LANGUAGES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "es": "🇪🇸 Español",
    "de": "🇩🇪 Deutsch",
    "fr": "🇫🇷 Français",
}


def init(
    config: Config,
    aai_client: AssemblyAIClient,
    access_manager: AccessManager,
) -> None:
    global _config, _aai, _access
    _config = config
    _aai = aai_client
    _access = access_manager


def _user_full_name(user: User | None) -> str:
    if not user:
        return ""
    parts = [user.first_name or "", user.last_name or ""]
    return " ".join(p for p in parts if p).strip()


def _format_user_entry(u: UserEntry) -> str:
    role = "👑 Админ" if u.is_admin else "✅ Пользователь"
    uname = f"@{u.username}" if u.username else "без @username"
    uid = f"ID: {u.user_id}" if u.user_id else "ID: привяжется при входе"
    name = f" ({u.full_name})" if u.full_name else ""
    return f"• {role}: {uname}{name} [{uid}]"


async def _check_access(client: Client, message: Message) -> bool:
    """Verify if message sender is allowed to use the bot.

    - If no admin exists yet, promotes the first user to admin automatically.
    - If unauthorized, replies with a notice and sends an approval button to admins.
    """
    assert _access is not None
    user = message.from_user
    if not user:
        return False

    full_name = _user_full_name(user)

    # Auto-assign the very first user as admin if none configured yet
    if not _access.has_any_admin():
        _access.ensure_first_admin(
            user_id=user.id,
            username=user.username,
            full_name=full_name,
        )
        await message.reply_text(
            "👑 Вы автоматически назначены главным администратором бота!\n"
            "Управление доступом других аккаунтов: /users"
        )
        return True

    if _access.is_allowed(user.id, user.username):
        # Update full_name if needed
        _access.add_user(
            user_id=user.id,
            username=user.username,
            full_name=full_name,
        )
        return True

    # Unauthorized user
    await message.reply_text(
        "🔒 Доступ к этому боту ограничен.\n"
        "Запрос на подключение отправлен администратору — как только он "
        "подтвердит ваш аккаунт, бот пришлёт уведомление."
    )

    if _access.mark_notified(user.id):
        uname_str = f"@{user.username}" if user.username else "нет"
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Разрешить доступ",
                    callback_data=f"acl_allow:{user.id}:{user.username or ''}",
                ),
                InlineKeyboardButton(
                    "❌ Отклонить",
                    callback_data=f"acl_deny:{user.id}",
                ),
            ]
        ])
        for admin in _access.get_admins():
            if admin.user_id:
                try:
                    await client.send_message(
                        chat_id=admin.user_id,
                        text=(
                            "🔔 Запрос на подключение к боту:\n\n"
                            f"👤 Имя: {full_name or '—'}\n"
                            f"🔗 Юзернейм: {uname_str}\n"
                            f"🆔 ID: {user.id}\n\n"
                            "Дать этому аккаунту доступ к боту?"
                        ),
                        reply_markup=kb,
                    )
                except Exception:
                    logger.warning("Could not notify admin %s", admin.user_id)

    return False


def _users_panel_text_and_kb() -> tuple[str, InlineKeyboardMarkup | None]:
    assert _access is not None
    users = _access.get_all_users()
    lines = [
        "👥 Управление доступом к боту\n",
        "Подключённые аккаунты:",
    ]
    for u in users:
        lines.append(_format_user_entry(u))

    lines.extend([
        "",
        "Как подключить ещё один аккаунт (например, жены):",
        "1️⃣ Попроси её нажать /start в этом боте — тебе сюда придёт кнопка «✅ Разрешить доступ».",
        "2️⃣ Или добавь командой: /add @username (или /add ID)",
        "3️⃣ Или просто отправь боту её контакт из телефонной книги.",
    ])

    buttons: list[list[InlineKeyboardButton]] = []
    for u in users:
        if not u.is_admin:
            ident = str(u.user_id) if u.user_id else (u.username or "")
            label = f"@{u.username}" if u.username else (u.full_name or str(u.user_id))
            if ident:
                buttons.append([
                    InlineKeyboardButton(
                        f"🗑 Отключить {label}",
                        callback_data=f"acl_rm:{ident}",
                    )
                ])

    kb = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), kb


def register_handlers(app: Client) -> None:

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    @app.on_message(filters.command("start"))
    async def cmd_start(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        assert _access is not None
        user = message.from_user
        is_adm = bool(user and _access.is_admin(user.id, user.username))
        admin_hint = (
            "\n\n👑 Команды администратора:\n"
            "• /users — список подключённых аккаунтов и управление доступом\n"
            "• /add @username — подключить аккаунт"
            if is_adm
            else ""
        )
        await message.reply_text(
            "Привет! Я бот для расшифровки аудио и видео (включая записи Zoom).\n\n"
            "Отправь мне:\n"
            "• голосовое сообщение или видео-кружочек\n"
            "• видеофайл или запись встречи Zoom (.mp4, .mov, .mkv, .webm)\n"
            "• аудиофайл (.mp3, .m4a, .wav, .ogg, .aac, .flac)\n"
            "• документ с аудио или видео\n\n"
            "Из видео я сам извлеку звуковую дорожку, спрошу язык и количество "
            "спикеров, а затем пришлю готовый .md файл с разбивкой по спикерам."
            f"{admin_hint}"
        )

    @app.on_message(filters.command("help"))
    async def cmd_help(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        assert _config is not None
        assert _access is not None
        user = message.from_user
        is_adm = bool(user and _access.is_admin(user.id, user.username))
        admin_section = (
            "\n\n👑 Управление доступом (для админа):\n"
            "• /users — список пользователей и кнопки удаления\n"
            "• /add @username или /add ID — добавить пользователя\n"
            "• /remove @username или /remove ID — удалить пользователя\n"
            "• Также можно просто отправить контакт человека боту."
            if is_adm
            else ""
        )
        await message.reply_text(
            "Как пользоваться ботом:\n\n"
            "1. Отправь голосовое, аудиофайл или видео (например, .mp4 запись Zoom).\n"
            "2. Выбери язык записи.\n"
            "3. Укажи количество спикеров.\n"
            "4. Получи .md файл с расшифровкой по спикерам.\n\n"
            f"Максимальный размер файла: {_config.MAX_FILE_MB} МБ.\n"
            "Из видеофайлов (.mp4, .mov и др.) автоматически извлекается аудиодорожка."
            f"{admin_section}"
        )

    # ------------------------------------------------------------------ #
    # Admin User Management Commands                                       #
    # ------------------------------------------------------------------ #

    @app.on_message(filters.command(["users", "admin"]))
    async def cmd_users(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        assert _access is not None
        user = message.from_user
        if not user or not _access.is_admin(user.id, user.username):
            await message.reply_text("Эта команда доступна только администратору.")
            return

        text, kb = _users_panel_text_and_kb()
        await message.reply_text(text, reply_markup=kb)

    @app.on_message(filters.command(["add", "allow"]))
    async def cmd_add_user(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        assert _access is not None
        user = message.from_user
        if not user or not _access.is_admin(user.id, user.username):
            await message.reply_text("Эта команда доступна только администратору.")
            return

        if len(message.command) < 2:
            await message.reply_text(
                "Укажи @username или Telegram ID пользователя.\n"
                "Пример: /add @username"
            )
            return

        target_raw = message.command[1].strip()
        target_id: int | None = None
        target_username: str | None = None
        target_name = ""

        # Try resolving via Telegram API first
        try:
            lookup: int | str = (
                int(target_raw) if target_raw.lstrip("-").isdigit() else target_raw
            )
            tg_user = await client.get_users(lookup)
            if isinstance(tg_user, User):
                target_id = tg_user.id
                target_username = tg_user.username
                target_name = _user_full_name(tg_user)
        except Exception:
            if target_raw.lstrip("-").isdigit():
                target_id = int(target_raw)
            else:
                target_username = target_raw.lstrip("@")

        entry = _access.add_user(
            user_id=target_id,
            username=target_username,
            full_name=target_name,
        )
        await message.reply_text(
            f"✅ Доступ открыт:\n{_format_user_entry(entry)}"
        )

        if entry.user_id:
            try:
                await client.send_message(
                    chat_id=entry.user_id,
                    text=(
                        "✅ Администратор открыл вам доступ к боту!\n"
                        "Теперь вы можете отправлять голосовые, аудио и видео "
                        "(например, записи Zoom) для расшифровки."
                    ),
                )
            except Exception:
                pass

    @app.on_message(filters.command(["remove", "revoke"]))
    async def cmd_remove_user(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        assert _access is not None
        user = message.from_user
        if not user or not _access.is_admin(user.id, user.username):
            await message.reply_text("Эта команда доступна только администратору.")
            return

        if len(message.command) < 2:
            await message.reply_text(
                "Укажи @username или ID пользователя для отключения.\n"
                "Пример: /remove @username"
            )
            return

        removed = _access.remove_user(message.command[1].strip())
        if not removed:
            await message.reply_text(
                "Пользователь не найден (или является администратором)."
            )
            return

        await message.reply_text(
            f"🗑 Доступ закрыт для: {_format_user_entry(removed)}"
        )

    @app.on_message(filters.contact)
    async def handle_contact(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        assert _access is not None
        user = message.from_user
        if not user or not _access.is_admin(user.id, user.username):
            return

        contact = message.contact
        if not contact.user_id:
            await message.reply_text(
                "В этом контакте нет Telegram ID. Добавь пользователя через /add @username."
            )
            return

        full_name = " ".join(
            p for p in [contact.first_name or "", contact.last_name or ""] if p
        ).strip()
        entry = _access.add_user(user_id=contact.user_id, full_name=full_name)
        await message.reply_text(
            f"✅ Контакт добавлен в список разрешённых:\n{_format_user_entry(entry)}"
        )

    # ------------------------------------------------------------------ #
    # Audio & Video (MP4 / Zoom) intake                                    #
    # ------------------------------------------------------------------ #

    @app.on_message(filters.voice)
    async def handle_voice(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        await _intake(message, "voice.oga", message.voice.file_size, message.voice)

    @app.on_message(filters.video_note)
    async def handle_video_note(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        await _intake(
            message,
            "video_note.mp4",
            message.video_note.file_size,
            message.video_note,
        )

    @app.on_message(filters.video)
    async def handle_video(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        video = message.video
        default_name = video.file_name or "video.mp4"
        if not Path(default_name).suffix:
            default_name += ".mp4"
        await _intake(message, default_name, video.file_size, video)

    @app.on_message(filters.audio)
    async def handle_audio(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        audio = message.audio
        await _intake(message, audio.file_name or "audio.mp3", audio.file_size, audio)

    @app.on_message(filters.document)
    async def handle_document(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return
        doc = message.document
        filename = doc.file_name or ""
        ext = Path(filename).suffix.lower()
        mime = (doc.mime_type or "").lower()

        # Accept by extension or by audio/* / video/* MIME type (e.g. Zoom recordings)
        if ext not in SUPPORTED_MEDIA_EXTENSIONS:
            if mime.startswith("video/"):
                filename = (filename or "video") + ".mp4"
            elif mime.startswith("audio/"):
                filename = (filename or "audio") + ".mp3"
            else:
                await message.reply_text(
                    "Формат не поддерживается.\n"
                    "Принимаю аудио (.mp3, .m4a, .wav, .ogg, .aac, .flac) "
                    "и видео / записи Zoom (.mp4, .mov, .mkv, .webm, .avi).\n"
                    "/help — подробности."
                )
                return
        await _intake(message, filename, doc.file_size, doc)

    # ------------------------------------------------------------------ #
    # Inline button callbacks (Access control + Transcription)             #
    # ------------------------------------------------------------------ #

    @app.on_callback_query(filters.regex(r"^acl_allow:(-?\d+):(.*)$"))
    async def cb_acl_allow(client: Client, cq: CallbackQuery) -> None:
        assert _access is not None
        caller = cq.from_user
        if not caller or not _access.is_admin(caller.id, caller.username):
            await cq.answer("Только для администратора.", show_alert=True)
            return

        target_id = int(cq.matches[0].group(1))
        target_uname = cq.matches[0].group(2) or None

        entry = _access.add_user(user_id=target_id, username=target_uname)
        await cq.answer("Доступ разрешён!")
        await cq.message.edit_text(
            f"✅ Доступ предоставлен пользователю:\n{_format_user_entry(entry)}"
        )

        try:
            await client.send_message(
                chat_id=target_id,
                text=(
                    "✅ Администратор открыл вам доступ к боту!\n"
                    "Отправьте голосовое сообщение, аудиофайл или видео "
                    "(например, запись встречи Zoom в .mp4) для расшифровки."
                ),
            )
        except Exception:
            logger.warning("Could not send welcome message to %s", target_id)

    @app.on_callback_query(filters.regex(r"^acl_deny:(-?\d+)$"))
    async def cb_acl_deny(client: Client, cq: CallbackQuery) -> None:
        assert _access is not None
        caller = cq.from_user
        if not caller or not _access.is_admin(caller.id, caller.username):
            await cq.answer("Только для администратора.", show_alert=True)
            return

        target_id = int(cq.matches[0].group(1))
        await cq.answer("Запрос отклонён.")
        await cq.message.edit_text(f"❌ Запрос от пользователя ID {target_id} отклонён.")

    @app.on_callback_query(filters.regex(r"^acl_rm:(.+)$"))
    async def cb_acl_remove(client: Client, cq: CallbackQuery) -> None:
        assert _access is not None
        caller = cq.from_user
        if not caller or not _access.is_admin(caller.id, caller.username):
            await cq.answer("Только для администратора.", show_alert=True)
            return

        ident = cq.matches[0].group(1)
        removed = _access.remove_user(ident)
        if removed:
            await cq.answer("Пользователь отключён.")
        else:
            await cq.answer("Пользователь уже удалён.")

        text, kb = _users_panel_text_and_kb()
        await cq.message.edit_text(text, reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^lang:(.+)$"))
    async def cb_language(client: Client, cq: CallbackQuery) -> None:
        assert _access is not None
        caller = cq.from_user
        if not caller or not _access.is_allowed(caller.id, caller.username):
            await cq.answer("Нет доступа.", show_alert=True)
            return

        lang_code = cq.matches[0].group(1)
        chat_id = cq.message.chat.id
        job = state.get(chat_id)
        if not job:
            await cq.answer("Сессия истекла, отправь файл заново.", show_alert=True)
            return

        if lang_code == "other":
            job.step = "custom_language"
            state.save(chat_id, job)
            await cq.message.edit_text(
                "Напиши код языка (например: uk, pl, tr, it, zh):",
            )
            await cq.answer()
            return

        job.language = lang_code
        job.step = "speakers"
        state.save(chat_id, job)
        await cq.message.edit_text(
            f"Язык: {LANGUAGES.get(lang_code, lang_code)}\n\n"
            "Сколько спикеров в записи?",
            reply_markup=_speakers_keyboard(),
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^spk:(.+)$"))
    async def cb_speakers(client: Client, cq: CallbackQuery) -> None:
        assert _access is not None
        caller = cq.from_user
        if not caller or not _access.is_allowed(caller.id, caller.username):
            await cq.answer("Нет доступа.", show_alert=True)
            return

        value = cq.matches[0].group(1)
        chat_id = cq.message.chat.id
        job = state.get(chat_id)
        if not job:
            await cq.answer("Сессия истекла, отправь файл заново.", show_alert=True)
            return

        job.speakers = None if value == "auto" else int(value)
        state.delete(chat_id)
        await cq.answer("Начинаю обработку…")
        await cq.message.edit_text("Принял, скачиваю файл…")
        await _transcribe(client, cq.message, job)

    # ------------------------------------------------------------------ #
    # Custom language text input                                           #
    # ------------------------------------------------------------------ #

    @app.on_message(
        filters.text
        & ~filters.command(["start", "help", "users", "admin", "add", "allow", "remove", "revoke"])
    )
    async def handle_text(client: Client, message: Message) -> None:
        if not await _check_access(client, message):
            return

        chat_id = message.chat.id
        job = state.get(chat_id)

        if job and job.step == "custom_language":
            lang_code = message.text.strip().lower()[:10]
            job.language = lang_code
            job.step = "speakers"
            state.save(chat_id, job)
            await message.reply_text(
                f"Язык: {lang_code}\n\nСколько спикеров в записи?",
                reply_markup=_speakers_keyboard(),
            )
            return

        await message.reply_text(
            "Отправь мне голосовое сообщение, аудиофайл или видео "
            "(например, .mp4 запись встречи Zoom).\n"
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

    is_vid = is_video_file(Path(default_name))
    prefix = "Видео получено (извлеку из него звук)." if is_vid else "Аудио получено."
    await message.reply_text(
        f"{prefix} Выбери язык записи:",
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
            InlineKeyboardButton("4+ / Авто",      callback_data="spk:auto"),
        ],
    ])


async def _transcribe(client: Client, status_msg: Message, job: PendingJob) -> None:
    """Download → extract/compress audio → transcribe → send .md"""
    assert _config is not None
    assert _aai is not None

    async with temp_audio_dir() as tmp_dir:
        try:
            # Download via MTProto
            download_path = tmp_dir / job.default_name
            logger.info("Downloading %s", job.default_name)
            await client.download_media(job.media, file_name=str(download_path))
            logger.info("Downloaded %.1f MB", download_path.stat().st_size / 1024 / 1024)

            # Extract audio from video (.mp4 / Zoom) or compress large/opus audio
            if needs_conversion(download_path):
                if is_video_file(download_path):
                    await status_msg.edit_text("Видео скачано, извлекаю звуковую дорожку…")
                else:
                    await status_msg.edit_text("Файл скачан, подготавливаю аудио…")

                mp3_path = tmp_dir / (download_path.stem + "_c.mp3")
                logger.info("Converting/extracting audio → %s", mp3_path.name)
                audio_path = await convert_to_mp3(download_path, mp3_path)
                logger.info("Prepared audio: %.1f MB", audio_path.stat().st_size / 1024 / 1024)
            else:
                audio_path = download_path

            await status_msg.edit_text("Отправляю аудио на распознавание…")

            # Transcribe
            result: TranscriptionResult = await _aai.transcribe(
                audio_path,
                language=job.language or _config.ASSEMBLYAI_LANGUAGE,
                speakers_expected=job.speakers,
            )

            if not result.text.strip():
                await status_msg.edit_text(
                    "Расшифровка пустая — возможно, в записи нет речи."
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
            await status_msg.edit_text(f"Ошибка: {exc}")
        except Exception:
            logger.exception("Unexpected error")
            await status_msg.edit_text(
                "Не получилось обработать запись, попробуй ещё раз."
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
