"""SSH-only ticket issuer. Never expose this command through a public API."""
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

from yt_dlp.cookies import extract_cookies_from_browser
from app import JAPAN_PROXY_URL, _create_niconico_session


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r'sm\d+|so\d+|nm\d+', sys.argv[1]):
        raise SystemExit('Usage: private_niconico.py VIDEO_ID')
    if not JAPAN_PROXY_URL:
        raise SystemExit('Japan proxy is required')
    jar = extract_cookies_from_browser('chrome', '/browser-profile', keyring='BASICTEXT')
    for cookie in list(jar):
        if cookie.domain.lstrip('.') != 'nicovideo.jp':
            jar.clear(cookie.domain, cookie.path, cookie.name)
    if not any(c.name == 'user_session' for c in jar):
        raise SystemExit('Log in to NicoNico in the VPS browser first')
    master, cookie = _create_niconico_session(sys.argv[1], jar, JAPAN_PROXY_URL)
    folder = Path('/run/private-niconico')
    now = time.time()
    for old in folder.glob('*.json'):
        if old.stat().st_mtime < now - 7200:
            old.unlink()
    token = secrets.token_urlsafe(32)
    path = folder / (token + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'master': master, 'cookie': cookie, 'expires': now + 3600}, f)
    os.chown(path, 10001, 10001)
    print('https://video.piloton.cc/private/niconico/' + token + '.mp4')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never log cookies, proxy credentials, or upstream signed URLs.
        print('Private ticket creation failed: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
