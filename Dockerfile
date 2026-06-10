# GitHub Actions Usage Tracker
# Collect workflow data, generate dashboard, and run audits
FROM python:3.12-slim

WORKDIR /app

# Install system deps for apprise notifications (optional)
RUN apt-get update && apt-get install -y --no-install-recommends \
    apprise \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY collect.py generate.py audit.py repos.txt ./
COPY templates/ templates/

# Data and output dirs
RUN mkdir -p /app/data /app/docs

ENV DB_PATH=/app/data/actions.db
ENV OUTPUT_DIR=/app/docs

# Default: collect + generate
CMD ["sh", "-c", "python collect.py && python generate.py"]
