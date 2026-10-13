FROM python:3.12-slim

WORKDIR /app

# Instala dependências do sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copia só o requirements primeiro (cache de camadas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código
COPY tracker.py .

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-10000}/health || exit 1

EXPOSE 10000

# Roda com gunicorn (1 worker + 8 threads — tracker é stateless)
CMD gunicorn tracker:app \
    --workers 1 \
    --threads 8 \
    --timeout 60 \
    --bind 0.0.0.0:${PORT:-10000} \
    --access-logfile - \
    --error-logfile -
