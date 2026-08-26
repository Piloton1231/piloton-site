FROM brainicism/bgutil-ytdlp-pot-provider:1.3.2-node

USER root
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/resolver-venv/bin:$PATH

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/resolver
COPY requirements.txt ./
RUN python3 -m venv /opt/resolver-venv \
    && pip install --no-cache-dir --requirement requirements.txt

COPY app.py ./
USER node

EXPOSE 8000
ENTRYPOINT []
CMD ["sh", "-c", "node /app/build/main.js & exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --proxy-headers --forwarded-allow-ips '*' "]
