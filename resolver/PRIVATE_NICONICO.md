# Private NicoNico playback

The public `/stream` route remains anonymous. It never reads the browser profile.
Ticket issuance is SSH-only; there is deliberately no public issuance API.

On the VPS, with the existing Japanese proxy configured and NicoNico logged in
in `/home/nico-browser/profile`, run:

```sh
python3 /opt/piloton-site/resolver/issue_private_niconico.py sm46647529
```

The short-lived issuer container has a read-only browser profile mount. It keeps
only NicoNico cookies in memory and writes an opaque playback ticket, not the
account login cookies. Resource limits: 384 MiB, 0.5 CPU, 64 processes.
Close the login browser before testing on the 2 GiB VPS.

Tickets live in `/opt/piloton-private-niconico`, directory mode 0700, owned by
UID/GID 10001. Files are mode 0600. The public resolver mounts it read-only.
Playback uses the Japanese proxy and an existing media-specific session cookie.
Links expire one hour after issuance, may expire earlier upstream, and stop
working if their ticket file is removed. Old expired ticket files are pruned on
subsequent issuance. At most two private streams run concurrently; stalled
streams stop after 30 seconds without output, and each run is limited to one hour.

Anyone holding a playback link can play that single video until expiration.
Treat links as bearer credentials; do not publish them or submit them to third
party link checkers. Account credentials and upstream session data are not in
the link. This is not per-viewer authentication. Seeking support depends on the
player; this fragmented MP4 is intended for Live/Stream playback.

Never print exception command lines, Cookie headers, browser cookies, or proxy
credentials while debugging. Revocation: remove only the specific ticket file;
to revoke the login itself, use NicoNico's session/account settings.
