FROM python:3.11-slim

# Runs as a normal user rather than root, so the SQLite ledger written to the bind-mounted ./data
# directory belongs to you on the host instead of to root. UID/GID default to 1000, which is the first
# user on a typical Linux desktop; override at build time if yours differ:
#   docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)
ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencies first so editing application code does not reinstall them on every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ holds the database and workspace/ holds generated deliverables; both are bind-mounted at runtime.
RUN groupadd --gid "${APP_GID}" app 2>/dev/null || true \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin app 2>/dev/null || true \
 && mkdir -p /app/data /app/workspace \
 && chown -R "${APP_UID}:${APP_GID}" /app

USER ${APP_UID}:${APP_GID}

EXPOSE 8000

# Matches the compose healthcheck; useful when the image is run without compose.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
