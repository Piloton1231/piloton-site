import asyncio
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


app = FastAPI(
    title="Piloton Video URL Resolver",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://piloton.cc", "https://www.piloton.cc"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
ALLOWED_MEDIA_SUFFIXES = (".googlevideo.com",)
ALLOWED_REDGIFS_HOSTS = {"redgifs.com", "www.redgifs.com"}
REDGIFS_MEDIA_HOST = "media.redgifs.com"
REDGIFS_ID_PATTERN = re.compile(r"[A-Za-z0-9]+")
REDGIFS_AUTH_URL = "https://api.redgifs.com/v2/auth/temporary"
REDGIFS_GIF_URL = "https://api.redgifs.com/v2/gifs/{}"
REDGIFS_TOKEN_SECONDS = 600
REDGIFS_HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.redgifs.com",
    "Referer": "https://www.redgifs.com/",
    "User-Agent": "PilotonRedGifsResolver/1.0",
}
FORMAT_SELECTOR = os.getenv(
    "FORMAT_SELECTOR",
    "18/best[ext=mp4][vcodec!=none][acodec!=none][height<=?720]",
)
PLAYER_CLIENT = os.getenv("PLAYER_CLIENT", "android_vr")
CACHE_SECONDS = max(0, int(os.getenv("CACHE_SECONDS", "180")))
RATE_LIMIT = max(1, int(os.getenv("RATE_LIMIT", "12")))
RATE_WINDOW_SECONDS = max(1, int(os.getenv("RATE_WINDOW_SECONDS", "60")))
MAX_CONCURRENT_EXTRACTS = max(1, int(os.getenv("MAX_CONCURRENT_EXTRACTS", "2")))
KSYNC_FALLBACK = os.getenv("KSYNC_FALLBACK", "1").lower() in {"1", "true", "yes"}
REDIRECTOR_BASE_URL = "https://r.0cm.org/?url="
KSYNC_BASE_URL = "https://ksync.arcanescripts.com/custom/redir-url?videoUrl="

_cache: dict[str, tuple[float, str]] = {}
_requests: dict[str, deque[float]] = defaultdict(deque)
_extract_slots = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTS)
_redgifs_slots = asyncio.Semaphore(4)
_redgifs_token: tuple[float, str] | None = None
_redgifs_token_lock = threading.Lock()


def _validate_youtube_url(value: str) -> str:
    if len(value) > 2048:
        raise HTTPException(status_code=400, detail="URL is too long")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid URL") from error

    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or host not in ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise HTTPException(status_code=400, detail="Only HTTPS YouTube URLs are allowed")

    if host == "youtu.be" and not parsed.path.strip("/"):
        raise HTTPException(status_code=400, detail="Video ID is missing")

    if host != "youtu.be" and not (
        parsed.path == "/watch"
        or parsed.path.startswith("/shorts/")
        or parsed.path.startswith("/live/")
        or parsed.path.startswith("/embed/")
    ):
        raise HTTPException(status_code=400, detail="A YouTube video URL is required")

    return value


def _validate_redgifs_url(value: str) -> str:
    if len(value) > 2048:
        raise HTTPException(status_code=400, detail="URL is too long")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid URL") from error

    host = (parsed.hostname or "").lower().rstrip(".")
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or host not in ALLOWED_REDGIFS_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or len(path_parts) != 2
        or path_parts[0].lower() not in {"watch", "ifr"}
        or not REDGIFS_ID_PATTERN.fullmatch(path_parts[1])
    ):
        raise HTTPException(status_code=400, detail="A public HTTPS RedGifs video URL is required")

    return path_parts[1]


def _check_rate_limit(client: str) -> None:
    now = time.monotonic()
    recent = _requests[client]
    while recent and recent[0] <= now - RATE_WINDOW_SECONDS:
        recent.popleft()
    if len(recent) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests")
    recent.append(now)


def _extract_media_url(video_url: str) -> str:
    options = {
        "format": FORMAT_SELECTOR,
        "noplaylist": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "extractor_args": {
            "youtube": {"player_client": [PLAYER_CLIENT]},
            "youtube-ejs": {"jitless": ["true"]},
        },
    }

    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(video_url, download=False)

    direct_url = info.get("url") if isinstance(info, dict) else None
    if not direct_url and isinstance(info, dict):
        downloads = info.get("requested_downloads") or []
        if downloads:
            direct_url = downloads[0].get("url")

    if not direct_url:
        raise ValueError("No compatible media URL was returned")

    media_host = (urlsplit(direct_url).hostname or "").lower().rstrip(".")
    if not any(media_host.endswith(suffix) for suffix in ALLOWED_MEDIA_SUFFIXES):
        raise ValueError("Unexpected media host")

    return direct_url


def _build_ksync_fallback_url(video_url: str) -> str:
    ksync_url = f"{KSYNC_BASE_URL}{quote(video_url, safe='')}"
    return f"{REDIRECTOR_BASE_URL}{quote(ksync_url, safe='')}"


def _read_redgifs_json(endpoint: str, token: str | None = None) -> dict:
    headers = REDGIFS_HEADERS.copy()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = UrlRequest(endpoint, headers=headers, method="GET")
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Unexpected RedGifs API response")
    return payload


def _get_redgifs_token(force_refresh: bool = False) -> str:
    global _redgifs_token

    with _redgifs_token_lock:
        now = time.monotonic()
        if not force_refresh and _redgifs_token and _redgifs_token[0] > now:
            return _redgifs_token[1]

        payload = _read_redgifs_json(REDGIFS_AUTH_URL)
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("RedGifs did not return a temporary token")
        _redgifs_token = (now + REDGIFS_TOKEN_SECONDS, token)
        return token


def _resolve_redgifs_media(media_id: str) -> tuple[str, str, str]:
    payload = None
    for attempt in range(2):
        token = _get_redgifs_token(force_refresh=attempt == 1)
        try:
            payload = _read_redgifs_json(REDGIFS_GIF_URL.format(media_id.lower()), token)
            break
        except HTTPError as error:
            if error.code == 401 and attempt == 0:
                continue
            raise

    gif = payload.get("gif") if payload else None
    if not isinstance(gif, dict):
        raise ValueError("RedGifs video data is missing")

    urls = gif.get("urls")
    if not isinstance(urls, dict):
        raise ValueError("RedGifs media URLs are missing")

    quality = "hd" if urls.get("hd") else "sd"
    direct_url = urls.get(quality)
    resolved_id = gif.get("id") or media_id
    if not isinstance(direct_url, str) or not isinstance(resolved_id, str):
        raise ValueError("RedGifs did not return an MP4 URL")

    parsed = urlsplit(direct_url)
    media_host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or media_host != REDGIFS_MEDIA_HOST
        or not parsed.path.lower().endswith(".mp4")
    ):
        raise ValueError("Unexpected RedGifs media URL")

    return resolved_id, quality, direct_url


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/resolve")
async def resolve_video(
    request: Request,
    url: str = Query(min_length=1, max_length=2048),
) -> RedirectResponse:
    video_url = _validate_youtube_url(url)
    client = request.client.host if request.client else "unknown"
    _check_rate_limit(client)

    now = time.monotonic()
    cached = _cache.get(video_url)
    if cached and cached[0] > now:
        return RedirectResponse(cached[1], status_code=307, headers={"Cache-Control": "no-store"})

    try:
        async with _extract_slots:
            direct_url = await asyncio.to_thread(_extract_media_url, video_url)
    except DownloadError as error:
        print(f"yt-dlp extraction failed: {error}", file=sys.stderr, flush=True)
        if KSYNC_FALLBACK:
            return RedirectResponse(
                _build_ksync_fallback_url(video_url),
                status_code=307,
                headers={"Cache-Control": "no-store", "X-Resolver-Path": "ksync-fallback"},
            )
        raise HTTPException(status_code=422, detail="YouTube could not provide a compatible stream") from error
    except (ValueError, OSError) as error:
        print(f"resolver extraction failed: {error}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="Could not resolve the video") from error

    _cache[video_url] = (now + CACHE_SECONDS, direct_url)
    return RedirectResponse(direct_url, status_code=307, headers={"Cache-Control": "no-store"})


@app.get("/redgifs/resolve")
async def resolve_redgifs(
    request: Request,
    url: str = Query(min_length=1, max_length=2048),
) -> JSONResponse:
    media_id = _validate_redgifs_url(url)
    client = request.client.host if request.client else "unknown"
    _check_rate_limit(client)

    try:
        async with _redgifs_slots:
            resolved_id, quality, direct_url = await asyncio.to_thread(_resolve_redgifs_media, media_id)
    except HTTPError as error:
        if error.code == 404:
            raise HTTPException(status_code=404, detail="RedGifs video was not found") from error
        print(f"RedGifs API returned HTTP {error.code}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="RedGifs could not provide the video URL") from error
    except (URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as error:
        print(f"RedGifs resolver failed: {type(error).__name__}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="Could not resolve the RedGifs video") from error

    return JSONResponse(
        {"id": resolved_id, "quality": quality, "url": direct_url},
        headers={"Cache-Control": "no-store"},
    )
