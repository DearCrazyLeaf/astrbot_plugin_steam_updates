import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import TZPATH


PLUGIN_PATH = Path("/mnt/s/Projects/astrbot_plugin_steam_updates/main.py")
MODULE_NAME = "steam_updates_main_under_test"


def _install_stub_modules() -> None:
    if "astrbot" in sys.modules:
        return

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

        def exception(self, *args, **kwargs):
            return None

    def _identity_decorator(*args, **kwargs):
        def _wrap(func):
            return func

        return _wrap

    class _EventMessageType:
        GROUP_MESSAGE = "group"

    filter_obj = types.SimpleNamespace(
        EventMessageType=_EventMessageType,
        command=_identity_decorator,
        event_message_type=_identity_decorator,
    )

    class _Star:
        def __init__(self, context=None):
            self.context = context

    class _StarTools:
        @staticmethod
        def get_data_dir(plugin_name=None):
            return Path("/tmp/astrbot_plugin_steam_updates_tests")

    class _AstrBotConfig(dict):
        pass

    class _Plain:
        def __init__(self, text=""):
            self.text = text

    class _Image:
        def __init__(self, file=""):
            self.file = file

    class _MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

    class _AstrMessageEvent:
        pass

    class _AiocqhttpMessageEvent:
        pass

    astrbot_mod = types.ModuleType("astrbot")
    astrbot_api_mod = types.ModuleType("astrbot.api")
    astrbot_api_mod.logger = _Logger()

    astrbot_api_event_mod = types.ModuleType("astrbot.api.event")
    astrbot_api_event_mod.filter = filter_obj

    astrbot_api_star_mod = types.ModuleType("astrbot.api.star")
    astrbot_api_star_mod.Context = object
    astrbot_api_star_mod.Star = _Star
    astrbot_api_star_mod.StarTools = _StarTools

    astrbot_core_config_mod = types.ModuleType("astrbot.core.config.astrbot_config")
    astrbot_core_config_mod.AstrBotConfig = _AstrBotConfig

    astrbot_message_components_mod = types.ModuleType("astrbot.core.message.components")
    astrbot_message_components_mod.Image = _Image
    astrbot_message_components_mod.Plain = _Plain

    astrbot_message_result_mod = types.ModuleType(
        "astrbot.core.message.message_event_result"
    )
    astrbot_message_result_mod.MessageChain = _MessageChain

    astrbot_event_mod = types.ModuleType("astrbot.core.platform.astr_message_event")
    astrbot_event_mod.AstrMessageEvent = _AstrMessageEvent

    astrbot_aiocq_mod = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    astrbot_aiocq_mod.AiocqhttpMessageEvent = _AiocqhttpMessageEvent

    pil_mod = types.ModuleType("PIL")
    pil_image_mod = types.ModuleType("PIL.Image")
    pil_draw_mod = types.ModuleType("PIL.ImageDraw")
    pil_font_mod = types.ModuleType("PIL.ImageFont")

    class _PilImage:
        pass

    class _Draw:
        pass

    class _Font:
        pass

    pil_image_mod.Image = _PilImage
    pil_draw_mod.ImageDraw = _Draw
    pil_font_mod.FreeTypeFont = _Font

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = astrbot_api_mod
    sys.modules["astrbot.api.event"] = astrbot_api_event_mod
    sys.modules["astrbot.api.star"] = astrbot_api_star_mod
    sys.modules["astrbot.core.config.astrbot_config"] = astrbot_core_config_mod
    sys.modules["astrbot.core.message.components"] = astrbot_message_components_mod
    sys.modules["astrbot.core.message.message_event_result"] = astrbot_message_result_mod
    sys.modules["astrbot.core.platform.astr_message_event"] = astrbot_event_mod
    sys.modules[
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    ] = astrbot_aiocq_mod
    sys.modules["PIL"] = pil_mod
    sys.modules["PIL.Image"] = pil_image_mod
    sys.modules["PIL.ImageDraw"] = pil_draw_mod
    sys.modules["PIL.ImageFont"] = pil_font_mod


def _load_module():
    _install_stub_modules()
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class FreeGamesLogicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _make_plugin(self):
        plugin = object.__new__(self.mod.SteamUpdatePush)
        plugin._trace_seq = 0
        plugin._log_warn = lambda *args, **kwargs: None
        plugin._log_debug = lambda *args, **kwargs: None
        plugin._debug = lambda *args, **kwargs: None
        plugin._cfg = lambda key, default=None: default
        plugin._format_time = lambda ts: "2026/04/07 12:00" if ts else ""
        return plugin

    async def _async_noop(self):
        return None

    def _async_return(self, value):
        async def _inner(*args, **kwargs):
            return value

        return _inner

    def _make_poll_plugin(
        self,
        *,
        updates_by_app,
        free_items,
        previous_free_gids,
        workshop_updates=None,
    ):
        plugin = self._make_plugin()
        config = {
            "enable_push": True,
            "notify_group_ids": ["10001"],
            "steam_appids": ["730"],
            "message_mode": "card",
            "free_games_enable": True,
            "workshop_enable": bool(workshop_updates),
            "workshop_item_ids": ["ws1"] if workshop_updates else [],
        }
        plugin._cfg = lambda key, default=None: config.get(key, default)
        plugin._ensure_http_client = self._async_noop
        plugin._normalize_appids = lambda values: ["730"]
        plugin._normalize_workshop_item_ids = lambda values: ["ws1"] if workshop_updates else []
        plugin._workshop_enabled = lambda: bool(workshop_updates)
        plugin._get_max_days = lambda: 1
        plugin._filter_recent_days = lambda items, max_days: list(items)
        plugin._load_state = lambda: {
            "free_games_active_gids": json.dumps(previous_free_gids, ensure_ascii=False),
        }
        plugin._save_state = lambda state: setattr(plugin, "_saved_state", state)
        plugin._fetch_news = (
            self._async_return([self.mod.NewsItem("n1", "Patch", "u1", "body", 100)])
            if updates_by_app
            else self._async_return([])
        )
        plugin._collect_workshop_updates_for_poll = self._async_return(
            (workshop_updates or [], {})
        )
        plugin._fetch_free_game_items = self._async_return(free_items)
        plugin._build_sections = (
            self._async_return(
                [self.mod.AppSection("730", "Counter-Strike 2", updates_by_app["730"])]
            )
            if updates_by_app
            else self._async_return([])
        )
        plugin._build_workshop_sections = self._async_return(
            [self.mod.AppSection("workshop:730", "创意工坊", workshop_updates or [])]
        )
        plugin._render_payloads = []

        async def _render_card(sections, publish_text, query_text, card_kind="game"):
            plugin._render_payloads.append((card_kind, sections))
            return b"img"

        plugin._render_card = _render_card
        plugin._push_image = self._async_return(True)
        plugin._push_text = self._async_return(True)
        plugin._next_trace_id = lambda prefix: f"{prefix}-test"
        return plugin

    def test_format_free_game_title_appends_official_name(self):
        plugin = self._make_plugin()
        title = plugin._format_free_game_title("Portal", "传送门")
        self.assertEqual(title, "Portal（传送门）")

    def test_format_free_game_title_skips_duplicate_name(self):
        plugin = self._make_plugin()
        title = plugin._format_free_game_title("传送门", "传送门")
        self.assertEqual(title, "传送门")

    def test_is_free_game_active_requires_future_end_time(self):
        plugin = self._make_plugin()
        active = plugin._is_free_game_active(
            {
                "status": "Active",
                "end_date": "2099-12-31 23:59:59",
            },
            now_ts=1_775_536_800,
        )
        expired = plugin._is_free_game_active(
            {
                "status": "Active",
                "end_date": "2020-01-01 00:00:00",
            },
            now_ts=1_775_536_800,
        )
        self.assertTrue(active)
        self.assertFalse(expired)

    def test_is_free_game_active_uses_utc_source_time_independent_of_display_timezone(self):
        plugin = self._make_plugin()
        plugin._cfg = lambda key, default=None: {"display_timezone": "Asia/Shanghai"}.get(key, default)
        before_deadline = datetime(2026, 4, 10, 7, 59, 59, tzinfo=timezone.utc).timestamp()
        at_deadline = datetime(2026, 4, 10, 8, 0, 0, tzinfo=timezone.utc).timestamp()

        active = plugin._is_free_game_active(
            {"status": "Active", "end_date": "2026-04-10 08:00:00"},
            now_ts=before_deadline,
        )
        expired = plugin._is_free_game_active(
            {"status": "Active", "end_date": "2026-04-10 08:00:00"},
            now_ts=at_deadline,
        )

        self.assertTrue(active)
        self.assertFalse(expired)

    def test_free_game_entry_to_news_maps_required_fields(self):
        plugin = self._make_plugin()
        item = plugin._free_game_entry_to_news(
            {
                "id": 9001,
                "title": "Portal",
                "worth": "$19.99",
                "thumbnail": "https://example.com/portal.jpg",
                "open_giveaway_url": "https://example.com/giveaway/portal",
                "published_date": "2026-04-07 08:00:00",
                "end_date": "2026-04-10 08:00:00",
                "instructions": "Install and claim",
                "status": "Active",
            },
            official_name="传送门",
        )
        self.assertEqual(item.gid, "9001")
        self.assertEqual(item.title, "Portal（传送门）")
        self.assertEqual(item.url, "https://example.com/giveaway/portal")
        self.assertEqual(item.image_url, "https://example.com/portal.jpg")
        self.assertIn("截止时间:", item.contents)
        self.assertIn("原价:", item.contents)

    def test_free_game_entry_to_news_compacts_contents_and_formats_deadline_in_display_timezone(self):
        plugin = self._make_plugin()
        config = {"display_timezone": "Asia/Shanghai"}
        plugin._cfg = lambda key, default=None: config.get(key, default)

        item = plugin._free_game_entry_to_news(
            {
                "id": 9001,
                "title": "Portal",
                "worth": "$19.99",
                "thumbnail": "https://example.com/portal.jpg",
                "open_giveaway_url": "https://example.com/giveaway/portal",
                "published_date": "2026-04-07 08:00:00",
                "end_date": "2026-04-10 08:00:00",
                "instructions": "Install and claim",
                "status": "Active",
            },
            official_name="传送门",
        )

        self.assertIn("截止时间: 2026/04/10 16:00", item.contents)
        self.assertIn("原价: $19.99", item.contents)
        self.assertNotIn("领取方式:", item.contents)
        self.assertNotIn("活动链接:", item.contents)

    def test_build_text_message_omits_publish_time_and_link_for_free_games(self):
        plugin = self._make_plugin()
        sections = [
            self.mod.AppSection(
                "free_games",
                "限时免费领取",
                [
                    self.mod.NewsItem(
                        "9001",
                        "Portal（传送门）",
                        "https://store.steampowered.com/app/400/",
                        "截止时间: 2026/04/10 16:00\n原价: $19.99",
                        1_775_536_800,
                    )
                ],
            )
        ]

        text = plugin._build_text_message(sections, "2026/04/07 12:00")

        self.assertIn("Portal（传送门）", text)
        self.assertIn("截止时间: 2026/04/10 16:00", text)
        self.assertIn("原价: $19.99", text)
        self.assertNotIn("发布于：", text)
        self.assertNotIn("链接：", text)

    def test_build_section_blocks_omits_publish_time_and_link_for_free_games(self):
        plugin = self._make_plugin()
        plugin._load_font = lambda size, bold=False: types.SimpleNamespace(size=size)
        plugin._wrap_blocks = (
            lambda text, font, color, max_width: [self.mod.RenderBlock("text", text, font, color, 0)] if text else []
        )
        plugin._summarize_text = lambda text, max_chars: text
        plugin._scale_image = lambda img, width, height: img

        section_title_font = types.SimpleNamespace(size=26)
        body_font = types.SimpleNamespace(size=18)
        small_font = types.SimpleNamespace(size=14)
        blocks = plugin._build_section_blocks(
            self.mod.AppSection(
                "free_games",
                "限时免费领取",
                [
                    self.mod.NewsItem(
                        "9001",
                        "Portal（传送门）",
                        "https://store.steampowered.com/app/400/",
                        "截止时间: 2026/04/10 16:00\n原价: $19.99",
                        1_775_536_800,
                    )
                ],
            ),
            600,
            section_title_font,
            body_font,
            (255, 255, 255),
            small_font,
            (150, 150, 150),
            (102, 192, 244),
            {},
            600,
            300,
            800,
            1,
            {},
        )

        texts = [block.text for block in blocks if block.kind == "text" and block.text]
        self.assertIn("Portal（传送门）", texts)
        self.assertIn("截止时间: 2026/04/10 16:00\n原价: $19.99", texts)
        self.assertFalse(any(text.startswith("发布于：") for text in texts))
        self.assertNotIn("https://store.steampowered.com/app/400/", texts)

    def test_build_text_message_trims_legacy_free_game_fields(self):
        plugin = self._make_plugin()
        sections = [
            self.mod.AppSection(
                "free_games",
                "限时免费领取",
                [
                    self.mod.NewsItem(
                        "9001",
                        "Portal（传送门）",
                        "https://store.steampowered.com/app/400/",
                        "\n".join(
                            [
                                "截止时间: 2026/04/10 16:00",
                                "原价: $19.99",
                                "领取方式: 1. Click the button to visit the giveaway page.",
                                "活动链接: https://store.steampowered.com/app/400/",
                            ]
                        ),
                        1_775_536_800,
                    )
                ],
            )
        ]

        text = plugin._build_text_message(sections, "2026/04/07 12:00")

        self.assertIn("截止时间: 2026/04/10 16:00", text)
        self.assertIn("原价: $19.99", text)
        self.assertNotIn("领取方式:", text)
        self.assertNotIn("活动链接:", text)
        self.assertNotIn("Click the button to visit the giveaway page.", text)

    def test_build_section_blocks_trims_legacy_free_game_fields(self):
        plugin = self._make_plugin()
        plugin._load_font = lambda size, bold=False: types.SimpleNamespace(size=size)
        plugin._wrap_blocks = (
            lambda text, font, color, max_width: [self.mod.RenderBlock("text", text, font, color, 0)] if text else []
        )
        plugin._summarize_text = lambda text, max_chars: text
        plugin._scale_image = lambda img, width, height: img

        section_title_font = types.SimpleNamespace(size=26)
        body_font = types.SimpleNamespace(size=18)
        small_font = types.SimpleNamespace(size=14)
        blocks = plugin._build_section_blocks(
            self.mod.AppSection(
                "free_games",
                "限时免费领取",
                [
                    self.mod.NewsItem(
                        "9001",
                        "Portal（传送门）",
                        "https://store.steampowered.com/app/400/",
                        "\n".join(
                            [
                                "截止时间: 2026/04/10 16:00",
                                "原价: $19.99",
                                "领取方式: 1. Click the button to visit the giveaway page.",
                                "活动链接: https://store.steampowered.com/app/400/",
                            ]
                        ),
                        1_775_536_800,
                    )
                ],
            ),
            600,
            section_title_font,
            body_font,
            (255, 255, 255),
            small_font,
            (150, 150, 150),
            (102, 192, 244),
            {},
            600,
            300,
            800,
            1,
            {},
        )

        texts = [block.text for block in blocks if block.kind == "text" and block.text]
        self.assertIn("截止时间: 2026/04/10 16:00\n原价: $19.99", texts)
        self.assertFalse(any("领取方式:" in text for text in texts))
        self.assertFalse(any("活动链接:" in text for text in texts))

    def test_get_display_timezone_uses_system_timezone_when_config_empty(self):
        plugin = self._make_plugin()
        plugin._cfg = lambda key, default=None: {"display_timezone": ""}.get(key, default)

        tzinfo = plugin._get_display_timezone()
        expected_offset = datetime.now().astimezone().utcoffset()
        actual_offset = datetime.now(tzinfo).utcoffset()

        self.assertEqual(actual_offset, expected_offset)

    def test_get_display_timezone_falls_back_to_system_when_value_invalid(self):
        plugin = self._make_plugin()
        warnings = []
        plugin._cfg = lambda key, default=None: {"display_timezone": "Mars/Olympus"}.get(key, default)
        plugin._log_warn = lambda *args, **kwargs: warnings.append((args, kwargs))

        tzinfo = plugin._get_display_timezone()
        expected_offset = datetime.now().astimezone().utcoffset()
        actual_offset = datetime.now(tzinfo).utcoffset()

        self.assertEqual(actual_offset, expected_offset)
        self.assertTrue(warnings)

    def test_get_display_timezone_uses_tz_env_with_dst_rules(self):
        if not hasattr(time, "tzset"):
            self.skipTest("tzset unavailable")

        plugin = self._make_plugin()
        plugin._cfg = lambda key, default=None: {"display_timezone": ""}.get(key, default)
        original_tz = os.environ.get("TZ")

        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()

            winter_ts = int(
                datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc).timestamp()
            )
            summer_ts = int(
                datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc).timestamp()
            )

            self.assertEqual(
                plugin._format_free_game_time(winter_ts),
                "2026/01/10 07:00",
            )
            self.assertEqual(
                plugin._format_free_game_time(summer_ts),
                "2026/07/10 08:00",
            )
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

    def test_get_display_timezone_falls_back_to_localtime_file_rules(self):
        zoneinfo_source = None
        for base in TZPATH:
            candidate = Path(base) / "America/New_York"
            if candidate.is_file():
                zoneinfo_source = candidate
                break
        if zoneinfo_source is None:
            self.skipTest("America/New_York zoneinfo file unavailable")

        plugin = self._make_plugin()
        plugin._cfg = lambda key, default=None: {"display_timezone": ""}.get(key, default)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            localtime_path = tmpdir_path / "localtime"
            localtime_path.write_bytes(zoneinfo_source.read_bytes())

            def _fake_path(value):
                text = str(value)
                if text == "/etc/localtime":
                    return localtime_path
                if text in {"/etc/timezone", "/etc/sysconfig/clock", "/etc/conf.d/clock"}:
                    return tmpdir_path / text.strip("/").replace("/", "_")
                return Path(value)

            with patch.dict(self.mod.os.environ, {"TZ": ""}, clear=False):
                with patch.object(self.mod, "Path", new=_fake_path):
                    winter_ts = int(
                        datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc).timestamp()
                    )
                    summer_ts = int(
                        datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc).timestamp()
                    )

                    self.assertEqual(
                        plugin._format_free_game_time(winter_ts),
                        "2026/01/10 07:00",
                    )
                    self.assertEqual(
                        plugin._format_free_game_time(summer_ts),
                        "2026/07/10 08:00",
                    )

    def test_free_game_entry_prefers_resolved_store_url_and_appid(self):
        plugin = self._make_plugin()
        item = plugin._free_game_entry_to_news(
            {
                "id": 9002,
                "title": "Chamber Survival",
                "_store_url": "https://store.steampowered.com/app/2943780/Chamber_Survival/",
                "open_giveaway_url": "https://www.gamerpower.com/open/chamber-survival-steam-giveaway",
                "published_date": "2026-04-07 08:00:00",
                "end_date": "2026-04-10 08:00:00",
                "status": "Active",
            }
        )
        self.assertEqual(item.url, "https://store.steampowered.com/app/2943780/Chamber_Survival/")
        self.assertEqual(item.appid, "2943780")

    def test_free_game_entry_fallback_gid_uses_end_date(self):
        plugin = self._make_plugin()
        first = plugin._free_game_entry_to_news(
            {
                "title": "Portal",
                "published_date": "2026-04-07 08:00:00",
                "end_date": "2026-04-10 08:00:00",
                "status": "Active",
            }
        )
        second = plugin._free_game_entry_to_news(
            {
                "title": "Portal",
                "published_date": "2026-04-07 08:00:00",
                "end_date": "2026-04-11 08:00:00",
                "status": "Active",
            }
        )

        self.assertNotEqual(first.gid, second.gid)

    def test_split_new_free_game_items_detects_unseen_entries(self):
        plugin = self._make_plugin()
        items = [
            self.mod.NewsItem("old", "Old", "u1", "c1", 100),
            self.mod.NewsItem("new", "New", "u2", "c2", 200),
        ]
        new_items, snapshot = plugin._split_new_free_game_items(items, ["old"])
        self.assertEqual([item.gid for item in new_items], ["new"])
        self.assertEqual(snapshot, ["old", "new"])

    def test_split_new_free_game_items_treats_reopened_activity_as_new_gid(self):
        plugin = self._make_plugin()
        current = [
            self.mod.NewsItem(
                "giveaway-2026-04-12",
                "Portal",
                "https://store.steampowered.com/app/400/",
                "c1",
                200,
                appid="400",
            )
        ]

        new_items, snapshot = plugin._split_new_free_game_items(
            current,
            ["giveaway-2026-04-01"],
        )

        self.assertEqual([item.gid for item in new_items], ["giveaway-2026-04-12"])
        self.assertEqual(snapshot, ["giveaway-2026-04-12"])

    def test_select_poll_free_game_items_appends_active_items_when_game_updates_exist(self):
        plugin = self._make_plugin()
        active = [self.mod.NewsItem("free-old", "Portal", "u1", "c1", 100)]
        new = [self.mod.NewsItem("free-new", "Portal", "u2", "c2", 200)]

        selection = plugin._select_poll_free_game_items(
            has_game_updates=True,
            active_items=active,
            new_items=new,
        )

        self.assertEqual(
            [item.gid for item in selection.attached_items],
            ["free-old"],
        )
        self.assertEqual(selection.standalone_items, [])

    def test_select_poll_free_game_items_uses_new_items_for_standalone_push(self):
        plugin = self._make_plugin()
        active = [self.mod.NewsItem("free-old", "Portal", "u1", "c1", 100)]
        new = [self.mod.NewsItem("free-new", "Portal", "u2", "c2", 200)]

        selection = plugin._select_poll_free_game_items(
            has_game_updates=False,
            active_items=active,
            new_items=new,
        )

        self.assertEqual(selection.attached_items, [])
        self.assertEqual(
            [item.gid for item in selection.standalone_items],
            ["free-new"],
        )

    def test_manual_query_appends_free_games_after_game_fallback(self):
        plugin = self._make_plugin()
        config = {
            "steam_appids": ["730"],
            "message_mode": "card",
            "free_games_enable": True,
            "free_games_manual_only_when_no_news": False,
            "workshop_enable": False,
            "workshop_item_ids": [],
        }
        plugin._cfg = lambda key, default=None: config.get(key, default)
        plugin._ensure_http_client = self._async_noop
        plugin._normalize_appids = lambda values: ["730"]
        plugin._normalize_workshop_item_ids = lambda values: []
        plugin._workshop_enabled = lambda: False
        plugin._get_max_days = lambda: 1
        fallback_item = self.mod.NewsItem("fallback", "Older Patch", "u1", "body", 100)

        async def _fetch_news(appid, count, only_today=True):
            return [] if only_today else [fallback_item]

        plugin._fetch_news = _fetch_news
        plugin._fetch_workshop_news_items = self._async_return([])
        plugin._fetch_free_game_items = self._async_return(
            [self.mod.NewsItem("free-new", "Portal", "u2", "c2", 200)]
        )
        plugin._build_sections = self._async_return(
            [self.mod.AppSection("730", "Counter-Strike 2", [fallback_item])]
        )
        plugin._build_workshop_sections = self._async_return([])
        plugin._render_payloads = []

        async def _render_card(
            sections,
            publish_text,
            query_text,
            notice="",
            card_kind="game",
        ):
            plugin._render_payloads.append((card_kind, sections))
            return b"img"

        plugin._render_card = _render_card

        results, err = asyncio.run(plugin._manual_query(query_kind="game"))

        self.assertIsNone(err)
        self.assertEqual(results, [b"img"])
        self.assertEqual(
            [section.appid for section in plugin._render_payloads[0][1]],
            ["730", "free_games"],
        )

    def test_merge_sections_returns_free_only_when_enabled(self):
        plugin = self._make_plugin()
        free_items = [self.mod.NewsItem("new", "Portal", "u", "c", 200)]
        merged = plugin._merge_game_sections_with_free_games(
            game_sections=[],
            free_game_items=free_items,
            free_only_when_no_news=True,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].appid, "free_games")
        self.assertEqual(merged[0].title, "限时免费领取")

    def test_merge_sections_appends_free_section_after_game_updates(self):
        plugin = self._make_plugin()
        game_sections = [
            self.mod.AppSection(
                appid="730",
                title="Counter-Strike 2",
                updates=[self.mod.NewsItem("g1", "Patch Notes", "u1", "c1", 100)],
            )
        ]
        free_items = [self.mod.NewsItem("new", "Portal", "u", "c", 200)]
        merged = plugin._merge_game_sections_with_free_games(
            game_sections=game_sections,
            free_game_items=free_items,
            free_only_when_no_news=True,
        )
        self.assertEqual([section.appid for section in merged], ["730", "free_games"])

    def test_merge_sections_skips_free_only_when_disabled_and_no_news(self):
        plugin = self._make_plugin()
        free_items = [self.mod.NewsItem("new", "Portal", "u", "c", 200)]
        merged = plugin._merge_game_sections_with_free_games(
            game_sections=[],
            free_game_items=free_items,
            free_only_when_no_news=False,
        )
        self.assertEqual(merged, [])

    def test_poll_appends_active_free_games_to_game_push(self):
        free_items = [
            self.mod.NewsItem("free-old", "Portal", "u1", "c1", 200),
            self.mod.NewsItem("free-new", "Half-Life", "u2", "c2", 300),
        ]
        plugin = self._make_poll_plugin(
            updates_by_app={"730": [self.mod.NewsItem("n1", "Patch", "u1", "body", 100)]},
            free_items=free_items,
            previous_free_gids=["free-old"],
            workshop_updates=[],
        )

        asyncio.run(plugin._poll_once())

        self.assertEqual(len(plugin._render_payloads), 1)
        card_kind, sections = plugin._render_payloads[0]
        self.assertEqual(card_kind, "game")
        self.assertEqual([section.appid for section in sections], ["730", "free_games"])
        self.assertEqual([item.gid for item in sections[-1].updates], ["free-old", "free-new"])

    def test_poll_does_not_append_seen_free_games_to_workshop_push(self):
        free_items = [self.mod.NewsItem("free-old", "Portal", "u1", "c1", 200)]
        workshop_items = [self.mod.NewsItem("ws1", "Workshop Patch", "u2", "c2", 300)]
        plugin = self._make_poll_plugin(
            updates_by_app={},
            free_items=free_items,
            previous_free_gids=["free-old"],
            workshop_updates=workshop_items,
        )

        asyncio.run(plugin._poll_once())

        self.assertEqual(len(plugin._render_payloads), 1)
        card_kind, sections = plugin._render_payloads[0]
        self.assertEqual(card_kind, "workshop")
        self.assertEqual([section.appid for section in sections], ["workshop:730"])

    def test_poll_keeps_standalone_free_games_separate_from_workshop_push(self):
        free_items = [self.mod.NewsItem("free-new", "Portal", "u1", "c1", 200)]
        workshop_items = [self.mod.NewsItem("ws1", "Workshop Patch", "u2", "c2", 300)]
        plugin = self._make_poll_plugin(
            updates_by_app={},
            free_items=free_items,
            previous_free_gids=[],
            workshop_updates=workshop_items,
        )

        asyncio.run(plugin._poll_once())

        self.assertEqual(
            [(card_kind, [section.appid for section in sections]) for card_kind, sections in plugin._render_payloads],
            [("game", ["free_games"]), ("workshop", ["workshop:730"])],
        )

    def test_poll_preserves_free_game_snapshot_when_fetch_fails(self):
        plugin = self._make_poll_plugin(
            updates_by_app={},
            free_items=[],
            previous_free_gids=["free-old"],
            workshop_updates=[],
        )
        plugin._fetch_free_game_items = self._async_return(None)

        asyncio.run(plugin._poll_once())

        self.assertEqual(
            plugin._saved_state["free_games_active_gids"],
            json.dumps(["free-old"], ensure_ascii=False),
        )

    def test_manual_query_returns_free_games_when_no_appids(self):
        plugin = self._make_plugin()
        config = {
            "steam_appids": [],
            "message_mode": "card",
            "free_games_enable": True,
            "free_games_manual_only_when_no_news": False,
            "workshop_enable": False,
            "workshop_item_ids": [],
        }
        plugin._cfg = lambda key, default=None: config.get(key, default)
        plugin._ensure_http_client = self._async_noop
        plugin._normalize_appids = lambda values: []
        plugin._normalize_workshop_item_ids = lambda values: []
        plugin._workshop_enabled = lambda: False
        plugin._get_max_days = lambda: 1
        plugin._fetch_workshop_news_items = self._async_return([])
        plugin._fetch_free_game_items = self._async_return(
            [self.mod.NewsItem("free-new", "Portal", "u2", "c2", 200)]
        )
        plugin._build_sections = self._async_return([])
        plugin._build_workshop_sections = self._async_return([])
        plugin._render_payloads = []
        notices = []

        async def _render_card(
            sections,
            publish_text,
            query_text,
            notice="",
            card_kind="game",
        ):
            plugin._render_payloads.append((card_kind, sections))
            notices.append(notice)
            return b"img"

        plugin._render_card = _render_card

        results, err = asyncio.run(plugin._manual_query(query_kind="game"))

        self.assertIsNone(err)
        self.assertEqual(results, [b"img"])
        self.assertEqual(
            [section.appid for section in plugin._render_payloads[0][1]],
            ["free_games"],
        )
        self.assertEqual(notices, [""])


if __name__ == "__main__":
    unittest.main()
