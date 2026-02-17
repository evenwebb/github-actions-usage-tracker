# GitHub Actions Usage Tracker
# Collect workflow data, generate dashboard, and run audits
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY collect.py generate.py audit.py ./
COPY templates/ templates/

# Data and output dirs (mount or create)
RUN mkdir -p /app/data /app/docs

ENV DB_PATH=/app/data/actions.db
ENV OUTPUT_DIR=/app/docs

# Default: collect + generate
CMD ["sh", "-c", "python collect.py && python generate.py"]
