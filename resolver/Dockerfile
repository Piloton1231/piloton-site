FROM brainicism/bgutil-ytdlp-pot-provider:1.3.2-node

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PLAYER_CLIENTS=mweb,android_vr \
    JS_RUNTIME=node \
    POT_PROVIDER=1 \
    POT_SERVER_URL=http://127.0.0.1:4416 \
    FORCE_IPV4=1 \
    KSYNC_FALLBACK=0

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 resolver \
    && python3 -m venv /opt/venv

WORKDIR /resolver

COPY requirements.txt requirements-docker.txt ./
RUN pip install --no-cache-dir --requirement requirements-docker.txt

COPY app.py ./
USER resolver

EXPOSE 8000
ENTRYPOINT []
CMD ["sh", "-c", "node /app/build/main.js & exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --proxy-headers --forwarded-allow-ips '*' "]
