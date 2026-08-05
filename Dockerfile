# Power BI Control Center — Azure App Service container
# Base aligned with the known-good Azure "dockerfile", plus current app needs
# (Gunicorn, Playwright Chromium for visual fallback, ODBC, longer timeouts).
FROM python:3.12.10-bullseye

# Bust layer cache when Azure Pipelines rebuilds
ARG BUILD_DATE
ARG BUILD_ID
ENV BUILD_DATE=${BUILD_DATE}
ENV BUILD_ID=${BUILD_ID}

WORKDIR /app

# System packages: build tools, ODBC, curl/health, Chromium runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    gnupg2 \
    git \
    unixodbc \
    unixodbc-dev \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    libx11-xcb1 \
    libxcb1 \
    libxext6 \
    libx11-6 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Microsoft ODBC Driver 17 (SQL tools optional path for scripts)
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \
    && curl -fsSL https://packages.microsoft.com/config/debian/11/prod.list \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 mssql-tools \
    && echo 'export PATH="$PATH:/opt/mssql-tools/bin"' >> /etc/bash.bashrc \
    && rm -rf /var/lib/apt/lists/*

# Python deps first (better layer cache). requirements includes gunicorn + playwright.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Chromium for Playwright visual metadata fallback (OS libs installed above).
# Best-effort only: many DevOps agents block playwright CDN downloads.
# App already degrades gracefully if browsers are missing (Scanner API path).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /ms-playwright \
    && (python -m playwright install chromium \
        && chmod -R a+rX /ms-playwright \
        && echo "Playwright Chromium installed") \
    || echo "WARN: Playwright Chromium install skipped (network/CDN). Core app still runs."

# Application source
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

# Prefer /health; fall back to / if older deployments lack the route
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || curl -fsS http://127.0.0.1:8000/ || exit 1

# 15m timeout for large catalog / lineage work
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--timeout", "900", "--workers", "4", "--access-logfile", "-", "--error-logfile", "-", "app:app"]

