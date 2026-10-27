import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from support import _load_module


class ReverseProxyRoutingTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def setUp(self):
        self.plugin = object.__new__(self.mod.SteamUpdatePush)
        self.mode = "reverse"
        self.plugin._cfg = lambda key, default=None: (
            self.mode if key == "proxy_mode" else default
        )
        self.plugin._client = AsyncMock()
        self.plugin._ensure_http_client = AsyncMock()

    def test_reverse_proxy_rewrites_only_supported_hosts_and_preserves_path_query(self):
        cases = {
            "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=730":
                "https://steam-api.4oi.site/ISteamNews/GetNewsForApp/v2/?appid=730",
            "https://store.steampowered.com/feeds/news/app/730/?l=english":
                "https://steam-api.4oi.site/feeds/news/app/730/?l=english",
            "https://www.steamgriddb.com/api/v2/search/123":
                "https://steamgriddb.4oi.site/api/v2/search/123",
            "https://cdn2.steamgriddb.com/grid/abc.jpg":
                "https://steamgriddb.4oi.site/grid/abc.jpg",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    self.plugin._route_reverse_proxy_url(source),
                    expected,
                )

    def test_reverse_proxy_leaves_unconfigured_hosts_and_other_modes_unchanged(self):
        source = "https://steamcommunity.com/sharedfiles/filedetails/?id=123"
        self.assertEqual(self.plugin._route_reverse_proxy_url(source), source)

        self.mode = "system"
        api_url = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
        self.assertEqual(self.plugin._route_reverse_proxy_url(api_url), api_url)
        self.mode = "reverse"
        self.assertEqual(
            self.plugin._route_reverse_proxy_url(
                "ftp://api.steampowered.com/path"
            ),
            "ftp://api.steampowered.com/path",
        )

    async def test_request_uses_reverse_proxy_rewritten_url(self):
        await self.plugin._request_with_network_fallback(
            "GET",
            "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
            params={"appid": "730"},
        )

        self.plugin._client.request.assert_awaited_once_with(
            "GET",
            "https://steam-api.4oi.site/ISteamNews/GetNewsForApp/v2/",
            params={"appid": "730"},
        )

    async def test_reverse_proxy_retries_transient_network_error_once(self):
        request = httpx.Request(
            "GET",
            "https://steam-api.4oi.site/ISteamNews/GetNewsForApp/v2/",
        )
        expected = httpx.Response(200, request=request)
        self.plugin._client.request = AsyncMock(
            side_effect=[
                httpx.ConnectError("temporary network failure", request=request),
                expected,
            ]
        )

        with patch.object(self.mod.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            response = await self.plugin._request_with_network_fallback(
                "GET",
                "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
            )

        self.assertIs(response, expected)
        self.assertEqual(self.plugin._client.request.await_count, 2)
        sleep.assert_awaited_once_with(0.5)

    async def test_reverse_proxy_retries_transient_status_once(self):
        request = httpx.Request(
            "GET",
            "https://steam-api.4oi.site/ISteamNews/GetNewsForApp/v2/",
        )
        transient = httpx.Response(503, request=request)
        expected = httpx.Response(200, request=request)
        self.plugin._client.request = AsyncMock(side_effect=[transient, expected])

        with patch.object(self.mod.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            response = await self.plugin._request_with_network_fallback(
                "GET",
                "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
            )

        self.assertIs(response, expected)
        self.assertTrue(transient.is_closed)
        self.assertEqual(self.plugin._client.request.await_count, 2)
        sleep.assert_awaited_once_with(0.5)

    async def test_reverse_proxy_does_not_retry_non_transient_status(self):
        request = httpx.Request(
            "GET",
            "https://steam-api.4oi.site/ISteamNews/GetNewsForApp/v2/",
        )
        response = httpx.Response(404, request=request)
        self.plugin._client.request = AsyncMock(return_value=response)

        with patch.object(self.mod.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await self.plugin._request_with_network_fallback(
                "GET",
                "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
            )

        self.assertIs(result, response)
        self.plugin._client.request.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_reverse_proxy_does_not_retry_non_get_requests(self):
        request = httpx.Request(
            "POST",
            "https://steam-api.4oi.site/ISteamNews/GetNewsForApp/v2/",
        )
        self.plugin._client.request = AsyncMock(
            side_effect=httpx.ConnectError(
                "temporary network failure",
                request=request,
            )
        )

        with self.assertRaises(httpx.ConnectError):
            await self.plugin._request_with_network_fallback(
                "POST",
                "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
            )

        self.plugin._client.request.assert_awaited_once()

    async def test_reverse_proxy_does_not_fall_back_to_forward_proxy_after_retry_exhausted(self):
        request = httpx.Request(
            "GET",
            "https://steam-api.4oi.site/ISteamNews/GetNewsForApp/v2/",
        )
        self.plugin._client.request = AsyncMock(
            side_effect=httpx.ConnectError("proxy unavailable", request=request)
        )
        self.plugin._proxy_url = lambda: "http://127.0.0.1:7890"

        with patch.object(self.mod.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(httpx.ConnectError):
                await self.plugin._request_with_network_fallback(
                    "GET",
                    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                )

        self.assertEqual(self.plugin._client.request.await_count, 2)

    def test_steamgriddb_images_are_allowed_by_image_url_guard(self):
        self.assertTrue(
            self.mod.SteamUpdatePush._is_allowed_image_url(
                "https://cdn2.steamgriddb.com/grid/abc.jpg"
            )
        )

    def test_reverse_mode_does_not_inherit_environment_forward_proxy(self):
        with patch.object(self.mod.httpx, "AsyncClient") as async_client:
            self.plugin._build_http_client()

        self.assertFalse(async_client.call_args.kwargs["trust_env"])

    def test_proxy_mode_schema_lists_reverse_option(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        options = schema["network_proxy"]["items"]["proxy_mode"]["options"]

        self.assertEqual(options, ["off", "system", "custom", "reverse"])


if __name__ == "__main__":
    unittest.main()
