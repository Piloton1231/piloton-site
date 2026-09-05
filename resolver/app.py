import asyncio
import base64
import http.cookiejar
import ipaddress
import itertools
import json
import os
import re
import sys
import threading
import time
import zlib
from collections import defaultdict, deque
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request as UrlRequest, build_opener, urlopen

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from yt_dlp import YoutubeDL
from yt_dlp.extractor.abematv import AbemaLicenseRH, AbemaTVBaseIE
from yt_dlp.networking.exceptions import RequestError
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
SITE_ORIGINS = {"https://piloton.cc", "https://www.piloton.cc"}
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
    for client in os.getenv(
        "PLAYER_CLIENTS", os.getenv("PLAYER_CLIENT", "web_embedded,android_vr")
    ).split(",")
    if client.strip()
) or ("web_embedded", "android_vr")
JS_RUNTIME = os.getenv("JS_RUNTIME", "quickjs").strip()
BUNDLED_JS_RUNTIME_PATH = Path(__file__).resolve().with_name("qjs")
JS_RUNTIME_PATH = os.getenv("JS_RUNTIME_PATH", "").strip()
if not JS_RUNTIME_PATH and JS_RUNTIME == "quickjs" and BUNDLED_JS_RUNTIME_PATH.is_file():
    JS_RUNTIME_PATH = str(BUNDLED_JS_RUNTIME_PATH)
POT_PROVIDER = os.getenv("POT_PROVIDER", "0").lower() in {"1", "true", "yes"}
POT_SERVER_URL = os.getenv("POT_SERVER_URL", "http://127.0.0.1:4416").rstrip("/")
FORCE_IPV4 = os.getenv("FORCE_IPV4", "0").lower() in {"1", "true", "yes"}
YOUTUBE_PROXY_URL = os.getenv("YOUTUBE_PROXY_URL", "").strip()
CACHE_SECONDS = max(0, int(os.getenv("CACHE_SECONDS", "180")))
EDGE_CACHE_SECONDS = max(0, int(os.getenv("EDGE_CACHE_SECONDS", str(CACHE_SECONDS))))
METADATA_CACHE_SECONDS = max(0, int(os.getenv("METADATA_CACHE_SECONDS", "300")))
PLAYLIST_MAX_ITEMS = max(1, min(500, int(os.getenv("PLAYLIST_MAX_ITEMS", "200"))))
RATE_LIMIT = max(1, int(os.getenv("RATE_LIMIT", "30")))
RATE_WINDOW_SECONDS = max(1, int(os.getenv("RATE_WINDOW_SECONDS", "60")))
MAX_CONCURRENT_EXTRACTS = max(1, int(os.getenv("MAX_CONCURRENT_EXTRACTS", "2")))
KSYNC_FALLBACK = os.getenv("KSYNC_FALLBACK", "1").lower() in {"1", "true", "yes"}
REDIRECTOR_BASE_URL = "https://r.0cm.org/?url="
KSYNC_BASE_URL = "https://ksync.arcanescripts.com/custom/redir-url?videoUrl="
NICONICO_HOSTS = {
    "nicovideo.jp",
    "www.nicovideo.jp",
    "sp.nicovideo.jp",
}
NICONICO_ID_PATTERN = re.compile(r"(?:(?:sm|so|nm|nl)\d+|\d+)", re.IGNORECASE)
NICONICO_API_BASE_URL = "https://nvapi.nicovideo.jp"
NICONICO_DELIVERY_HOST = "delivery.domand.nicovideo.jp"
NICONICO_ASSET_HOST = "asset.domand.nicovideo.jp"
NICONICO_FRONTEND_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/137.0.0.0 Safari/537.36",
    "X-Frontend-ID": "6",
    "X-Frontend-Version": "0",
}
STREAM_SOURCE_HOST_ROOTS = (
    "x.com",
    "twitter.com",
    "tiktok.com",
    "mellow-fan.com",
    "openrec.tv",
    "twitcasting.tv",
    "abema.tv",
    "tver.jp",
    "piapro.jp",
    "soundcloud.com",
    "video.fc2.com",
)


class StreamCompatibilityError(ValueError):
    pass


DIRECT_STREAM_EXTENSIONS = (
    ".mp4",
    ".webm",
    ".m4v",
    ".mov",
    ".m3u8",
    ".mpd",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".wav",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".avif",
)
STREAM_FORMAT_SELECTOR = os.getenv(
    "STREAM_FORMAT_SELECTOR",
    "bestvideo*+bestaudio/best",
)
STREAM_EXTRACT_TIMEOUT_SECONDS = max(
    10, min(90, int(os.getenv("STREAM_EXTRACT_TIMEOUT_SECONDS", "35")))
)
STREAM_TOKEN_SECONDS = max(60, min(3600, int(os.getenv("STREAM_TOKEN_SECONDS", "1800"))))
STREAM_MANIFEST_MAX_BYTES = max(
    65536, min(2_000_000, int(os.getenv("STREAM_MANIFEST_MAX_BYTES", "1000000")))
)
STREAM_MEDIA_MAX_BYTES = max(
    1_000_000, min(32_000_000, int(os.getenv("STREAM_MEDIA_MAX_BYTES", "12000000")))
)
STREAM_PROXY_BASE_URL = "https://video.piloton.cc/stream/niconico"
ABEMA_KEY_PROXY_BASE_URL = "https://video.piloton.cc/stream/key.bin?token="
ABEMA_MEDIA_PROXY_BASE_URL = "https://video.piloton.cc/stream/abema/media"
TVER_MASTER_PROXY_BASE_URL = "https://video.piloton.cc/stream/tver/master.m3u8?token="
TVER_MEDIA_PROXY_BASE_URL = "https://video.piloton.cc/stream/tver/media"
TIKTOK_MEDIA_PROXY_BASE_URL = "https://video.piloton.cc/stream/tiktok/media.mp4?token="
TIKTOK_MEDIA_HOST_ROOTS = (
    "tiktok.com",
    "tiktokcdn.com",
    "tiktokv.com",
    "byteoversea.com",
)
TIKTOK_FORMAT_SELECTOR = os.getenv(
    "TIKTOK_FORMAT_SELECTOR",
    "best[ext=mp4][vcodec^=h264][acodec!=none][height<=?1280]/"
    "best[ext=mp4][vcodec^=h264][acodec!=none]/download",
)
TIKTOK_MEDIA_CHUNK_BYTES = max(
    1_000_000,
    min(4_000_000, int(os.getenv("TIKTOK_MEDIA_CHUNK_BYTES", "4000000"))),
)
STREAM_COOKIE_PATTERN = re.compile(r"[A-Za-z0-9._~-]{16,256}")

_cache: dict[str, tuple[float, str]] = {}
_metadata_cache: dict[str, tuple[float, dict]] = {}
_requests: dict[str, deque[float]] = defaultdict(deque)
_extract_slots = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTS)
_metadata_slots = asyncio.Semaphore(2)
_redgifs_slots = asyncio.Semaphore(4)
_stream_slots = asyncio.Semaphore(2)
_redgifs_token: tuple[float, str] | None = None
_redgifs_token_lock = threading.Lock()


def _direct_redirect_headers() -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store",
        "X-Resolver-Path": "direct-googlevideo",
    }
    if EDGE_CACHE_SECONDS:
        headers["Vercel-CDN-Cache-Control"] = (
            f"public, s-maxage={EDGE_CACHE_SECONDS}, stale-while-revalidate=60"
        )
    return headers


def _resolved_response(request: Request, direct_url: str) -> Response:
    if request.headers.get("origin", "").lower() in SITE_ORIGINS:
        return Response(
            status_code=204,
            headers={"Cache-Control": "no-store", "X-Resolver-Path": "prewarm-ready"},
        )
    return RedirectResponse(
        direct_url,
        status_code=307,
        headers=_direct_redirect_headers(),
    )


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


def _host_matches(host: str, root: str) -> bool:
    return host == root or host.endswith(f".{root}")


def _normalize_stream_source_url(value: str) -> str:
    value = value.strip()
    return re.sub(r"\\(?=[_&])", "", value)


def _validate_stream_source_url(value: str) -> tuple[str, str | None]:
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
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise HTTPException(status_code=400, detail="Only public HTTPS URLs are allowed")

    if host in NICONICO_HOSTS:
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            len(path_parts) != 2
            or path_parts[0].lower() != "watch"
            or not NICONICO_ID_PATTERN.fullmatch(path_parts[1])
        ):
            raise HTTPException(status_code=400, detail="A NicoNico watch URL is required")
        return "niconico", path_parts[1].lower()

    if host.endswith(".nicovideo.jp") or host == "nicovideo.jp":
        raise HTTPException(status_code=400, detail="NicoNico Live is not supported in original mode")

    if _host_matches(host, "soundcloud.com"):
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            len(path_parts) != 2
            or path_parts[0].lower() in {"charts", "discover", "search", "stream", "you"}
            or path_parts[1].lower() == "sets"
        ):
            raise HTTPException(status_code=400, detail="A single SoundCloud track URL is required")
        return "extract", None

    if any(_host_matches(host, root) for root in STREAM_SOURCE_HOST_ROOTS):
        return "extract", None

    if parsed.path.lower().endswith(DIRECT_STREAM_EXTENSIONS):
        return "direct", None

    raise HTTPException(status_code=400, detail="This site is not supported in original mode")


def _validate_direct_media_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Invalid media URL") from error

    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
    ):
        raise ValueError("Unexpected media URL")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Private media addresses are not allowed")

    return value


def _hls_attributes(line: str) -> dict[str, str]:
    _, _, attributes = line.partition(":")
    result = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', attributes):
        raw_value = match.group(2)
        result[match.group(1)] = (
            raw_value[1:-1] if raw_value.startswith('"') and raw_value.endswith('"') else raw_value
        )
    return result


def _replace_hls_attribute(line: str, name: str, value: str, *, quoted: bool = True) -> str:
    replacement = f'{name}="{value}"' if quoted else f"{name}={value}"
    pattern = rf'{re.escape(name)}=(?:"[^"]*"|[^,]*)'
    if re.search(pattern, line):
        return re.sub(pattern, replacement, line, count=1)
    return f"{line},{replacement}"


def _normalize_hls_audio_rendition(line: str) -> str:
    attributes = _hls_attributes(line)
    if not line.startswith("#EXT-X-MEDIA:") or attributes.get("TYPE") != "AUDIO":
        return line
    line = _replace_hls_attribute(line, "DEFAULT", "YES", quoted=False)
    line = _replace_hls_attribute(line, "AUTOSELECT", "YES", quoted=False)
    line = _replace_hls_attribute(line, "FORCED", "NO", quoted=False)
    if not attributes.get("LANGUAGE"):
        line = _replace_hls_attribute(line, "LANGUAGE", "ja")
    if not attributes.get("CHANNELS"):
        line = _replace_hls_attribute(line, "CHANNELS", "2")
    return line


def _read_public_hls_manifest(value: str) -> tuple[str, str]:
    value = _validate_direct_media_url(value)
    request = UrlRequest(
        value,
        headers={"User-Agent": NICONICO_FRONTEND_HEADERS["User-Agent"]},
        method="GET",
    )
    with urlopen(request, timeout=25) as response:
        final_url = _validate_direct_media_url(response.geturl())
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > STREAM_MANIFEST_MAX_BYTES:
            raise ValueError("HLS manifest is too large")
        body = response.read(STREAM_MANIFEST_MAX_BYTES + 1)
    if len(body) > STREAM_MANIFEST_MAX_BYTES:
        raise ValueError("HLS manifest is too large")
    try:
        manifest = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("The site returned an invalid HLS manifest") from error
    if not manifest.startswith("#EXTM3U"):
        raise ValueError("The site returned an invalid HLS manifest")
    return manifest, final_url


def _simplify_public_hls_master(master_url: str) -> str:
    manifest, final_url = _read_public_hls_manifest(master_url)
    lines = manifest.splitlines()
    audio_lines = {}
    variants = []

    for line in lines:
        if line.startswith("#EXT-X-MEDIA:"):
            attributes = _hls_attributes(line)
            if attributes.get("TYPE") == "AUDIO" and attributes.get("GROUP-ID"):
                audio_lines[attributes["GROUP-ID"]] = line

    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        media_uri = next(
            (
                candidate.strip()
                for candidate in lines[index + 1 :]
                if candidate.strip() and not candidate.startswith("#")
            ),
            None,
        )
        if not media_uri:
            continue
        attributes = _hls_attributes(line)
        resolution = attributes.get("RESOLUTION", "")
        try:
            height = int(resolution.rsplit("x", 1)[1])
        except (IndexError, ValueError):
            height = 0
        codecs = attributes.get("CODECS", "").lower()
        variants.append(
            {
                "line": line,
                "uri": media_uri,
                "audio_group": attributes.get("AUDIO"),
                "height": height,
                "is_h264": "avc1" in codecs,
            }
        )

    if not variants:
        raise ValueError("The site did not return an HLS master playlist")
    preferred = [variant for variant in variants if 0 < variant["height"] <= 720]
    if not preferred:
        preferred = variants
    selected = max(
        preferred,
        key=lambda variant: (variant["is_h264"], variant["height"]),
    )

    result = ["#EXTM3U"]
    version_line = next((line for line in lines if line.startswith("#EXT-X-VERSION:")), None)
    if version_line:
        result.append(version_line)
    if "#EXT-X-INDEPENDENT-SEGMENTS" in lines:
        result.append("#EXT-X-INDEPENDENT-SEGMENTS")

    audio_group = selected["audio_group"]
    if audio_group and audio_group in audio_lines:
        audio_line = audio_lines[audio_group]
        attributes = _hls_attributes(audio_line)
        audio_uri = attributes.get("URI")
        if audio_uri:
            absolute_audio_url = _validate_direct_media_url(urljoin(final_url, audio_uri))
            audio_line = _replace_hls_attribute(audio_line, "URI", absolute_audio_url)
            result.append(_normalize_hls_audio_rendition(audio_line))

    stream_line = re.sub(r',PATHWAY-ID=(?:"[^"]*"|[^,]*)', "", selected["line"])
    absolute_video_url = _validate_direct_media_url(urljoin(final_url, selected["uri"]))
    result.extend((stream_line, absolute_video_url))
    return "\n".join(result) + "\n"


def _validate_tver_upstream_url(value: str) -> str:
    value = _validate_direct_media_url(value)
    host = (urlsplit(value).hostname or "").lower().rstrip(".")
    if not (
        host == "streaks.jp"
        or host.endswith(".streaks.jp")
        or host == "tver.jp"
        or host.endswith(".tver.jp")
    ):
        raise ValueError("Unexpected TVer media host")
    return value


def _encode_tver_stream_token(upstream_url: str) -> str:
    payload = json.dumps(
        [_validate_tver_upstream_url(upstream_url), int(time.time()) + STREAM_TOKEN_SECONDS],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_tver_stream_token(token: str) -> str:
    if len(token) > 6000:
        raise HTTPException(status_code=400, detail="Invalid TVer stream token")
    try:
        padding = "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(token + padding))
    except (ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid TVer stream token") from error
    if not isinstance(payload, list) or len(payload) != 2:
        raise HTTPException(status_code=400, detail="Invalid TVer stream token")
    upstream_url, expires_at = payload
    if (
        not isinstance(upstream_url, str)
        or not isinstance(expires_at, int)
        or expires_at < int(time.time())
        or expires_at > int(time.time()) + 3600
    ):
        raise HTTPException(status_code=400, detail="Expired or invalid TVer stream token")
    try:
        return _validate_tver_upstream_url(upstream_url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid TVer stream target") from error


def _tver_proxy_url(upstream_url: str) -> str:
    token = _encode_tver_stream_token(upstream_url)
    extension = Path(urlsplit(upstream_url).path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        extension = ".bin"
    return f"{TVER_MEDIA_PROXY_BASE_URL}{extension}?token={quote(token, safe='')}"


def _rewrite_tver_hls_manifest(manifest: str, base_url: str) -> str:
    def replace_uri(match: re.Match) -> str:
        absolute_url = _validate_tver_upstream_url(urljoin(base_url, match.group(1)))
        return f'URI="{_tver_proxy_url(absolute_url)}"'

    rewritten = []
    for line in manifest.splitlines():
        if line.startswith("#"):
            line = re.sub(r'URI="([^"]+)"', replace_uri, line)
            line = _normalize_hls_audio_rendition(line)
            rewritten.append(line)
        elif line.strip():
            absolute_url = _validate_tver_upstream_url(urljoin(base_url, line.strip()))
            rewritten.append(_tver_proxy_url(absolute_url))
        else:
            rewritten.append(line)
    return "\n".join(rewritten) + "\n"


def _create_tver_wrapper(master_url: str) -> str:
    token = _encode_tver_stream_token(master_url)
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:6\n"
        "#EXT-X-INDEPENDENT-SEGMENTS\n"
        f"{TVER_MASTER_PROXY_BASE_URL}{quote(token, safe='')}\n"
    )


def _create_tver_master(master_url: str) -> str:
    simplified = _simplify_public_hls_master(_validate_tver_upstream_url(master_url))
    return _rewrite_tver_hls_manifest(simplified, master_url)


def _stream_format_score(stream_format: dict) -> tuple[int, int, int, float]:
    height = stream_format.get("height")
    height = height if isinstance(height, (int, float)) else 0
    return (
        int(stream_format.get("ext") == "mp4"),
        int(height <= 720),
        int(height),
        float(stream_format.get("tbr") or 0),
    )


def _encode_abema_key_token(key: bytes) -> str:
    if len(key) != 16:
        raise ValueError("ABEMA returned an invalid video key")
    payload = json.dumps(
        [key.hex(), int(time.time()) + STREAM_TOKEN_SECONDS],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_abema_key_token(token: str) -> bytes:
    if len(token) > 256:
        raise HTTPException(status_code=400, detail="Invalid video key token")
    try:
        padding = "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(token + padding))
    except (ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid video key token") from error
    if not isinstance(payload, list) or len(payload) != 2:
        raise HTTPException(status_code=400, detail="Invalid video key token")
    key_hex, expires_at = payload
    if (
        not isinstance(key_hex, str)
        or not re.fullmatch(r"[0-9a-f]{32}", key_hex)
        or not isinstance(expires_at, int)
        or expires_at < int(time.time())
        or expires_at > int(time.time()) + 3600
    ):
        raise HTTPException(status_code=400, detail="Expired or invalid video key token")
    return bytes.fromhex(key_hex)


def _validate_abema_upstream_url(value: str) -> str:
    value = _validate_direct_media_url(value)
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "vod-abematv.akamaized.net" or not parsed.path.startswith(("/tsvpg/", "/program/")):
        raise ValueError("Unexpected ABEMA media host")
    return value


def _encode_abema_stream_token(upstream_url: str) -> str:
    payload = json.dumps(
        [_validate_abema_upstream_url(upstream_url), int(time.time()) + STREAM_TOKEN_SECONDS],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_abema_stream_token(token: str) -> str:
    if len(token) > 6000:
        raise HTTPException(status_code=400, detail="Invalid ABEMA stream token")
    try:
        padding = "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(token + padding))
    except (ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid ABEMA stream token") from error
    if not isinstance(payload, list) or len(payload) != 2:
        raise HTTPException(status_code=400, detail="Invalid ABEMA stream token")
    upstream_url, expires_at = payload
    if (
        not isinstance(upstream_url, str)
        or not isinstance(expires_at, int)
        or expires_at < int(time.time())
        or expires_at > int(time.time()) + 3600
    ):
        raise HTTPException(status_code=400, detail="Expired or invalid ABEMA stream token")
    try:
        return _validate_abema_upstream_url(upstream_url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid ABEMA stream target") from error


def _abema_proxy_url(upstream_url: str) -> str:
    token = _encode_abema_stream_token(upstream_url)
    extension = Path(urlsplit(upstream_url).path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        extension = ".bin"
    return f"{ABEMA_MEDIA_PROXY_BASE_URL}{extension}?token={quote(token, safe='')}"


def _read_abema_video_key(downloader: YoutubeDL, license_url: str) -> bytes:
    parsed = urlsplit(license_url)
    if (
        parsed.scheme != "abematv-license"
        or not parsed.hostname
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", parsed.hostname)
    ):
        raise ValueError("ABEMA returned an invalid license URL")
    with downloader.urlopen(license_url) as response:
        key = response.read(17)
    if len(key) != 16:
        raise ValueError("ABEMA returned an invalid video key")
    return key


def _create_abema_manifest(downloader: YoutubeDL, info: dict) -> str:
    formats = [stream_format for stream_format in info.get("formats", []) if isinstance(stream_format, dict)]
    hls_formats = [
        stream_format
        for stream_format in formats
        if isinstance(stream_format.get("url"), str)
        and str(stream_format.get("protocol", "")).startswith("m3u8")
    ]
    preferred = [
        stream_format
        for stream_format in hls_formats
        if isinstance(stream_format.get("height"), (int, float))
        and 0 < stream_format["height"] <= 720
    ]
    if not preferred:
        preferred = hls_formats
    if not preferred:
        raise StreamCompatibilityError("ABEMA did not provide an HLS stream")
    selected = max(preferred, key=_stream_format_score)
    manifest, final_url = _read_public_hls_manifest(selected["url"])

    key_tokens = {}

    def replace_key_uri(match: re.Match) -> str:
        license_url = match.group(1)
        if license_url not in key_tokens:
            key = _read_abema_video_key(downloader, license_url)
            key_tokens[license_url] = _encode_abema_key_token(key)
        return f'URI="{ABEMA_KEY_PROXY_BASE_URL}{quote(key_tokens[license_url], safe="")}"'

    rewritten = []
    for line in manifest.splitlines():
        if line.startswith("#"):
            rewritten.append(
                re.sub(r'URI="(abematv-license://[^"]+)"', replace_key_uri, line)
            )
        elif line.strip():
            rewritten.append(_abema_proxy_url(urljoin(final_url, line.strip())))
        else:
            rewritten.append(line)
    return "\n".join(rewritten) + "\n"


def _select_soundcloud_progressive(info: dict) -> str | None:
    candidates = []
    for stream_format in info.get("formats", []):
        if not isinstance(stream_format, dict):
            continue
        format_url = stream_format.get("url")
        if not isinstance(format_url, str) or urlsplit(format_url).scheme != "https":
            continue
        if stream_format.get("vcodec") != "none" or stream_format.get("ext") not in {"mp3", "m4a"}:
            continue
        if stream_format.get("protocol") not in {"http", "https"}:
            continue
        candidates.append(stream_format)
    if not candidates:
        return None
    selected = max(
        candidates,
        key=lambda stream_format: (
            int(stream_format.get("ext") == "mp3"),
            float(stream_format.get("abr") or stream_format.get("tbr") or 0),
        ),
    )
    return _validate_direct_media_url(selected["url"])


def _validate_tiktok_media_url(value: str) -> str:
    value = _validate_direct_media_url(value)
    host = (urlsplit(value).hostname or "").lower().rstrip(".")
    if not any(_host_matches(host, root) for root in TIKTOK_MEDIA_HOST_ROOTS):
        raise ValueError("Unexpected TikTok media host")
    return value


def _validate_tiktok_source_url(value: str) -> str:
    value = _validate_direct_media_url(value)
    host = (urlsplit(value).hostname or "").lower().rstrip(".")
    if not _host_matches(host, "tiktok.com"):
        raise ValueError("Unexpected TikTok source host")
    return value


def _select_tiktok_progressive(info: dict) -> str | None:
    candidates = []
    for stream_format in info.get("formats", []):
        if not isinstance(stream_format, dict):
            continue
        format_url = stream_format.get("url")
        if not isinstance(format_url, str) or urlsplit(format_url).scheme != "https":
            continue
        vcodec = str(stream_format.get("vcodec") or "").lower()
        if not vcodec.startswith(("h264", "avc")):
            continue
        if stream_format.get("acodec") == "none" or stream_format.get("ext") != "mp4":
            continue
        candidates.append(stream_format)
    if not candidates:
        return None

    def score(stream_format: dict) -> tuple[int, int, int, float]:
        width = stream_format.get("width")
        height = stream_format.get("height")
        width = int(width) if isinstance(width, (int, float)) else 0
        height = int(height) if isinstance(height, (int, float)) else 0
        long_edge = max(width, height)
        pixels = width * height
        return (
            int(0 < long_edge <= 1280),
            int(str(stream_format.get("format_note") or "").lower() != "watermarked"),
            pixels,
            float(stream_format.get("tbr") or 0),
        )

    selected = max(candidates, key=score)
    return _validate_tiktok_media_url(selected["url"])


def _tiktok_cookie_header(cookie_jar: http.cookiejar.CookieJar) -> str:
    pairs = []
    for cookie in cookie_jar:
        domain = cookie.domain.lower().lstrip(".").rstrip(".")
        if not _host_matches(domain, "tiktok.com"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", cookie.name):
            continue
        if not isinstance(cookie.value, str) or any(ord(char) < 0x21 for char in cookie.value):
            continue
        pairs.append(f"{cookie.name}={cookie.value}")
    header = "; ".join(pairs)
    if len(header) > 4096:
        raise ValueError("TikTok cookies are too large")
    return header


def _encode_tiktok_media_token(upstream_url: str, source_url: str, cookie_header: str) -> str:
    if len(cookie_header) > 4096 or "\r" in cookie_header or "\n" in cookie_header:
        raise ValueError("Invalid TikTok cookies")
    payload = json.dumps(
        [
            _validate_tiktok_media_url(upstream_url),
            _validate_tiktok_source_url(source_url),
            cookie_header,
            int(time.time()) + STREAM_TOKEN_SECONDS,
        ],
        separators=(",", ":"),
    ).encode()
    return "z" + base64.urlsafe_b64encode(zlib.compress(payload, level=9)).decode().rstrip("=")


def _decode_tiktok_media_token(token: str) -> tuple[str, str, str]:
    if len(token) > 6000 or not token.startswith("z"):
        raise HTTPException(status_code=400, detail="Invalid TikTok media token")
    try:
        encoded = token[1:]
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(zlib.decompress(base64.urlsafe_b64decode(encoded + padding)))
    except (ValueError, TypeError, json.JSONDecodeError, zlib.error) as error:
        raise HTTPException(status_code=400, detail="Invalid TikTok media token") from error
    if not isinstance(payload, list) or len(payload) != 4:
        raise HTTPException(status_code=400, detail="Invalid TikTok media token")
    upstream_url, source_url, cookie_header, expires_at = payload
    if (
        not isinstance(upstream_url, str)
        or not isinstance(source_url, str)
        or not isinstance(cookie_header, str)
        or len(cookie_header) > 4096
        or "\r" in cookie_header
        or "\n" in cookie_header
        or not isinstance(expires_at, int)
        or expires_at < int(time.time())
        or expires_at > int(time.time()) + 3600
    ):
        raise HTTPException(status_code=400, detail="Expired or invalid TikTok media token")
    try:
        return (
            _validate_tiktok_media_url(upstream_url),
            _validate_tiktok_source_url(source_url),
            cookie_header,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid TikTok media target") from error


def _tiktok_proxy_url(upstream_url: str, source_url: str, cookie_header: str) -> str:
    token = _encode_tiktok_media_token(upstream_url, source_url, cookie_header)
    return f"{TIKTOK_MEDIA_PROXY_BASE_URL}{quote(token, safe='')}"


def _bounded_tiktok_range(range_header: str | None) -> str:
    if range_header:
        suffix_match = re.fullmatch(r"bytes=-(\d+)", range_header)
        if suffix_match:
            length = min(int(suffix_match.group(1)), TIKTOK_MEDIA_CHUNK_BYTES)
            if length > 0:
                return f"bytes=-{length}"
        range_match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
        if range_match:
            start = int(range_match.group(1))
            requested_end = int(range_match.group(2)) if range_match.group(2) else None
            maximum_end = start + TIKTOK_MEDIA_CHUNK_BYTES - 1
            end = min(requested_end, maximum_end) if requested_end is not None else maximum_end
            if end >= start:
                return f"bytes={start}-{end}"
    return f"bytes=0-{TIKTOK_MEDIA_CHUNK_BYTES - 1}"


def _read_tiktok_resource(
    upstream_url: str,
    source_url: str,
    cookie_header: str,
    range_header: str | None = None,
) -> tuple[bytes, int, str, dict[str, str]]:
    request_headers = {
        "Accept": "*/*",
        "Referer": _validate_tiktok_source_url(source_url),
        "User-Agent": NICONICO_FRONTEND_HEADERS["User-Agent"],
        "Range": _bounded_tiktok_range(range_header),
    }
    if cookie_header:
        request_headers["Cookie"] = cookie_header
    request = UrlRequest(
        _validate_tiktok_media_url(upstream_url),
        headers=request_headers,
        method="GET",
    )
    opener = (
        build_opener(ProxyHandler({"http": YOUTUBE_PROXY_URL, "https": YOUTUBE_PROXY_URL}))
        if YOUTUBE_PROXY_URL
        else build_opener()
    )
    with opener.open(request, timeout=25) as response:
        _validate_tiktok_media_url(response.geturl())
        content_type = response.headers.get("Content-Type", "video/mp4")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > TIKTOK_MEDIA_CHUNK_BYTES:
            raise ValueError("TikTok media response is too large")
        body = response.read(TIKTOK_MEDIA_CHUNK_BYTES + 1)
        if len(body) > TIKTOK_MEDIA_CHUNK_BYTES:
            raise ValueError("TikTok media response is too large")
        forwarded_headers = {
            name: response.headers[name]
            for name in ("Accept-Ranges", "Content-Range", "ETag", "Last-Modified")
            if response.headers.get(name)
        }
        return body, response.status, content_type, forwarded_headers


def _extract_stream_media(value: str) -> tuple[str, str]:
    source_host = (urlsplit(value).hostname or "").lower().rstrip(".")
    is_soundcloud = _host_matches(source_host, "soundcloud.com")
    is_abema = _host_matches(source_host, "abema.tv")
    is_tver = _host_matches(source_host, "tver.jp")
    is_tiktok = _host_matches(source_host, "tiktok.com")
    options = {
        "format": TIKTOK_FORMAT_SELECTOR if is_tiktok else STREAM_FORMAT_SELECTOR,
        "noplaylist": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
    }
    if is_soundcloud:
        options.update(
            {
                "playlistend": 1,
                "playlist_items": "1",
                "extract_flat": "discard_in_playlist",
            }
        )
    if is_tiktok and YOUTUBE_PROXY_URL:
        options["proxy"] = YOUTUBE_PROXY_URL
    if JS_RUNTIME:
        runtime_options = {"path": JS_RUNTIME_PATH} if JS_RUNTIME_PATH else {}
        options["js_runtimes"] = {JS_RUNTIME: runtime_options}

    tiktok_cookie_header = ""
    extract_attempts = 3 if is_tiktok else 1
    for extract_attempt in range(extract_attempts):
        try:
            with YoutubeDL(options) as downloader:
                if is_abema:
                    AbemaTVBaseIE._MEDIATOKEN = None
                    abema_extractor = downloader.get_info_extractor("AbemaTV")
                    downloader._request_director.add_handler(
                        AbemaLicenseRH(ie=abema_extractor, logger=None)
                    )
                info = downloader.extract_info(value, download=False)
                if is_tiktok:
                    tiktok_cookie_header = _tiktok_cookie_header(downloader.cookiejar)
                if isinstance(info, dict) and info.get("extractor_key") == "AbemaTV":
                    return "abema-manifest", _create_abema_manifest(downloader, info)
            break
        except DownloadError:
            if extract_attempt + 1 >= extract_attempts:
                raise

    direct_url = info.get("url") if isinstance(info, dict) else None
    if not isinstance(info, dict):
        raise ValueError("No stream information was returned")
    if is_tiktok:
        progressive_url = _select_tiktok_progressive(info)
        if progressive_url:
            return "redirect", _tiktok_proxy_url(progressive_url, value, tiktok_cookie_header)
    if is_soundcloud and info.get("entries") is not None:
        raise StreamCompatibilityError("SoundCloud profiles and playlists are not supported")
    if str(info.get("extractor_key", "")).lower() == "soundcloud":
        progressive_url = _select_soundcloud_progressive(info)
        if progressive_url:
            return "redirect", progressive_url
    if isinstance(direct_url, str):
        return "redirect", _validate_direct_media_url(direct_url)

    formats = [stream_format for stream_format in info.get("formats", []) if isinstance(stream_format, dict)]
    progressive_formats = []
    for stream_format in formats:
        format_url = stream_format.get("url")
        if not isinstance(format_url, str):
            continue
        if stream_format.get("protocol") != "https":
            continue
        if stream_format.get("vcodec") == "none" or stream_format.get("acodec") == "none":
            continue
        if stream_format.get("ext") not in {"mp4", "m4v", "mov", "webm"}:
            continue
        progressive_formats.append(stream_format)
    if progressive_formats:
        selected = max(progressive_formats, key=_stream_format_score)
        return "redirect", _validate_direct_media_url(selected["url"])

    manifest_url = next(
        (
            stream_format.get("manifest_url")
            for stream_format in formats
            if isinstance(stream_format.get("manifest_url"), str)
            and str(stream_format.get("protocol", "")).startswith("m3u8")
        ),
        None,
    )
    if manifest_url:
        if is_tver:
            return "tver-manifest", _create_tver_master(manifest_url)
        return "manifest", _simplify_public_hls_master(manifest_url)

    raise StreamCompatibilityError("The site only provided separate audio and video streams")


def _encode_stream_token(upstream_url: str, domand_cookie: str) -> str:
    expires_at = int(time.time()) + STREAM_TOKEN_SECONDS
    payload = json.dumps(
        [upstream_url, domand_cookie, expires_at],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    compressed = zlib.compress(payload, level=9)
    return "z" + base64.urlsafe_b64encode(compressed).decode().rstrip("=")


def _validate_niconico_upstream_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid stream token") from error

    host = (parsed.hostname or "").lower().rstrip(".")
    allowed_path = (
        host == NICONICO_DELIVERY_HOST and parsed.path.startswith("/hlsbid/")
    ) or (
        host == NICONICO_ASSET_HOST and parsed.path.startswith("/")
    )
    if (
        parsed.scheme != "https"
        or not allowed_path
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise HTTPException(status_code=400, detail="Invalid stream target")
    return value


def _decode_stream_token(token: str) -> tuple[str, str]:
    if len(token) > 12000:
        raise HTTPException(status_code=400, detail="Invalid stream token")

    try:
        if token.startswith("z"):
            encoded = token[1:]
            padding = "=" * (-len(encoded) % 4)
            compressed = base64.urlsafe_b64decode(encoded + padding)
            decompressor = zlib.decompressobj()
            decoded = decompressor.decompress(compressed, 20001)
            if len(decoded) > 20000 or decompressor.unconsumed_tail or not decompressor.eof:
                raise ValueError("Invalid compressed stream token")
        else:
            padding = "=" * (-len(token) % 4)
            decoded = base64.urlsafe_b64decode(token + padding)
        payload = json.loads(decoded)
    except (ValueError, zlib.error, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid stream token") from error

    if not isinstance(payload, list) or len(payload) != 3:
        raise HTTPException(status_code=400, detail="Invalid stream token")
    upstream_url, domand_cookie, expires_at = payload
    if (
        not isinstance(upstream_url, str)
        or not isinstance(domand_cookie, str)
        or not STREAM_COOKIE_PATTERN.fullmatch(domand_cookie)
        or not isinstance(expires_at, int)
        or expires_at < int(time.time())
        or expires_at > int(time.time()) + 3600
    ):
        raise HTTPException(status_code=400, detail="Expired or invalid stream token")

    return _validate_niconico_upstream_url(upstream_url), domand_cookie


def _proxy_stream_url(upstream_url: str, domand_cookie: str) -> str:
    token = _encode_stream_token(upstream_url, domand_cookie)
    upstream_path = urlsplit(upstream_url).path.lower()
    upstream_name = Path(upstream_path).name
    if "/audio/" in upstream_path or upstream_name.startswith("audio-"):
        track = "audio"
    elif "/video/" in upstream_path or upstream_name.startswith("video-"):
        track = "video"
    else:
        track = "media"
    extension = Path(upstream_path).suffix.lower()
    # NicoNico uses vendor-specific suffixes for fragmented MP4. Expose video
    # as .mp4 and AAC audio as .m4a so AVPro can identify each track correctly.
    if extension == ".cmfv":
        extension = ".mp4"
    elif extension == ".cmfa":
        extension = ".m4a"
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        extension = ".bin"
    return f"{STREAM_PROXY_BASE_URL}/{track}{extension}?token={quote(token, safe='')}"


def _rewrite_hls_manifest(manifest: str, base_url: str, domand_cookie: str) -> str:
    def replace_uri(match: re.Match) -> str:
        absolute_url = urljoin(base_url, match.group(1))
        _validate_niconico_upstream_url(absolute_url)
        return f'URI="{_proxy_stream_url(absolute_url, domand_cookie)}"'

    rewritten = []
    for line in manifest.splitlines():
        if line.startswith("#"):
            line = re.sub(r'URI="([^"]+)"', replace_uri, line)
            rewritten.append(_normalize_hls_audio_rendition(line))
        elif line.strip():
            absolute_url = urljoin(base_url, line.strip())
            _validate_niconico_upstream_url(absolute_url)
            rewritten.append(_proxy_stream_url(absolute_url, domand_cookie))
        else:
            rewritten.append(line)
    return "\n".join(rewritten) + "\n"


def _read_niconico_resource(
    upstream_url: str,
    domand_cookie: str,
    range_header: str | None = None,
) -> tuple[bytes, int, str, dict[str, str], str]:
    request_headers = {
        "Cookie": f"domand_bid={domand_cookie}",
        "User-Agent": NICONICO_FRONTEND_HEADERS["User-Agent"],
    }
    if range_header and re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header):
        request_headers["Range"] = range_header

    request = UrlRequest(upstream_url, headers=request_headers, method="GET")
    with urlopen(request, timeout=25) as response:
        final_url = _validate_niconico_upstream_url(response.geturl())
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        is_manifest = "mpegurl" in content_type.lower() or urlsplit(final_url).path.endswith(".m3u8")
        max_bytes = STREAM_MANIFEST_MAX_BYTES if is_manifest else STREAM_MEDIA_MAX_BYTES
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("Stream response is too large")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("Stream response is too large")
        forwarded_headers = {
            name: response.headers[name]
            for name in ("Accept-Ranges", "Content-Range", "ETag", "Last-Modified")
            if response.headers.get(name)
        }
        return body, response.status, content_type, forwarded_headers, final_url


def _read_tver_resource(
    upstream_url: str,
    range_header: str | None = None,
) -> tuple[bytes, int, str, dict[str, str], str]:
    request_headers = {"User-Agent": NICONICO_FRONTEND_HEADERS["User-Agent"]}
    if range_header and re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header):
        request_headers["Range"] = range_header

    request = UrlRequest(
        _validate_tver_upstream_url(upstream_url),
        headers=request_headers,
        method="GET",
    )
    with urlopen(request, timeout=25) as response:
        final_url = _validate_tver_upstream_url(response.geturl())
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        is_manifest = "mpegurl" in content_type.lower() or urlsplit(final_url).path.endswith(".m3u8")
        max_bytes = STREAM_MANIFEST_MAX_BYTES if is_manifest else STREAM_MEDIA_MAX_BYTES
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("TVer stream response is too large")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("TVer stream response is too large")
        forwarded_headers = {
            name: response.headers[name]
            for name in ("Accept-Ranges", "Content-Range", "ETag", "Last-Modified")
            if response.headers.get(name)
        }
        return body, response.status, content_type, forwarded_headers, final_url


def _read_abema_resource(
    upstream_url: str,
    range_header: str | None = None,
) -> tuple[bytes, int, str, dict[str, str]]:
    request_headers = {"User-Agent": NICONICO_FRONTEND_HEADERS["User-Agent"]}
    if range_header and re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header):
        request_headers["Range"] = range_header
    request = UrlRequest(
        _validate_abema_upstream_url(upstream_url),
        headers=request_headers,
        method="GET",
    )
    with urlopen(request, timeout=25) as response:
        _validate_abema_upstream_url(response.geturl())
        content_type = response.headers.get("Content-Type", "video/mp2t")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > STREAM_MEDIA_MAX_BYTES:
            raise ValueError("ABEMA stream response is too large")
        body = response.read(STREAM_MEDIA_MAX_BYTES + 1)
        if len(body) > STREAM_MEDIA_MAX_BYTES:
            raise ValueError("ABEMA stream response is too large")
        forwarded_headers = {
            name: response.headers[name]
            for name in ("Accept-Ranges", "Content-Range", "ETag", "Last-Modified")
            if response.headers.get(name)
        }
        return body, response.status, content_type, forwarded_headers


def _create_niconico_manifest(video_id: str) -> str:
    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    action_track_id = f"AAAAAAAAAA_{round(time.time() * 1000)}"
    watch_url = (
        f"https://www.nicovideo.jp/api/watch/v3_guest/{quote(video_id, safe='')}?"
        f"{urlencode({'actionTrackId': action_track_id})}"
    )
    watch_request = UrlRequest(watch_url, headers=NICONICO_FRONTEND_HEADERS, method="GET")
    with opener.open(watch_request, timeout=20) as response:
        watch_payload = json.load(response)

    if watch_payload.get("meta", {}).get("status") != 200:
        raise ValueError("NicoNico video is unavailable")
    watch_data = watch_payload.get("data")
    if not isinstance(watch_data, dict):
        raise ValueError("NicoNico watch data is missing")

    media = watch_data.get("media")
    domand = media.get("domand") if isinstance(media, dict) else None
    client = watch_data.get("client")
    if not isinstance(domand, dict) or not isinstance(client, dict):
        raise ValueError("NicoNico stream data is missing")
    video_items = [
        item
        for item in domand.get("videos", [])
        if isinstance(item, dict)
        and item.get("isAvailable")
        and isinstance(item.get("id"), str)
    ]
    videos_at_or_below_720p = [
        item
        for item in video_items
        if not isinstance(item.get("height"), (int, float)) or item["height"] <= 720
    ]
    if videos_at_or_below_720p:
        video_items = videos_at_or_below_720p
    audio_items = [
        item
        for item in domand.get("audios", [])
        if isinstance(item, dict)
        and item.get("isAvailable")
        and isinstance(item.get("id"), str)
    ]
    videos = [max(video_items, key=lambda item: item.get("bitRate") or 0)["id"]] if video_items else []
    audios = [max(audio_items, key=lambda item: item.get("bitRate") or 0)["id"]] if audio_items else []
    access_key = domand.get("accessRightKey")
    watch_track_id = client.get("watchTrackId")
    if not videos or not audios or not access_key or not watch_track_id:
        raise ValueError("NicoNico has no public stream formats")

    access_url = (
        f"{NICONICO_API_BASE_URL}/v1/watch/{quote(video_id, safe='')}/access-rights/hls?"
        f"{urlencode({'actionTrackId': watch_track_id})}"
    )
    access_headers = {
        **NICONICO_FRONTEND_HEADERS,
        "Accept": "application/json;charset=utf-8",
        "Content-Type": "application/json",
        "X-Access-Right-Key": access_key,
        "X-Request-With": "https://www.nicovideo.jp",
    }
    access_body = json.dumps(
        {"outputs": list(itertools.product(videos, audios))},
        separators=(",", ":"),
    ).encode()
    access_request = UrlRequest(
        access_url,
        data=access_body,
        headers=access_headers,
        method="POST",
    )
    with opener.open(access_request, timeout=20) as response:
        access_payload = json.load(response)
    access_data = access_payload.get("data")
    master_url = access_data.get("contentUrl") if isinstance(access_data, dict) else None
    if not isinstance(master_url, str):
        raise ValueError("NicoNico did not return an HLS manifest")
    _validate_niconico_upstream_url(master_url)

    domand_cookie = next(
        (cookie.value for cookie in cookie_jar if cookie.name == "domand_bid"),
        None,
    )
    if not domand_cookie or not STREAM_COOKIE_PATTERN.fullmatch(domand_cookie):
        raise ValueError("NicoNico stream cookie is missing")

    body, _, _, _, final_url = _read_niconico_resource(master_url, domand_cookie)
    try:
        manifest = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("NicoNico returned an invalid HLS manifest") from error
    if not manifest.startswith("#EXTM3U"):
        raise ValueError("NicoNico returned an invalid HLS manifest")
    return _rewrite_hls_manifest(manifest, final_url, domand_cookie)


def _check_rate_limit(client: str) -> None:
    now = time.monotonic()
    recent = _requests[client]
    while recent and recent[0] <= now - RATE_WINDOW_SECONDS:
        recent.popleft()
    if len(recent) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests")
    recent.append(now)


def _request_client_key(request: Request) -> str:
    for header_name in ("x-vercel-forwarded-for", "x-forwarded-for", "x-real-ip"):
        raw_value = request.headers.get(header_name)
        if not raw_value:
            continue
        candidate = raw_value.split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return request.client.host if request.client else "unknown"


def _extract_media_url(video_url: str) -> str:
    last_error = None
    for player_client in PLAYER_CLIENTS:
        youtube_args = {"player_client": [player_client]}
        extractor_args = {"youtube": youtube_args}
        if POT_PROVIDER and player_client in {"mweb", "web"}:
            youtube_args["fetch_pot"] = ["always"]
            extractor_args["youtubepot-bgutilhttp"] = {"base_url": [POT_SERVER_URL]}

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
            "extractor_args": extractor_args,
        }
        if JS_RUNTIME:
            runtime_options = {"path": JS_RUNTIME_PATH} if JS_RUNTIME_PATH else {}
            options["js_runtimes"] = {JS_RUNTIME: runtime_options}
        if FORCE_IPV4:
            options["source_address"] = "0.0.0.0"
        if YOUTUBE_PROXY_URL:
            options["proxy"] = YOUTUBE_PROXY_URL

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
    if YOUTUBE_PROXY_URL:
        options["proxy"] = YOUTUBE_PROXY_URL
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
async def health() -> dict[str, str | bool | int]:
    return {
        "status": "ok",
        "proxyEnabled": bool(YOUTUBE_PROXY_URL),
        "jsRuntimeBundled": BUNDLED_JS_RUNTIME_PATH.is_file(),
        "edgeCacheSeconds": EDGE_CACHE_SECONDS,
    }


@app.get("/stream")
async def resolve_stream(
    request: Request,
    url: str = Query(min_length=1, max_length=2048),
) -> Response:
    url = _normalize_stream_source_url(url)
    route, video_id = _validate_stream_source_url(url)
    client = _request_client_key(request)
    _check_rate_limit(client)

    if route == "direct":
        return RedirectResponse(
            _validate_direct_media_url(url),
            status_code=307,
            headers={"Cache-Control": "no-store", "X-Resolver-Path": "direct-media"},
        )

    try:
        async with _stream_slots:
            if route == "niconico" and video_id:
                manifest = await asyncio.to_thread(_create_niconico_manifest, video_id)
                return Response(
                    content=manifest,
                    media_type="application/vnd.apple.mpegurl",
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-store",
                        "X-Resolver-Path": "original-niconico-hls",
                    },
                )
            stream_kind, stream_content = await asyncio.wait_for(
                asyncio.to_thread(_extract_stream_media, url),
                timeout=STREAM_EXTRACT_TIMEOUT_SECONDS,
            )
    except DownloadError as error:
        print(f"stream extraction failed: {error}", file=sys.stderr, flush=True)
        message = str(error).lower()
        detail = (
            "This content requires a login or paid subscription"
            if "subscription" in message or "login" in message or "cookies" in message
            else "The site did not provide a VRChat-compatible stream"
        )
        raise HTTPException(
            status_code=422,
            detail=detail,
        ) from error
    except StreamCompatibilityError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except HTTPError as error:
        print(f"stream source returned HTTP {error.code}", file=sys.stderr, flush=True)
        status_code = 404 if error.code == 404 else 502
        raise HTTPException(status_code=status_code, detail="Could not open the stream source") from error
    except (URLError, RequestError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as error:
        print(f"original stream resolver failed: {type(error).__name__}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="Could not create the original stream") from error

    if stream_kind in {"manifest", "abema-manifest", "tver-manifest"}:
        resolver_path = {
            "manifest": "original-simplified-hls",
            "abema-manifest": "original-abema-hls",
            "tver-manifest": "original-tver-hls",
        }[stream_kind]
        return Response(
            content=stream_content,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
                "X-Resolver-Path": resolver_path,
            },
        )
    return RedirectResponse(
        stream_content,
        status_code=307,
        headers={"Cache-Control": "no-store", "X-Resolver-Path": "original-direct-media"},
    )


@app.get("/stream/key.bin")
async def serve_abema_video_key(
    token: str = Query(min_length=1, max_length=256),
) -> Response:
    key = _decode_abema_key_token(token)
    return Response(
        content=key,
        media_type="application/octet-stream",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=300",
            "X-Resolver-Path": "original-abema-key",
        },
    )


async def _proxy_abema_media_response(request: Request, token: str) -> Response:
    upstream_url = _decode_abema_stream_token(token)
    range_header = request.headers.get("range")
    try:
        body, status_code, content_type, upstream_headers = await asyncio.to_thread(
            _read_abema_resource,
            upstream_url,
            range_header,
        )
    except HTTPError as error:
        print(f"ABEMA media returned HTTP {error.code}", file=sys.stderr, flush=True)
        status = 410 if error.code in {401, 403, 404} else 502
        raise HTTPException(status_code=status, detail="The ABEMA stream has expired") from error
    except (URLError, TimeoutError, ValueError, OSError) as error:
        print(f"ABEMA media proxy failed: {type(error).__name__}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="Could not read the ABEMA stream") from error

    return Response(
        content=body,
        status_code=status_code,
        headers={
            **upstream_headers,
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=300",
            "Content-Type": content_type or "video/mp2t",
            "X-Resolver-Path": "original-abema-media",
        },
    )


@app.get("/stream/abema/media")
async def proxy_abema_media(
    request: Request,
    token: str = Query(min_length=1, max_length=6000),
) -> Response:
    return await _proxy_abema_media_response(request, token)


@app.get("/stream/abema/media.{extension}")
async def proxy_abema_media_with_extension(
    request: Request,
    extension: str,
    token: str = Query(min_length=1, max_length=6000),
) -> Response:
    if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
        raise HTTPException(status_code=404, detail="Unsupported ABEMA stream extension")
    return await _proxy_abema_media_response(request, token)


@app.get("/stream/tver/master.m3u8")
async def serve_tver_master(
    token: str = Query(min_length=1, max_length=6000),
) -> Response:
    master_url = _decode_tver_stream_token(token)
    try:
        manifest = await asyncio.to_thread(_create_tver_master, master_url)
    except HTTPError as error:
        print(f"TVer master returned HTTP {error.code}", file=sys.stderr, flush=True)
        status = 410 if error.code in {401, 403, 404} else 502
        raise HTTPException(status_code=status, detail="The TVer stream has expired") from error
    except (URLError, TimeoutError, ValueError, OSError) as error:
        print(f"TVer master proxy failed: {type(error).__name__}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="Could not create the TVer stream") from error
    return Response(
        content=manifest,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
            "X-Resolver-Path": "original-tver-master",
        },
    )


async def _proxy_tver_media_response(request: Request, token: str) -> Response:
    upstream_url = _decode_tver_stream_token(token)
    range_header = request.headers.get("range")
    try:
        body, status_code, content_type, upstream_headers, final_url = await asyncio.to_thread(
            _read_tver_resource,
            upstream_url,
            range_header,
        )
    except HTTPError as error:
        print(f"TVer media returned HTTP {error.code}", file=sys.stderr, flush=True)
        status = 410 if error.code in {401, 403, 404} else 502
        raise HTTPException(status_code=status, detail="The TVer stream has expired") from error
    except (URLError, TimeoutError, ValueError, OSError) as error:
        print(f"TVer media proxy failed: {type(error).__name__}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="Could not read the TVer stream") from error

    is_manifest = "mpegurl" in content_type.lower() or urlsplit(final_url).path.endswith(".m3u8")
    response_headers = {
        **upstream_headers,
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store" if is_manifest else "public, max-age=300",
        "Content-Type": "application/vnd.apple.mpegurl" if is_manifest else content_type,
        "X-Resolver-Path": "original-tver-manifest" if is_manifest else "original-tver-media",
    }
    if is_manifest:
        try:
            manifest = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HTTPException(status_code=502, detail="Invalid TVer stream manifest") from error
        body = _rewrite_tver_hls_manifest(manifest, final_url).encode()
        status_code = 200

    return Response(content=body, status_code=status_code, headers=response_headers)


@app.get("/stream/tver/media")
async def proxy_tver_media(
    request: Request,
    token: str = Query(min_length=1, max_length=6000),
) -> Response:
    return await _proxy_tver_media_response(request, token)


@app.get("/stream/tver/media.{extension}")
async def proxy_tver_media_with_extension(
    request: Request,
    extension: str,
    token: str = Query(min_length=1, max_length=6000),
) -> Response:
    if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
        raise HTTPException(status_code=404, detail="Unsupported TVer stream extension")
    return await _proxy_tver_media_response(request, token)


@app.get("/stream/tiktok/media.mp4")
async def proxy_tiktok_media(
    request: Request,
    token: str = Query(min_length=1, max_length=6000),
) -> Response:
    upstream_url, source_url, cookie_header = _decode_tiktok_media_token(token)
    try:
        body, status_code, content_type, upstream_headers = await asyncio.to_thread(
            _read_tiktok_resource,
            upstream_url,
            source_url,
            cookie_header,
            request.headers.get("range"),
        )
    except HTTPError as error:
        print(f"TikTok media returned HTTP {error.code}", file=sys.stderr, flush=True)
        status = 416 if error.code == 416 else 410 if error.code in {401, 403, 404} else 502
        raise HTTPException(status_code=status, detail="The TikTok stream has expired") from error
    except (URLError, TimeoutError, ValueError, OSError) as error:
        print(f"TikTok media proxy failed: {type(error).__name__}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="Could not read the TikTok stream") from error

    return Response(
        content=body,
        status_code=status_code,
        headers={
            **upstream_headers,
            "Accept-Ranges": upstream_headers.get("Accept-Ranges", "bytes"),
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
            "Content-Type": content_type if content_type.startswith("video/") else "video/mp4",
            "X-Resolver-Path": "original-tiktok-media",
        },
    )


async def _proxy_niconico_media_response(
    request: Request,
    token: str,
) -> Response:
    upstream_url, domand_cookie = _decode_stream_token(token)
    range_header = request.headers.get("range")
    try:
        body, status_code, content_type, upstream_headers, final_url = await asyncio.to_thread(
            _read_niconico_resource,
            upstream_url,
            domand_cookie,
            range_header,
        )
    except HTTPError as error:
        print(f"NicoNico media returned HTTP {error.code}", file=sys.stderr, flush=True)
        status = 410 if error.code in {401, 403, 404} else 502
        raise HTTPException(status_code=status, detail="The stream has expired") from error
    except (URLError, TimeoutError, ValueError, OSError) as error:
        print(f"NicoNico media proxy failed: {type(error).__name__}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail="Could not read the stream") from error

    is_manifest = "mpegurl" in content_type.lower() or urlsplit(final_url).path.endswith(".m3u8")
    response_headers = {
        **upstream_headers,
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store" if is_manifest else "public, max-age=300",
        "Content-Type": "application/vnd.apple.mpegurl" if is_manifest else content_type,
        "X-Resolver-Path": "original-niconico-manifest" if is_manifest else "original-niconico-media",
    }
    if is_manifest:
        try:
            manifest = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HTTPException(status_code=502, detail="Invalid stream manifest") from error
        body = _rewrite_hls_manifest(manifest, final_url, domand_cookie).encode()
        status_code = 200

    return Response(content=body, status_code=status_code, headers=response_headers)


@app.get("/stream/niconico/{track}.{extension}")
async def proxy_named_niconico_media(
    request: Request,
    track: str,
    extension: str,
    token: str = Query(min_length=1, max_length=12000),
) -> Response:
    if track not in {"audio", "video", "media"} or not re.fullmatch(
        r"[a-z0-9]{1,8}", extension
    ):
        raise HTTPException(status_code=404, detail="Unsupported stream resource")
    return await _proxy_niconico_media_response(request, token)


@app.get("/stream/media")
async def proxy_niconico_media(
    request: Request,
    token: str = Query(min_length=1, max_length=12000),
) -> Response:
    return await _proxy_niconico_media_response(request, token)


@app.get("/stream/media.{extension}")
async def proxy_niconico_media_with_extension(
    request: Request,
    extension: str,
    token: str = Query(min_length=1, max_length=12000),
) -> Response:
    if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
        raise HTTPException(status_code=404, detail="Unsupported stream extension")
    return await _proxy_niconico_media_response(request, token)


@app.get("/resolve")
async def resolve_video(
    request: Request,
    url: str = Query(min_length=1, max_length=2048),
) -> Response:
    video_url = _validate_youtube_url(url)
    client = _request_client_key(request)
    _check_rate_limit(client)

    now = time.monotonic()
    cached = _cache.get(video_url)
    if cached and cached[0] > now:
        return _resolved_response(request, cached[1])

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
    return _resolved_response(request, direct_url)


@app.get("/youtube/info")
async def youtube_info(
    request: Request,
    url: str = Query(min_length=1, max_length=2048),
) -> JSONResponse:
    _parse_youtube_info_url(url)
    client = _request_client_key(request)
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
    client = _request_client_key(request)
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
