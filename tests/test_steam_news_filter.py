import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from support import _load_module


class SteamNewsFilterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.object(self.m.StarTools, "get_data_dir", return_value=Path(self.tmp.name)):
            self.p = self.m.Main(None, {})
        self.url = "https://steamstore-a.akamaihd.net/news/externalpost/steam_community_announcements/1847286114044216"

    def response(self, *, data=None, text=None):
        return httpx.Response(200, json=data, text=text, request=httpx.Request("GET", "https://example.test"))

    async def test_api_retains_official_announcement_marked_external(self):
        self.p._request_with_network_fallback = AsyncMock(return_value=self.response(data={
            "appnews": {"newsitems": [
                {"gid": "1847286114044216", "url": self.url, "is_external_url": True,
                 "feedname": "steam_community_announcements", "title": "Official", "contents": "Body", "date": 123},
                {"gid": "media", "url": "https://steamstore-a.akamaihd.net/news/externalpost/PCGamer/123", "is_external_url": True},
            ]}
        }))
        items = await self.p._fetch_news_api("236390", 50)
        self.assertEqual([(i.gid, i.title, i.contents) for i in items], [("1847286114044216", "Official", "Body")])

    def test_filter_rejects_media_and_deceptive_official_links(self):
        for url in (
            "https://evil.test/news/externalpost/steam_community_announcements/123",
            "https://steamstore-a.akamaihd.net.evil.test/news/externalpost/steam_community_announcements/123",
            "https://store.steampowered.com/news/externalpost/PCGamer/123",
            "https://store.steampowered.com/news/externalpost/steam_community_announcements/123/extra",
        ):
            with self.subTest(url=url):
                self.assertTrue(self.p._is_external_news(url, True))
        self.assertFalse(self.p._is_external_news("https://store.steampowered.com/news/app/236390/view/123"))
        for host in ("steamstore-a.akamaihd.net", "store.steampowered.com"):
            for suffix in ("", "/"):
                self.assertFalse(self.p._is_external_news(
                    f"https://{host}/news/externalpost/steam_community_announcements/123{suffix}", True
                ))

    async def test_feed_retains_official_bridge_and_uses_same_gid_as_api(self):
        xml = f"""<rss><channel><item><title>Official</title><link>{self.url}</link>
        <description>Body</description><pubDate>Mon, 26 Oct 2026 08:00:00 GMT</pubDate></item>
        <item><title>Media</title><link>https://store.steampowered.com/news/externalpost/PCGamer/123</link></item>
        </channel></rss>"""
        self.p._request_with_network_fallback = AsyncMock(return_value=self.response(text=xml))
        items = await self.p._fetch_news_feed("236390", 50)
        self.assertEqual([i.gid for i in items], ["1847286114044216"])
