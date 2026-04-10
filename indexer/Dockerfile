# =============================================================================
# Indexer — parses, threads, embeds, and indexes email into SQLite
# =============================================================================
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    # sqlite-vec build dependency
    gcc \
    # Maildir watching
    inotify-tools \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

VOLUME ["/maildir", "/data"]

CMD ["python", "-m", "src.main"]
