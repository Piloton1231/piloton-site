import asyncio
import os
import sys
import time
from collections import defaultdict, deque
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


app = FastAPI(
    title="Piloton VRChat YouTube Resolver",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
ALLOWED_MEDIA_SUFFIXES = (".googlevideo.com",)
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
