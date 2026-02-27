FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency management.
RUN pip install uv --no-cache-dir

# Copy workspace root and shared package first so Docker can cache this layer.
COPY pyproject.toml /workspace/pyproject.toml
COPY uv.lock /workspace/uv.lock
COPY packages/shared /workspace/packages/shared
COPY services/api-server /workspace/services/api-server

WORKDIR /workspace/services/api-server

# Install dependencies into a virtual environment inside the service directory.
RUN uv sync --frozen

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "api_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
