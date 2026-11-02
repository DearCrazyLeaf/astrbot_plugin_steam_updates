from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import _load_module


class FreeGamesHistoryTest(unittest.TestCase):
    def setUp(self):
        self.module = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.object(
            self.module.StarTools,
            "get_data_dir",
            return_value=Path(self.tmp.name),
        ):
            self.plugin = self.module.SteamUpdatePush(None, {"free_games_enable": True})
        self.plugin._free_games_local_date = lambda: date(2026, 11, 1)

    def item(self, gid, title):
        return self.module.NewsItem(
            gid=gid,
            title=title,
            url=f"https://example.test/{gid}",
            contents=f"Description for {title}",
            date=1793491200,
            appid="730",
        )

    def test_calendar_has_seven_days_and_successful_empty_day(self):
        self.plugin._save_free_games_observation([])
        calendar = self.plugin._get_free_games_calendar()
        self.assertEqual(len(calendar), 7)
        self.assertEqual(calendar[-1]["date"], "2026-11-01")
        self.assertEqual(calendar[-1]["status"], "success")
        self.assertEqual(calendar[-1]["items"], [])
        self.assertEqual(calendar[0]["status"], "unknown")

    def test_observations_accumulate_and_failed_fetch_does_not_replace_success(self):
        first = self.item("gid-1", "Game One")
        second = self.item("gid-2", "Game Two")
        self.plugin._save_free_games_observation([first])
        self.plugin._save_free_games_observation([second])
        self.plugin._save_free_games_observation(None)

        record = self.plugin._load_free_games_history()["2026-11-01"]
        self.assertEqual(record["status"], "success")
        self.assertEqual({item["gid"] for item in record["items"]}, {"gid-1", "gid-2"})
        stored = json.loads(self.plugin._free_games_history_path.read_text(encoding="utf-8"))
        self.assertIn("days", stored)

    def test_history_message_uses_local_records_without_network(self):
        self.plugin._save_free_games_observation([self.item("gid-1", "Game One")])
        self.plugin._fetch_free_game_items = lambda: self.fail("unexpected network fetch")
        message = self.plugin._format_free_games_history_message()
        self.assertIn("近7天限时免费游戏记录", message)
        self.assertIn("Game One", message)
        self.assertIn("暂无记录", message)


if __name__ == "__main__":
    unittest.main()
