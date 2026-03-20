FROM python:3.12-slim

# ---- system deps ----
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

# ---- non-root user ----
RUN useradd --create-home --shell /bin/bash botuser

WORKDIR /app

# ---- python deps ----
COPY pyproject.toml README.md ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir poetry \
    && poetry config virtualenvs.create false \
    && poetry install --no-root --no-interaction --no-ansi

# ---- project source ----
COPY bot/ ./bot/

# Session directory (Pyrogram stores session file here)
RUN mkdir -p /app/sessions

ENTRYPOINT ["python", "-m", "bot.main"]
