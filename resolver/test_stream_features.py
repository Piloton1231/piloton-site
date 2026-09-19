import unittest
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
