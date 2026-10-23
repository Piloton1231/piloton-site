import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import app


class StreamFeatureTests(unittest.TestCase):
    def test_private_head_checks_ticket_without_starting_media(self):
        import asyncio
        with patch.object(app, '_read_private_nico_ticket', return_value=('url', 'cookie')) as read, patch.object(app, '_start_niconico_mux_process') as start:
            response = asyncio.run(app.private_niconico_head('a' * 43))
        read.assert_called_once_with('a' * 43)
        start.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'video/mp4')
        self.assertNotIn('content-length', response.headers)

    def test_private_nico_proxy_not_embedded_in_ffmpeg_arguments(self):
        proxy = 'http://user:password@proxy.example:8000'
        with patch.object(app, '_validate_niconico_upstream_url', side_effect=lambda x: x), patch.object(app.subprocess, 'Popen') as popen:
            app._start_niconico_mux_process('https://delivery.domand.nicovideo.jp/master.m3u8', 'a' * 24, proxy)
        self.assertEqual(popen.call_args.kwargs['env']['https_proxy'], proxy)
        self.assertNotIn(proxy, str(popen.call_args.args))

    def test_private_nico_ticket_validation(self):
        import time
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as error:
            app._read_private_nico_ticket('../secret')
        self.assertEqual(error.exception.status_code, 404)
        with patch.object(app.Path, 'read_text', return_value=json.dumps({'expires': 0})):
            with self.assertRaises(HTTPException) as error:
                app._read_private_nico_ticket('a' * 43)
            self.assertEqual(error.exception.status_code, 410)
        self.assertIsNotNone(app.STREAM_COOKIE_PATTERN.fullmatch('a' * 20 + ':123'))
        self.assertIsNone(app.STREAM_COOKIE_PATTERN.fullmatch('a' * 20 + '\r\nX: bad'))
        self.assertIsNone(app.STREAM_COOKIE_PATTERN.fullmatch('a' * 20 + ';user_session=bad'))

    def test_instagram_uses_og_for_all_input_hosts(self):
        for host in ("www.instagram.com", "www.oginstagram.com", "kkinstagram.com"):
            self.assertEqual(app._instagram_embed_url(
                f"https://{host}/reels/Derflurx348/?tracking=1"),
                "https://www.oginstagram.com/reels/Derflurx348/")

    def test_instagram_metadata_and_redirect(self):
        from email.message import Message
        class Response:
            def __init__(self, url, content_type, body=b""):
                self.url, self.body = url, body
                self.headers = Message()
                self.headers["Content-Type"] = content_type
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return self.url
            def read(self, limit): return self.body[:limit]
        page = "https://www.oginstagram.com/reels/Derflurx348/"
        offload = "https://www.oginstagram.com/offload/Derflurx348/1?a=1&b=2"
        cdn = "https://scontent.cdninstagram.com/video.mp4"
        redirect = HTTPError(offload, 302, "redirect", {"Location": cdn}, None)
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.side_effect = [
            Response(page, "text/html", b'<meta content="https://www.oginstagram.com/offload/Derflurx348/1?a=1&amp;b=2" property="og:video">'),
            redirect, Response(cdn, "video/mp4"),
        ]
        with patch.object(app, "build_opener", return_value=opener):
            self.assertEqual(app._resolve_instagram_media(
                "https://www.instagram.com/reels/Derflurx348/"), cdn)
        self.assertEqual(opener.open.call_args_list[1].args[0].full_url, offload)

    def test_instagram_rejects_untrusted_targets(self):
        for url in ("http://127.0.0.1/private", "https://evil.example/video.mp4",
                    "https://www.oginstagram.com/arbitrary",
                    "https://www.oginstagram.com.evil.example/offload/Derflurx348/1"):
            with self.assertRaises(ValueError):
                app._validate_instagram_target(url)

    def test_label_requires_verified_channel_id_and_artist_credit(self):
        track = {'title':'How-to','artists':['Mrs. GREEN APPLE'],'duration':237}
        entry = {'id':'e1ljAkVuQCA','title':'Mrs. GREEN APPLE - How-to','duration':237,
                 'channel':'UNIVERSAL MUSIC JAPAN','channel_is_verified':True,
                 'channel_id':'UCl2aT0nRejTCQO_LHZAftBw'}
        self.assertEqual(app._rank_spotify_candidates(track,[entry],True,15)[0]['id'],entry['id'])
        self.assertEqual(app._rank_spotify_candidates(track,[{**entry,'channel_id':'fake'}],True,15),[])
        self.assertEqual(app._rank_spotify_candidates(track,[{**entry,'title':'Another Artist - How-to'}],True,15),[])

    def test_romaji_official_album_audio_beats_reupload(self):
        track = {'title':'クダリ','artists':['Mrs. GREEN APPLE'],'duration':267,'album':'Attitude'}
        entries = [
            {'id':'WX_XvmoZhQQ','title':'クダリ - Mrs. GREEN APPLE 歌詞動画','duration':267,'channel':'Other'},
            {'id':'j7MPiIabcbE','title':'Kudari','duration':267,'channel':'Mrs. GREEN APPLE',
             'channel_is_verified':True,'description':'Provided to YouTube by Universal Music Group Kudari Attitude'},
        ]
        result = app._rank_spotify_candidates(track, entries, True, 15)
        self.assertEqual(result[0]['id'], 'j7MPiIabcbE')
        self.assertTrue(result[0]['album_match'])
        self.assertFalse(result[0]['official_mv'])

    def test_official_translated_title_beats_closer_live_recording(self):
        track = {'title':'インフェルノ', 'artists':['Mrs. GREEN APPLE'], 'duration':211.6}
        entries = [
            {'id':'fSddqAez-7A', 'title':'Mrs. GREEN APPLE インフェルノ 2025 生音源', 'duration':213, 'channel':'MGA MUSIC STOCK'},
            {'id':'wfCcs0vLysk', 'title':'Mrs. GREEN APPLE - インフェルノ（Inferno）', 'duration':222,
             'channel':'Mrs. GREEN APPLE', 'channel_is_verified':True},
            {'id':'abcdefghijk', 'title':'Mrs. GREEN APPLE インフェルノ 歌詞付き', 'duration':212, 'channel':'Other'},
        ]
        result = app._rank_spotify_candidates(track, entries, True, 15)
        self.assertEqual(result[0]['id'], 'wfCcs0vLysk')
        self.assertNotIn('fSddqAez-7A', [e['id'] for e in result])

    def test_spotify_matching_excludes_covers_and_wrong_durations(self):
        track = {"title": "Test Song", "artists": ["Artist"], "duration": 200}
        entries = [
            {"id": "abcdefghijk", "title": "Artist Test Song Cover", "duration": 200},
            {"id": "abcdefghijl", "title": "Artist Test Song", "duration": 201},
            {"id": "abcdefghijm", "title": "Artist Test Song", "duration": 290},
            {"id": "abcdefghijn", "title": "Someone Else Test Song", "duration": 200},
        ]
        for entry in entries[:3]:
            entry.update(channel='Artist', channel_is_verified=True)
        result = app._rank_spotify_candidates(track, entries, True, 15)
        self.assertEqual([entry["id"] for entry in result], ["abcdefghijl"])
        self.assertEqual(len(app._rank_spotify_candidates(track, entries, False, 15)), 2)

    def test_no_unofficial_fallback_even_when_mv_preference_disabled(self):
        track = {'title':'嘘じゃないよ','artists':['Mrs. GREEN APPLE'],'duration':286}
        fan = {'id':'ONoV4KA9M60','title':'【MAD】君の名は×嘘じゃないよ Mrs.green Apple',
               'duration':286,'channel':'クモズ','channel_is_verified':True}
        spoof = {**fan, 'channel':'Mrs. GREEN APPLE', 'channel_is_verified':False}
        for prefer_mv in (True, False):
            self.assertEqual(app._rank_spotify_candidates(track,[fan,spoof],False,60,prefer_mv), [])

    def test_jya_ja_spelling_and_official_audio_selection(self):
        self.assertEqual(app._music_romaji('嘘じゃないよ'), app._music_romaji('Usojya Naiyo'))
        track = {'title':'嘘じゃないよ','artists':['Mrs. GREEN APPLE'],'duration':286,'album':'Attitude'}
        entry = {'id':'ZjjY2n_vwc0','title':'Usojya Naiyo','duration':286,
                 'channel':'Mrs. GREEN APPLE','channel_is_verified':True,
                 'description':'Provided to YouTube by Universal Music Group Usojya Naiyo Attitude'}
        result = app._rank_spotify_candidates(track,[entry],True,15)
        self.assertEqual(result[0]['id'],'ZjjY2n_vwc0')
        self.assertTrue(result[0]['official_source'])
        self.assertTrue(result[0]['album_match'])

    def test_spotify_rejects_non_track_or_external_urls(self):
        for url in ("https://example.com/track/0Lr4kGOYn9l83EjuK6cZFQ",
                    "https://open.spotify.com/playlist/0Lr4kGOYn9l83EjuK6cZFQ"):
            with self.assertRaises(app.HTTPException):
                app._spotify_track(url)

    def test_vrchat_head_routes_are_registered(self):
        for path in ("/stream", "/stream/pornhub/media.mp4", "/stream/rutube/mux.mp4"):
            self.assertTrue(
                any(
                    route.path == path and route.methods and "HEAD" in route.methods
                    for route in app.app.routes
                ),
                path,
            )

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

    def test_rutube_mux_token_only_allows_rutube_hls(self):
        upstream = "https://river-1.rutube.ru/hls/example/index.m3u8"
        mux_url = app._rutube_mux_url(upstream)
        token = parse_qs(urlsplit(mux_url).query)["token"][0]
        self.assertEqual(app._decode_rutube_mux_token(token), upstream)
        with self.assertRaises(ValueError):
            app._encode_rutube_mux_token("https://example.com/index.m3u8")
        with self.assertRaises(ValueError):
            app._encode_rutube_mux_token("https://river-1.rutube.ru/video.mp4")

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

    def test_x_fallback_selects_vrchat_compatible_mp4_quality(self):
        payload = {
            "tweet": {
                "media": {
                    "videos": [{
                        "formats": [
                            {
                                "url": "https://video.twimg.com/amplify_video/example/vid/avc1/640x360/low.mp4",
                                "container": "mp4",
                                "bitrate": 832000,
                            },
                            {
                                "url": "https://video.twimg.com/amplify_video/example/vid/avc1/1280x720/high.mp4",
                                "container": "mp4",
                                "bitrate": 2176000,
                            },
                            {
                                "url": "https://video.twimg.com/amplify_video/example/pl/master.m3u8",
                                "container": "m3u8",
                            },
                        ]
                    }]
                }
            }
        }

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def geturl(self):
                return "https://api.fxtwitter.com/zorstyx/status/2102558139359113712"

            def read(self, _limit):
                return json.dumps(payload).encode()

        source = "https://x.com/zorstyx/status/2102558139359113712?s=20"
        transient_error = HTTPError(
            "https://api.fxtwitter.com/zorstyx/status/2102558139359113712",
            404,
            "Not Found",
            {},
            None,
        )
        with patch.object(
            app, "urlopen", side_effect=[transient_error, FakeResponse(), FakeResponse()]
        ) as open_request:
            with patch.object(app.time, "sleep"):
                self.assertTrue(app._resolve_x_fallback_media(source).endswith("/high.mp4"))
                self.assertTrue(app._resolve_x_fallback_media(source, 360).endswith("/low.mp4"))
        self.assertEqual(open_request.call_count, 3)

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
