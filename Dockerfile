FROM python:3.11-slim

WORKDIR /app

# Runtime libs necesarias (onnxruntime / opencv-headless)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Toolchain mínimo para compilar insightface (extension C/C++)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential g++ gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV INSIGHTFACE_HOME=/app/.insightface

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
