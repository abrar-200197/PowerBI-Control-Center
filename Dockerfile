# Use Python 3.12 as specified in pyproject.toml
FROM python:3.12.10-bullseye

# Set working directory
WORKDIR /app

# Install system dependencies and Microsoft repository keys
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    gnupg2 \
    git \
    unixodbc \
    unixodbc-dev

# Add Microsoft repository and keys
RUN curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \
    && curl https://packages.microsoft.com/config/debian/11/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update

# Install ODBC Driver and tools with EULA acceptance
RUN ACCEPT_EULA=Y apt-get install -y msodbcsql17 mssql-tools
RUN echo 'export PATH="$PATH:/opt/mssql-tools/bin"' >> ~/.bashrc

# Copy only Requirements.txt first (for better caching)
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

# Copy the rest of the application code
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Azure Web Apps use PORT environment variable
ENV PORT=8000

# Expose the port (Azure will override this dynamically)
EXPOSE 8000

# # Command to run Streamlit with dynamic port binding
# CMD streamlit run app.py \
#     --server.port=$PORT \
#     --server.address=0.0.0.0 \
#     --server.headless=true \
#     --server.enableCORS=false \
#     --server.enableXsrfProtection=false


# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
CMD curl -f http://localhost:8000/ || exit 1

# Run application with Gunicorn
# Increased timeout to 900 seconds (15 minutes) to handle large data operations
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--timeout", "900", "--workers", "4", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
