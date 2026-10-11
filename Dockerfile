FROM node:24-bookworm-slim AS frontend
WORKDIR /build
ENV NODE_OPTIONS=--max-old-space-size=256
RUN npm install --global pnpm@11.25.0
COPY mini_app/package.json mini_app/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY mini_app/index.html mini_app/tsconfig.json mini_app/vite.config.ts ./
COPY mini_app/src ./src
COPY mini_app/public ./public
RUN pnpm build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[api]' \
    && groupadd --gid 10001 meetingbot \
    && useradd --uid 10001 --gid meetingbot --no-create-home --shell /usr/sbin/nologin meetingbot
COPY scripts/check_server_readiness.py scripts/run_server.py ./scripts/
COPY --from=frontend /build/dist ./mini_app/dist
USER 10001:10001
CMD ["python", "scripts/run_server.py", "bot"]
