# Use Python 3.12 as specified in pyproject.toml
FROM python:3.12.10-bullseye

# CRITICAL: Build argument to bust cache on every build
ARG BUILD_DATE
ARG BUILD_ID
ENV BUILD_DATE=${BUILD_DATE}
ENV BUILD_ID=${BUILD_ID}

# Set working directory
WORKDIR /app

# Install system dependencies and Microsoft repository keys
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    gnupg2 \
    git \
    unixodbc \
    unixodbc-dev \
    # Playwright/Chromium dependencies
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
    && rm -rf /var/lib/apt/lists/*

# Add Microsoft repository and keys
RUN curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \
    && curl https://packages.microsoft.com/config/debian/11/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update

# Install ODBC Driver and tools with EULA acceptance
RUN ACCEPT_EULA=Y apt-get install -y msodbcsql17 mssql-tools
RUN echo 'export PATH="$PATH:/opt/mssql-tools/bin"' >> ~/.bashrc

# Copy only requirements.txt first (for better caching)
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir --force-reinstall -r requirements.txt

# Install Playwright browsers (Chromium only for headless automation)
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy the rest of the application code
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1
# Prevent Python from writing .pyc files (prevents bytecode cache issues)
ENV PYTHONDONTWRITEBYTECODE=1

# Azure Web Apps use PORT environment variable
ENV PORT=8000

# Expose the port (Azure will override this dynamically)
EXPOSE 8000

# Health check - uses /health endpoint for Docker health monitoring
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
CMD curl -f http://localhost:8000/health || exit 1

# Run application with Gunicorn
# Increased timeout to 900 seconds (15 minutes) to handle large data operations
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--timeout", "900", "--workers", "4", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
