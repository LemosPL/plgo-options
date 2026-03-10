FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY data/ data/

RUN pip install --no-cache-dir .

ENV PYTHONPATH=/app/src

# When DB_DIR is set, SQLite writes to that path (e.g. GCS FUSE mount).
# Unset = uses local data/ directory (dev mode).
# ENV DB_DIR=/mnt/gcs/plgo

EXPOSE 8080

CMD ["uvicorn", "plgo_options.web.app:app", "--host", "0.0.0.0", "--port", "8080"]
