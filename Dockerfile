FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd -m engine && chown -R engine /app
USER engine

EXPOSE 8000
# Same image runs both processes; pick with the command:
#   API + MCP: uvicorn api.app:app --host 0.0.0.0 --port 8000
#   Worker:    python -m engine.worker
CMD ["sh", "-c", "uvicorn api.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
