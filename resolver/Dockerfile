FROM denoland/deno:bin-2.9.5 AS deno

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DENO_DIR=/home/resolver/.cache/deno

RUN useradd --create-home --uid 10001 resolver
WORKDIR /app

COPY --from=deno /deno /usr/local/bin/deno

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY app.py ./
USER resolver

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
