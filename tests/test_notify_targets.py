import asyncio
import json
import unittest
from pathlib import Path

from test_free_games import _load_module


ROOT = Path(__file__).resolve().parents[1]


class NotifyTargetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _make_plugin(self, config=None):
        plugin = object.__new__(self.mod.SteamUpdatePush)
        values = dict(config or {})
        plugin.config = values
        plugin._cfg = lambda key, default=None: values.get(key, default)
        plugin._last_platform_id = None
        plugin._last_bot = None
        plugin._trace_seq = 0
        plugin._warnings = []
        plugin._debug_logs = []
        plugin._log_warn = (
            lambda *args, **kwargs:
            plugin._warnings.append((args, kwargs))
        )
        plugin._log_debug = (
            lambda *args, **kwargs:
            plugin._debug_logs.append((args, kwargs))
        )
        plugin._debug = lambda *args, **kwargs: None
        return plugin

    def _target(self, umo, legacy_group_id=""):
        platform_id = umo.split(":", 1)[0]
        return self.mod.NotifyTarget(
            umo=umo,
            platform_id=platform_id,
            legacy_group_id=legacy_group_id,
        )

    def test_resolve_accepts_all_message_types_and_multiple_instances(self):
        plugin = self._make_plugin(
            {
                "notify_umos": [
                    "qq-a:GroupMessage:100",
                    "qq-b:GroupMessage:100",
                    "qq-b:FriendMessage:200",
                    "telegram:OtherMessage:room:thread:7",
                ],
                "notify_group_ids": [],
            }
        )

        targets = plugin._resolve_notify_targets()

        self.assertEqual(
            [target.umo for target in targets],
            [
                "qq-a:GroupMessage:100",
                "qq-b:GroupMessage:100",
                "qq-b:FriendMessage:200",
                "telegram:OtherMessage:room:thread:7",
            ],
        )

    def test_resolve_merges_new_and_legacy_targets_in_order(self):
        plugin = self._make_plugin(
            {
                "notify_umos": [
                    "qq-a:GroupMessage:100",
                    "telegram:FriendMessage:user:42",
                ],
                "notify_group_ids": [
                    "qq-a:GroupMessage:100",
                    "200",
                    "qq-b:OtherMessage:room",
                ],
                "platform_id": "qq-a",
            }
        )

        targets = plugin._resolve_notify_targets()

        self.assertEqual(
            [(target.umo, target.legacy_group_id) for target in targets],
            [
                ("qq-a:GroupMessage:100", ""),
                ("telegram:FriendMessage:user:42", ""),
                ("qq-a:GroupMessage:200", "200"),
                ("qq-b:OtherMessage:room", ""),
            ],
        )

    def test_resolve_skips_invalid_values_without_logging_them(self):
        plugin = self._make_plugin(
            {
                "notify_umos": [
                    "",
                    "broken",
                    "qq-a:InvalidMessage:secret",
                    "qq-a:GroupMessage:valid",
                ],
                "notify_group_ids": ["300"],
                "platform_id": "",
            }
        )

        targets = plugin._resolve_notify_targets()

        self.assertEqual(
            [target.umo for target in targets],
            ["qq-a:GroupMessage:valid"],
        )
        target_warnings = [
            kwargs
            for args, kwargs in plugin._warnings
            if args and args[0] == "notify_target"
        ]
        self.assertEqual(len(target_warnings), 4)
        self.assertEqual(
            [fields["target_index"] for fields in target_warnings],
            [1, 2, 3, 1],
        )
        for fields in target_warnings:
            self.assertRegex(fields["target_ref"], r"^[0-9a-f]{12}$")
            self.assertEqual(
                set(fields),
                {
                    "target_index",
                    "target_ref",
                    "message_type",
                    "legacy",
                },
            )
        rendered_logs = json.dumps(
            plugin._warnings,
            ensure_ascii=False,
            default=str,
        )
        self.assertNotIn("qq-a:InvalidMessage:secret", rendered_logs)
        self.assertNotIn('"300"', rendered_logs)
        self.assertIn("target_ref", rendered_logs)
        self.assertIn("message_type", rendered_logs)

    def test_manual_permission_uses_full_umo_or_legacy_group_id(self):
        plugin = self._make_plugin(
            {
                "notify_umos": ["qq-a:GroupMessage:100"],
                "notify_group_ids": [
                    "200",
                    "qq-b:GroupMessage:300",
                ],
                "platform_id": "qq-a",
            }
        )

        self.assertTrue(
            plugin._manual_query_allowed(
                "qq-a:GroupMessage:100",
                "100",
            )
        )
        self.assertTrue(
            plugin._manual_query_allowed(
                "qq-z:GroupMessage:200",
                "200",
            )
        )
        self.assertTrue(
            plugin._manual_query_allowed(
                "qq-b:GroupMessage:300",
                "300",
            )
        )
        self.assertFalse(
            plugin._manual_query_allowed(
                "qq-c:GroupMessage:400",
                "400",
            )
        )

    def test_ping_captures_platform_instance_id_and_aiocq_bot(self):
        plugin = self._make_plugin()
        event = self.mod.AiocqhttpMessageEvent()
        event.bot = object()
        event.get_platform_id = lambda: "aiocqhttp-instance-2"
        event.get_platform_name = lambda: "aiocqhttp"
        event.plain_result = lambda text: text

        async def collect():
            return [
                item
                async for item in plugin.steam_update_ping(event)
            ]

        results = asyncio.run(collect())

        self.assertEqual(
            plugin._last_platform_id,
            "aiocqhttp-instance-2",
        )
        self.assertIs(plugin._last_bot, event.bot)
        self.assertEqual(results, ["Steam 更新推送已就绪"])


class NotifyContractTest(unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
