import unittest

from tests.test_free_games import _load_module


class NewsTitleTranslationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _make_plugin(self, llm_text, template="{content}"):
        plugin = object.__new__(self.mod.SteamUpdatePush)
        plugin._resolve_app_names = self._async_return({"123": "Game"})
        plugin._cfg = lambda key, default=None: template if key == "llm_prompt" else default
        plugin._llm_prompts = []
        async def call_llm(prompt, umo):
            plugin._llm_prompts.append(prompt)
            return llm_text
        plugin._call_llm = call_llm
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
    async def test_llm_protocol_limits_simplified_chinese_to_title(self):
        plugin = self._make_plugin("【标题】\nSummer update\n【正文】\nBody remains in English", "正文必须使用英文。{content}")
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]
        await plugin._build_sections_llm(["123"], {"123": items}, None)
        self.assertEqual(len(plugin._llm_prompts), 1)
        prompt = plugin._llm_prompts[0]
        self.assertIn("标题必须使用简体中文", prompt)
        self.assertIn("正文继续遵循前述提示词的语言与格式要求", prompt)
        self.assertNotIn("请仅使用简体中文", prompt)

    async def test_llm_structured_response_preserves_extended_han_original_title(self):
        plugin = self._make_plugin("【标题】\n模型标题\n【正文】\n正文")
        items = [self.mod.NewsItem("1", "𠮷野家 Update", "url-1", "source", 1, "123")]
        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)
        self.assertEqual(sections[0].updates[0].title, "𠮷野家 Update")


    @staticmethod
    def _async_return(value):
        async def _inner(*args, **kwargs):
            return value

        return _inner
