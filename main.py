import asyncio
import hashlib
import html
import json
import math
import os
import re
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
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
STEAM_NEWS_FEED_API = "https://store.steampowered.com/feeds/news/app/{appid}/"
STEAM_IMAGE_DOMAINS = {
    "steamcommunity.com",
    "steampowered.com",
    "steamstatic.com",
    "akamaihd.net",
}
MAX_NEWS_IMAGE_BYTES = 4_000_000
MAX_NEWS_IMAGE_PIXELS = 3_500_000


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
        self._trace_seq = 0

    # --- lifecycle ---
    async def initialize(self):
        self._client = self._build_http_client()
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

    def _proxy_mode(self) -> str:
        mode = str(self._cfg("proxy_mode", "system")).strip().lower()
        if mode not in {"off", "system", "custom"}:
            return "system"
        return mode

    def _proxy_url(self) -> str:
        return str(self._cfg("proxy_url", "")).strip()

    @staticmethod
    def _mask_proxy_url(url: str) -> str:
        if not url:
            return ""
        # Hide credentials if user accidentally configured auth in URL.
        return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:***@", url)

    def _build_http_client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(10.0)
        mode = self._proxy_mode()

        if mode == "off":
            self._log_debug("network", "client init", proxy_mode=mode, trust_env=False)
            return httpx.AsyncClient(timeout=timeout, trust_env=False)

        if mode == "system":
            # Use OS/container proxy env such as HTTP_PROXY / HTTPS_PROXY.
            self._log_debug("network", "client init", proxy_mode=mode, trust_env=True)
            return httpx.AsyncClient(timeout=timeout, trust_env=True)

        proxy_url = self._proxy_url()
        if not proxy_url:
            self._log_warn(
                "network",
                "proxy_mode=custom but proxy_url is empty; fallback to direct",
            )
            return httpx.AsyncClient(timeout=timeout, trust_env=False)

        masked = self._mask_proxy_url(proxy_url)
        try:
            self._log_debug("network", "client init", proxy_mode=mode, proxy=masked)
            return httpx.AsyncClient(timeout=timeout, proxy=proxy_url, trust_env=False)
        except Exception as exc:
            self._log_warn(
                "network",
                "custom proxy init failed; fallback to direct",
                proxy=masked,
                error=exc,
            )
            return httpx.AsyncClient(timeout=timeout, trust_env=False)

    def _next_trace_id(self, prefix: str) -> str:
        self._trace_seq = (self._trace_seq + 1) % 1_000_000
        return f"{prefix}-{datetime.now().strftime('%H%M%S')}-{self._trace_seq:06d}"

    @staticmethod
    def _fmt_kv(**kwargs: Any) -> str:
        parts = []
        for key, value in kwargs.items():
            if value is None:
                continue
            parts.append(f"{key}={value}")
        return " ".join(parts)

    def _log_debug(self, stage: str, msg: str, **kwargs: Any) -> None:
        if not self._cfg("debug_log", False):
            return
        kv = self._fmt_kv(**kwargs)
        if kv:
            logger.info(f"[steam_updates][{stage}] {msg} | {kv}")
        else:
            logger.info(f"[steam_updates][{stage}] {msg}")

    def _log_warn(self, stage: str, msg: str, **kwargs: Any) -> None:
        kv = self._fmt_kv(**kwargs)
        if kv:
            logger.warning(f"[steam_updates][{stage}] {msg} | {kv}")
        else:
            logger.warning(f"[steam_updates][{stage}] {msg}")

    def _log_error(self, stage: str, msg: str, **kwargs: Any) -> None:
        kv = self._fmt_kv(**kwargs)
        if kv:
            logger.error(f"[steam_updates][{stage}] {msg} | {kv}")
        else:
            logger.error(f"[steam_updates][{stage}] {msg}")

    def _get_max_days(self) -> int:
        raw = self._cfg("max_days", 1)
        try:
            value = int(raw)
        except Exception:
            value = 1
        return max(1, value)

    def _enable_feed_fallback(self) -> bool:
        return bool(self._cfg("enable_feed_fallback", True))

    def _get_feed_timeout_sec(self) -> int:
        try:
            timeout = int(self._cfg("feed_timeout_sec", 10))
        except Exception:
            timeout = 10
        return max(3, min(timeout, 60))

    @staticmethod
    def _font_height(font: ImageFont.FreeTypeFont | None, sample: str = "测") -> int:
        if not font:
            return 0
        x0, y0, x1, y1 = font.getbbox(sample)
        return max(0, y1 - y0)

    @staticmethod
    def _is_allowed_image_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        for domain in STEAM_IMAGE_DOMAINS:
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def _get_poll_start_time(self) -> tuple[int, int]:
        raw = str(self._cfg("poll_start_time", "00:00")).strip()
        match = re.match(r"^(\d{1,2}):(\d{1,2})$", raw)
        if not match:
            return 0, 0
        hour = max(0, min(23, int(match.group(1))))
        minute = max(0, min(59, int(match.group(2))))
        return hour, minute

    def _get_poll_interval_sec(self) -> int:
        try:
            interval = int(self._cfg("poll_interval_sec", 600))
        except Exception:
            interval = 600
        return max(30, interval)

    def _next_poll_time(self, now: datetime) -> datetime:
        hour, minute = self._get_poll_start_time()
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now <= start:
            return start
        interval = self._get_poll_interval_sec()
        elapsed = (now - start).total_seconds()
        k = math.ceil(elapsed / interval)
        return start + timedelta(seconds=k * interval)

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
        while not self._stop_event.is_set():
            now = datetime.now().astimezone()
            next_run = self._next_poll_time(now)
            wait_sec = max(0, (next_run - now).total_seconds())
            self._log_debug("poll", "next schedule", now=now.isoformat(), next=next_run.isoformat(), wait_sec=int(wait_sec))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_sec)
                if self._stop_event.is_set():
                    break
            except asyncio.TimeoutError:
                pass
            try:
                await self._poll_once()
            except Exception:
                logger.exception("[steam_updates][poll] loop execution failed")

    async def _poll_once(self):
        trace = self._next_trace_id("poll")
        self._log_debug("poll", "start", trace=trace)
        if not bool(self._cfg("enable_push", True)):
            self._log_debug("poll", "skip: plugin disabled", trace=trace)
            return
        group_ids = [str(x).strip() for x in self._cfg("notify_group_ids", []) or []]
        group_ids = [g for g in group_ids if g]
        if not group_ids:
            self._log_debug("poll", "skip: notify_group_ids empty", trace=trace)
            return
        appids = self._normalize_appids(self._cfg("steam_appids", []))
        if not appids:
            self._log_debug("poll", "skip: steam_appids empty", trace=trace)
            return

        state = self._load_state()
        max_days = self._get_max_days()
        # Ensure enough items to cover all updates of today (Steam API has an upper bound).
        fetch_count = max(max_days, 50)
        self._log_debug(
            "poll",
            "resolved config",
            trace=trace,
            app_count=len(appids),
            group_count=len(group_ids),
            max_days=max_days,
            fetch_count=fetch_count,
        )
        updates_by_app: dict[str, list[NewsItem]] = {}
        for appid in appids:
            items = await self._fetch_news(appid, fetch_count)
            if not items:
                self._log_debug("poll", "appid has no updates", trace=trace, appid=appid)
                continue
            latest_gid = items[0].gid
            if state.get(appid) == latest_gid:
                self._log_debug(
                    "poll",
                    "appid unchanged",
                    trace=trace,
                    appid=appid,
                    gid=latest_gid,
                )
                continue
            updates_by_app[appid] = self._filter_recent_days(items, max_days)

        if not updates_by_app:
            self._log_debug("poll", "no app has new updates", trace=trace)
            return
        self._log_debug(
            "poll",
            "updates collected",
            trace=trace,
            app_count=len(updates_by_app),
            groups=len(group_ids),
        )

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
                sent = await self._push_image(group_ids, image_bytes)
                if not sent:
                    self._log_warn("poll", "image push failed, fallback to text", trace=trace)
                    text = self._build_text_message(sections, publish_text)
                    await self._push_text(group_ids, text)
            else:
                text = self._build_text_message(sections, publish_text)
                await self._push_text(group_ids, text)
        self._log_debug(
            "poll",
            "push finished",
            trace=trace,
            app_count=len(updates_by_app),
            groups=len(group_ids),
        )

        # update state only after push
        for appid, items in updates_by_app.items():
            state[appid] = items[0].gid

        self._save_state(state)
        self._log_debug("poll", "state updated", trace=trace, state_size=len(state))

    async def _manual_query(self, umo: str | None = None):
        trace = self._next_trace_id("manual")
        appids = self._normalize_appids(self._cfg("steam_appids", []))
        if not appids:
            self._log_warn("manual", "skip: steam_appids empty", trace=trace)
            return None, "未配置 AppID，无法查询"

        max_days = self._get_max_days()
        fetch_count = max(max_days, 50)
        self._log_debug(
            "manual",
            "start",
            trace=trace,
            app_count=len(appids),
            max_days=max_days,
            fetch_count=fetch_count,
            umo=umo,
        )

        updates_by_app: dict[str, list[NewsItem]] = {}
        for appid in appids:
            items = await self._fetch_news(appid, fetch_count, only_today=True)
            if not items:
                self._log_debug("manual", "today has no updates", trace=trace, appid=appid)
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
                    self._log_debug("manual", "fallback has no updates", trace=trace, appid=appid)
                    continue
                updates_by_app[appid] = self._filter_recent_days(items, max_days)
            self._log_debug(
                "manual",
                "fallback path used",
                trace=trace,
                app_count=len(updates_by_app),
                max_days=max_days,
            )

        if not updates_by_app:
            self._log_warn(
                "manual",
                "no updates from all sources",
                trace=trace,
                appids=",".join(appids),
            )
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
            self._log_debug("manual", "done", trace=trace, mode="text")
            return text_msg, None

        image_bytes = await self._render_card(sections, publish_text, query_text, notice)
        if image_bytes:
            self._log_debug("manual", "done", trace=trace, mode="card")
            return image_bytes, None
        text_msg = self._build_text_message(sections, publish_text, notice)
        self._log_warn("manual", "card render failed, fallback text", trace=trace)
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
    def _steam_lang(self) -> str:
        return str(self._cfg("steam_lang", "schinese")).strip() or "schinese"

    async def _fetch_news(self, appid: str, count: int, only_today: bool = True) -> list[NewsItem]:
        if not self._client:
            self._log_warn("fetch", "http client not ready", appid=appid)
            return []

        api_items = await self._fetch_news_api(appid, count)
        source = "api"
        items = api_items
        if not items and self._enable_feed_fallback():
            self._log_debug("fetch", "api empty, fallback to feed", appid=appid)
            items = await self._fetch_news_feed(appid, count)
            source = "feed"

        if not items:
            self._log_warn("fetch", "no news from api/feed", appid=appid, only_today=only_today)
            return []

        if only_today:
            filtered = self._filter_today_items(items)
            self._log_debug(
                "fetch",
                "news fetched and filtered",
                appid=appid,
                source=source,
                total=len(items),
                today=len(filtered),
            )
            return filtered

        self._log_debug("fetch", "news fetched", appid=appid, source=source, total=len(items))
        return items

    async def _fetch_news_api(self, appid: str, count: int) -> list[NewsItem]:
        params = {
            "appid": appid,
            "count": max(1, count),
            "maxlength": 0,
            "format": "json",
            "l": self._steam_lang(),
        }
        api_key = str(self._cfg("steam_web_api_key", "")).strip()
        has_key = bool(api_key)
        if api_key:
            params["key"] = api_key
        try:
            resp = await self._client.get(STEAM_NEWS_API, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self._log_warn("fetch_api", "request failed", appid=appid, error=exc, has_key=has_key)
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
        self._log_debug(
            "fetch_api",
            "request ok",
            appid=appid,
            status=resp.status_code,
            item_count=len(results),
            has_key=has_key,
        )
        return results

    @staticmethod
    def _feed_text_to_plain(text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?is)<[^>]+>", "", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _gid_from_feed(link: str, title: str, ts: int) -> str:
        link = (link or "").strip()
        match = re.search(r"/view/(\d+)", link)
        if match:
            return match.group(1)
        raw = f"{link}|{title}|{ts}"
        return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]

    @staticmethod
    def _parse_feed_pub_ts(pub_date: str) -> int:
        if not pub_date:
            return 0
        try:
            dt = parsedate_to_datetime(pub_date)
            if dt is None:
                return 0
            return int(dt.timestamp())
        except Exception:
            return 0

    async def _fetch_news_feed(self, appid: str, count: int) -> list[NewsItem]:
        feed_url = STEAM_NEWS_FEED_API.format(appid=appid)
        params = {"l": self._steam_lang()}
        timeout = self._get_feed_timeout_sec()
        try:
            resp = await self._client.get(feed_url, params=params, timeout=timeout)
            resp.raise_for_status()
            body = resp.text
        except Exception as exc:
            self._log_warn("fetch_feed", "request failed", appid=appid, error=exc)
            return []

        try:
            root = ET.fromstring(body)
        except Exception as exc:
            self._log_warn("fetch_feed", "xml parse failed", appid=appid, error=exc)
            return []

        items = root.findall(".//item")
        max_count = max(1, count)
        results: list[NewsItem] = []
        for item in items[:max_count]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "").strip()
            ts = self._parse_feed_pub_ts(pub_date)
            gid = self._gid_from_feed(link, title, ts)
            results.append(
                NewsItem(
                    gid=gid,
                    title=title,
                    url=link,
                    contents=self._feed_text_to_plain(desc),
                    date=ts,
                )
            )
        self._log_debug(
            "fetch_feed",
            "request ok",
            appid=appid,
            status=resp.status_code,
            item_count=len(results),
        )
        return results

    def _filter_today_items(self, items: list[NewsItem]) -> list[NewsItem]:
        if not items:
            return []
        now = datetime.now().astimezone()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts = int(start_of_day.timestamp())
        return [item for item in items if item.date >= start_ts]

    def _filter_recent_days(self, items: list[NewsItem], max_days: int) -> list[NewsItem]:
        if not items:
            return []
        max_days = max(1, max_days)
        tz = datetime.now().astimezone().tzinfo
        items_sorted = sorted(items, key=lambda x: x.date, reverse=True)
        day_keys: list[tuple[int, int, int]] = []
        keep_days: set[tuple[int, int, int]] = set()
        for item in items_sorted:
            dt = datetime.fromtimestamp(item.date, tz)
            key = (dt.year, dt.month, dt.day)
            if key not in keep_days:
                day_keys.append(key)
                keep_days.add(key)
                if len(day_keys) >= max_days:
                    break
        return [
            item
            for item in items_sorted
            if (datetime.fromtimestamp(item.date, tz).year,
                datetime.fromtimestamp(item.date, tz).month,
                datetime.fromtimestamp(item.date, tz).day) in keep_days
        ]

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

    def _draw_card_header(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        header_h: int,
        padding: int,
        header_font: ImageFont.FreeTypeFont,
        small_font: ImageFont.FreeTypeFont,
        title_color: tuple[int, int, int],
        muted: tuple[int, int, int],
        accent: tuple[int, int, int],
    ) -> tuple[int, int, int]:
        header_bg = (32, 36, 41)
        divider = (58, 66, 78)
        draw.rectangle([0, 0, width, header_h], fill=header_bg)
        draw.line([(0, header_h), (width, header_h)], fill=divider, width=1)

        icon_y = header_h // 2
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
        return header_bg

    def _draw_card_footer(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        total_height: int,
        footer_h: int,
        padding: int,
        header_bg: tuple[int, int, int],
        title_color: tuple[int, int, int],
        accent: tuple[int, int, int],
        small_font: ImageFont.FreeTypeFont,
        query_text: str,
    ) -> None:
        footer_y = total_height - footer_h
        draw.rectangle([0, footer_y, width, total_height], fill=header_bg)
        footer_x = max(0, padding - 18)
        text_y = footer_y + 16
        draw.text((footer_x, text_y), f"查询时间：{query_text}", font=small_font, fill=title_color)
        text_y += self._font_height(small_font, "Mg") + 6
        draw.text((footer_x, text_y), "Powered by Steam News API, AstrBot", font=small_font, fill=accent)

    def _draw_blocks(
        self,
        img: PilImage.Image,
        draw: ImageDraw.ImageDraw,
        blocks: list[RenderBlock],
        width: int,
        padding: int,
        start_y: int,
    ) -> int:
        y = start_y
        for block in blocks:
            if block.kind == "text":
                if block.text:
                    draw.text((padding, y), block.text, font=block.font, fill=block.color)
                y += self._font_height(block.font) + block.gap
            elif block.kind == "image":
                if block.image:
                    img.paste(
                        block.image,
                        (padding, y),
                        block.image if block.image.mode == "RGBA" else None,
                    )
                    y += block.image.height + block.gap
            else:
                draw.line([(0, y), (width, y)], fill=block.color, width=1)
                y += 1 + block.gap
        return y

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

        title_color = (199, 213, 224)
        muted = (143, 152, 160)
        accent = (102, 192, 244)
        header_bg = self._draw_card_header(
            draw,
            width,
            header_h,
            padding,
            header_font,
            small_font,
            title_color,
            muted,
            accent,
        )

        y = header_h + 28
        self._draw_blocks(img, draw, blocks, width, padding, y)
        self._draw_card_footer(
            draw,
            width,
            total_height,
            footer_h,
            padding,
            header_bg,
            title_color,
            accent,
            small_font,
            query_text,
        )

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
            blocks.extend(
                self._build_section_blocks(
                    sec,
                    max_text_width,
                    section_title_font,
                    body_font,
                    body_color,
                    small_font,
                    muted,
                    accent,
                    image_map,
                    image_max_width,
                    max_img_h,
                    max_chars,
                    max_imgs,
                    header_map,
                )
            )

        return blocks

    def _build_section_blocks(
        self,
        sec: AppSection,
        max_text_width: int,
        section_title_font: ImageFont.FreeTypeFont,
        body_font: ImageFont.FreeTypeFont,
        body_color: tuple[int, int, int],
        small_font: ImageFont.FreeTypeFont,
        muted: tuple[int, int, int],
        accent: tuple[int, int, int],
        image_map: dict[str, PilImage.Image],
        image_max_width: int,
        max_img_h: int,
        max_chars: int,
        max_imgs: int,
        header_map: dict[str, PilImage.Image],
    ) -> list[RenderBlock]:
        blocks: list[RenderBlock] = []
        blocks.append(RenderBlock("text", f"【{sec.title}】", section_title_font, accent, 14))
        if not sec.updates:
            blocks.append(RenderBlock("text", "暂无更新", body_font, muted, 16))
            header_img = header_map.get(sec.appid)
            if header_img:
                header_img = self._scale_image(header_img, image_max_width, max_img_h)
                blocks.append(RenderBlock("image", image=header_img, gap=14))
            return blocks
        for item in sec.updates:
            blocks.extend(self._wrap_blocks(f"• {item.title}", body_font, body_color, max_text_width))
            summary = self._summarize_text(item.contents, max_chars)
            if summary:
                blocks.extend(self._wrap_blocks(summary, body_font, body_color, max_text_width))

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
            header_img = self._scale_image(header_img, image_max_width, max_img_h)
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
                height += self._font_height(block.font) + block.gap
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
            clean = text.strip()
            if len(clean) > max_chars:
                clean = clean[: max_chars - 3].rstrip() + "..."
            return clean
        clean = self._format_news_text(text)
        clean = re.sub(r"[ 	]+", " ", clean).strip()
        if len(clean) > max_chars:
            clean = clean[: max_chars - 3].rstrip() + "..."
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
        urls = re.findall(r"https?://\S+\.(?:png|jpg|jpeg|webp|gif)", text, flags=re.I)
        return [u.rstrip(")") for u in urls]

    async def _download_image(self, url: str) -> PilImage.Image | None:
        if not self._client:
            return None
        if not self._is_allowed_image_url(url):
            self._debug(f"skip untrusted image url: {url}")
            return None
        try:
            async with self._client.stream(
                "GET", url, timeout=10, follow_redirects=False
            ) as resp:
                resp.raise_for_status()
                content_type = (resp.headers.get("content-type") or "").lower()
                if content_type and not content_type.startswith("image/"):
                    self._debug(f"skip non-image content: {url} {content_type}")
                    return None
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_NEWS_IMAGE_BYTES:
                        self._debug(f"image too large: {url}")
                        return None
            if not data:
                return None
            img = PilImage.open(BytesIO(data))
            if img.width * img.height > MAX_NEWS_IMAGE_PIXELS:
                self._debug(f"image too large (pixels): {url}")
                return None
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            return img
        except Exception as exc:
            self._debug(f"image download failed: {url} {exc}")
            return None

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
                img = await self._download_image(url)
                if img:
                    results[url] = img

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
    async def _push_text(self, group_ids: list[str], text: str) -> bool:
        chain = MessageChain(chain=[Plain(text)])
        return await self._push_chain(group_ids, chain)

    async def _push_image(self, group_ids: list[str], image_bytes: bytes) -> bool:
        path = self._save_temp_image(image_bytes)
        if not path:
            self._log_warn("push", "save temp image failed")
            return False
        try:
            chain = MessageChain(chain=[Image(file=path)])
        except Exception as exc:
            self._log_warn("push", "build image chain failed", path=path, error=exc)
            return False
        return await self._push_chain(group_ids, chain)

    async def _push_chain(self, group_ids: list[str], chain: MessageChain) -> bool:
        self._log_debug("push", "start", group_count=len(group_ids), chain_size=len(chain.chain))
        any_success = False
        for gid in group_ids:
            sent = await self._send_to_group(gid, chain)
            if not sent:
                self._log_warn("push", "group failed", group_id=gid)
            else:
                self._log_debug("push", "group sent", group_id=gid)
                any_success = True
        return any_success

    async def _send_to_group(self, group_id: str, chain: MessageChain) -> bool:
        session = self._build_session_id(group_id)
        if session:
            try:
                await self.context.send_message(session=session, message_chain=chain)
                self._log_debug("send", "via session", group_id=group_id, session=session)
                return True
            except Exception as exc:
                self._log_warn("send", "session failed", group_id=group_id, session=session, error=exc)

        # fallback: aiocqhttp bot
        if self._last_bot and hasattr(self._last_bot, "send_group_msg"):
            try:
                await self._last_bot.send_group_msg(
                    group_id=int(group_id), message=chain.chain
                )
                self._log_debug("send", "via bot", group_id=group_id)
                return True
            except Exception as exc:
                self._log_warn("send", "bot fallback failed", group_id=group_id, error=exc)
        else:
            self._log_warn("send", "no available sender", group_id=group_id)
        return False

    def _build_session_id(self, group_id: str) -> str | None:
        if ":" in group_id:
            return group_id
        platform_id = str(self._cfg("platform_id", "")).strip()
        if not platform_id:
            if self._last_platform_name:
                platform_id = self._last_platform_name
            else:
                self._log_warn("send", "platform id unresolved", group_id=group_id)
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
        user_id = ""
        for attr in ("get_sender_id", "get_user_id"):
            fn = getattr(event, attr, None)
            if callable(fn):
                try:
                    user_id = str(fn() or "").strip()
                    if user_id:
                        break
                except Exception:
                    continue
        self._log_debug(
            "manual_cmd",
            "command matched",
            command=text,
            group_id=group_id,
            user_id=user_id,
        )
        if not group_id:
            self._log_warn("manual_cmd", "reject: no group id", command=text, user_id=user_id)
            yield event.plain_result("请在群聊中使用该指令")
            return

        if not bool(self._cfg("enable_push", True)):
            self._log_warn("manual_cmd", "reject: plugin disabled", group_id=group_id, user_id=user_id)
            yield event.plain_result("插件未启用")
            return

        allowed = [str(x).strip() for x in self._cfg("notify_group_ids", []) or []]
        allowed = [g for g in allowed if g]
        if not allowed:
            self._log_warn("manual_cmd", "reject: notify_group_ids empty", group_id=group_id, user_id=user_id)
            yield event.plain_result("未配置生效群，手动查询已禁用")
            return
        if group_id not in allowed:
            self._log_warn("manual_cmd", "reject: group not allowed", group_id=group_id, user_id=user_id)
            yield event.plain_result("当前群未启用插件")
            return

        yield event.plain_result("正在查询更新，请稍后...")
        result, err = await self._manual_query(event.unified_msg_origin)
        if err:
            self._log_warn("manual_cmd", "query failed", group_id=group_id, user_id=user_id, error=err)
            yield event.plain_result(err)
            return
        if isinstance(result, bytes):
            path = self._save_temp_image(result)
            if path:
                self._log_debug("manual_cmd", "reply image", group_id=group_id, user_id=user_id, path=path)
                yield event.image_result(path)
            else:
                self._log_warn("manual_cmd", "reply image failed", group_id=group_id, user_id=user_id)
                yield event.plain_result("图片生成失败，请稍后重试")
        else:
            self._log_debug("manual_cmd", "reply text", group_id=group_id, user_id=user_id)
            yield event.plain_result(result or "暂无更新")




    @filter.command("steam_update_ping")
    async def steam_update_ping(self, event: AstrMessageEvent):
        """捕获平台信息用于推送（可隐藏）"""
        self._last_platform_name = event.get_platform_name()
        if isinstance(event, AiocqhttpMessageEvent):
            self._last_bot = event.bot
        self._log_debug(
            "ping",
            "captured sender context",
            platform=self._last_platform_name,
            has_bot=bool(self._last_bot),
        )
        yield event.plain_result("Steam 更新推送已就绪")

