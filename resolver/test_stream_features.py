import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app


class StreamFeatureTests(unittest.TestCase):
    def test_new_source_hosts(self):
        for source in (
            "https://bsky.app/profile/zapyzapzap.bsky.social/post/3mvsmf3d4jk2i",
            "https://www.bilibili.com/video/BV1pn8E6iEww/",
        ):
            self.assertEqual(app._validate_stream_source_url(source)[0], "extract")

    def test_bilibili_mux_selects_h264_at_requested_height(self):
        info = {
            "formats": [
                {"url": "https://a.bilivideo.com/360.mp4", "protocol": "https", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 360},
                {"url": "https://a.bilivideo.com/720.mp4", "protocol": "https", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 720},
                {"url": "https://a.bilivideo.com/1080.mp4", "protocol": "https", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 1080},
                {"url": "https://upos-hz-mirrorakam.akamaized.net/audio.m4a", "protocol": "https", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128},
            ]
        }
        mux_url = app._bilibili_mux_url(info, 720)
        token = parse_qs(urlsplit(mux_url).query)["token"][0]
        self.assertEqual(
            app._decode_bilibili_mux_token(token),
            ("https://a.bilivideo.com/720.mp4", "https://upos-hz-mirrorakam.akamaized.net/audio.m4a"),
        )
        with self.assertRaises(ValueError):
            app._encode_bilibili_mux_token(
                "https://example.com/video.mp4", "https://upos-hz-mirrorakam.akamaized.net/audio.m4a"
            )

    def test_pornhub_token_keeps_source_for_refresh(self):
        upstream = "https://ev.phncdn.com/video.mp4?validto=9999999999"
        source = "https://jp.pornhub.com/view_video.php?viewkey=example"
        token = app._encode_pornhub_media_token(upstream, source, "720")
        self.assertEqual(app._decode_pornhub_media_token(token), (upstream, source, "720"))
        self.assertEqual(app._bounded_tiktok_range("bytes=1000-", 2_000_000), "bytes=1000-2000999")

    def test_pornhub_reuses_bounded_media_connection(self):
        upstream = "https://ev.phncdn.com/video.mp4?validto=9999999999"

        class FakeSession:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append(kwargs["headers"]["Range"])
                kwargs["content_callback"](b"video-data")
                return SimpleNamespace(
                    url=url,
                    status_code=206,
                    reason="Partial Content",
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": "10",
                        "Content-Range": "bytes 0-9/100",
                    },
                )

        session = FakeSession()
        with patch.object(app._pornhub_session_state, "session", session, create=True):
            first = app._read_pornhub_resource(upstream, "bytes=0-")
            second = app._read_pornhub_resource(upstream, "bytes=10-")
        self.assertEqual(first[0], b"video-data")
        self.assertEqual(second[1], 206)
        self.assertEqual(session.calls, ["bytes=0-1999999", "bytes=10-2000009"])

    def test_pornhub_range_ignoring_cdn_uses_bounded_fallback(self):
        upstream = "https://ev.phncdn.com/video.mp4?validto=9999999999"

        class FakeSession:
            def get(self, url, **kwargs):
                return SimpleNamespace(url=url, status_code=200, headers={})

        expected = (b"video", 206, "video/mp4", {"Content-Range": "bytes 0-4/100"})
        with patch.object(app._pornhub_session_state, "session", FakeSession(), create=True):
            with patch.object(app, "_read_pornhub_resource_urllib", return_value=expected) as fallback:
                self.assertEqual(app._read_pornhub_resource(upstream, "bytes=0-"), expected)
        fallback.assert_called_once_with(upstream, "bytes=0-")

    def test_tver_local_segment_manifest(self):
        segment = {
            "url": "https://a.streaks.jp/video/seg_1.ts",
            "duration": 4.0,
            "key_url": "https://key.streaks.jp/encryption.key",
            "key_iv": None,
            "sequence": 1,
        }
        manifest = app._single_tver_segment_manifest(
            segment, "file:///tmp/video.ts", "file:///tmp/video.key"
        )
        self.assertIn("file:///tmp/video.ts", manifest)
        self.assertIn('URI="file:///tmp/video.key"', manifest)
        self.assertNotIn("https://a.streaks.jp", manifest)


if __name__ == "__main__":
    unittest.main()
