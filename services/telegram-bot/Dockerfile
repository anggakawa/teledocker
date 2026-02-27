FROM python:3.12-slim

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY pyproject.toml /workspace/pyproject.toml
COPY packages/shared /workspace/packages/shared
COPY services/telegram-bot /workspace/services/telegram-bot

WORKDIR /workspace/services/telegram-bot

RUN uv sync --frozen

EXPOSE 8080

CMD ["uv", "run", "python", "-m", "telegram_bot.main"]
