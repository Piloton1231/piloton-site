import asyncio
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
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
YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
YOUTUBE_PLAYLIST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{10,128}")
YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed?{}"
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
PLAYER_CLIENTS = tuple(
    client.strip()
    for client in os.getenv("PLAYER_CLIENTS", "android_vr").split(",")
    if client.strip()
)
JS_RUNTIME = os.getenv("JS_RUNTIME", "").strip()
POT_PROVIDER = os.getenv("POT_PROVIDER", "0").lower() in {"1", "true", "yes"}
POT_SERVER_URL = os.getenv("POT_SERVER_URL", "http://127.0.0.1:4416").rstrip("/")
FORCE_IPV4 = os.getenv("FORCE_IPV4", "0").lower() in {"1", "true", "yes"}
CACHE_SECONDS = max(0, int(os.getenv("CACHE_SECONDS", "180")))
METADATA_CACHE_SECONDS = max(0, int(os.getenv("METADATA_CACHE_SECONDS", "300")))
PLAYLIST_MAX_ITEMS = max(1, min(500, int(os.getenv("PLAYLIST_MAX_ITEMS", "200"))))
RATE_LIMIT = max(1, int(os.getenv("RATE_LIMIT", "12")))
RATE_WINDOW_SECONDS = max(1, int(os.getenv("RATE_WINDOW_SECONDS", "60")))
MAX_CONCURRENT_EXTRACTS = max(1, int(os.getenv("MAX_CONCURRENT_EXTRACTS", "2")))
KSYNC_FALLBACK = os.getenv("KSYNC_FALLBACK", "0").lower() in {"1", "true", "yes"}
REDIRECTOR_BASE_URL = "https://r.0cm.org/?url="
KSYNC_BASE_URL = "https://ksync.arcanescripts.com/custom/redir-url?videoUrl="

_cache: dict[str, tuple[float, str]] = {}
_metadata_cache: dict[str, tuple[float, dict]] = {}
_requests: dict[str, deque[float]] = defaultdict(deque)
_extract_slots = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTS)
_metadata_slots = asyncio.Semaphore(2)
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


def _parse_youtube_info_url(value: str) -> tuple[str | None, str | None, int | None]:
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
        or host not in ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise HTTPException(status_code=400, detail="Only HTTPS YouTube URLs are allowed")

    query = parse_qs(parsed.query)
    path_parts = [part for part in parsed.path.split("/") if part]
    video_id = None
    playlist_id = (query.get("list") or [None])[0]

    if host == "youtu.be":
        if len(path_parts) != 1:
            raise HTTPException(status_code=400, detail="A YouTube video URL is required")
        video_id = path_parts[0]
    elif parsed.path == "/watch":
        video_id = (query.get("v") or [None])[0]
    elif parsed.path == "/playlist":
        pass
    elif len(path_parts) == 2 and path_parts[0] in {"shorts", "live", "embed"}:
        video_id = path_parts[1]
    else:
        raise HTTPException(status_code=400, detail="A YouTube video or playlist URL is required")

    if video_id is not None and not YOUTUBE_VIDEO_ID_PATTERN.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube video ID")
    if playlist_id is not None and not YOUTUBE_PLAYLIST_ID_PATTERN.fullmatch(playlist_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube playlist ID")
    if video_id is None and playlist_id is None:
        raise HTTPException(status_code=400, detail="Video or playlist ID is missing")

    index = None
    index_value = (query.get("index") or [None])[0]
    if index_value is not None:
        try:
            index = int(index_value)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid playlist index") from error
        if index < 1 or index > 100000:
            raise HTTPException(status_code=400, detail="Invalid playlist index")

    return video_id, playlist_id, index


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
    last_error = None
    for player_client in PLAYER_CLIENTS:
        youtube_args = {"player_client": [player_client]}
        extractor_args = {"youtube": youtube_args}
        if POT_PROVIDER and player_client in {"mweb", "web", "web_safari"}:
            youtube_args["fetch_pot"] = ["always"]
            extractor_args["youtubepot-bgutilhttp"] = {"base_url": [POT_SERVER_URL]}

        options = {
            "format": FORMAT_SELECTOR,
            "noplaylist": True,
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "cachedir": False,
            "socket_timeout": 25,
            "retries": 1,
            "extractor_retries": 1,
            "extractor_args": extractor_args,
        }
        if JS_RUNTIME:
            options["js_runtimes"] = {JS_RUNTIME: {}}
        if FORCE_IPV4:
            options["source_address"] = "0.0.0.0"

        try:
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
        except (DownloadError, ValueError, OSError) as error:
            last_error = error
            print(
                f"YouTube direct strategy failed: {player_client} ({type(error).__name__})",
                file=sys.stderr,
                flush=True,
            )

    if isinstance(last_error, DownloadError):
        raise last_error
    raise ValueError("No compatible Google Video URL was returned") from last_error


def _get_cached_metadata(key: str) -> dict | None:
    cached = _metadata_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    return None


def _cache_metadata(key: str, value: dict) -> dict:
    _metadata_cache[key] = (time.monotonic() + METADATA_CACHE_SECONDS, value)
    return value


def _canonical_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _extract_single_video_metadata(video_id: str) -> dict:
    cache_key = f"video:{video_id}"
    cached = _get_cached_metadata(cache_key)
    if cached:
        return cached

    canonical_url = _canonical_youtube_url(video_id)
    endpoint = YOUTUBE_OEMBED_URL.format(
        urlencode({"url": canonical_url, "format": "json"})
    )
    request = UrlRequest(
        endpoint,
        headers={"Accept": "application/json", "User-Agent": "PilotonVideoResolver/1.0"},
        method="GET",
    )
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    title = payload.get("title") if isinstance(payload, dict) else None
    if not isinstance(title, str) or not title.strip():
        raise ValueError("YouTube title is missing")

    return _cache_metadata(
        cache_key,
        {"id": video_id, "title": title.strip(), "position": 1, "url": canonical_url},
    )


def _extract_playlist_metadata(playlist_id: str) -> dict:
    cache_key = f"playlist:{playlist_id}"
    cached = _get_cached_metadata(cache_key)
    if cached:
        return cached

    playlist_url = f"https://www.youtube.com/playlist?{urlencode({'list': playlist_id})}"
    options = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "playlistend": PLAYLIST_MAX_ITEMS,
    }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(playlist_url, download=False)
    if not isinstance(info, dict):
        raise ValueError("YouTube playlist data is missing")

    entries = []
    for fallback_position, entry in enumerate(info.get("entries") or [], start=1):
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if not isinstance(video_id, str) or not YOUTUBE_VIDEO_ID_PATTERN.fullmatch(video_id):
            continue
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            title = "タイトルを取得できません"
        position = entry.get("playlist_index")
        if not isinstance(position, int) or position < 1:
            position = fallback_position
        entries.append(
            {
                "id": video_id,
                "title": title.strip(),
                "position": position,
                "url": _canonical_youtube_url(video_id),
            }
        )
    if not entries:
        raise ValueError("YouTube playlist has no public videos")

    playlist_title = info.get("title")
    if not isinstance(playlist_title, str) or not playlist_title.strip():
        playlist_title = "YouTube プレイリスト"
    reported_total = info.get("playlist_count")
    if not isinstance(reported_total, int) or reported_total < len(entries):
        reported_total = len(entries)

    return _cache_metadata(
        cache_key,
        {
            "title": playlist_title.strip(),
            "entries": entries,
            "total": reported_total,
            "truncated": reported_total > len(entries),
        },
    )


def _build_youtube_listing(value: str) -> dict:
    video_id, playlist_id, requested_index = _parse_youtube_info_url(value)
    if not playlist_id:
        entry = dict(_extract_single_video_metadata(video_id))
        entry["selected"] = True
        return {
            "kind": "video",
            "title": entry["title"],
            "selected_video_id": video_id,
            "total": 1,
            "truncated": False,
            "entries": [entry],
        }

    playlist = _extract_playlist_metadata(playlist_id)
    entries = [dict(entry, selected=False) for entry in playlist["entries"]]
    selected_offset = next(
        (offset for offset, entry in enumerate(entries) if entry["id"] == video_id),
        None,
    )
    if selected_offset is None and requested_index is not None:
        selected_offset = next(
            (offset for offset, entry in enumerate(entries) if entry["position"] == requested_index),
            None,
        )

    selected_video_id = None
    if selected_offset is not None:
        selected_entry = entries.pop(selected_offset)
        selected_entry["selected"] = True
        selected_video_id = selected_entry["id"]
        entries.insert(0, selected_entry)
    elif video_id:
        selected_entry = dict(_extract_single_video_metadata(video_id), selected=True)
        if requested_index is not None:
            selected_entry["position"] = requested_index
        if len(entries) >= PLAYLIST_MAX_ITEMS:
            entries.pop()
        entries.insert(0, selected_entry)
        selected_video_id = video_id

    return {
        "kind": "playlist",
        "title": playlist["title"],
        "selected_video_id": selected_video_id,
        "total": playlist["total"],
        "truncated": playlist["truncated"],
        "entries": entries,
    }


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
        return RedirectResponse(
            cached[1],
            status_code=307,
            headers={"Cache-Control": "no-store", "X-Resolver-Path": "direct-googlevideo-cache"},
        )

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
    return RedirectResponse(
        direct_url,
        status_code=307,
        headers={"Cache-Control": "no-store", "X-Resolver-Path": "direct-googlevideo"},
    )


@app.get("/youtube/info")
async def youtube_info(
    request: Request,
    url: str = Query(min_length=1, max_length=2048),
) -> JSONResponse:
    _parse_youtube_info_url(url)
    client = request.client.host if request.client else "unknown"
    _check_rate_limit(client)

    try:
        async with _metadata_slots:
            listing = await asyncio.to_thread(_build_youtube_listing, url)
    except HTTPError as error:
        if error.code == 404:
            raise HTTPException(status_code=404, detail="YouTube video or playlist was not found") from error
        print(f"YouTube metadata API returned HTTP {error.code}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="YouTube could not provide title information") from error
    except DownloadError as error:
        print(f"YouTube playlist extraction failed: {error}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="Could not read the YouTube playlist") from error
    except (URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as error:
        print(f"YouTube metadata lookup failed: {type(error).__name__}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="Could not read YouTube title information") from error

    return JSONResponse(listing, headers={"Cache-Control": "no-store"})


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
