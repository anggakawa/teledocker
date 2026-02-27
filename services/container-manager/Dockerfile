FROM python:3.12-slim

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY pyproject.toml /workspace/pyproject.toml
COPY uv.lock /workspace/uv.lock
COPY packages/shared /workspace/packages/shared
COPY services/container-manager /workspace/services/container-manager

WORKDIR /workspace/services/container-manager

RUN uv sync --frozen

EXPOSE 8001

CMD ["uv", "run", "uvicorn", "container_manager.main:app", "--host", "0.0.0.0", "--port", "8001"]
