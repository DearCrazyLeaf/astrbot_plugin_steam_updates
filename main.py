import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from io import BytesIO
from PIL import Image as PilImage
from PIL import ImageDraw, ImageFont

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

PLUGIN_ID = "astrbot_plugin_steam_updates"
STEAM_NEWS_API = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"


@dataclass
class NewsItem:
    gid: str
    title: str
    url: str
    contents: str
    date: int


@dataclass
class AppSection:
    appid: str
    title: str
    updates: list[NewsItem]


@dataclass
class RenderBlock:
    kind: str  # "text" | "image" | "divider"
    text: str = ""
    font: ImageFont.FreeTypeFont | None = None
    color: tuple[int, int, int] = (255, 255, 255)
    gap: int = 0
    image: PilImage.Image | None = None


class SteamUpdatePush(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._data_dir = StarTools.get_data_dir(PLUGIN_ID)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._data_dir / "state.json"
        self._app_header_dir = self._data_dir / "app_headers"
        self._app_header_dir.mkdir(parents=True, exist_ok=True)

        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_platform_name: str | None = None
        self._last_bot: Any | None = None
        self._appid_name_map: dict[str, Any] | None = None
        self._name_cache: dict[str, str] | None = None
        self._name_cache_path = self._data_dir / "app_name_cache.json"

    # --- lifecycle ---
    async def initialize(self):
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self):
        self._stop_event.set()
        if self._poll_task:
            self._poll_task.cancel()
        if self._client:
            await self._client.aclose()

    # --- config helpers ---
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _debug(self, msg: str):
        if self._cfg("debug_log", False):
            logger.info("[steam_updates] " + msg)

    # --- state ---
    def _load_state(self) -> dict[str, str]:
        if not self._state_path.exists():
            return {}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._debug(f"load state failed: {exc}")
            return {}

    def _save_state(self, state: dict[str, str]) -> None:
        try:
            self._state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self._debug(f"save state failed: {exc}")

    # --- polling ---
    async def _poll_loop(self):
        await asyncio.sleep(3)
        while not self._stop_event.is_set():
            try:
                await self._poll_once()
            except Exception as exc:
                logger.error("[steam_updates] poll failed: %s", exc)
            interval = int(self._cfg("poll_interval_sec", 600))
            interval = max(30, interval)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self):
        if not bool(self._cfg("enable_push", True)):
            return
        group_ids = [str(x).strip() for x in self._cfg("notify_group_ids", []) or []]
        group_ids = [g for g in group_ids if g]
        if not group_ids:
            self._debug("notify_group_ids empty, skip")
            return
        appids = self._normalize_appids(self._cfg("steam_appids", []))
        if not appids:
            self._debug("steam_appids empty, skip")
            return

        state = self._load_state()
        max_days = int(self._cfg("max_items_per_app", 1))
        max_days = max(1, max_days)
        # Ensure enough items to cover all updates of today (Steam API has an upper bound).
        fetch_count = max(max_days, 50)
        updates_by_app: dict[str, list[NewsItem]] = {}
        for appid in appids:
            items = await self._fetch_news(appid, fetch_count)
            if not items:
                continue
            latest_gid = items[0].gid
            if state.get(appid) == latest_gid:
                continue
            updates_by_app[appid] = items

        if not updates_by_app:
            self._debug("no updates")
            return

        sections = await self._build_sections(appids, updates_by_app)
        latest_ts = max(
            (item.date for items in updates_by_app.values() for item in items if item.date),
            default=int(datetime.now().timestamp()),
        )
        publish_text = datetime.fromtimestamp(latest_ts).strftime("%Y/%m/%d %H:%M")
        query_text = datetime.now().strftime("%Y/%m/%d %H:%M")

        if str(self._cfg("message_mode", "card")).lower() == "text":
            text = self._build_text_message(sections, publish_text)
            await self._push_text(group_ids, text)
        else:
            image_bytes = await self._render_card(sections, publish_text, query_text)
            if image_bytes:
                await self._push_image(group_ids, image_bytes)
            else:
                text = self._build_text_message(sections, publish_text)
                await self._push_text(group_ids, text)

        # update state only after push
        for appid, items in updates_by_app.items():
            state[appid] = items[0].gid

        self._save_state(state)

    async def _manual_query(self, umo: str | None = None):
        appids = self._normalize_appids(self._cfg("steam_appids", []))
        if not appids:
            return None, "未配置 AppID，无法查询"

        max_days = int(self._cfg("max_items_per_app", 1))
        max_days = max(1, max_days)
        fetch_count = max(max_days, 50)

        updates_by_app: dict[str, list[NewsItem]] = {}
        for appid in appids:
            items = await self._fetch_news(appid, fetch_count, only_today=True)
            if not items:
                continue
            updates_by_app[appid] = items

        notice = ""
        if not updates_by_app:
            if max_days > 1:
                notice = f"没有找到当天的更新信息，以下是最近 {max_days} 天的更新内容"
            else:
                notice = "没有找到当天的更新信息，以下是最近一次的更新内容"
            for appid in appids:
                items = await self._fetch_news(appid, fetch_count, only_today=False)
                if not items:
                    continue
                updates_by_app[appid] = self._filter_recent_days(items, max_days)

        if not updates_by_app:
            return None, "未获取到更新数据"

        sections = await self._build_sections(appids, updates_by_app, umo)
        latest_ts = max(
            (item.date for items in updates_by_app.values() for item in items if item.date),
            default=int(datetime.now().timestamp()),
        )
        publish_text = datetime.fromtimestamp(latest_ts).strftime("%Y/%m/%d %H:%M")
        query_text = datetime.now().strftime("%Y/%m/%d %H:%M")

        if str(self._cfg("message_mode", "card")).lower() == "text":
            text_msg = self._build_text_message(sections, publish_text, notice)
            return text_msg, None

        image_bytes = await self._render_card(sections, publish_text, query_text, notice)
        if image_bytes:
            return image_bytes, None
        text_msg = self._build_text_message(sections, publish_text, notice)
        return text_msg, None


    
    def _manual_query_commands(self) -> list[str]:
        raw = self._cfg("manual_query_command", ["STEAM更新"])
        commands: list[str] = []
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.split(",")]
            commands.extend([p for p in parts if p])
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                val = str(item).strip()
                if val:
                    commands.append(val)
        else:
            val = str(raw).strip()
            if val:
                commands.append(val)
        if not commands:
            commands = ["STEAM更新"]
        # de-dup while keeping order
        seen: set[str] = set()
        uniq: list[str] = []
        for cmd in commands:
            if cmd not in seen:
                seen.add(cmd)
                uniq.append(cmd)
        return uniq

    # --- data fetch ---
    async def _fetch_news(self, appid: str, count: int, only_today: bool = True) -> list[NewsItem]:
        if not self._client:
            return []
        params = {
            "appid": appid,
            "count": max(1, count),
            "maxlength": 0,
            "format": "json",
            "l": str(self._cfg("steam_lang", "schinese")).strip() or "schinese",
        }
        api_key = str(self._cfg("steam_web_api_key", "")).strip()
        if api_key:
            params["key"] = api_key
        try:
            resp = await self._client.get(STEAM_NEWS_API, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("[steam_updates] fetch news failed (%s): %s", appid, exc)
            return []

        items = data.get("appnews", {}).get("newsitems", []) or []
        results: list[NewsItem] = []
        for item in items:
            results.append(
                NewsItem(
                    gid=str(item.get("gid", "")),
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    contents=str(item.get("contents", "")),
                    date=int(item.get("date", 0)),
                )
            )
        if only_today:
            return self._filter_today_items(results)
        return results

    def _filter_today_items(self, items: list[NewsItem]) -> list[NewsItem]:
        if not items:
            return []
        now = datetime.now()
        start_of_day = datetime(now.year, now.month, now.day)
        start_ts = int(start_of_day.timestamp())
        return [item for item in items if item.date >= start_ts]

    def _filter_recent_days(self, items: list[NewsItem], max_days: int) -> list[NewsItem]:
        if not items:
            return []
        max_days = max(1, max_days)
        items_sorted = sorted(items, key=lambda x: x.date, reverse=True)
        day_keys: list[tuple[int, int, int]] = []
        keep_days: set[tuple[int, int, int]] = set()
        for item in items_sorted:
            dt = datetime.fromtimestamp(item.date)
            key = (dt.year, dt.month, dt.day)
            if key not in keep_days:
                day_keys.append(key)
                keep_days.add(key)
                if len(day_keys) >= max_days:
                    break
        return [item for item in items_sorted if (datetime.fromtimestamp(item.date).year,
                                                  datetime.fromtimestamp(item.date).month,
                                                  datetime.fromtimestamp(item.date).day) in keep_days]

    def _normalize_appids(self, raw_list: Any) -> list[str]:
        appids: list[str] = []
        for item in raw_list or []:
            val = str(item).strip()
            if not val:
                continue
            appids.append(val)
        return appids

    def _appid_map_path(self) -> Path:
        return Path(__file__).with_name("appid_map.json")

    def _load_appid_name_map(self) -> dict[str, Any]:
        if self._appid_name_map is not None:
            return self._appid_name_map
        path = self._appid_map_path()
        if path.exists():
            try:
                self._appid_name_map = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(self._appid_name_map, dict):
                    return self._appid_name_map
            except Exception as exc:
                self._debug(f"load appid map failed: {exc}")
        self._appid_name_map = {}
        return self._appid_name_map

    def _save_appid_name_map(self) -> None:
        if self._appid_name_map is None:
            return
        try:
            path = self._appid_map_path()
            path.write_text(
                json.dumps(self._appid_name_map, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self._debug(f"save appid map failed: {exc}")

    def _load_name_cache(self) -> dict[str, str]:
        if self._name_cache is not None:
            return self._name_cache
        if self._name_cache_path.exists():
            try:
                data = json.loads(self._name_cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._name_cache = {str(k): str(v) for k, v in data.items()}
                    return self._name_cache
            except Exception as exc:
                self._debug(f"load name cache failed: {exc}")
        self._name_cache = {}
        return self._name_cache

    def _save_name_cache(self) -> None:
        if self._name_cache is None:
            return
        try:
            self._name_cache_path.write_text(
                json.dumps(self._name_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self._debug(f"save name cache failed: {exc}")

    def _pick_name_from_map(self, value: Any, lang: str) -> str:
        if not value:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in (lang, lang.lower(), lang.upper()):
                if key in value:
                    return str(value[key]).strip()
            for key in ("default", "english", "en", "name"):
                if key in value:
                    return str(value[key]).strip()
        return ""

    async def _get_app_name(self, appid: str) -> str:
        lang = str(self._cfg("steam_lang", "schinese")).strip().lower() or "schinese"
        appid_map = self._load_appid_name_map()
        mapped = self._pick_name_from_map(appid_map.get(str(appid)), lang)
        if mapped:
            return mapped

        cache = self._load_name_cache()
        cache_key = f"{appid}:{lang}"
        if cache_key in cache:
            return cache[cache_key]

        if not self._client:
            return f"AppID {appid}"

        try:
            resp = await self._client.get(
                "https://store.steampowered.com/api/appdetails",
                params={"appids": appid, "l": lang},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            entry = data.get(str(appid)) or data.get(int(appid)) or {}
            if entry.get("success") and isinstance(entry.get("data"), dict):
                name = str(entry["data"].get("name", "")).strip()
                if name:
                    cache[cache_key] = name
                    self._save_name_cache()
                    try:
                        current = appid_map.get(str(appid))
                        if isinstance(current, dict):
                            current[str(lang)] = name
                        elif current:
                            current = {"default": str(current), str(lang): name}
                        else:
                            current = {str(lang): name}
                        appid_map[str(appid)] = current
                        self._save_appid_name_map()
                    except Exception as exc:
                        self._debug(f"update appid map failed: {exc}")
                    return name
        except Exception as exc:
            self._debug(f"fetch app name failed ({appid}): {exc}")

        return f"AppID {appid}"

    async def _resolve_app_names(self, appids: list[str]) -> dict[str, str]:
        if not appids:
            return {}
        tasks = [self._get_app_name(appid) for appid in appids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        names: dict[str, str] = {}
        for appid, name in zip(appids, results):
            if isinstance(name, Exception):
                names[appid] = f"AppID {appid}"
            else:
                names[appid] = str(name)
        return names

    # --- llm helpers ---
    async def _resolve_llm_provider_id(self, umo: str | None) -> str | None:
        provider_id = str(self._cfg("llm_provider_id", "")).strip()
        if provider_id:
            return provider_id
        if umo:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                if provider_id:
                    return str(provider_id)
            except Exception as exc:
                self._debug(f"get current chat provider failed: {exc}")
        try:
            providers = self.context.get_all_providers() or []
        except Exception as exc:
            self._debug(f"list providers failed: {exc}")
            providers = []
        for provider in providers:
            try:
                meta = provider.meta()
                pid = getattr(meta, "id", None)
                if pid:
                    return str(pid)
            except Exception:
                continue
        return None

    def _list_llm_provider_ids(self) -> list[str]:
        try:
            providers = self.context.get_all_providers() or []
        except Exception:
            providers = []
        ids: list[str] = []
        for provider in providers:
            try:
                meta = provider.meta()
                pid = getattr(meta, "id", None)
                if pid:
                    ids.append(str(pid))
            except Exception:
                continue
        return ids

    def _apply_prompt_template(self, template: str, mapping: dict[str, str]) -> str:
        text = template or ""
        for key, value in mapping.items():
            text = text.replace("{" + key + "}", value)
        return text

    def _build_llm_input(self, app_name: str, appid: str, items: list[NewsItem]) -> str:
        lines: list[str] = [f"游戏：{app_name}", f"AppID：{appid}", ""]
        for item in items:
            if item.title:
                lines.append(f"标题：{item.title}")
            if item.date:
                lines.append(f"时间：{self._format_time(item.date)}")
            content = self._format_news_text(item.contents)
            if content:
                lines.append(content)
            if item.url:
                lines.append(f"链接：{item.url}")
            lines.append("")
        return "\n".join(lines).strip()

    async def _call_llm(self, prompt: str, umo: str | None) -> str | None:
        provider_id = await self._resolve_llm_provider_id(umo)
        if not provider_id:
            providers = self._list_llm_provider_ids()
            if providers:
                self._debug(f"llm provider not configured. available={providers}")
            else:
                self._debug("llm provider not configured.")
            return None
        max_chars = int(self._cfg("content_max_chars", 800))
        max_tokens = min(2048, max(256, max_chars * 2))
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=0.2,
            )
        except Exception as exc:
            self._debug(f"llm request failed: {exc}")
            return None
        text = ""
        try:
            if hasattr(resp, "completion_text"):
                text = resp.completion_text or ""
            else:
                text = str(resp)
        except Exception:
            text = str(resp)
        text = self._clean_llm_output(text)
        return text.strip() if text else None

    def _clean_llm_output(self, text: str) -> str:
        if not text:
            return ""
        cleaned = text.strip()
        if "```" in cleaned:
            cleaned = cleaned.replace("```", "")
        return cleaned.strip()

    # --- message build ---
    async def _build_sections_native(
        self,
        appids: list[str],
        updates_by_app: dict[str, list[NewsItem]],
    ) -> list[AppSection]:
        names = await self._resolve_app_names(appids)
        sections: list[AppSection] = []
        for appid in appids:
            updates = updates_by_app.get(appid, [])
            title = names.get(appid) or f"AppID {appid}"
            sections.append(AppSection(appid=appid, title=title, updates=updates))
        return sections

    async def _build_sections_llm(
        self,
        appids: list[str],
        updates_by_app: dict[str, list[NewsItem]],
        umo: str | None,
    ) -> list[AppSection] | None:
        names = await self._resolve_app_names(appids)
        template = str(self._cfg("llm_prompt", "")).strip()
        if not template:
            self._debug("llm prompt empty, skip")
            return None
        max_chars = int(self._cfg("content_max_chars", 800))
        sections: list[AppSection] = []
        for appid in appids:
            items = updates_by_app.get(appid, [])
            title = names.get(appid) or f"AppID {appid}"
            if not items:
                sections.append(AppSection(appid=appid, title=title, updates=[]))
                continue
            raw_input = self._build_llm_input(title, appid, items)
            prompt = self._apply_prompt_template(
                template,
                {
                    "appid": appid,
                    "app_name": title,
                    "lang": str(self._cfg("steam_lang", "schinese")),
                    "max_chars": str(max_chars),
                    "content": raw_input,
                },
            )
            llm_text = await self._call_llm(prompt, umo)
            if not llm_text:
                sections.append(AppSection(appid=appid, title=title, updates=items))
                continue
            latest_ts = max((it.date for it in items if it.date), default=items[0].date)
            merged = NewsItem(
                gid=items[0].gid,
                title=items[0].title or "更新内容",
                url=items[0].url,
                contents=llm_text,
                date=latest_ts,
            )
            sections.append(AppSection(appid=appid, title=title, updates=[merged]))
        return sections

    async def _build_sections(
        self,
        appids: list[str],
        updates_by_app: dict[str, list[NewsItem]],
        umo: str | None = None,
    ) -> list[AppSection]:
        mode = str(self._cfg("content_process_mode", "plugin")).lower().strip()
        if mode == "llm":
            sections = await self._build_sections_llm(appids, updates_by_app, umo)
            if sections is not None:
                return sections
            self._debug("llm failed, fallback to plugin mode")
        return await self._build_sections_native(appids, updates_by_app)

    def _build_text_message(
        self, sections: list[AppSection], publish_text: str, notice: str = ""
    ) -> str:
        lines: list[str] = []
        lines.append("更新日志")
        lines.append(f"发布时间：{publish_text}")
        if notice:
            lines.append(notice)
        lines.append("")
        max_chars = int(self._cfg("content_max_chars", 800))
        for sec in sections:
            lines.append(f"【{sec.title}】")
            if not sec.updates:
                lines.append("暂无更新")
                lines.append("")
                continue
            for item in sec.updates:
                lines.append(f"- {item.title}")
                summary = self._summarize_text(item.contents, max_chars)
                if summary:
                    lines.append(summary)
                date_text = self._format_time(item.date)
                if date_text:
                    lines.append(f"发布于：{date_text}")
                if item.url:
                    lines.append(f"链接：{item.url}")
                lines.append("")
        return "\n".join(lines).strip()

    # --- render card ---
    async def _render_card(
        self,
        sections: list[AppSection],
        publish_text: str,
        query_text: str,
        notice: str = "",
    ) -> bytes | None:
        image_map = await self._prefetch_images(sections)
        header_map = await self._prefetch_app_headers(sections)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._render_card_sync(
                sections, publish_text, query_text, notice, image_map, header_map
            ),
        )

    def _render_card_sync(
        self,
        sections: list[AppSection],
        publish_text: str,
        query_text: str,
        notice: str,
        image_map: dict[str, PilImage.Image],
        header_map: dict[str, PilImage.Image],
    ) -> bytes | None:
        width = 900
        padding = 52
        header_h = 98
        max_text_width = width - padding * 2
        title_font = self._load_font(30, bold=True)
        header_font = self._load_font(18, bold=True)
        body_font = self._load_font(18, bold=False)
        section_title_size = max(12, int(getattr(title_font, "size", 30) * 0.95))
        section_title_font = self._load_font(section_title_size, bold=True)
        small_font = self._load_font(14, bold=False)

        blocks = self._build_card_blocks(
            sections,
            publish_text,
            max_text_width,
            title_font,
            header_font,
            body_font,
            section_title_font,
            small_font,
            image_map,
            max_text_width,
            header_map,
            notice,
        )
        body_height = self._measure_blocks_height(blocks, 0)
        footer_h = 70
        total_height = header_h + 28 + body_height + footer_h

        img = PilImage.new("RGB", (width, total_height), (23, 26, 33))
        draw = ImageDraw.Draw(img)
        self._draw_gradient(draw, width, total_height)

        # header bar
        header_bg = (32, 36, 41)
        divider = (58, 66, 78)
        draw.rectangle([0, 0, width, header_h], fill=header_bg)
        draw.line([(0, header_h), (width, header_h)], fill=divider, width=1)

        icon_y = header_h // 2
        title_color = (199, 213, 224)
        muted = (143, 152, 160)
        accent = (102, 192, 244)

        steam_font = self._load_font(30, bold=True)
        left_x = padding
        steam_text = "STEAM 游戏更新推送"
        draw.text((left_x, icon_y - 24), steam_text, font=steam_font, fill=title_color)

        header_cn = "STEAM GAME UPDATE PUSH"
        draw.text((left_x, icon_y + 16), header_cn, font=small_font, fill=muted)

        right_label = "更新日志"
        right_sub = "UPDATE LOG"
        right_w = draw.textlength(right_label, font=header_font)
        right_sub_w = draw.textlength(right_sub, font=small_font)
        draw.text(
            (width - padding - right_w, icon_y - 22),
            right_label,
            font=header_font,
            fill=accent,
        )
        draw.text(
            (width - padding - right_sub_w, icon_y + 10),
            right_sub,
            font=small_font,
            fill=muted,
        )

        y = header_h + 28
        for block in blocks:
            if block.kind == "text":
                if block.text:
                    draw.text((padding, y), block.text, font=block.font, fill=block.color)
                y += (block.font.getbbox("测")[3] if block.font else 0) + block.gap
            elif block.kind == "image":
                if block.image:
                    img.paste(block.image, (padding, y), block.image if block.image.mode == "RGBA" else None)
                    y += block.image.height + block.gap
            else:
                draw.line([(0, y), (width, y)], fill=block.color, width=1)
                y += 1 + block.gap

        footer_y = total_height - footer_h
        draw.rectangle([0, footer_y, width, total_height], fill=header_bg)
        footer_x = max(0, padding - 18)
        text_y = footer_y + 16
        draw.text((footer_x, text_y), f"查询时间：{query_text}", font=small_font, fill=title_color)
        text_y += (small_font.getbbox("Mg")[3] if small_font else 0) + 6
        draw.text((footer_x, text_y), "Powered by Steam News API, AstrBot", font=small_font, fill=accent)

        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _build_card_blocks(
        self,
        sections: list[AppSection],
        publish_text: str,
        max_text_width: int,
        title_font: ImageFont.FreeTypeFont,
        header_font: ImageFont.FreeTypeFont,
        body_font: ImageFont.FreeTypeFont,
        section_title_font: ImageFont.FreeTypeFont,
        small_font: ImageFont.FreeTypeFont,
        image_map: dict[str, PilImage.Image],
        image_max_width: int,
        header_map: dict[str, PilImage.Image],
        notice: str = "",
    ) -> list[RenderBlock]:
        blocks: list[RenderBlock] = []
        title_color = (199, 213, 224)
        accent = (102, 192, 244)
        muted = (143, 152, 160)
        body_color = (199, 213, 224)

        date_str = ""
        try:
            dt = datetime.strptime(publish_text, "%Y/%m/%d %H:%M")
            date_str = f"{dt.year}年{dt.month}月{dt.day}日"
        except Exception:
            date_str = publish_text.split(" ")[0]
        if date_str:
            blocks.append(RenderBlock("text", date_str, header_font, muted, 14))
        if notice:
            blocks.append(RenderBlock("text", notice, body_font, muted, 12))

        blocks.append(RenderBlock("text", "游戏更新日志", title_font, title_color, 18))

        max_chars = int(self._cfg("content_max_chars", 800))
        max_imgs = int(self._cfg("image_max_per_item", 1))
        max_img_h = int(self._cfg("image_max_height", 320))
        for sec in sections:
            blocks.append(RenderBlock("text", f"【{sec.title}】", section_title_font, accent, 14))
            if not sec.updates:
                blocks.append(RenderBlock("text", "暂无更新", body_font, muted, 16))
                header_img = header_map.get(sec.appid)
                if header_img:
                    header_img = self._scale_image(header_img, image_max_width, int(self._cfg("image_max_height", 320)))
                    blocks.append(RenderBlock("image", image=header_img, gap=14))
                continue
            for item in sec.updates:
                blocks.extend(self._wrap_blocks(f"• {item.title}", body_font, body_color, max_text_width))
                summary = self._summarize_text(item.contents, max_chars)
                if summary:
                    blocks.extend(self._wrap_blocks(summary, body_font, body_color, max_text_width))

                # images (first N)
                image_urls = self._extract_image_urls(item.contents)[: max(0, max_imgs)]
                for url in image_urls:
                    img = image_map.get(url)
                    if img:
                        img = self._scale_image(img, image_max_width, max_img_h)
                        blocks.append(RenderBlock("image", image=img, gap=10))
                date_text = self._format_time(item.date)
                if date_text:
                    blocks.append(RenderBlock("text", "", small_font, muted, 4))
                    blocks.append(RenderBlock("text", f"发布于：{date_text}", small_font, muted, 10))
                if item.url:
                    blocks.append(RenderBlock("text", f"{item.url}", small_font, muted, 14))
            header_img = header_map.get(sec.appid)
            if header_img:
                header_img = self._scale_image(header_img, image_max_width, int(self._cfg("image_max_height", 320)))
                blocks.append(RenderBlock("image", image=header_img, gap=14))
            blocks.append(RenderBlock("text", "", body_font, body_color, 8))

        return blocks

    def _wrap_blocks(
        self,
        text: str,
        font: ImageFont.FreeTypeFont,
        color: tuple[int, int, int],
        max_width: int,
    ) -> list[RenderBlock]:
        if not text:
            return []
        dummy = PilImage.new("RGB", (10, 10))
        draw = ImageDraw.Draw(dummy)
        lines: list[str] = []
        current = ""
        for ch in text:
            if ch == "\n":
                lines.append(current)
                current = ""
                continue
            if draw.textlength(current + ch, font=font) <= max_width:
                current += ch
            else:
                lines.append(current)
                current = ch
        if current:
            lines.append(current)
        return [RenderBlock("text", line, font, color, 6) for line in lines]

    def _measure_blocks_height(
        self,
        blocks: list[RenderBlock],
        padding: int,
    ) -> int:
        height = padding
        for block in blocks:
            if block.kind == "text":
                height += (block.font.getbbox("测")[3] if block.font else 0) + block.gap
            elif block.kind == "image":
                height += (block.image.height if block.image else 0) + block.gap
            else:
                height += 1 + block.gap
        height += padding
        return max(400, height)

    def _draw_gradient(self, draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
        top = (27, 40, 56)
        bottom = (23, 26, 33)
        for y in range(height):
            ratio = y / max(1, height - 1)
            r = int(top[0] * (1 - ratio) + bottom[0] * ratio)
            g = int(top[1] * (1 - ratio) + bottom[1] * ratio)
            b = int(top[2] * (1 - ratio) + bottom[2] * ratio)
            draw.line([(0, y), (width, y)], fill=(r, g, b))

    # --- text helpers ---
    def _summarize_text(self, text: str, max_chars: int) -> str:
        if not text:
            return ""
        if str(self._cfg("content_process_mode", "plugin")).lower().strip() == "llm":
            return text.strip()
        clean = self._format_news_text(text)
        clean = re.sub(r"[ \t]+", " ", clean).strip()
        if len(clean) > max_chars:
            clean = clean[: max_chars - 1] + "…"
        return clean


    def _format_news_text(self, text: str) -> str:
        if not text:
            return ""
        # Normalize line breaks
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Strip HTML tags
        text = re.sub(r"<[^>]+>", "", text)
        # Convert common BBCode-like tags
        text = re.sub(r"\[/?b\]", "", text, flags=re.I)
        text = re.sub(r"\[/?i\]", "", text, flags=re.I)
        text = re.sub(r"\[/?u\]", "", text, flags=re.I)
        # Convert headings to separated lines
        text = re.sub(r"\[h1\](.*?)\[/h1\]", r"\n\1\n", text, flags=re.I | re.S)
        text = re.sub(r"\[h2\](.*?)\[/h2\]", r"\n\1\n", text, flags=re.I | re.S)
        text = re.sub(r"\[h3\](.*?)\[/h3\]", r"\n\1\n", text, flags=re.I | re.S)
        # Convert list items
        text = re.sub(r"\[/?list\]", "\n", text, flags=re.I)
        text = re.sub(r"\[\*\]", "\n- ", text)
        # Remove remaining BBCode
        text = re.sub(r"\[[^\]]+\]", "", text)
        # Remove image links from content
        text = re.sub(r"https?://\S+\.(?:png|jpg|jpeg|webp|gif)", "", text, flags=re.I)
        # Cleanup spaces and redundant blank lines
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _extract_image_urls(self, text: str) -> list[str]:
        if not text:
            return []
        urls = re.findall(r"https?://\\S+\\.(?:png|jpg|jpeg|webp|gif)", text, flags=re.I)
        return [u.rstrip(")") for u in urls]

    async def _prefetch_images(self, sections: list[AppSection]) -> dict[str, PilImage.Image]:
        if not self._client:
            return {}
        urls: list[str] = []
        max_imgs = int(self._cfg("image_max_per_item", 1))
        for sec in sections:
            for item in sec.updates:
                for url in self._extract_image_urls(item.contents)[: max(0, max_imgs)]:
                    if url not in urls:
                        urls.append(url)

        if not urls:
            return {}

        semaphore = asyncio.Semaphore(3)
        results: dict[str, PilImage.Image] = {}

        async def _fetch(url: str):
            async with semaphore:
                try:
                    resp = await self._client.get(url, timeout=10)
                    resp.raise_for_status()
                    data = resp.content
                    img = PilImage.open(BytesIO(data))
                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGB")
                    results[url] = img
                except Exception as exc:
                    self._debug(f"image download failed: {url} {exc}")

        await asyncio.gather(*[_fetch(u) for u in urls])
        return results

    async def _prefetch_app_headers(self, sections: list[AppSection]) -> dict[str, PilImage.Image]:
        if not self._client:
            return {}
        appids = {sec.appid for sec in sections}
        results: dict[str, PilImage.Image] = {}
        semaphore = asyncio.Semaphore(2)

        async def _load_one(appid: str):
            async with semaphore:
                img = await self._get_app_header_image(appid)
                if img:
                    results[appid] = img

        await asyncio.gather(*[_load_one(appid) for appid in appids])
        return results

    async def _get_app_header_image(self, appid: str) -> PilImage.Image | None:
        cache_path = self._app_header_dir / f"{appid}.jpg"
        if cache_path.exists():
            try:
                img = PilImage.open(cache_path)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                return img
            except Exception:
                try:
                    cache_path.unlink()
                except Exception:
                    pass

        if not self._client:
            return None

        candidates = [
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_hero.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/hero_capsule.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
        ]
        max_bytes = 6_000_000
        for url in candidates:
            try:
                resp = await self._client.get(url, timeout=10)
                resp.raise_for_status()
                if resp.headers.get("Content-Length"):
                    if int(resp.headers["Content-Length"]) > max_bytes:
                        continue
                data = resp.content
                if len(data) > max_bytes:
                    continue
                img = PilImage.open(BytesIO(data))
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                try:
                    img.save(cache_path, format="JPEG", quality=88)
                except Exception:
                    pass
                return img
            except Exception as exc:
                self._debug(f"header download failed: {url} {exc}")
                continue
        return None

    def _scale_image(self, img: PilImage.Image, max_w: int, max_h: int) -> PilImage.Image:
        if img.width <= max_w and img.height <= max_h:
            return img
        ratio = min(max_w / img.width, max_h / img.height)
        new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        return img.resize(new_size, PilImage.LANCZOS)

    def _format_time(self, ts: int) -> str:
        if not ts:
            return ""
        return datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M")

    # --- font ---
    def _load_font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        font_dir = Path(__file__).with_name("font")
        # 1) plugin bundled fonts (prefer files in font/ folder)
        def _pick_from_dir(dir_path: Path) -> ImageFont.FreeTypeFont | None:
            if not dir_path.exists():
                return None
            fonts = [
                p
                for p in dir_path.iterdir()
                if p.is_file() and p.suffix.lower() in {".ttf", ".otf", ".ttc"}
            ]
            if not fonts:
                return None
            fonts.sort(key=lambda p: p.name.lower())
            if bold:
                bold_fonts = [
                    p
                    for p in fonts
                    if any(k in p.stem.lower() for k in ("bold", "black", "heavy", "bd"))
                ]
                for candidate in bold_fonts:
                    try:
                        return ImageFont.truetype(str(candidate), size)
                    except Exception:
                        pass
            else:
                normal_fonts = [
                    p
                    for p in fonts
                    if not any(k in p.stem.lower() for k in ("bold", "black", "heavy", "bd"))
                ]
                for candidate in normal_fonts:
                    try:
                        return ImageFont.truetype(str(candidate), size)
                    except Exception:
                        pass
            for candidate in fonts:
                try:
                    return ImageFont.truetype(str(candidate), size)
                except Exception:
                    pass
            return None

        font = _pick_from_dir(font_dir)
        if font:
            return font

        # 2) system font fallback (common CJK fonts)
        preferred = (
            [
                "msyhbd.ttc",
                "msyhbd.ttf",
                "MicrosoftYaHeiBold.ttf",
                "NotoSansCJKsc-Bold.otf",
                "NotoSansCJKsc-Bold.ttf",
                "NotoSansCJK-Bold.ttc",
            ]
            if bold
            else [
                "msyh.ttc",
                "msyh.ttf",
                "MicrosoftYaHei.ttf",
                "NotoSansCJKsc-Regular.otf",
                "NotoSansCJKsc-Regular.ttf",
                "NotoSansCJK-Regular.ttc",
            ]
        )
        system_dirs: list[Path] = []
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
        if windir:
            system_dirs.append(Path(windir) / "Fonts")
        system_dirs += [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
        ]

        for font_root in system_dirs:
            if not font_root.exists():
                continue
            for name in preferred:
                candidate = font_root / name
                if candidate.exists():
                    try:
                        return ImageFont.truetype(str(candidate), size)
                    except Exception:
                        pass

        return ImageFont.load_default()

    # --- send ---
    async def _push_text(self, group_ids: list[str], text: str):
        chain = MessageChain(chain=[Plain(text)])
        await self._push_chain(group_ids, chain)

    async def _push_image(self, group_ids: list[str], image_bytes: bytes):
        chain = MessageChain(chain=[Image(image_bytes)])
        await self._push_chain(group_ids, chain)

    async def _push_chain(self, group_ids: list[str], chain: MessageChain):
        for gid in group_ids:
            sent = await self._send_to_group(gid, chain)
            if not sent:
                logger.warning("[steam_updates] push failed: group %s", gid)

    async def _send_to_group(self, group_id: str, chain: MessageChain) -> bool:
        session = self._build_session_id(group_id)
        if session:
            try:
                await self.context.send_message(session=session, message_chain=chain)
                return True
            except Exception as exc:
                self._debug(f"send_message failed: {exc}")

        # fallback: aiocqhttp bot
        if self._last_bot and isinstance(self._last_bot, object):
            try:
                if hasattr(self._last_bot, "send_group_msg"):
                    await self._last_bot.send_group_msg(
                        group_id=int(group_id), message=chain.chain
                    )
                    return True
            except Exception as exc:
                self._debug(f"bot send_group_msg failed: {exc}")
        return False

    def _build_session_id(self, group_id: str) -> str | None:
        if ":" in group_id:
            return group_id
        platform_id = str(self._cfg("platform_id", "")).strip()
        if not platform_id:
            if self._last_platform_name:
                platform_id = self._last_platform_name
            else:
                return None
        return f"{platform_id}:GroupMessage:{group_id}"

    def _save_temp_image(self, image_bytes: bytes) -> str | None:
        if not image_bytes:
            return None
        temp_dir = self._data_dir / "temp"
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = temp_dir / f"manual_{ts}.png"
            path.write_bytes(image_bytes)
            self._trim_temp_dir(temp_dir, keep=10)
            return str(path)
        except Exception as exc:
            self._debug(f"save temp image failed: {exc}")
            return None

    def _trim_temp_dir(self, temp_dir: Path, keep: int = 10) -> None:
        try:
            files = [p for p in temp_dir.iterdir() if p.is_file()]
        except Exception:
            return
        if len(files) <= keep:
            return
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[keep:]:
            try:
                p.unlink()
            except Exception:
                pass

    def _get_event_text(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        if text:
            return text
        # fallback to adapter helpers if available
        for attr in ("get_message_plain_text", "get_plain_text", "get_message_text"):
            fn = getattr(event, attr, None)
            if callable(fn):
                try:
                    text = str(fn()).strip()
                    if text:
                        return text
                except Exception:
                    pass
        # fallback to message chain
        msg = getattr(event, "message", None)
        if msg is None:
            msg = getattr(event, "message_chain", None)
        chain = getattr(msg, "chain", None) if msg is not None else None
        parts: list[str] = []
        try:
            if isinstance(chain, list):
                for comp in chain:
                    if isinstance(comp, Plain):
                        parts.append(comp.text or "")
            elif isinstance(msg, MessageChain):
                for comp in msg.chain:
                    if isinstance(comp, Plain):
                        parts.append(comp.text or "")
        except Exception:
            pass
        text = "".join(parts).strip()
        return text

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def steam_updates_on_group_message(self, event: AstrMessageEvent):
        commands = self._manual_query_commands()
        text = self._get_event_text(event)
        if text.startswith(("/", "\uFF0F")):
            text = text[1:].strip()
        if not text:
            return
        if commands:
            if text in commands:
                pass
            elif text.casefold() in {c.casefold() for c in commands}:
                pass
            else:
                return
        else:
            return

        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if not group_id:
            yield event.plain_result("请在群聊中使用该指令")
            return

        if not bool(self._cfg("enable_push", True)):
            yield event.plain_result("插件未启用")
            return

        allowed = [str(x).strip() for x in self._cfg("notify_group_ids", []) or []]
        allowed = [g for g in allowed if g]
        if not allowed:
            yield event.plain_result("未配置生效群，手动查询已禁用")
            return
        if group_id not in allowed:
            yield event.plain_result("当前群未启用插件")
            return

        yield event.plain_result("正在查询更新，请稍后...")
        result, err = await self._manual_query(event.unified_msg_origin)
        if err:
            yield event.plain_result(err)
            return
        if isinstance(result, bytes):
            path = self._save_temp_image(result)
            if path:
                yield event.image_result(path)
            else:
                yield event.plain_result("图片生成失败，请稍后重试")
        else:
            yield event.plain_result(result or "暂无更新")




    @filter.command("steam_update_ping")
    async def steam_update_ping(self, event: AstrMessageEvent):
        """捕获平台信息用于推送（可隐藏）"""
        self._last_platform_name = event.get_platform_name()
        if isinstance(event, AiocqhttpMessageEvent):
            self._last_bot = event.bot
        yield event.plain_result("Steam 更新推送已就绪")

