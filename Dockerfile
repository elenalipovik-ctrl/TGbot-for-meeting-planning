FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && groupadd --gid 10001 meetingbot \
    && useradd --uid 10001 --gid meetingbot --no-create-home --shell /usr/sbin/nologin meetingbot
COPY scripts/check_server_readiness.py scripts/run_server.py ./scripts/

USER 10001:10001
CMD ["python", "scripts/run_server.py", "bot"]
