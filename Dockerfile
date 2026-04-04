FROM python:3.13-slim

WORKDIR /app

# System deps (gcc needed by some ccxt/cryptography wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package installer)
RUN pip install --no-cache-dir uv

# Install Python deps first (layer cache — rebuilds only when requirements change)
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# Copy project files
COPY . .

# Ensure runtime dirs exist
RUN mkdir -p logs

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default: agent. Override in compose for GUI.
CMD ["python", "main.py"]
