# Multi-stage build for minimal final image
FROM python:3.11-slim AS builder

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local

# Make sure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH

# Copy application
COPY main.py .

# Create volume for SQLite database persistence
VOLUME ["/app/data"]

# Set environment variables
ENV WATCHDOG_DB=/app/data/watchdog.db
ENV CHECK_INTERVAL=60
ENV HTTP_TIMEOUT=10

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
