import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from support import _load_module


class Config(dict):
    fail = False

    def save_config(self):
        if self.fail:
            raise OSError("disk full")
        self.saved = deepcopy(dict(self))


class SubscriptionsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.m = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a, self.b = "qq-a:GroupMessage:100", "qq-b:GroupMessage:100"
        self.config = Config(
            notify_umos=[self.a, self.b],
            steam_appids=["730", "570"],
            message_mode="text",
            free_games_enable=False,
            workshop_enable=False,
            workshop_item_ids=[],
        )
        with patch.object(self.m.StarTools, "get_data_dir", return_value=self.root):
            self.p = self.m.SteamUpdatePush(None, self.config)
        self.p._is_current_poll_instance = lambda: True
        self.sent, self.fetches = [], []
        self.news, self.failed, self.workshop, self.free = {}, set(), [], []
        self.outcomes = {}

        async def ensure():
            pass

        async def fetch(ids, *args, **kwargs):
            self.fetches.append(list(ids))
            return {
                i: self.news.get(i, []) for i in ids if i not in self.failed
            }, self.failed.intersection(ids)

        async def workshops(ids=None):
            self.workshop_fetch = list(ids or [])
            return [i for i in self.workshop if i.gid.split(":", 1)[0] in ids]

        async def free():
            return self.free

        async def build(ids, updates, *args, **kwargs):
            return [self.m.AppSection(i, "Game " + i, list(updates[i])) for i in ids]

        async def build_workshop(items):
            return [self.m.AppSection("workshop", "Workshop", list(items))]

        async def errors(ids):
            return [
                self.m.AppSection(i, "ERROR " + i, [], query_error="failed")
                for i in ids
            ]

        async def deliver(targets, sections, publish, query, kind, mode):
            self.sent.append(
                (tuple(t.umo for t in targets), deepcopy(sections), kind, mode)
            )
            return self.m.PushResult(
                [
                    t
                    for t in targets
                    if self.outcomes.get(t.umo, "success") == "success"
                ],
                [t for t in targets if self.outcomes.get(t.umo) == "failed"],
                [t for t in targets if self.outcomes.get(t.umo) == "uncertain"],
            )

        self.p._ensure_http_client = ensure
        self.p._fetch_news_for_appids = fetch
        self.p._fetch_workshop_news_items = workshops
        self.p._fetch_free_game_items = free
        self.p._build_sections = build
        self.p._build_workshop_sections = build_workshop
        self.p._build_query_error_sections = errors
        self.p._deliver_poll_payload = deliver

    def rule(self, umo, games=None, **kwargs):
        return {
            "__template_key": "session",
            "umo": umo,
            "enabled": True,
            "steam_appids": games or [],
            "workshop_item_ids": [],
            "free_games_mode": "off",
            "message_mode": "inherit",
            **kwargs,
        }

    def rules(self, *rules):
        self.config["subscription_settings"] = {"sessions": list(rules)}

    def item(self, gid, appid="730", date=None):
        return self.m.NewsItem(
            gid,
            "Title " + gid,
            "https://example.test/" + gid,
            "body",
            int(time.time()) if date is None else date,
            appid=appid,
        )

    def event(self, umo=None, admin=False, role=None):
        umo = umo or self.a

        async def group(group_id):
            return SimpleNamespace(
                group_id=group_id,
                group_owner="user" if role == "owner" else "owner",
                group_admins=["user"] if role == "admin" else [],
            )

        return SimpleNamespace(
            unified_msg_origin=umo,
            is_admin=lambda: admin,
            get_group_id=lambda: (
                umo.split(":", 2)[2] if ":GroupMessage:" in umo else ""
            ),
            get_sender_id=lambda: "user",
            get_group=group,
        )

    def test_explicit_empty_and_pause_do_not_fall_back(self):
        self.rules(self.rule(self.a), self.rule(self.b, ["570"], enabled=False))
        profiles = self.p._resolve_subscriptions()
        self.assertEqual(profiles[self.a].games, ())
        self.assertFalse(profiles[self.b].enabled)
        self.assertEqual(profiles[self.b].games, ("570",))

    def test_duplicate_or_invalid_explicit_rule_blocks_legacy_target(self):
        for entries in (
            [self.rule(self.a), self.rule(self.a)],
            [self.rule(self.a, ["bad"])],
        ):
            self.rules(*entries)
            profiles = self.p._resolve_subscriptions()
            self.assertNotIn(self.a, profiles)
            self.assertIn(self.b, profiles)

    async def test_group_admin_and_owner_edit_current_session_only(self):
        for role in ("admin", "owner"):
            self.assertTrue(
                await self.p._subscription_can_edit(self.event(role=role), self.a)
            )
            self.assertFalse(
                await self.p._subscription_can_edit(self.event(role=role), self.b)
            )
            self.assertFalse(
                await self.p._subscription_can_edit(self.event(role=role), self.a, True)
            )

    async def test_member_private_and_failed_role_lookup_are_readonly(self):
        for event in (
            self.event(),
            self.event("qq-a:FriendMessage:user", role="owner"),
        ):
            result = await self.p._subscription_command(event, "添加 游戏 42")
            self.assertIn("权限不足", result)
        event = self.event()

        async def unavailable(*args):
            raise RuntimeError("unsupported")

        event.get_group = unavailable
        self.assertFalse(await self.p._subscription_can_edit(event, self.a))
        self.assertIn("730", await self.p._subscription_command(event, "列表"))
        self.assertNotIn("subscription_settings", self.config)

    async def test_admin_cross_session_with_spaces_and_colons(self):
        target = "bot with space:OtherMessage:room:thread"
        result = await self.p._subscription_command(
            self.event(admin=True), f"「{target}」 添加 游戏 42", True
        )
        self.assertIn("已添加", result)
        self.assertEqual(self.p._resolve_subscriptions()[target].games, ("42",))
        self.assertIn(self.a, self.p._resolve_subscriptions())

    async def test_command_copies_legacy_then_keeps_empty_override(self):
        result = await self.p._subscription_command(
            self.event(role="admin"), "删除 游戏 730 570"
        )
        self.assertIn("已删除", result)
        self.assertEqual(self.p._resolve_subscriptions()[self.a].games, ())
        self.assertEqual(self.p._resolve_subscriptions()[self.b].games, ("730", "570"))
        self.assertEqual(
            self.config.saved["subscription_settings"]["sessions"][0]["steam_appids"],
            [],
        )

    async def test_parallel_commands_and_save_failure(self):
        await asyncio.gather(
            *(
                self.p._subscription_command(
                    self.event(admin=True), "添加 游戏 " + str(i)
                )
                for i in (10, 20)
            )
        )
        self.assertEqual(
            self.p._resolve_subscriptions()[self.a].games, ("730", "570", "10", "20")
        )
        before = deepcopy(dict(self.config))
        self.config.fail = True
        result = await self.p._subscription_command(
            self.event(admin=True), "删除 游戏 730"
        )
        self.assertIn("保存失败", result)
        self.assertEqual(dict(self.config), before)

    async def test_union_fetch_and_per_session_payload(self):
        self.rules(self.rule(self.a, ["730"]), self.rule(self.b, ["570"]))
        self.news = {"730": [self.item("a")], "570": [self.item("b", "570")]}
        await self.p._poll_once()
        self.assertEqual(self.fetches, [["730", "570"]])
        self.assertEqual(
            {
                targets[0]: [s.appid for s in sections]
                for targets, sections, _, _ in self.sent
            },
            {self.a: ["730"], self.b: ["570"]},
        )

    async def test_failed_target_retries_without_resending_successful_target(self):
        self.news = {"730": [self.item("n1")]}
        self.outcomes[self.b] = "failed"
        await self.p._poll_once()
        self.sent.clear()
        self.outcomes[self.b] = "success"
        await self.p._poll_once()
        self.assertEqual([t for batch in self.sent for t in batch[0]], [self.b])

    async def test_uncertain_target_is_not_automatically_retried(self):
        self.news = {"730": [self.item("n1")]}
        self.outcomes[self.a] = "uncertain"
        await self.p._poll_once()
        self.sent.clear()
        await self.p._poll_once()
        self.assertEqual(self.sent, [])

    async def test_new_target_baselines_only_after_successful_fetch(self):
        target = "tg:GroupMessage:new"
        self.config["notify_umos"] = []
        self.rules(self.rule(target, ["42"]))
        self.failed = {"42"}
        await self.p._poll_once()
        self.assertTrue(
            all(s.query_error for _, sections, _, _ in self.sent for s in sections)
        )
        self.sent.clear()
        self.failed.clear()
        self.news = {"42": [self.item("old", "42")]}
        await self.p._poll_once()
        self.assertEqual(self.sent, [])
        self.news["42"].insert(0, self.item("new", "42"))
        await self.p._poll_once()
        self.assertEqual(
            [
                i.gid
                for _, sections, _, _ in self.sent
                for s in sections
                for i in s.updates
            ],
            ["new"],
        )

    async def test_pause_resume_baselines_and_empty_subscription_does_not_send(self):
        self.rules(self.rule(self.a, ["730"], enabled=False), self.rule(self.b))
        self.news = {"730": [self.item("old")]}
        await self.p._poll_once()
        self.assertEqual(self.sent, [])
        self.config["subscription_settings"]["sessions"][0]["enabled"] = True
        await self.p._poll_once()
        self.assertEqual(self.sent, [])
        self.news["730"].insert(0, self.item("new"))
        await self.p._poll_once()
        self.assertEqual(self.sent[0][0], (self.a,))

    async def test_workshop_and_free_games_retry_only_failed_session(self):
        self.config.update(
            steam_appids=[],
            workshop_enable=True,
            workshop_item_ids=["99"],
            workshop_push_on_first_seen=True,
            free_games_enable=True,
        )
        self.workshop = [self.item("99:1", date=int(time.time()))]
        self.free = [self.item("free1")]
        self.outcomes[self.b] = "failed"
        await self.p._poll_once()
        self.assertEqual(self.workshop_fetch, ["99"])
        self.sent.clear()
        self.outcomes[self.b] = "success"
        await self.p._poll_once()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(targets == (self.b,) for targets, _, _, _ in self.sent))

    async def test_disabled_global_features_cannot_be_enabled_per_session(self):
        self.rules(self.rule(self.a, workshop_item_ids=["99"], free_games_mode="on"))
        p = self.p._resolve_subscriptions()[self.a]
        self.assertFalse(p.free_games)
        self.assertEqual(p.workshop, ())

    async def test_old_global_seen_state_is_migrated_without_duplicate(self):
        self.p._save_state({"730": "old", "news_seen:730": '["old"]'})
        self.news = {"730": [self.item("old")]}
        await self.p._poll_once()
        self.assertEqual(self.sent, [])
        backup = json.loads(
            (self.root / "state.before-subscriptions-v1.json").read_text()
        )
        self.assertEqual(backup["730"], "old")
        self.news["730"].insert(0, self.item("new"))
        await self.p._poll_once()
        self.assertEqual(
            len(self.sent), 1
        )  # Identical payload rendered/sent as one group.
        self.assertEqual(set(self.sent[0][0]), {self.a, self.b})

    async def test_query_errors_only_reach_subscribers_and_respect_cooldown(self):
        self.rules(self.rule(self.a, ["730"]), self.rule(self.b, ["570"]))
        self.failed = {"730"}
        await self.p._poll_once()
        self.assertEqual(self.sent[0][0], (self.a,))
        self.sent.clear()
        await self.p._poll_once()
        self.assertEqual(self.sent, [])

    async def test_change_during_render_prevents_stale_send(self):
        self.config["notify_umos"] = [self.a]
        self.news = {"730": [self.item("n")]}

        async def build(*args, **kwargs):
            self.rules(self.rule(self.a))
            return [self.m.AppSection("730", "Game", self.news["730"])]

        self.p._build_sections = build
        await self.p._poll_once()
        self.assertEqual(self.sent, [])

    async def test_manual_query_uses_only_current_session_sources(self):
        self.rules(self.rule(self.a, ["730"]), self.rule(self.b, ["570"]))
        self.news = {"730": [self.item("one")], "570": [self.item("two", "570")]}
        results, error = await self.p._manual_query(self.a, "game")
        self.assertIsNone(error)
        self.assertEqual(self.fetches, [["730"]])
        self.assertIn("Game 730", results[0])
        self.assertNotIn("570", results[0])

    async def test_failed_initial_legacy_fetch_is_not_discarded_on_retry(self):
        self.rules(self.rule(self.a, ["730"]), self.rule(self.b, []))
        self.failed = {"730"}
        await self.p._poll_once()
        self.sent.clear()
        self.failed.clear()
        self.news = {"730": [self.item("missed")]}
        await self.p._poll_once()
        self.assertEqual(
            [i.gid for _, secs, _, _ in self.sent for s in secs for i in s.updates],
            ["missed"],
        )

    async def test_atomic_state_write_failure_preserves_old_state_and_does_not_send(
        self,
    ):
        self.p._save_state({"sentinel": "old"})
        self.news = {"730": [self.item("n")]}
        with patch.object(self.m.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.p._poll_once()
        self.assertEqual(self.p._load_state(), {"sentinel": "old"})
        self.assertEqual(self.sent, [])
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    async def test_quick_pause_resume_does_not_replay_backlog(self):
        self.config["notify_umos"] = [self.a]
        self.news = {"730": [self.item("first")]}
        await self.p._poll_once()
        self.sent.clear()
        event = self.event(admin=True)
        await self.p._subscription_command(event, "暂停")
        await self.p._subscription_command(event, "恢复")
        self.news["730"].insert(0, self.item("while-paused"))
        await self.p._poll_once()
        self.assertEqual(self.sent, [])
        await self.p._subscription_command(
            event, "恢复"
        )  # Idempotent when already enabled.
        self.news["730"].insert(0, self.item("after-resume"))
        await self.p._poll_once()
        self.assertEqual(
            [i.gid for _, secs, _, _ in self.sent for s in secs for i in s.updates],
            ["after-resume"],
        )

    async def test_new_workshop_and_free_subscription_baselines_without_backfill(self):
        self.config.update(notify_umos=[], workshop_enable=True, free_games_enable=True)
        self.rules(self.rule(self.a, workshop_item_ids=["99"], free_games_mode="on"))
        self.workshop = [self.item("99:1", date=100)]
        self.free = [self.item("old-free")]
        await self.p._poll_once()
        self.assertEqual(self.sent, [])
        self.workshop = [self.item("99:2", date=200)]
        self.free.insert(0, self.item("new-free"))
        await self.p._poll_once()
        self.assertEqual(
            {i.gid for _, secs, _, _ in self.sent for s in secs for i in s.updates},
            {"99:2", "new-free"},
        )

    async def test_free_fetch_failure_leaves_baseline_unset(self):
        self.config.update(notify_umos=[], free_games_enable=True)
        self.rules(self.rule(self.a, free_games_mode="on"))
        self.free = None
        await self.p._poll_once()
        self.free = [self.item("existing")]
        await self.p._poll_once()
        self.assertEqual(self.sent, [])

    async def test_message_modes_split_identical_content_and_global_disable_stops_all(
        self,
    ):
        self.rules(
            self.rule(self.a, ["730"], message_mode="card"),
            self.rule(self.b, ["730"], message_mode="text"),
        )
        self.news = {"730": [self.item("n")]}
        await self.p._poll_once()
        self.assertEqual(
            {targets[0]: mode for targets, _, _, mode in self.sent},
            {self.a: "card", self.b: "text"},
        )
        self.config["enable_push"] = False
        self.sent.clear()
        self.news["730"].insert(0, self.item("new"))
        await self.p._poll_once()
        self.assertEqual(self.sent, [])

    async def test_removed_rule_returns_to_global_only_after_explicit_confirmation(
        self,
    ):
        self.rules(self.rule(self.a))
        event = self.event(admin=True)
        result = await self.p._subscription_command(event, "恢复全局")
        self.assertIn("用法", result)
        self.assertEqual(self.p._resolve_subscriptions()[self.a].games, ())
        await self.p._subscription_command(event, "恢复全局 确认")
        self.assertEqual(self.p._resolve_subscriptions()[self.a].games, ("730", "570"))

    async def test_group_metadata_from_different_group_does_not_grant_permission(self):
        event = self.event(role="admin")

        async def wrong_group(*args):
            return SimpleNamespace(
                group_id="other", group_owner="user", group_admins=["user"]
            )

        event.get_group = wrong_group
        self.assertFalse(await self.p._subscription_can_edit(event, self.a))

    async def test_handler_retired_during_permission_lookup_cannot_save_old_config(
        self,
    ):
        event = self.event(role="admin")
        original = event.get_group

        async def get_group(group_id):
            self.p._stop_event.set()
            return await original(group_id)

        event.get_group = get_group
        before = deepcopy(dict(self.config))
        result = await self.p._subscription_command(event, "添加 游戏 42")
        self.assertIn("重载", result)
        self.assertEqual(dict(self.config), before)


if __name__ == "__main__":
    unittest.main()
