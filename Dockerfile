FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY gantry.py .

# Ensure logs directory exists
RUN mkdir -p logs

# We use sh -c so the shell can expand the variable from your .env at runtime
CMD sh -c "uvicorn gantry:app --host 0.0.0.0 --port ${GANTRY_PORT:-8787}"