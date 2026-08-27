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

    async def test_llm_empty_structured_title_omits_title_and_keeps_body(self):
        response = "【标题】\n \n【正文】\n修复了崩溃问题"
        plugin = self._make_plugin(response)
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "")
        self.assertEqual(merged.contents, "修复了崩溃问题")

    async def test_llm_english_structured_title_omits_title_and_keeps_body(self):
        response = "【标题】\nSummer Update\n【正文】\nFixed multiplayer issues"
        plugin = self._make_plugin(response)
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "")
        self.assertEqual(merged.contents, "Fixed multiplayer issues")

    async def test_llm_multiline_structured_title_omits_title_and_sanitizes_body(self):
        response = "【标题】\n夏季更新\n补充说明\n【正文】\n修复了崩溃问题"
        plugin = self._make_plugin(response)
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "")
        self.assertEqual(merged.contents, "夏季更新\n补充说明\n修复了崩溃问题")

    async def test_llm_structured_response_allows_blank_lines_and_same_line_values(self):
        response = "\r\n  【标题】：夏季更新\r\n\r\n【正文】:  修复了多人模式问题\r\n  "
        plugin = self._make_plugin(response)
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "夏季更新")
        self.assertEqual(merged.contents, "修复了多人模式问题")

    async def test_llm_structured_response_accepts_supported_outer_markdown_fences(self):
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]
        for language in ("", "text", "markdown", "plain"):
            with self.subTest(language=language):
                fence = "```" + language
                response = f"\n{fence}\n【标题】\n夏季更新\n【正文】\n修复了多人模式问题\n```\n"
                plugin = self._make_plugin(response)
                sections = await plugin._build_sections_llm(["123"], {"123": items}, None)
                merged = sections[0].updates[0]
                self.assertEqual(merged.title, "夏季更新")
                self.assertEqual(merged.contents, "修复了多人模式问题")

    async def test_llm_structured_body_preserves_internal_newlines_and_indentation(self):
        response = "【标题】\n夏季更新\n【正文】\n  第一行\n\n    第二行\n"
        plugin = self._make_plugin(response)
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        self.assertEqual(sections[0].updates[0].contents, "第一行\n\n    第二行")

    async def test_llm_duplicate_markers_omit_title_and_remove_protocol_lines(self):
        response = "【标题】\n夏季更新\n【标题】\n重复标题\n【正文】\n正文内容"
        plugin = self._make_plugin(response)
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "")
        self.assertEqual(merged.contents, "夏季更新\n重复标题\n正文内容")
        self.assertNotIn("【标题】", merged.contents)
        self.assertNotIn("【正文】", merged.contents)

    async def test_llm_reversed_markers_omit_title_and_remove_protocol_lines(self):
        response = "【正文】\n正文内容\n【标题】\n夏季更新"
        plugin = self._make_plugin(response)
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "")
        self.assertEqual(merged.contents, "正文内容\n夏季更新")
        self.assertNotIn("【标题】", merged.contents)
        self.assertNotIn("【正文】", merged.contents)

    async def test_llm_structurally_valid_english_title_keeps_body_without_protocol_markers(self):
        response = "【标题】Summer Update\n【正文】\nFixed multiplayer issues"
        plugin = self._make_plugin(response)
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "")
        self.assertEqual(merged.contents, "Fixed multiplayer issues")
        self.assertNotIn("【标题】", merged.contents)
        self.assertNotIn("【正文】", merged.contents)

    def test_text_message_omits_empty_announcement_title(self):
        plugin = object.__new__(self.mod.SteamUpdatePush)
        plugin._cfg = lambda key, default=None: default
        plugin._summarize_text = lambda text, max_chars: text
        plugin._format_time = lambda timestamp: "2026/08/27 12:00"
        section = self.mod.AppSection(
            "123",
            "Game",
            [self.mod.NewsItem("1", "", "url-1", "正文内容", 1, "123")],
        )

        text = plugin._build_text_message([section], "2026/08/27 12:00")

        self.assertNotRegex(text, r"(?m)^-[ \t]*$")
        self.assertIn("正文内容", text)

    async def test_llm_inline_title_rejects_extra_nonempty_title_lines(self):
        response = "【标题】夏季更新\n补充说明\n【正文】\n正文内容"
        plugin = self._make_plugin(response)
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(merged.title, "")
        self.assertEqual(merged.contents, "夏季更新\n补充说明\n正文内容")
        self.assertNotIn("【标题】", merged.contents)
        self.assertNotIn("【正文】", merged.contents)

    async def test_llm_structured_response_preserves_extension_i_han_original_title(self):
        plugin = self._make_plugin("【标题】\n模型标题\n【正文】\n正文")
        items = [self.mod.NewsItem("1", "\U0002ebf0 Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        self.assertEqual(sections[0].updates[0].title, "\U0002ebf0 Update")

    async def test_llm_structured_response_preserves_extension_j_han_original_title(self):
        plugin = self._make_plugin("【标题】\n模型标题\n【正文】\n正文")
        items = [self.mod.NewsItem("1", "\U000323b0 Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        self.assertEqual(sections[0].updates[0].title, "\U000323b0 Update")

    async def test_llm_protocol_limits_simplified_chinese_to_title(self):
        plugin = self._make_plugin("【标题】\nSummer update\n【正文】\nBody remains in English", "正文必须使用英文。{content}")
        items = [self.mod.NewsItem("1", "Summer Update", "url-1", "source", 1, "123")]
        await plugin._build_sections_llm(["123"], {"123": items}, None)
        self.assertEqual(len(plugin._llm_prompts), 1)
        prompt = plugin._llm_prompts[0]
        self.assertIn("单个返回标题必须为“第一条公告原标题”的简体中文译文", prompt)
        self.assertIn("标题必须使用简体中文", prompt)
        self.assertIn("正文继续遵循前述提示词的语言与格式要求", prompt)
        self.assertIn("第一条公告原标题：Summer Update", prompt)
        self.assertNotIn("请仅使用简体中文", prompt)

    async def test_llm_structured_response_preserves_extended_han_original_title(self):
        plugin = self._make_plugin("【标题】\n模型标题\n【正文】\n正文")
        items = [self.mod.NewsItem("1", "𠮷野家 Update", "url-1", "source", 1, "123")]
        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)
        self.assertEqual(sections[0].updates[0].title, "𠮷野家 Update")

    async def test_llm_structured_response_preserves_compatibility_supplement_han_original_title(self):
        plugin = self._make_plugin("【标题】\n模型标题\n【正文】\n正文")
        items = [self.mod.NewsItem("1", "丽 Update", "url-1", "source", 1, "123")]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        self.assertEqual(sections[0].updates[0].title, "丽 Update")

    async def test_llm_multi_notice_uses_first_title_and_preserves_merged_metadata(self):
        plugin = self._make_plugin("【标题】\n首条公告译文\n【正文】\n合并后的正文")
        items = [
            self.mod.NewsItem(
                "first-gid",
                "First English Notice",
                "https://example.test/first",
                "first source",
                100,
                "123",
                image_url="https://img.test/first-cover.jpg",
                image_candidates=(
                    "https://img.test/first-1.jpg",
                    "https://img.test/shared.jpg",
                ),
            ),
            self.mod.NewsItem(
                "second-gid",
                "后续公告",
                "https://example.test/second",
                "second source",
                200,
                "123",
                image_url="https://img.test/second-cover.jpg",
                image_candidates=(
                    "https://img.test/shared.jpg",
                    "https://img.test/second-1.jpg",
                ),
            ),
        ]

        sections = await plugin._build_sections_llm(["123"], {"123": items}, None)

        merged = sections[0].updates[0]
        self.assertEqual(len(plugin._llm_prompts), 1)
        self.assertEqual(merged.title, "首条公告译文")
        self.assertEqual(merged.gid, "first-gid")
        self.assertEqual(merged.url, "https://example.test/first")
        self.assertEqual(merged.date, 200)
        self.assertEqual(
            merged.image_candidates,
            (
                "https://img.test/first-1.jpg",
                "https://img.test/shared.jpg",
                "https://img.test/first-cover.jpg",
                "https://img.test/second-1.jpg",
                "https://img.test/second-cover.jpg",
            ),
        )


    @staticmethod
    def _async_return(value):
        async def _inner(*args, **kwargs):
            return value

        return _inner
