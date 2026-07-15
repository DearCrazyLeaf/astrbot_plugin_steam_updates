import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_free_games


ROOT = Path(__file__).resolve().parents[1]


class SendContext:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def send_message(self, session, message_chain):
        self.calls.append((session, message_chain))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BotRecorder:
    def __init__(self):
        self.calls = []

    async def send_group_msg(self, group_id, message):
        self.calls.append((group_id, message))


class NotifyTargetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = test_free_games._load_module()

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

    def _chain(self, text="payload"):
        return self.mod.MessageChain(
            chain=[self.mod.Plain(text)]
        )

    def _make_poll_plugin(self):
        helper = test_free_games.FreeGamesLogicTest()
        helper.mod = self.mod
        return helper._make_poll_plugin(
            updates_by_app={
                "730": [
                    self.mod.NewsItem(
                        "n1",
                        "Patch",
                        "u1",
                        "body",
                        100,
                    )
                ]
            },
            free_items=[],
            previous_free_gids=[],
            workshop_updates=[],
        )

    def test_push_chain_tracks_true_false_and_exception(self):
        targets = [
            self._target("qq-a:GroupMessage:100"),
            self._target("qq-b:FriendMessage:200"),
            self._target("qq-c:FriendMessage:300"),
            self._target("telegram:OtherMessage:room:7"),
        ]
        context = SendContext(
            [
                True,
                1,
                False,
                RuntimeError(
                    "failed telegram:OtherMessage:room:7"
                ),
            ]
        )
        plugin = self._make_plugin()
        plugin.context = context

        result = asyncio.run(
            plugin._push_chain(targets, self._chain())
        )

        self.assertEqual(result.succeeded, [targets[0]])
        self.assertEqual(result.failed, targets[1:])
        self.assertEqual(
            [session for session, _ in context.calls],
            [target.umo for target in targets],
        )
        rendered_logs = json.dumps(
            plugin._warnings,
            ensure_ascii=False,
            default=str,
        )
        self.assertNotIn(targets[3].umo, rendered_logs)
        self.assertIn("RuntimeError", rendered_logs)

    def test_bot_fallback_accepts_matching_numeric_legacy_group(self):
        target = self._target(
            "qq-a:GroupMessage:100",
            legacy_group_id="100",
        )
        plugin = self._make_plugin()
        plugin.context = SendContext([False])
        plugin._last_platform_id = "qq-a"
        plugin._last_bot = BotRecorder()

        sent = asyncio.run(
            plugin._send_to_target(
                target,
                self._chain(),
                target_index=1,
            )
        )

        self.assertTrue(sent)
        self.assertEqual(len(plugin._last_bot.calls), 1)
        group_id, message = plugin._last_bot.calls[0]
        self.assertEqual(group_id, 100)
        self.assertEqual(message[0].text, "payload")

    def test_bot_fallback_rejects_full_mismatch_and_non_numeric(self):
        cases = [
            (
                self._target("qq-a:GroupMessage:100"),
                "qq-a",
            ),
            (
                self._target(
                    "qq-b:GroupMessage:100",
                    legacy_group_id="100",
                ),
                "qq-a",
            ),
            (
                self._target(
                    "qq-a:GroupMessage:not-number",
                    legacy_group_id="not-number",
                ),
                "qq-a",
            ),
        ]
        invalid_legacy_ids = [
            "+100",
            "-100",
            " 100",
            "100 ",
            "1_00",
            "１００",
        ]
        cases.extend(
            (
                self._target(
                    f"qq-a:GroupMessage:{legacy_group_id}",
                    legacy_group_id=legacy_group_id,
                ),
                "qq-a",
            )
            for legacy_group_id in invalid_legacy_ids
        )
        for target, captured_platform in cases:
            with self.subTest(target=target):
                plugin = self._make_plugin()
                plugin.context = SendContext([False])
                plugin._last_platform_id = captured_platform
                plugin._last_bot = BotRecorder()

                sent = asyncio.run(
                    plugin._send_to_target(
                        target,
                        self._chain(),
                        target_index=1,
                    )
                )

                self.assertFalse(sent)
                self.assertEqual(plugin._last_bot.calls, [])

    def test_push_image_marks_every_target_failed_when_file_save_fails(self):
        targets = [
            self._target("qq-a:GroupMessage:100"),
            self._target("qq-b:FriendMessage:200"),
        ]
        plugin = self._make_plugin()
        plugin._save_temp_image = lambda image_bytes: None

        result = asyncio.run(
            plugin._push_image(targets, b"image")
        )

        self.assertEqual(result.succeeded, [])
        self.assertEqual(result.failed, targets)

    def test_save_temp_image_logs_only_exception_type(self):
        plugin = self._make_plugin()
        plugin._debug = (
            lambda *args, **kwargs:
            plugin._debug_logs.append((args, kwargs))
        )
        with tempfile.TemporaryDirectory(
            prefix="sensitive-image-save-",
        ) as temp_dir:
            data_root = Path(temp_dir) / "private-data-root"
            data_root.write_text("blocking file", encoding="utf-8")
            plugin._data_dir = data_root

            saved = plugin._save_temp_image(b"image")

            sensitive_path = str(data_root)

        self.assertIsNone(saved)
        rendered_logs = json.dumps(
            [plugin._warnings, plugin._debug_logs],
            ensure_ascii=False,
            default=str,
        )
        self.assertNotIn("sensitive-image-save-", rendered_logs)
        self.assertNotIn(sensitive_path, rendered_logs)
        self.assertIn("NotADirectoryError", rendered_logs)
        save_warnings = [
            (args, kwargs)
            for args, kwargs in plugin._warnings
            if args == ("push", "save temp image failed")
        ]
        self.assertEqual(
            save_warnings,
            [
                (
                    ("push", "save temp image failed"),
                    {"error_type": "NotADirectoryError"},
                )
            ],
        )

    def test_push_image_build_failure_logs_only_exception_type(self):
        targets = [
            self._target("qq-a:GroupMessage:100"),
            self._target("qq-b:FriendMessage:200"),
        ]
        sensitive_path = "/tmp/sensitive-image-build-path.png"
        sensitive_error = "sensitive-image-build-error"
        plugin = self._make_plugin()
        plugin._save_temp_image = (
            lambda image_bytes: sensitive_path
        )

        with patch.object(
            self.mod,
            "Image",
            side_effect=RuntimeError(sensitive_error),
        ):
            result = asyncio.run(
                plugin._push_image(targets, b"image")
            )

        self.assertEqual(result.succeeded, [])
        self.assertEqual(result.failed, targets)
        rendered_logs = json.dumps(
            plugin._warnings,
            ensure_ascii=False,
            default=str,
        )
        self.assertNotIn(sensitive_path, rendered_logs)
        self.assertNotIn(sensitive_error, rendered_logs)
        self.assertIn("RuntimeError", rendered_logs)
        build_warnings = [
            (args, kwargs)
            for args, kwargs in plugin._warnings
            if args == ("push", "build image chain failed")
        ]
        self.assertEqual(
            build_warnings,
            [
                (
                    ("push", "build image chain failed"),
                    {"error_type": "RuntimeError"},
                )
            ],
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

    def test_poll_all_invalid_targets_stops_before_http_and_state(self):
        plugin = self._make_plugin(
            {
                "enable_push": True,
                "notify_umos": ["invalid"],
                "notify_group_ids": [],
                "steam_appids": ["730"],
            }
        )
        plugin._is_current_poll_instance = lambda: True
        calls = {"http": 0, "state": 0}

        async def ensure_http():
            calls["http"] += 1

        def load_state():
            calls["state"] += 1
            return {}

        plugin._ensure_http_client = ensure_http
        plugin._load_state = load_state
        plugin._next_trace_id = lambda prefix: "poll-invalid"

        asyncio.run(plugin._poll_once())

        self.assertEqual(calls, {"http": 0, "state": 0})

    def test_poll_text_fallback_only_uses_image_failed_targets(self):
        plugin = self._make_poll_plugin()
        first = self._target("qq-a:GroupMessage:100")
        second = self._target("qq-b:FriendMessage:200")
        plugin._resolve_notify_targets = lambda: [first, second]
        text_calls = []

        async def push_image(targets, image_bytes):
            return self.mod.PushResult([first], [second])

        async def push_text(targets, text):
            text_calls.append(list(targets))
            return self.mod.PushResult(list(targets), [])

        plugin._push_image = push_image
        plugin._push_text = push_text

        asyncio.run(plugin._poll_once())

        self.assertEqual(text_calls, [[second]])
        self.assertTrue(hasattr(plugin, "_saved_state"))

    def test_poll_preserves_state_when_every_send_fails(self):
        plugin = self._make_poll_plugin()
        target = self._target("qq-a:GroupMessage:100")
        plugin._resolve_notify_targets = lambda: [target]

        async def fail(targets, payload):
            return self.mod.PushResult([], list(targets))

        plugin._push_image = fail
        plugin._push_text = fail

        asyncio.run(plugin._poll_once())

        self.assertFalse(hasattr(plugin, "_saved_state"))

    def test_poll_partial_text_success_saves_global_state(self):
        plugin = self._make_poll_plugin()
        first = self._target("qq-a:GroupMessage:100")
        second = self._target("qq-b:FriendMessage:200")
        plugin._resolve_notify_targets = lambda: [first, second]
        previous_cfg = plugin._cfg
        plugin._cfg = (
            lambda key, default=None:
            "text"
            if key == "message_mode"
            else previous_cfg(key, default)
        )

        async def partial(targets, text):
            return self.mod.PushResult([first], [second])

        plugin._push_text = partial

        asyncio.run(plugin._poll_once())

        self.assertTrue(hasattr(plugin, "_saved_state"))

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
