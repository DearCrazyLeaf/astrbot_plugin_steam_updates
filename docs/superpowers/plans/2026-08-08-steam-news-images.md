# Steam News Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每条 Steam 游戏公告显示首张有效公告图，并在缺图或图片失败时显示固定游戏图，同时按原始比例完整展示图片。

**Architecture:** `NewsItem.image_url` 保存跨原生模式与 LLM 模式的首个规范化图片候选地址；统一候选函数解析完整 URL 和 Steam clan 占位符。预取过程按公告依次尝试候选并记录首张成功图片，渲染过程按公告选择公告图或 AppID 固定图。公告图采用仅缩小宽度的独立缩放函数，图片块负责水平居中。

**Tech Stack:** Python 3.12、`unittest`、`httpx`、Pillow、AstrBot 插件接口

## Global Constraints

- 功能分支基点为 `origin/master` 提交 `b2a13cd`。
- 每条公告最多展示一张有效公告图。
- 公告图片缺失、下载失败或解码失败时，使用对应 AppID 的固定游戏图。
- 同一游戏的多条公告分别处理，固定游戏图允许重复出现。
- 公告图宽度上限为卡片正文宽度 `796px`；原图宽度不超过上限时保持原始尺寸。
- 高度仅由原始纵横比计算，不设置公告图高度上限，不裁剪、不拉伸、不补边。
- 公告图和逐公告固定图水平居中。
- 现有图片域名许可、下载字节限制、解码像素限制、缓存和失败冷却保持生效。
- 创意工坊、限时免费、文本消息和 UMO 推送行为保持现状。
- 上游提交不包含本地维护分支的线上专有功能。

---

### Task 1: Steam 图片引用解析与 RSS 保留

**Files:**
- Create: `tests/test_news_images.py`
- Modify: `main.py:2538-2650`
- Modify: `main.py:3576-3596`

**Interfaces:**
- Produces: `_extract_news_image_candidates(text: str) -> list[str]`
- Produces: `_first_feed_image_url(item: ET.Element, description: str) -> str`
- Produces: `NewsItem.image_url` containing the first normalized image candidate URL for API, RSS, and LLM preservation

- [ ] **Step 1: Write failing parser tests**

Add `NewsImageParsingTest` to `tests/test_news_images.py`. Set `plugin._steam_lang = lambda: "english"`. Use literal fixtures for one complete URL, one `{STEAM_CLAN_IMAGE}` token, one `{STEAM_CLAN_LOC_IMAGE}` token, duplicate references, and RSS escaped HTML. Assert these exact candidates:

```python
[
    "https://clan.fastly.steamstatic.com/images/4437469/banner.png",
    "https://clan.fastly.steamstatic.com/images/3703047/localized/english.png",
    "https://clan.fastly.steamstatic.com/images/3703047/localized.png",
]
```

For `{STEAM_CLAN_IMAGE}/4437469/banner.png`, assert the first literal URL above. For `{STEAM_CLAN_LOC_IMAGE}/3703047/localized.png`, assert the two literal localized and base URLs in that order. Assert RSS `image_url` retains the first `<img src>` before `_feed_text_to_plain()` removes markup.

- [ ] **Step 2: Run parser tests and verify RED**

Copy `main.py`, `tests/test_news_images.py`, and `tests/test_free_games.py` to `/tmp/codex-steam-news-images-task1` in `astrbot-dev-test`, then run:

```bash
docker exec astrbot-dev-test sh -lc 'cd /tmp/codex-steam-news-images-task1 && python -m unittest tests.test_news_images.NewsImageParsingTest -v'
```

Expected: FAIL because `_extract_news_image_candidates` and RSS image preservation do not exist.

- [ ] **Step 3: Implement candidate extraction**

In `main.py`, add `_extract_news_image_candidates(text)` with these exact rules:

```python
STEAM_CLAN_IMAGE -> https://clan.fastly.steamstatic.com/images/<clanid>/<file>
STEAM_CLAN_LOC_IMAGE -> https://clan.fastly.steamstatic.com/images/<clanid>/<stem>/<steam_lang><suffix>, then base URL
complete official URL -> preserve verbatim
```

Preserve source order and remove duplicates. Keep `_extract_image_urls()` as a compatibility wrapper or caller of the new function where existing workshop behavior requires it. Extract RSS `<img src>` and `<enclosure url>` before cleaning the description. Set `NewsItem.image_url` to the first normalized candidate URL without downloading the image.

- [ ] **Step 4: Preserve image reference through LLM sections**

In `_build_sections_llm`, copy the first source item image reference into each one-to-one summary item. For merged summaries, use the first image reference from the source updates in source order. The native section builder keeps original `NewsItem` objects.

- [ ] **Step 5: Run parser tests and verify GREEN**

Run the Task 1 command again. Expected: all `NewsImageParsingTest` tests pass.

- [ ] **Step 6: Run existing text and section tests**

```bash
docker exec astrbot-dev-test sh -lc 'cd /tmp/codex-steam-news-images-task1 && python -m unittest tests.test_free_games -v'
```

Expected: existing tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add main.py tests/test_news_images.py
git commit -m "feat: parse Steam announcement image references"
```

### Task 2: 每条公告选择首张有效图片并回退固定图

**Files:**
- Modify: `tests/test_news_images.py`
- Modify: `main.py:3075-3109`
- Modify: `main.py:3348-3438`
- Modify: `main.py:3698-3733`

**Interfaces:**
- Consumes: `_extract_news_image_candidates(text: str) -> list[str]`
- Produces: `_item_image_candidates(item: NewsItem) -> list[str]`
- Produces: `_prefetch_images(sections: list[AppSection]) -> dict[str, PilImage.Image]`, containing only the first successful candidate URL for each ordinary news item
- Produces: `_first_prefetched_item_image(item: NewsItem, image_map: dict[str, PilImage.Image]) -> PilImage.Image | None`

- [ ] **Step 1: Write failing first-valid selection tests**

Add asynchronous tests using real `_prefetch_images()` with `_download_image` replaced only at the external download boundary. Use two items and literal candidate URLs. Assert observable results:

```python
item_a: first candidate returns None, second returns image_a
item_b: first candidate returns image_b, second candidate must never be requested
```

Assert the returned map contains only the successful candidate for each item. Assert candidate attempts remain sequential within an item while different items remain eligible for concurrent execution.

- [ ] **Step 2: Run selection tests and verify RED**

```bash
docker exec astrbot-dev-test sh -lc 'cd /tmp/codex-steam-news-images-task2 && python -m unittest tests.test_news_images.NewsImageSelectionTest -v'
```

Expected: FAIL because ordinary news prefetch currently truncates candidates before validating the first image.

- [ ] **Step 3: Implement first-valid prefetch**

Build one candidate list per item from `image_url` followed by `contents`, preserving order and removing duplicates. For ordinary game news, attempt candidates sequentially under the existing semaphore and stop after the first successful `_download_image()`. Store only the successful URL and image in `image_map`. Keep workshop and free-game image behavior unchanged.

- [ ] **Step 4: Write failing per-item fallback rendering tests**

Create one game section with three announcements:

```text
announcement A -> valid announcement image
announcement B -> candidates exist but image_map has no successful image
announcement C -> no candidates
```

Provide one fixed AppID image in `header_map`. Assert `_build_section_blocks()` emits images in this order:

```text
announcement image A
fixed image for B
fixed image for C
```

Assert the game section adds no extra fixed image after the last announcement.

- [ ] **Step 5: Run fallback tests and verify RED**

```bash
docker exec astrbot-dev-test sh -lc 'cd /tmp/codex-steam-news-images-task2 && python -m unittest tests.test_news_images.NewsImageFallbackTest -v'
```

Expected: FAIL because the current renderer appends the fixed image once per game section.

- [ ] **Step 6: Implement per-item fallback**

For ordinary game news, resolve the first prefetched image for each item. Add that image after the announcement summary; when no prefetched image exists, add `header_map[sec.appid]`. Remove the ordinary game section footer image. Keep the existing empty-section fixed image and keep workshop and free-game branches unchanged.

- [ ] **Step 7: Run Task 2 tests and verify GREEN**

Run both Task 2 test classes. Expected: all selection and fallback tests pass.

- [ ] **Step 8: Commit Task 2**

```bash
git add main.py tests/test_news_images.py
git commit -m "feat: select announcement images per news item"
```

### Task 3: 完整比例缩放、动态高度与水平居中

**Files:**
- Modify: `tests/test_news_images.py`
- Modify: `main.py:73-82`
- Modify: `main.py:3175-3197`
- Modify: `main.py:3348-3438`
- Modify: `main.py:3803-3808`
- Modify: `_conf_schema.json:277-285`

**Interfaces:**
- Produces: `RenderBlock.align: str = "left"`
- Produces: `_scale_news_image(img: PilImage.Image, max_w: int) -> PilImage.Image`

- [ ] **Step 1: Write failing scaling tests**

Use real Pillow images with hand-checked dimensions and unique colors at all four corners. Assert:

```python
1600 × 900 -> 796 × 448
400 × 1200 -> 400 × 1200
796 × 3000 -> 796 × 3000
```

Assert all four corner colors remain present after scaling. The production change these tests catch is crop, enlargement, independent height limiting, or incorrect ratio calculation.

- [ ] **Step 2: Run scaling tests and verify RED**

```bash
docker exec astrbot-dev-test sh -lc 'cd /tmp/codex-steam-news-images-task3 && python -m unittest tests.test_news_images.NewsImageLayoutTest.test_news_image_scaling -v'
```

Expected: FAIL because `_scale_news_image` does not exist.

- [ ] **Step 3: Implement width-only scaling**

Add `_scale_news_image(img, max_w)`. Return the original image when `img.width <= max_w`; otherwise resize to `(max_w, max(1, round(img.height * max_w / img.width)))` with `PilImage.LANCZOS`. Use this function only for ordinary announcement images. Fixed game images continue using `_scale_image(img, max_w, image_max_height)`.

- [ ] **Step 4: Write failing centering and dynamic-height tests**

Add an image block with width `400`, card width `900`, and padding `52`. Assert `_draw_blocks()` pastes it at x coordinate `250`:

```text
52 + (796 - 400) // 2 = 250
```

Build blocks containing a `400 × 1200` image and assert `_measure_blocks_height()` includes all 1200 image pixels plus the configured gap. Render a card and assert its output height grows by the full image height.

- [ ] **Step 5: Run layout tests and verify RED**

Run `NewsImageLayoutTest`. Expected: centering test fails because images are currently left aligned.

- [ ] **Step 6: Implement image alignment**

Add `align` to `RenderBlock`. In `_draw_blocks()`, calculate centered x coordinate inside `width - 2 * padding` when `align == "center"`; retain `padding` for left alignment. Mark ordinary announcement images and per-item fixed images as centered.

- [ ] **Step 7: Make the one-image rule explicit**

Change `_conf_schema.json` `image_max_per_item.default` from `10` to `1` and update its description to indicate that each update displays one image. Existing runtime values remain readable; ordinary game news still enforces one successful image regardless of a larger stored value.

- [ ] **Step 8: Run Task 3 tests and verify GREEN**

Run `NewsImageLayoutTest`. Expected: scaling, corner preservation, centering, and dynamic height tests pass.

- [ ] **Step 9: Run the complete test suite**

Copy the current worktree files to `/tmp/codex-steam-news-images-full` in `astrbot-dev-test`, then run:

```bash
docker exec astrbot-dev-test sh -lc 'cd /tmp/codex-steam-news-images-full && python -m unittest discover -s tests -v'
```

Expected: all existing and new tests pass with no warnings or errors.

- [ ] **Step 10: Run syntax verification**

```bash
docker exec astrbot-dev-test sh -lc 'cd /tmp/codex-steam-news-images-full && python -m py_compile main.py tests/test_news_images.py'
```

Expected: exit status 0 and no output.

- [ ] **Step 11: Commit Task 3**

```bash
git add main.py _conf_schema.json tests/test_news_images.py docs/superpowers/specs/2026-08-08-steam-news-images-design.md docs/superpowers/plans/2026-08-08-steam-news-images.md
git commit -m "feat: render Steam announcement images without cropping"
```

### Task 4: 官方样本验证与分支审查

**Files:**
- Modify only if a failing test demonstrates a defect: `main.py`, `_conf_schema.json`, `tests/test_news_images.py`

**Interfaces:**
- Consumes all prior task interfaces
- Produces no new public interface

- [ ] **Step 1: Run fixture simulation for official samples**

Feed literal API bodies from CS2 AppID `730`, Dota 2 AppID `570`, and TF2 AppID `440` into candidate extraction. Assert CS2 yields its complete CDN URL and Dota 2 and TF2 yield localized plus base Steam CDN candidates.

- [ ] **Step 2: Verify first-valid behavior without downloading production images**

Use controlled `_download_image` results for each official sample and verify exactly one selected image per announcement. Verify a fully failing sample selects the AppID fixed image during block construction.

- [ ] **Step 3: Run complete tests again**

```bash
docker exec astrbot-dev-test sh -lc 'cd /tmp/codex-steam-news-images-full && python -m unittest discover -s tests -v'
```

Expected: complete suite passes.

- [ ] **Step 4: Inspect upstream-only diff**

```bash
git diff --stat origin/master...HEAD
git diff --check origin/master...HEAD
git log --oneline origin/master..HEAD
```

Expected: changes are limited to announcement image code, configuration, tests, and the two feature documents; `git diff --check` produces no output.

- [ ] **Step 5: Commit any test-driven correction**

Only when Step 1 or Step 2 first fails for a product defect, follow a new RED-GREEN cycle and commit:

```bash
git add main.py _conf_schema.json tests/test_news_images.py
git commit -m "fix: handle Steam announcement image fallback"
```

If no correction is needed, create no empty commit.
