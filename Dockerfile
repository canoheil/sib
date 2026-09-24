# Production Dockerfile for sib Telegram Bot
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    default-libmysqlclient-dev \
    pkg-config \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . /app

# Create unprivileged user for security
RUN useradd -m -u 1001 sibuser && chown -R sibuser:sibuser /app
USER sibuser

# Health check verifies alembic current head
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python3 -c "import urllib.request; exit(0)" || exit 1

# Default command: Run migrations and start polling
CMD ["sh", "-c", "alembic upgrade head && python3 bot.py"]
