# Telegram Whisper Bot

A production-ready Telegram bot that transcribes Russian audio (voice messages, interviews, phone calls) using any OpenAI-compatible Whisper API endpoint.

---

## Features

- Accepts voice messages, audio attachments, and documents (`.mp3`, `.m4a`, `.wav`, `.ogg`, `.oga`)
- Converts `.oga` / `.ogg` (Telegram voice) to mp3 via **ffmpeg** before sending to the API
- Forces `language=ru` for best Russian transcription quality
- Configurable file-size guard (default 100 MB)
- Supports both **long polling** and **webhook** modes
- All config via environment variables — no cloud dependency

---

## Quick Start

### 1. Create a Telegram bot token

1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts.
3. Copy the token (looks like `123456:ABC-DEF…`).

### 2. Set environment variables

Copy the example env file and fill in your values:

```bash
cp .env.example .env
# edit .env and set TELEGRAM_BOT_TOKEN, WHISPER_API_URL, WHISPER_API_KEY
```

Minimum required variables:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `WHISPER_API_URL` | Full URL, e.g. `https://api.openai.com/v1/audio/transcriptions` |
| `WHISPER_API_KEY` | Bearer API key for the Whisper endpoint |

All optional variables with their defaults are documented in `.env.example`.

---

## Running Locally (without Docker)

**Prerequisites:** Python 3.10+, `ffmpeg` installed and on `$PATH`.

```bash
# Install dependencies (Poetry)
pip install poetry
poetry install --only main

# Run
python -m bot.main
```

---

## Running via Docker Compose

```bash
# Build and start
docker compose up -d --build

# Follow logs
docker compose logs -f

# Stop
docker compose down
```

The `.env` file is automatically mounted into the container.

---

## Switching Between Polling and Webhook

### Long Polling (default, no public URL needed)

```ini
USE_WEBHOOK=false
```

### Webhook (requires a public HTTPS URL)

```ini
USE_WEBHOOK=true
WEBHOOK_URL=https://your.public.domain.com
WEBAPP_HOST=0.0.0.0
WEBAPP_PORT=8080
```

The bot registers the webhook automatically at startup at:
```
https://your.public.domain.com/webhook/<BOT_TOKEN>
```

If you run behind an nginx/Caddy reverse proxy, forward traffic on that path to `WEBAPP_PORT`.

---

## Project Structure

```
telegram-whisper-bot/
├── bot/
│   ├── __init__.py
│   ├── config.py          # env-var configuration
│   ├── whisper_client.py  # async Whisper HTTP client
│   ├── handlers.py        # Telegram update handlers
│   ├── utils.py           # temp files, audio conversion
│   └── main.py            # entrypoint
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .env.example
└── README.md
```

---

## Adding Speaker Diarization Later

`WhisperClient.transcribe()` returns a `TranscriptionResult` dataclass that stores both the plain `text` and the full `raw` JSON response from the API. When your Whisper endpoint starts returning speaker labels, simply read them from `result.raw` in `handlers.py` — no changes needed to the HTTP client itself.

---

## Environment Variable Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Telegram bot token |
| `WHISPER_API_URL` | ✅ | — | Whisper endpoint URL |
| `WHISPER_API_KEY` | ✅ | — | API key (Bearer) |
| `WHISPER_MODEL` | | `whisper-1` | Model name sent to the API |
| `WHISPER_LANGUAGE` | | `ru` | Language code for transcription |
| `MAX_FILE_MB` | | `100` | Max file size in MB |
| `WHISPER_TIMEOUT` | | `600` | HTTP timeout in seconds |
| `USE_WEBHOOK` | | `false` | `true` to enable webhook mode |
| `WEBHOOK_URL` | | — | Public base URL for webhook |
| `WEBAPP_HOST` | | `0.0.0.0` | Bind host for webhook server |
| `WEBAPP_PORT` | | `8080` | Bind port for webhook server |
