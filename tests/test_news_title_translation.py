import unittest

from tests.test_free_games import _load_module


class NewsTitleTranslationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _make_plugin(self, llm_text):
        plugin = object.__new__(self.mod.SteamUpdatePush)
        plugin._resolve_app_names = self._async_return({"123": "Game"})
        plugin._cfg = lambda key, default=None: "{content}" if key == "llm_prompt" else default
        plugin._call_llm = self._async_return(llm_text)
        return plugin

    async def test_llm_structured_response_uses_simplified_chinese_title_and_body(self):
        plugin = self._make_plugin("【标题】\n夏季更新\n【正文】\n修复了多人模式问题")
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "夏季更新")
        self.assertEqual(merged.contents, "修复了多人模式问题")

    async def test_llm_structured_response_preserves_original_title_containing_han_characters(self):
        plugin = self._make_plugin("【标题】\n模型改写标题\n【正文】\n已修复问题")
        items = [self.mod.NewsItem("1", "夏日 Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "夏日 Update")
        self.assertEqual(merged.contents, "已修复问题")

    async def test_llm_malformed_response_keeps_original_title_and_complete_response_as_body(self):
        plugin = self._make_plugin("未按结构返回的完整摘要")
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "Summer Update")
        self.assertEqual(merged.contents, "未按结构返回的完整摘要")

    async def test_llm_empty_structured_title_keeps_original_title_and_complete_response_as_body(self):
        response = "【标题】\n \n【正文】\n修复了崩溃问题"
        plugin = self._make_plugin(response)
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "Summer Update")
        self.assertEqual(merged.contents, response)

    @staticmethod
    def _async_return(value):
        async def _inner(*args, **kwargs):
            return value

        return _inner
