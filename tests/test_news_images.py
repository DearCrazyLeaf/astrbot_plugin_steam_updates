import asyncio
import unittest
import xml.etree.ElementTree as ET

from tests.test_free_games import _load_module


class _Response:
    def __init__(self, *, text="", payload=None):
        self.text = text
        self._payload = payload or {}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class NewsImageParsingTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _make_plugin(self):
        plugin = object.__new__(self.mod.SteamUpdatePush)
        plugin._steam_lang = lambda: "english"
        plugin._cfg = lambda key, default=None: default
        plugin._get_feed_timeout_sec = lambda: 10
        plugin._log_warn = lambda *args, **kwargs: None
        plugin._log_debug = lambda *args, **kwargs: None
        plugin._debug = lambda *args, **kwargs: None
        return plugin

    def test_extracts_normalized_candidates_in_source_order_without_duplicates(self):
        plugin = self._make_plugin()
        text = (
            "https://clan.fastly.steamstatic.com/images/4437469/banner.png "
            "{STEAM_CLAN_IMAGE}/4437469/banner.png "
            "{STEAM_CLAN_LOC_IMAGE}/3703047/localized.png "
            "https://clan.fastly.steamstatic.com/images/4437469/banner.png"
        )

        self.assertEqual(
            plugin._extract_news_image_candidates(text),
            [
                "https://clan.fastly.steamstatic.com/images/4437469/banner.png",
                "https://clan.fastly.steamstatic.com/images/3703047/localized/english.png",
                "https://clan.fastly.steamstatic.com/images/3703047/localized.png",
            ],
        )

    def test_legacy_image_extractor_uses_normalized_candidates(self):
        plugin = self._make_plugin()

        self.assertEqual(
            plugin._extract_image_urls("{STEAM_CLAN_IMAGE}/4437469/banner.png"),
            ["https://clan.fastly.steamstatic.com/images/4437469/banner.png"],
        )

    def test_bbcode_image_tags_do_not_become_part_of_candidates(self):
        plugin = self._make_plugin()
        fixtures = [
            (
                "[img]{STEAM_CLAN_IMAGE}/4437469/banner.png[/img]",
                ["https://clan.fastly.steamstatic.com/images/4437469/banner.png"],
            ),
            (
                "[img]{STEAM_CLAN_LOC_IMAGE}/3703047/localized.png[/img]",
                [
                    "https://clan.fastly.steamstatic.com/images/3703047/localized/english.png",
                    "https://clan.fastly.steamstatic.com/images/3703047/localized.png",
                ],
            ),
            (
                "[img]https://clan.fastly.steamstatic.com/images/4437469/banner.png[/img]",
                ["https://clan.fastly.steamstatic.com/images/4437469/banner.png"],
            ),
        ]

        for text, expected in fixtures:
            with self.subTest(text=text):
                self.assertEqual(
                    plugin._extract_news_image_candidates(text),
                    expected,
                )

    async def test_api_item_keeps_first_normalized_candidate(self):
        plugin = self._make_plugin()
        plugin._request_with_network_fallback = self._async_return(
            _Response(
                payload={
                    "appnews": {
                        "newsitems": [
                            {
                                "gid": "1",
                                "title": "Update",
                                "url": "https://example.test/update",
                                "contents": "{STEAM_CLAN_IMAGE}/4437469/banner.png",
                                "date": 1,
                            }
                        ]
                    }
                }
            )
        )

        items = await plugin._fetch_news_api("123", 1)

        self.assertEqual(
            items[0].image_url,
            "https://clan.fastly.steamstatic.com/images/4437469/banner.png",
        )

    def test_feed_candidate_prefers_escaped_img_before_enclosure(self):
        plugin = self._make_plugin()
        description = (
            '&lt;p&gt;&lt;img src="https://clan.fastly.steamstatic.com/images/4437469/banner.png"&gt;'
            "Patch notes&lt;/p&gt;"
        )
        item = ET.fromstring(
            "<item>"
            '<enclosure url="https://clan.fastly.steamstatic.com/images/3703047/localized.png" />'
            "</item>"
        )

        self.assertEqual(
            plugin._first_feed_image_url(item, description),
            "https://clan.fastly.steamstatic.com/images/4437469/banner.png",
        )

    def test_feed_candidate_falls_back_to_enclosure(self):
        plugin = self._make_plugin()
        item = ET.fromstring(
            "<item>"
            '<enclosure url="https://clan.fastly.steamstatic.com/images/3703047/localized.png" />'
            "</item>"
        )

        self.assertEqual(
            plugin._first_feed_image_url(item, "Patch notes"),
            "https://clan.fastly.steamstatic.com/images/3703047/localized.png",
        )

    async def test_feed_keeps_image_before_description_markup_is_removed(self):
        plugin = self._make_plugin()
        body = """<?xml version="1.0" encoding="UTF-8"?>
<rss><channel><item>
<title>Update</title>
<link>https://steamcommunity.com/games/123/announcements/detail/1</link>
<pubDate>Sat, 08 Aug 2026 00:00:00 +0000</pubDate>
<description>&lt;p&gt;&lt;img src="https://clan.fastly.steamstatic.com/images/4437469/banner.png"&gt;Patch notes&lt;/p&gt;</description>
</item></channel></rss>"""
        plugin._request_with_network_fallback = self._async_return(_Response(text=body))

        items = await plugin._fetch_news_feed("123", 1)

        self.assertEqual(items[0].contents, "Patch notes")
        self.assertEqual(
            items[0].image_url,
            "https://clan.fastly.steamstatic.com/images/4437469/banner.png",
        )

    async def test_llm_merge_keeps_first_source_image_reference(self):
        plugin = self._make_plugin()
        plugin._resolve_app_names = self._async_return({"123": "Game"})
        plugin._cfg = lambda key, default=None: "{content}" if key == "llm_prompt" else default
        plugin._call_llm = self._async_return("Summary")
        items = [
            self.mod.NewsItem("1", "First", "url-1", "one", 1, "123"),
            self.mod.NewsItem(
                "2",
                "Second",
                "url-2",
                "two",
                2,
                "123",
                "https://clan.fastly.steamstatic.com/images/4437469/banner.png",
            ),
        ]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        self.assertEqual(
            sections[0].updates[0].image_url,
            "https://clan.fastly.steamstatic.com/images/4437469/banner.png",
        )

    @staticmethod
    def _async_return(value):
        async def _inner(*args, **kwargs):
            return value

        return _inner


class NewsImageSelectionTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _make_plugin(self):
        plugin = object.__new__(self.mod.SteamUpdatePush)
        plugin._client = object()
        plugin._steam_lang = lambda: "english"
        plugin._cfg = lambda key, default=None: default
        plugin._prefetch_image_concurrency = lambda: 2
        plugin._trim_news_image_cache = lambda: None
        return plugin

    async def test_prefetch_uses_first_successful_candidate_per_item(self):
        plugin = self._make_plugin()
        a_first = "https://clan.fastly.steamstatic.com/images/100/a-first.png"
        a_second = "https://clan.fastly.steamstatic.com/images/100/a-second.png"
        b_first = "https://clan.fastly.steamstatic.com/images/200/b-first.png"
        b_second = "https://clan.fastly.steamstatic.com/images/200/b-second.png"
        image_a = object()
        image_b = object()
        attempts = []
        a_started = asyncio.Event()
        b_started = asyncio.Event()
        a_first_completed = False

        async def download(url):
            nonlocal a_first_completed
            attempts.append(url)
            if url == a_first:
                a_started.set()
                await asyncio.wait_for(b_started.wait(), timeout=1)
                a_first_completed = True
                return None
            if url == a_second:
                self.assertTrue(a_first_completed)
                return image_a
            if url == b_first:
                b_started.set()
                await asyncio.wait_for(a_started.wait(), timeout=1)
                return image_b
            self.fail(f"successful item requested another candidate: {url}")

        plugin._download_image = download
        sections = [
            self.mod.AppSection(
                "100",
                "Game A",
                [
                    self.mod.NewsItem(
                        "a",
                        "Announcement A",
                        "https://example.test/a",
                        f"[img]{a_first}[/img] [img]{a_second}[/img]",
                        1,
                        "100",
                        a_first,
                    )
                ],
            ),
            self.mod.AppSection(
                "200",
                "Game B",
                [
                    self.mod.NewsItem(
                        "b",
                        "Announcement B",
                        "https://example.test/b",
                        f"[img]{b_first}[/img] [img]{b_second}[/img]",
                        2,
                        "200",
                        b_first,
                    )
                ],
            ),
        ]

        result = await plugin._prefetch_images(sections)

        self.assertEqual(result, {a_second: image_a, b_first: image_b})
        self.assertLess(attempts.index(a_first), attempts.index(a_second))
        self.assertNotIn(b_second, attempts)


class NewsImageFallbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _make_plugin(self):
        plugin = object.__new__(self.mod.SteamUpdatePush)
        plugin._steam_lang = lambda: "english"
        plugin._load_font = lambda size, bold=False: _Font(size)
        plugin._wrap_blocks = lambda *args, **kwargs: []
        plugin._summarize_text = lambda text, max_chars: text
        plugin._format_time = lambda ts: ""
        plugin._scale_image = lambda image, max_w, max_h: image
        return plugin

    def test_each_announcement_uses_its_image_or_fixed_fallback(self):
        plugin = self._make_plugin()
        a_url = "https://clan.fastly.steamstatic.com/images/100/a.png"
        b_url = "https://clan.fastly.steamstatic.com/images/100/b.png"
        image_a = object()
        fixed_image = object()
        section = self.mod.AppSection(
            "100",
            "Game",
            [
                self.mod.NewsItem(
                    "a",
                    "Announcement A",
                    "",
                    f"[img]{a_url}[/img]",
                    0,
                    "100",
                    a_url,
                ),
                self.mod.NewsItem(
                    "b",
                    "Announcement B",
                    "",
                    f"[img]{b_url}[/img]",
                    0,
                    "100",
                    b_url,
                ),
                self.mod.NewsItem(
                    "c",
                    "Announcement C",
                    "",
                    "No image",
                    0,
                    "100",
                ),
            ],
        )

        blocks = plugin._build_section_blocks(
            section,
            796,
            _Font(26),
            _Font(18),
            (220, 220, 220),
            _Font(14),
            (140, 140, 140),
            (100, 180, 255),
            {a_url: image_a},
            796,
            320,
            1000,
            1,
            {"100": fixed_image},
        )

        self.assertEqual(
            [block.image for block in blocks if block.kind == "image"],
            [image_a, fixed_image, fixed_image],
        )


class _Font:
    def __init__(self, size):
        self.size = size


if __name__ == "__main__":
    unittest.main()
