import base64
import importlib
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import _load_module


class Config(dict):
    def save_config(self):
        self.saved = deepcopy(dict(self))


class WebUITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.main = _load_module()
        self.web_module = importlib.import_module("steam_subscription_tests.webui")
        self.request = importlib.import_module("astrbot.api.web").request
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(
            enable_push=True,
            steam_appids=["730"],
            steam_web_api_key="secret",
            subscription_settings={"sessions": []},
        )
        with patch.object(
            self.main.StarTools, "get_data_dir", return_value=Path(self.tmp.name)
        ):
            self.plugin = self.main.SteamUpdatePush(None, self.config)
        self.webui = self.web_module.SteamUpdatesWebUI(self.plugin)

    async def test_settings_hide_secrets_and_write_flat_config(self):
        result = await self.webui.settings()
        data = result["payload"]["data"]
        self.assertEqual(data["secrets"]["steam_web_api_key"], True)
        self.assertEqual(data["groups"]["game_updates"]["values"]["steam_web_api_key"], "")

        self.request.payload = {
            "groups": {
                "game_updates": {
                    "steam_appids": ["730", "570"],
                    "steam_web_api_key": "",
                }
            }
        }
        result = await self.webui.save_settings()
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(self.config["steam_appids"], ["730", "570"])
        self.assertEqual(self.config["steam_web_api_key"], "secret")
        self.assertEqual(self.config["game_updates"]["steam_appids"], ["730", "570"])

    async def test_grouped_framework_config_is_saved_in_schema_groups(self):
        config = Config(
            basic_settings={"enable_push": True},
            game_updates={"steam_appids": ["730"], "steam_web_api_key": ""},
        )
        with patch.object(
            self.main.StarTools,
            "get_data_dir",
            return_value=Path(self.tmp.name) / "grouped-plugin-data",
        ):
            plugin = self.main.SteamUpdatePush(None, config)
        webui = self.web_module.SteamUpdatesWebUI(plugin)
        self.request.payload = {
            "groups": {
                "basic_settings": {"enable_push": False},
                "game_updates": {"steam_appids": ["570"], "steam_web_api_key": ""},
            }
        }
        result = await webui.save_settings()
        self.assertEqual(result["status_code"], 200)
        self.assertFalse(config["basic_settings"]["enable_push"])
        self.assertEqual(config["game_updates"]["steam_appids"], ["570"])
        self.assertNotIn("enable_push", config)
        self.assertNotIn("steam_appids", config)

    async def test_invalid_settings_roll_back_previous_group_changes(self):
        self.request.payload = {
            "groups": {
                "basic_settings": {"enable_push": False},
                "game_updates": {"steam_appids": ["not-an-appid"]},
            }
        }
        result = await self.webui.save_settings()
        self.assertEqual(result["status_code"], 400)
        self.assertTrue(self.config["enable_push"])
        self.assertNotIn("steam_appids", self.config["game_updates"] if "game_updates" in self.config else {})

    async def test_dashboard_returns_subscription_and_calendar_summary(self):
        self.config["subscription_settings"] = {
            "sessions": [
                {
                    "__template_key": "session",
                    "label": "测试群",
                    "umo": "qq-a:GroupMessage:100",
                    "enabled": True,
                    "steam_appids": ["730"],
                    "workshop_item_ids": [],
                    "free_games_mode": "inherit",
                    "message_mode": "text",
                    "baseline_token": "preserve-me",
                }
            ]
        }
        result = await self.webui.dashboard()
        data = result["payload"]["data"]
        self.assertEqual(data["session_count"], 1)
        self.assertEqual(data["sessions"][0]["label"], "测试群")
        self.assertEqual(data["sessions"][0]["target"], "qq-a:GroupMessage:•••")
        self.assertEqual(len(data["calendar"]), 7)

    async def test_games_endpoint_loads_automatic_name_map(self):
        empty_map = Path(self.tmp.name) / "missing-appid-map.json"
        with (
            patch.object(self.plugin, "_bundled_appid_map_path", return_value=empty_map),
            patch.object(self.plugin, "_appid_map_path", return_value=empty_map),
        ):
            result = await self.webui.games()
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["payload"]["data"]["items"], [])

    async def test_session_settings_round_trip_hidden_baseline_token(self):
        self.config["subscription_settings"] = {
            "sessions": [
                {
                    "__template_key": "session",
                    "label": "测试群",
                    "umo": "qq-a:GroupMessage:100",
                    "enabled": True,
                    "steam_appids": ["730"],
                    "workshop_item_ids": [],
                    "free_games_mode": "inherit",
                    "message_mode": "text",
                    "baseline_token": "preserve-me",
                }
            ]
        }
        self.request.payload = {
            "groups": {
                "subscription_settings": {
                    "sessions": [
                        {
                            "__template_key": "session",
                            "label": "新备注",
                            "umo": "qq-a:GroupMessage:100",
                            "enabled": False,
                            "steam_appids": ["570"],
                            "workshop_item_ids": [],
                            "free_games_mode": "off",
                            "message_mode": "card",
                            "baseline_token": "preserve-me",
                        }
                    ]
                }
            }
        }
        result = await self.webui.save_settings()
        self.assertEqual(result["status_code"], 200)
        saved = self.config["subscription_settings"]["sessions"][0]
        self.assertEqual(saved["steam_appids"], ["570"])
        self.assertEqual(saved["baseline_token"], "preserve-me")
        self.assertFalse(saved["enabled"])

    async def test_clear_secret_and_validate_command_configuration(self):
        self.request.payload = {
            "groups": {"game_updates": {"steam_web_api_key": ""}},
            "clear_secrets": ["steam_web_api_key"],
        }
        result = await self.webui.save_settings()
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(self.config["steam_web_api_key"], "")
        self.assertEqual(self.config.saved["steam_web_api_key"], "")

        self.request.payload = {
            "groups": {
                "manual_commands": {
                    "manual_query_game_command": ["查更新"],
                    "manual_query_workshop_command": ["查更新 工坊"],
                    "manual_query_free_games_history_command": ["免费游戏"],
                }
            }
        }
        result = await self.webui.save_settings()
        self.assertEqual(result["status_code"], 400)
        self.assertNotIn("manual_query_game_command", self.config)

    async def test_manual_name_override_is_persisted_separately_and_removed(self):
        self.request.payload = {
            "appid": "730",
            "language": "schinese",
            "name": "反恐精英",
        }
        result = await self.webui.save_game()
        self.assertEqual(result["status_code"], 200)
        manual_map = json.loads(self.plugin._manual_appid_name_map_path.read_text(encoding="utf-8"))
        self.assertEqual(manual_map["730"]["schinese"], "反恐精英")
        self.assertNotIn("730", self.plugin._auto_appid_name_map or {})

        self.request.payload = {
            "appid": "730",
            "language": "schinese",
            "delete": True,
        }
        result = await self.webui.save_game()
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(
            json.loads(self.plugin._manual_appid_name_map_path.read_text(encoding="utf-8")),
            {},
        )

    async def test_cover_routes_use_astrbot_parameter_syntax_and_paths_are_confined(self):
        registrations = []

        class Context:
            def register_web_api(self, *args):
                registrations.append(args)

        self.plugin.context = Context()
        self.webui.register()
        prefix = "/astrbot_plugin_steam_updates"
        self.assertIn(f"{prefix}/covers/<appid>", [route[0] for route in registrations])
        self.assertIn(f"{prefix}/covers/<appid>/delete", [route[0] for route in registrations])

        self.plugin._app_header_dir.mkdir(parents=True, exist_ok=True)
        image_data = b"cover"
        (self.plugin._app_header_dir / "730.jpg").write_bytes(image_data)
        result = await self.webui.cover_image("730")
        self.assertEqual(result["status_code"], 200)
        payload = result["payload"]["data"]
        self.assertEqual(payload["content_type"], "image/jpeg")
        self.assertEqual(base64.b64decode(payload["base64"]), image_data)

    async def test_cover_image_rejects_invalid_missing_and_oversized_files(self):
        self.plugin._app_header_dir.mkdir(parents=True, exist_ok=True)
        (self.plugin._app_header_dir / "123.jpg").write_bytes(b"x" * 6_000_001)

        invalid_appid = await self.webui.cover_image("../123")
        missing = await self.webui.cover_image("456")
        oversized = await self.webui.cover_image("123")

        self.assertEqual(invalid_appid["status_code"], 400)
        self.assertEqual(missing["status_code"], 404)
        self.assertEqual(oversized["status_code"], 413)

    async def test_cover_image_reports_read_errors(self):
        self.plugin._app_header_dir.mkdir(parents=True, exist_ok=True)
        (self.plugin._app_header_dir / "730.jpg").write_bytes(b"cover")
        with patch.object(Path, "read_bytes", side_effect=OSError("disk unavailable")):
            result = await self.webui.cover_image("730")
        self.assertEqual(result["status_code"], 500)

    def test_plugin_initialization_registers_all_web_api_routes(self):
        registrations = []
        context = type(
            "Context",
            (),
            {"register_web_api": lambda _self, *args: registrations.append(args)},
        )()
        with patch.object(
            self.main.StarTools,
            "get_data_dir",
            return_value=Path(self.tmp.name) / "plugin-data",
        ):
            self.main.SteamUpdatePush(context, self.config)
        routes = {(route[0], tuple(route[2])) for route in registrations}
        prefix = "/astrbot_plugin_steam_updates"
        self.assertIn((f"{prefix}/dashboard", ("GET",)), routes)
        self.assertIn((f"{prefix}/settings", ("GET",)), routes)
        self.assertIn((f"{prefix}/settings", ("POST",)), routes)
        self.assertIn((f"{prefix}/games", ("GET",)), routes)
        self.assertIn((f"{prefix}/games", ("POST",)), routes)
        self.assertIn((f"{prefix}/covers", ("GET",)), routes)
        self.assertIn((f"{prefix}/covers/<appid>", ("GET",)), routes)
        self.assertIn((f"{prefix}/covers/<appid>/delete", ("POST",)), routes)


if __name__ == "__main__":
    unittest.main()
