# Linux image for benchmarks/server.py: uvicorn, gunicorn, wrk, and py-spy.
FROM python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends wrk \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY licenses ./licenses
COPY src ./src
RUN pip install --no-cache-dir -e ".[drf,bench-server]" "psycopg[binary]" py-spy
COPY benchmarks ./benchmarks
