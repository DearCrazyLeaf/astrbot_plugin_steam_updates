import asyncio
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None
import hashlib
import html
import json
import math
import os
import re
import time
import uuid
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

PLUGIN_ID = "astrbot_plugin_steam_updates"
STEAM_NEWS_API = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
STEAM_NEWS_FEED_API = "https://store.steampowered.com/feeds/news/app/{appid}/"
FREE_GAMES_API = "https://www.gamerpower.com/api/giveaways?platform=steam&type=game"
STEAM_WORKSHOP_DETAILS_API = "/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
STEAM_IMAGE_DOMAINS = {
    "steamcommunity.com",
    "steampowered.com",
    "steamstatic.com",
    "steamusercontent.com",
    "akamaihd.net",
}
MAX_NEWS_IMAGE_BYTES = 4_000_000
MAX_NEWS_IMAGE_PIXELS = 3_500_000
MAX_NEWS_IMAGE_CACHE_FILES = 400
MAX_CARD_RENDER_PIXELS = 20_000_000


@dataclass
class NewsItem:
    gid: str
    title: str
    url: str
    contents: str
    date: int
    appid: str = ""
    image_url: str = ""
    image_candidates: tuple[str, ...] = ()


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
    align: str = "left"
    top_gap: int = 0


@dataclass(frozen=True)
class NotifyTarget:
    umo: str
    platform_id: str
    legacy_group_id: str = ""


@dataclass
class PushResult:
    succeeded: list[NotifyTarget]
    failed: list[NotifyTarget]


class SteamUpdatePush(Star):
    _CFG_GROUP_KEYS = (
        "basic_settings",
        "game_updates",
        "free_games",
        "workshop_updates",
        "manual_commands",
        "polling_settings",
        "network_proxy",
        "content_processing",
        "rendering_and_performance",
        "debug_settings",
    )

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._data_dir = StarTools.get_data_dir(PLUGIN_ID)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._data_dir / "state.json"
        self._app_header_dir = self._data_dir / "app_headers"
        self._app_header_dir.mkdir(parents=True, exist_ok=True)
        self._news_image_dir = self._data_dir / "news_images"
        self._news_image_dir.mkdir(parents=True, exist_ok=True)

        self._client: httpx.AsyncClient | None = None
        self._client_signature: str = ""
        self._poll_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_platform_id: str | None = None
        self._last_bot: Any | None = None
        self._appid_name_map: dict[str, Any] | None = None
        self._name_cache: dict[str, str] | None = None
        self._name_cache_path = self._data_dir / "app_name_cache.json"
        self._trace_seq = 0
        self._image_fail_until: dict[str, float] = {}
        self._header_fail_until: dict[str, float] = {}
        self._last_workshop_url_network_error = False
        self._poll_lock_path = self._data_dir / ".poll.lock"
        self._poll_lock_fd: int | None = None
        self._poll_lock_owner = False
        self._poll_instance_path = self._data_dir / ".poll.instance"
        self._poll_instance_token = ""

    # --- lifecycle ---
    async def initialize(self):
        await self._ensure_http_client()
        self._cancel_legacy_poll_tasks()
        self._claim_poll_instance()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self):
        self._stop_event.set()
        if self._poll_task:
            self._poll_task.cancel()
        self._release_poll_lock()
        self._release_poll_instance_claim()
        if self._client:
            await self._client.aclose()
        self._client = None
        self._client_signature = ""

    def _try_acquire_poll_lock(self) -> bool:
        if fcntl is None:
            return True
        if self._poll_lock_owner:
            return True
        try:
            if self._poll_lock_fd is None:
                self._poll_lock_path.parent.mkdir(parents=True, exist_ok=True)
                self._poll_lock_fd = os.open(
                    self._poll_lock_path,
                    os.O_RDWR | os.O_CREAT,
                    0o644,
                )
            fcntl.flock(self._poll_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._poll_lock_owner = True
            return True
        except BlockingIOError:
            return False
        except Exception as exc:
            self._log_warn("poll", "poll lock acquire failed", error=exc)
            return True

    def _release_poll_lock(self) -> None:
        fd = self._poll_lock_fd
        self._poll_lock_owner = False
        self._poll_lock_fd = None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception as exc:
            self._log_warn("poll", "poll lock release failed", error=exc)
        try:
            os.close(fd)
        except Exception as exc:
            self._log_warn("poll", "poll lock close failed", error=exc)

    def _get_poll_instance_path(self) -> Path:
        path = getattr(self, "_poll_instance_path", None)
        if isinstance(path, Path):
            return path
        data_dir = getattr(self, "_data_dir", None)
        if isinstance(data_dir, Path):
            return data_dir / ".poll.instance"
        return Path(".poll.instance")

    @staticmethod
    def _new_poll_instance_token() -> str:
        return f"{os.getpid()}-{uuid.uuid4().hex}"

    def _claim_poll_instance(self) -> None:
        token = self._new_poll_instance_token()
        path = self._get_poll_instance_path()
        self._poll_instance_token = token
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token, encoding="utf-8")
        except Exception as exc:
            self._log_warn("poll", "poll instance claim failed", error=exc)

    def _release_poll_instance_claim(self) -> None:
        token = str(getattr(self, "_poll_instance_token", "")).strip()
        if not token:
            return
        path = self._get_poll_instance_path()
        try:
            current_token = path.read_text(encoding="utf-8").strip() if path.exists() else ""
            if current_token == token:
                path.unlink(missing_ok=True)
        except Exception as exc:
            self._log_warn("poll", "poll instance release failed", error=exc)
        self._poll_instance_token = ""

    def _is_current_poll_instance(self) -> bool:
        token = str(getattr(self, "_poll_instance_token", "")).strip()
        if not token:
            return True
        path = self._get_poll_instance_path()
        try:
            claimed_token = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return False
        except Exception as exc:
            self._log_warn("poll", "poll instance read failed", error=exc)
            return False
        return claimed_token == token

    def _cancel_legacy_poll_tasks(self) -> int:
        cancelled = 0
        for task in asyncio.all_tasks():
            try:
                coro = task.get_coro()
            except Exception:
                continue
            qualname = str(getattr(coro, "__qualname__", "") or "")
            if not qualname.endswith("SteamUpdatePush._poll_loop"):
                continue
            frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
            owner = frame.f_locals.get("self") if frame and getattr(frame, "f_locals", None) else None
            if owner is None:
                continue
            owner_dir = getattr(owner, "_data_dir", None)
            if owner_dir != self._data_dir:
                continue
            try:
                task.cancel()
                cancelled += 1
            except Exception as exc:
                self._log_warn("poll", "legacy poll task cancel failed", error=exc)
        if cancelled:
            self._log_warn("poll", "cancelled legacy poll tasks", count=cancelled)
        return cancelled

    # --- config helpers ---
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, None)
            if value is not None:
                return value

            # Support grouped config schema in WebUI while keeping backward
            # compatibility with legacy flat-key configs.
            for group_key in self._CFG_GROUP_KEYS:
                group_value = self.config.get(group_key, None)
                if isinstance(group_value, dict):
                    nested = group_value.get(key, None)
                    if nested is not None:
                        return nested
            return default
        except Exception:
            return default

    def _cfg_list_values(self, key: str) -> list[str]:
        raw = self._cfg(key, []) or []
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        return [str(value or "").strip() for value in values]

    @staticmethod
    def _target_ref(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    def _target_log_fields(
        self,
        value: str,
        target_index: int,
        *,
        legacy: bool,
        message_type: str = "",
    ) -> dict[str, Any]:
        detected_type = message_type
        if not detected_type:
            try:
                detected_type = (
                    MessageSession.from_str(value)
                    .message_type
                    .value
                )
            except Exception:
                detected_type = ""
        return {
            "target_index": target_index,
            "target_ref": self._target_ref(value),
            "message_type": detected_type,
            "legacy": legacy,
        }

    def _invalid_notify_target(
        self,
        value: str,
        target_index: int,
        *,
        legacy: bool,
    ) -> None:
        self._log_warn(
            "notify_target",
            "invalid umo",
            **self._target_log_fields(
                value,
                target_index,
                legacy=legacy,
            ),
        )
        return None

    def _parse_notify_target(
        self,
        value: str,
        *,
        target_index: int,
        legacy_group_id: str = "",
    ) -> NotifyTarget | None:
        legacy = bool(legacy_group_id)
        if not value:
            return self._invalid_notify_target(
                value,
                target_index,
                legacy=legacy,
            )
        try:
            session = MessageSession.from_str(value)
        except Exception:
            return self._invalid_notify_target(
                value,
                target_index,
                legacy=legacy,
            )
        normalized = str(session)
        return NotifyTarget(
            umo=normalized,
            platform_id=str(session.platform_id),
            legacy_group_id=legacy_group_id,
        )

    def _resolve_notify_targets(self) -> list[NotifyTarget]:
        targets: list[NotifyTarget] = []
        seen: set[str] = set()

        def append(target: NotifyTarget | None) -> None:
            if target is None or target.umo in seen:
                return
            seen.add(target.umo)
            targets.append(target)

        for index, value in enumerate(
            self._cfg_list_values("notify_umos"),
            start=1,
        ):
            append(
                self._parse_notify_target(
                    value,
                    target_index=index,
                )
            )

        platform_id = str(self._cfg("platform_id", "") or "").strip()
        if not platform_id:
            platform_id = str(self._last_platform_id or "").strip()

        for index, value in enumerate(
            self._cfg_list_values("notify_group_ids"),
            start=1,
        ):
            if ":" in value:
                append(
                    self._parse_notify_target(
                        value,
                        target_index=index,
                    )
                )
                continue
            if not value:
                continue
            if not platform_id:
                self._log_warn(
                    "notify_target",
                    "legacy target missing platform id",
                    **self._target_log_fields(
                        value,
                        index,
                        legacy=True,
                        message_type="GroupMessage",
                    ),
                )
                continue
            append(
                self._parse_notify_target(
                    f"{platform_id}:GroupMessage:{value}",
                    target_index=index,
                    legacy_group_id=value,
                )
            )
        return targets

    def _manual_query_allowed(
        self,
        event_umo: str,
        group_id: str,
    ) -> bool:
        legacy_values = self._cfg_list_values("notify_group_ids")
        if group_id and group_id in {
            value
            for value in legacy_values
            if value and ":" not in value
        }:
            return True
        try:
            normalized_event = str(MessageSession.from_str(event_umo))
        except Exception:
            return False

        configured_full = self._cfg_list_values("notify_umos")
        configured_full.extend(
            value for value in legacy_values if ":" in value
        )
        for index, value in enumerate(configured_full, start=1):
            target = self._parse_notify_target(
                value,
                target_index=index,
            )
            if target is not None and target.umo == normalized_event:
                return True
        return False

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

    def _http_client_signature(self) -> str:
        return f"{self._proxy_mode()}|{self._proxy_url()}"

    async def _ensure_http_client(self) -> None:
        signature = self._http_client_signature()
        if self._client is not None and self._client_signature == signature:
            return
        old_client = self._client
        self._client = self._build_http_client()
        self._client_signature = signature
        if old_client is not None:
            try:
                await old_client.aclose()
            except Exception:
                pass

    @staticmethod
    def _exc_text(exc: Exception | None) -> str:
        if exc is None:
            return ""
        return f"{type(exc).__name__}: {exc!r}"

    @staticmethod
    def _is_retryable_network_error(exc: Exception) -> bool:
        return isinstance(exc, httpx.RequestError | httpx.TimeoutException)

    async def _request_with_network_fallback(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        await self._ensure_http_client()
        if self._client is None:
            raise RuntimeError("http client not ready")

        try:
            return await self._client.request(method, url, **kwargs)
        except Exception as exc:
            if not isinstance(exc, Exception) or not self._is_retryable_network_error(exc):
                raise

            mode = self._proxy_mode()
            fallback_kind = ""
            fallback_kwargs: dict[str, Any] = {"trust_env": False}
            if mode == "custom":
                # Custom proxy may be unavailable; retry direct once.
                fallback_kind = "direct"
            else:
                # If user filled proxy_url but mode is not custom, retry once with proxy URL.
                proxy_url = self._proxy_url()
                if proxy_url:
                    fallback_kind = "custom_proxy"
                    fallback_kwargs["proxy"] = proxy_url

            if not fallback_kind:
                raise

            self._log_warn(
                "network",
                "primary request failed, retrying once",
                method=method.upper(),
                mode=mode,
                fallback=fallback_kind,
                error=self._exc_text(exc),
            )
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), **fallback_kwargs) as client:
                return await client.request(method, url, **kwargs)

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

    def _free_games_enabled(self) -> bool:
        return bool(self._cfg("free_games_enable", False))

    def _free_games_manual_only_when_no_news(self) -> bool:
        return bool(self._cfg("free_games_manual_only_when_no_news", True))

    def _get_feed_timeout_sec(self) -> int:
        try:
            timeout = int(self._cfg("feed_timeout_sec", 10))
        except Exception:
            timeout = 10
        return max(3, min(timeout, 60))

    def _workshop_enabled(self) -> bool:
        return bool(self._cfg("workshop_enable", False))

    def _workshop_api_base(self) -> str:
        base = str(self._cfg("workshop_api_base", "https://api.steampowered.com")).strip()
        if not base:
            base = "https://api.steampowered.com"
        return base.rstrip("/")

    def _workshop_timeout_sec(self) -> int:
        try:
            timeout = int(self._cfg("workshop_timeout_sec", 10))
        except Exception:
            timeout = 10
        return max(3, min(timeout, 60))

    def _workshop_api_retry_attempts(self) -> int:
        try:
            value = int(self._cfg("workshop_api_retry_attempts", 2))
        except Exception:
            value = 2
        return max(1, min(value, 5))

    def _workshop_url_retry_attempts(self) -> int:
        try:
            value = int(self._cfg("workshop_url_retry_attempts", 2))
        except Exception:
            value = 2
        return max(1, min(value, 5))

    def _llm_timeout_sec(self) -> int:
        try:
            value = int(self._cfg("llm_timeout_sec", 20))
        except Exception:
            value = 20
        return max(5, min(value, 180))

    def _workshop_push_on_first_seen(self) -> bool:
        return bool(self._cfg("workshop_push_on_first_seen", False))

    def _news_image_timeout_sec(self) -> int:
        try:
            timeout = int(self._cfg("image_download_timeout_sec", 6))
        except Exception:
            timeout = 6
        return max(2, min(timeout, 20))

    def _header_download_timeout_sec(self) -> int:
        try:
            timeout = int(self._cfg("header_download_timeout_sec", 6))
        except Exception:
            timeout = 6
        return max(2, min(timeout, 20))

    def _prefetch_image_concurrency(self) -> int:
        try:
            n = int(self._cfg("prefetch_image_concurrency", 3))
        except Exception:
            n = 3
        return max(1, min(n, 8))

    def _prefetch_header_concurrency(self) -> int:
        try:
            n = int(self._cfg("prefetch_header_concurrency", 2))
        except Exception:
            n = 2
        return max(1, min(n, 6))

    def _enable_app_headers(self) -> bool:
        return bool(self._cfg("enable_app_headers", True))

    def _failed_download_cooldown_sec(self) -> int:
        try:
            n = int(self._cfg("failed_download_cooldown_sec", 1800))
        except Exception:
            n = 1800
        return max(30, min(n, 86_400))

    def _is_in_fail_cooldown(self, cache: dict[str, float], key: str) -> bool:
        if not key:
            return False
        now = time.monotonic()
        until = cache.get(key, 0.0)
        if until <= now:
            if key in cache:
                cache.pop(key, None)
            return False
        return True

    def _mark_fail_cooldown(self, cache: dict[str, float], key: str) -> None:
        if not key:
            return
        cache[key] = time.monotonic() + float(self._failed_download_cooldown_sec())
        # Keep the cooldown map bounded.
        if len(cache) > 2000:
            now = time.monotonic()
            for k in list(cache.keys())[:1000]:
                if cache.get(k, 0.0) <= now:
                    cache.pop(k, None)

    @staticmethod
    def _clear_fail_cooldown(cache: dict[str, float], key: str) -> None:
        if key in cache:
            cache.pop(key, None)

    def _normalize_workshop_item_ids(self, raw_list: Any) -> list[str]:
        item_ids: list[str] = []
        for item in raw_list or []:
            val = str(item).strip()
            if not val:
                continue
            if val.isdigit():
                item_ids.append(val)
            else:
                self._log_warn("workshop", "skip invalid workshop id", value=val)
        # de-dup while keeping order
        seen: set[str] = set()
        uniq: list[str] = []
        for item_id in item_ids:
            if item_id not in seen:
                seen.add(item_id)
                uniq.append(item_id)
        return uniq

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
            if not self._is_current_poll_instance():
                self._release_poll_lock()
                self._log_warn("poll", "stale poll instance detected; exiting loop")
                break
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
            if not self._is_current_poll_instance():
                self._release_poll_lock()
                self._log_warn("poll", "stale poll instance detected before execution")
                break
            if not self._try_acquire_poll_lock():
                self._log_debug("poll", "skip: lock held by another instance")
                continue
            try:
                await self._poll_once()
            except Exception:
                logger.exception("[steam_updates][poll] loop execution failed")

    async def _poll_once(self):
        trace = self._next_trace_id("poll")
        self._log_debug("poll", "start", trace=trace)
        if not self._is_current_poll_instance():
            self._log_warn(
                "poll",
                "skip: stale poll instance",
                trace=trace,
            )
            return
        if not bool(self._cfg("enable_push", True)):
            self._log_debug(
                "poll",
                "skip: plugin disabled",
                trace=trace,
            )
            return
        targets = self._resolve_notify_targets()
        if not targets:
            self._log_debug(
                "poll",
                "skip: no valid notification targets",
                trace=trace,
            )
            return
        await self._ensure_http_client()
        appids = self._normalize_appids(self._cfg("steam_appids", []))
        free_games_enabled = self._free_games_enabled()
        workshop_ids = self._normalize_workshop_item_ids(self._cfg("workshop_item_ids", []))
        workshop_enabled = self._workshop_enabled() and bool(workshop_ids)
        if not appids and not workshop_enabled and not free_games_enabled:
            self._log_debug("poll", "skip: steam_appids/workshop/free_games all empty", trace=trace)
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
            workshop_count=len(workshop_ids) if workshop_enabled else 0,
            target_count=len(targets),
            max_days=max_days,
            fetch_count=fetch_count,
        )
        updates_by_app: dict[str, list[NewsItem]] = {}
        app_state_updates: dict[str, str] = {}
        for appid in appids:
            items = await self._fetch_news(appid, fetch_count)
            if not items:
                self._log_debug("poll", "appid has no updates", trace=trace, appid=appid)
                continue

            filtered_items = self._filter_recent_days(items, max_days)
            if not filtered_items:
                self._log_debug("poll", "appid has no recent updates", trace=trace, appid=appid)
                continue

            latest_gid = filtered_items[0].gid
            seen_key = self._news_seen_state_key(appid)
            seen_gids = self._decode_news_seen_state(state.get(seen_key, ""))
            legacy_gid = str(state.get(appid, "")).strip()

            if seen_gids:
                seen_set = set(seen_gids)
                unseen_items = [item for item in filtered_items if item.gid not in seen_set]
            elif legacy_gid:
                boundary_idx = next((idx for idx, item in enumerate(filtered_items) if item.gid == legacy_gid), -1)
                if boundary_idx >= 0:
                    unseen_items = filtered_items[:boundary_idx]
                else:
                    unseen_items = [item for item in filtered_items if item.gid != legacy_gid]
            else:
                unseen_items = filtered_items

            app_state_updates[appid] = latest_gid
            app_state_updates[seen_key] = self._encode_news_seen_state(filtered_items)

            if not unseen_items:
                self._log_debug(
                    "poll",
                    "appid unchanged",
                    trace=trace,
                    appid=appid,
                    gid=latest_gid,
                )
                continue

            updates_by_app[appid] = unseen_items

        workshop_updates: list[NewsItem] = []
        workshop_state_updates: dict[str, str] = {}
        if workshop_enabled:
            workshop_updates, workshop_state_updates = await self._collect_workshop_updates_for_poll(state)
            self._log_debug(
                "poll",
                "workshop collected",
                trace=trace,
                update_count=len(workshop_updates),
                state_update_count=len(workshop_state_updates),
            )

        free_game_items_result = await self._fetch_free_game_items() if free_games_enabled else []
        free_game_items = list(free_game_items_result or [])
        free_game_state_updates: dict[str, str] = {}
        new_free_game_items: list[NewsItem] = []
        selected_free_game_items = self.PollFreeGameSelection(
            attached_items=[],
            standalone_items=[],
        )
        if free_games_enabled and free_game_items_result is not None:
            free_state_key = self._free_games_state_key()
            previous_free_gids = self._decode_news_seen_state(state.get(free_state_key, ""))
            new_free_game_items, free_snapshot = self._split_new_free_game_items(
                free_game_items,
                previous_free_gids,
            )
            selected_free_game_items = self._select_poll_free_game_items(
                has_game_updates=bool(updates_by_app),
                active_items=free_game_items,
                new_items=new_free_game_items,
            )
            free_game_state_updates[free_state_key] = json.dumps(free_snapshot, ensure_ascii=False)
            self._log_debug(
                "free_games",
                "poll collected",
                trace=trace,
                active_count=len(free_game_items),
                new_count=len(new_free_game_items),
                attached_count=len(selected_free_game_items.attached_items),
                standalone_count=len(selected_free_game_items.standalone_items),
            )
        elif free_games_enabled:
            self._log_debug(
                "free_games",
                "poll skipped snapshot update due fetch failure",
                trace=trace,
            )

        if not updates_by_app and not workshop_updates and not selected_free_game_items.standalone_items:
            self._log_debug("poll", "no app/workshop/standalone_free_game has updates", trace=trace)
            if workshop_state_updates:
                state.update(workshop_state_updates)
            if free_game_state_updates:
                state.update(free_game_state_updates)
            if not self._is_current_poll_instance():
                self._log_warn("poll", "skip state update: stale poll instance", trace=trace)
                return
            self._save_state(state)
            return
        self._log_debug(
            "poll",
            "updates collected",
            trace=trace,
            app_count=len(updates_by_app),
            workshop_update_count=len(workshop_updates),
            free_game_new_count=len(new_free_game_items),
            free_game_attached_count=len(selected_free_game_items.attached_items),
            free_game_standalone_count=len(selected_free_game_items.standalone_items),
            targets=len(targets),
        )
        if not self._is_current_poll_instance():
            self._log_warn("poll", "abort push: stale poll instance", trace=trace)
            return

        payload_batches: list[tuple[str, list[AppSection]]] = []
        if updates_by_app:
            updated_appids = [aid for aid in appids if aid in updates_by_app]
            game_sections = await self._build_sections(updated_appids, updates_by_app) if updated_appids else []
            merged_game_sections = self._merge_game_sections_with_free_games(
                game_sections,
                selected_free_game_items.attached_items,
                free_only_when_no_news=True,
            )
            if merged_game_sections:
                payload_batches.append(("game", merged_game_sections))
        elif selected_free_game_items.standalone_items:
            free_only_sections = self._merge_game_sections_with_free_games(
                [],
                selected_free_game_items.standalone_items,
                free_only_when_no_news=True,
            )
            if free_only_sections:
                payload_batches.append(("game", free_only_sections))
        if workshop_updates:
            workshop_sections = await self._build_workshop_sections(workshop_updates)
            payload_batches.append(("workshop", workshop_sections))

        query_text = datetime.now().strftime("%Y/%m/%d %H:%M")
        mode = str(self._cfg("message_mode", "card")).lower()
        push_results: list[PushResult] = []
        for card_kind, sections in payload_batches:
            latest_ts = max(
                (
                    item.date
                    for sec in sections
                    for item in sec.updates
                    if item.date
                ),
                default=int(datetime.now().timestamp()),
            )
            publish_text = datetime.fromtimestamp(
                latest_ts
            ).strftime("%Y/%m/%d %H:%M")
            result = await self._deliver_poll_payload(
                targets,
                sections,
                publish_text,
                query_text,
                card_kind,
                mode,
            )
            push_results.append(result)
            if result.failed:
                self._log_warn(
                    "poll",
                    "payload has failed targets",
                    trace=trace,
                    card_kind=card_kind,
                    failed_count=len(result.failed),
                    succeeded_count=len(result.succeeded),
                )
        self._log_debug(
            "poll",
            "push finished",
            trace=trace,
            app_count=len(updates_by_app),
            workshop_update_count=len(workshop_updates),
            free_game_attached_count=len(selected_free_game_items.attached_items),
            free_game_standalone_count=len(selected_free_game_items.standalone_items),
            payload_count=len(payload_batches),
            targets=len(targets),
        )

        if not any(result.succeeded for result in push_results):
            self._log_warn(
                "poll",
                "all notification sends failed; state preserved",
                trace=trace,
                payload_count=len(payload_batches),
                target_count=len(targets),
            )
            return

        # update state only after push
        if app_state_updates:
            state.update(app_state_updates)
        if workshop_state_updates:
            state.update(workshop_state_updates)
        if free_game_state_updates:
            state.update(free_game_state_updates)

        if not self._is_current_poll_instance():
            self._log_warn("poll", "skip final state update: stale poll instance", trace=trace)
            return
        self._save_state(state)
        self._log_debug("poll", "state updated", trace=trace, state_size=len(state))

    async def _deliver_poll_payload(
        self,
        targets: list[NotifyTarget],
        sections: list[AppSection],
        publish_text: str,
        query_text: str,
        card_kind: str,
        mode: str,
    ) -> PushResult:
        if mode == "text":
            text = self._build_text_message(
                sections,
                publish_text,
            )
            return await self._push_text(targets, text)

        image_bytes = await self._render_card(
            sections,
            publish_text,
            query_text,
            card_kind=card_kind,
        )
        if not image_bytes:
            text = self._build_text_message(
                sections,
                publish_text,
            )
            return await self._push_text(targets, text)

        image_result = await self._push_image(
            targets,
            image_bytes,
        )
        if not image_result.failed:
            return image_result

        self._log_warn(
            "poll",
            "image target failed, fallback to text",
            card_kind=card_kind,
            failed_count=len(image_result.failed),
        )
        text = self._build_text_message(
            sections,
            publish_text,
        )
        text_result = await self._push_text(
            image_result.failed,
            text,
        )
        succeeded_targets = set(image_result.succeeded)
        succeeded_targets.update(text_result.succeeded)
        failed_targets = set(text_result.failed)
        return PushResult(
            succeeded=[
                target for target in targets
                if target in succeeded_targets
            ],
            failed=[
                target for target in targets
                if target in failed_targets
            ],
        )

    async def _manual_query(self, umo: str | None = None, query_kind: str = "all"):
        await self._ensure_http_client()
        trace = self._next_trace_id("manual")
        kind = str(query_kind or "all").strip().lower()
        if kind not in {"all", "game", "workshop"}:
            kind = "all"
        want_game = kind in {"all", "game"}
        want_workshop = kind in {"all", "workshop"}
        want_free_games = want_game and self._free_games_enabled()

        appids = self._normalize_appids(self._cfg("steam_appids", []))
        workshop_ids = self._normalize_workshop_item_ids(self._cfg("workshop_item_ids", []))
        workshop_enabled = want_workshop and self._workshop_enabled() and bool(workshop_ids)
        game_source_available = bool(appids) or want_free_games

        if kind == "game" and not game_source_available:
            self._log_warn("manual", "skip: no source for game query", trace=trace, query_kind=kind)
            if kind == "game":
                return None, "\u672a\u914d\u7f6e\u6e38\u620f AppID \u6216\u672a\u542f\u7528\u9650\u65f6\u514d\u8d39\u9886\u53d6\uff0c\u65e0\u6cd5\u67e5\u8be2"

        if kind == "workshop" and not workshop_enabled:
            self._log_warn("manual", "skip: workshop disabled or ids empty", trace=trace, query_kind=kind)
            if kind == "workshop":
                return None, "\u672a\u542f\u7528\u521b\u610f\u5de5\u574a\u8ba2\u9605\u76d1\u63a7\u6216\u672a\u914d\u7f6e\u8ba2\u9605ID\uff0c\u65e0\u6cd5\u67e5\u8be2"

        if not game_source_available and not workshop_enabled:
            self._log_warn("manual", "skip: no effective source", trace=trace, query_kind=kind)
            return None, "\u672a\u914d\u7f6e\u53ef\u7528\u7684\u67e5\u8be2\u6e90\uff0c\u65e0\u6cd5\u67e5\u8be2"

        max_days = self._get_max_days()
        fetch_count = max(max_days, 50)
        self._log_debug(
            "manual",
            "start",
            trace=trace,
            query_kind=kind,
            app_count=len(appids),
            workshop_count=len(workshop_ids) if workshop_enabled else 0,
            max_days=max_days,
            fetch_count=fetch_count,
            umo=umo,
        )

        updates_by_app: dict[str, list[NewsItem]] = {}
        if want_game:
            for appid in appids:
                items = await self._fetch_news(appid, fetch_count, only_today=True)
                if not items:
                    self._log_debug("manual", "today has no updates", trace=trace, appid=appid)
                    continue
                updates_by_app[appid] = items

        workshop_all = await self._fetch_workshop_news_items() if workshop_enabled else []
        workshop_updates = self._filter_today_items(workshop_all) if workshop_all else []
        free_game_items_result = await self._fetch_free_game_items() if want_free_games else []
        free_game_items = list(free_game_items_result or [])

        if want_game and free_game_items and not updates_by_app and not workshop_updates and self._free_games_manual_only_when_no_news():
            self._log_debug(
                "manual",
                "free games only path used",
                trace=trace,
                query_kind=kind,
                free_game_count=len(free_game_items),
            )
            game_sections = self._merge_game_sections_with_free_games(
                [],
                free_game_items,
                free_only_when_no_news=True,
            )
            payload_batches = [("game", game_sections)] if game_sections else []
            query_text = datetime.now().strftime("%Y/%m/%d %H:%M")
            mode = str(self._cfg("message_mode", "card")).lower()
            results: list[bytes | str] = []
            for card_kind, sections in payload_batches:
                latest_ts = max(
                    (item.date for sec in sections for item in sec.updates if item.date),
                    default=int(datetime.now().timestamp()),
                )
                publish_text = datetime.fromtimestamp(latest_ts).strftime("%Y/%m/%d %H:%M")
                if mode == "text":
                    results.append(self._build_text_message(sections, publish_text))
                    continue
                image_bytes = await self._render_card(
                    sections,
                    publish_text,
                    query_text,
                    card_kind=card_kind,
                )
                results.append(image_bytes if image_bytes else self._build_text_message(sections, publish_text))
            return results, None

        notice = ""
        free_games_only_without_appids = bool(want_game and free_game_items and not appids)
        if not updates_by_app and not workshop_updates:
            if max_days > 1:
                notice = f"\u6ca1\u6709\u627e\u5230\u5f53\u5929\u7684\u66f4\u65b0\u4fe1\u606f\uff0c\u4ee5\u4e0b\u662f\u6700\u8fd1 {max_days} \u5929\u7684\u66f4\u65b0\u5185\u5bb9"
            else:
                notice = "\u6ca1\u6709\u627e\u5230\u5f53\u5929\u7684\u66f4\u65b0\u4fe1\u606f\uff0c\u4ee5\u4e0b\u662f\u6700\u8fd1\u4e00\u6b21\u7684\u66f4\u65b0\u5185\u5bb9"
            if want_game:
                for appid in appids:
                    items = await self._fetch_news(appid, fetch_count, only_today=False)
                    if not items:
                        self._log_debug("manual", "fallback has no updates", trace=trace, appid=appid)
                        continue
                    updates_by_app[appid] = self._filter_recent_days(items, max_days)
            if workshop_all:
                # Workshop fallback keeps latest item of each subscribed ID.
                workshop_updates = workshop_all
            self._log_debug(
                "manual",
                "fallback path used",
                trace=trace,
                query_kind=kind,
                app_count=len(updates_by_app),
                workshop_update_count=len(workshop_updates),
                max_days=max_days,
            )

        if not updates_by_app and not workshop_updates:
            self._log_warn(
                "manual",
                "no updates from all sources",
                trace=trace,
                query_kind=kind,
                appids=",".join(appids),
            )
            if not free_games_only_without_appids:
                return None, "\u672a\u83b7\u53d6\u5230\u66f4\u65b0\u6570\u636e"
            notice = ""

        payload_batches: list[tuple[str, list[AppSection]]] = []
        if want_game and (updates_by_app or free_game_items):
            visible_appids = [appid for appid in appids if appid in updates_by_app]
            game_sections = await self._build_sections(visible_appids, updates_by_app, umo) if visible_appids else []
            merged_game_sections = self._merge_game_sections_with_free_games(
                game_sections,
                free_game_items,
                free_only_when_no_news=self._free_games_manual_only_when_no_news() or not appids,
            )
            if merged_game_sections:
                payload_batches.append(("game", merged_game_sections))
        if workshop_enabled:
            workshop_sections = await self._build_workshop_sections(workshop_updates)
            if workshop_sections:
                payload_batches.append(("workshop", workshop_sections))

        query_text = datetime.now().strftime("%Y/%m/%d %H:%M")
        mode = str(self._cfg("message_mode", "card")).lower()
        results: list[bytes | str] = []
        first_batch = True
        for card_kind, sections in payload_batches:
            latest_ts = max(
                (
                    item.date
                    for sec in sections
                    for item in sec.updates
                    if item.date
                ),
                default=int(datetime.now().timestamp()),
            )
            publish_text = datetime.fromtimestamp(latest_ts).strftime("%Y/%m/%d %H:%M")
            batch_notice = notice if first_batch else ""
            first_batch = False

            if mode == "text":
                results.append(self._build_text_message(sections, publish_text, batch_notice))
                continue

            image_bytes = await self._render_card(
                sections,
                publish_text,
                query_text,
                notice=batch_notice,
                card_kind=card_kind,
            )
            if image_bytes:
                results.append(image_bytes)
            else:
                self._log_warn("manual", "card render failed, fallback text", trace=trace, card_kind=card_kind)
                results.append(self._build_text_message(sections, publish_text, batch_notice))

        self._log_debug("manual", "done", trace=trace, mode=mode, payload_count=len(results))
        return results, None


    def _normalize_manual_commands(self, raw: Any, default_commands: list[str]) -> list[str]:
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
            commands = [str(c).strip() for c in default_commands if str(c).strip()]
        # de-dup while keeping order
        seen: set[str] = set()
        uniq: list[str] = []
        for cmd in commands:
            if cmd not in seen:
                seen.add(cmd)
                uniq.append(cmd)
        return uniq

    def _manual_game_query_commands(self) -> list[str]:
        raw = self._cfg("manual_query_game_command", None)
        if raw is None:
            # Backward compatibility for old config key.
            raw = self._cfg("manual_query_command", ["STEAM更新"])
        return self._normalize_manual_commands(raw, ["STEAM更新"])

    def _manual_workshop_query_commands(self) -> list[str]:
        raw = self._cfg("manual_query_workshop_command", ["\u521b\u610f\u5de5\u574a\u66f4\u65b0"])
        return self._normalize_manual_commands(raw, ["\u521b\u610f\u5de5\u574a\u66f4\u65b0"])

    @staticmethod
    def _normalize_manual_command_text(text: str) -> str:
        clean = str(text or "").strip()
        if clean.startswith(("/", "\uFF0F")):
            clean = clean[1:].strip()
        clean = re.sub(r"\s+", " ", clean)
        return clean

    def _match_command(self, text: str, commands: list[str]) -> bool:
        if not commands:
            return False
        normalized_text = self._normalize_manual_command_text(text)
        if not normalized_text:
            return False
        text_cf = normalized_text.casefold()
        normalized_commands = [
            self._normalize_manual_command_text(cmd) for cmd in commands if self._normalize_manual_command_text(cmd)
        ]
        if not normalized_commands:
            return False
        if text_cf in {cmd.casefold() for cmd in normalized_commands}:
            return True
        # Allow suffix args, e.g. "创意工坊更新 3240880604"
        return any(text_cf.startswith(cmd.casefold() + " ") for cmd in normalized_commands)

    # --- data fetch ---
    def _steam_lang(self) -> str:
        return str(self._cfg("steam_lang", "schinese")).strip() or "schinese"


    @staticmethod
    def _stable_news_gid(item: NewsItem) -> str:
        gid = str(item.gid or "").strip()
        if gid:
            return gid
        raw = f"{item.url}|{item.title}|{item.date}|{item.contents[:120]}"
        return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]

    def _normalize_news_items(self, items: list[NewsItem]) -> list[NewsItem]:
        merged: dict[str, NewsItem] = {}
        for item in items or []:
            gid = self._stable_news_gid(item)
            if not gid:
                continue
            normalized = NewsItem(
                gid=gid,
                title=item.title,
                url=item.url,
                contents=item.contents,
                date=int(item.date or 0),
                appid=str(item.appid or ""),
                image_url=str(item.image_url or ""),
                image_candidates=tuple(getattr(item, "image_candidates", ()) or ()),
            )
            old = merged.get(gid)
            if old is None:
                merged[gid] = normalized
                continue

            if normalized.date > old.date:
                old.date = normalized.date
            if len(normalized.contents or "") > len(old.contents or ""):
                old.contents = normalized.contents
            if normalized.title and not old.title:
                old.title = normalized.title
            if normalized.url and not old.url:
                old.url = normalized.url
            if normalized.appid and not old.appid:
                old.appid = normalized.appid
            if normalized.image_url and not old.image_url:
                old.image_url = normalized.image_url
            if normalized.image_candidates:
                old.image_candidates = tuple(
                    dict.fromkeys(
                        (*old.image_candidates, *normalized.image_candidates)
                    )
                )

        result = list(merged.values())
        result.sort(key=lambda x: (int(x.date or 0), str(x.gid)), reverse=True)
        return result

    @staticmethod
    def _news_seen_state_key(appid: str) -> str:
        return f"news_seen:{appid}"

    def _news_seen_state_limit(self) -> int:
        try:
            limit = int(self._cfg("news_seen_state_limit", 120))
        except Exception:
            limit = 120
        return max(20, min(limit, 500))

    def _encode_news_seen_state(self, items: list[NewsItem]) -> str:
        gids: list[str] = []
        seen: set[str] = set()
        for item in items:
            gid = str(item.gid or "").strip()
            if not gid or gid in seen:
                continue
            seen.add(gid)
            gids.append(gid)
            if len(gids) >= self._news_seen_state_limit():
                break
        return json.dumps(gids, ensure_ascii=False)

    @staticmethod
    def _decode_news_seen_state(raw: Any) -> list[str]:
        if raw is None:
            return []
        text = str(raw).strip()
        if not text:
            return []
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
        return [x.strip() for x in text.split(",") if x.strip()]

    @staticmethod
    def _free_games_state_key() -> str:
        return "free_games_active_gids"

    def _free_games_display_timezone_name(self) -> str:
        return str(self._cfg("display_timezone", "")).strip()

    @staticmethod
    def _system_tzinfo():
        def _zoneinfo_name_from_path(path_text: str) -> str:
            raw = str(path_text or "").strip()
            if not raw:
                return ""
            path = Path(raw)
            try:
                resolved = path.resolve()
            except Exception:
                resolved = path
            parts = resolved.parts
            for idx, part in enumerate(parts):
                if part == "zoneinfo" and idx + 1 < len(parts):
                    return "/".join(parts[idx + 1 :])
            return ""

        def _load_zoneinfo(candidate: str):
            tz_name = str(candidate or "").strip()
            if not tz_name:
                return None
            if tz_name.startswith(":"):
                tz_name = tz_name[1:].strip()
            if not tz_name:
                return None
            try:
                return ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError):
                zone_name = _zoneinfo_name_from_path(tz_name)
                if not zone_name:
                    return None
                try:
                    return ZoneInfo(zone_name)
                except (ZoneInfoNotFoundError, ValueError):
                    return None

        for candidate in (
            os.environ.get("TZ", ""),
            Path("/etc/timezone").read_text(encoding="utf-8", errors="ignore").strip()
            if Path("/etc/timezone").is_file()
            else "",
        ):
            tzinfo = _load_zoneinfo(candidate)
            if tzinfo is not None:
                return tzinfo

        for config_path in (Path("/etc/sysconfig/clock"), Path("/etc/conf.d/clock")):
            if not config_path.is_file():
                continue
            try:
                lines = config_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            for line in lines:
                match = re.match(
                    r'^\s*(?:ZONE|TIMEZONE)\s*=\s*["\']?([^"\']+)["\']?\s*$',
                    line,
                )
                if not match:
                    continue
                tzinfo = _load_zoneinfo(match.group(1))
                if tzinfo is not None:
                    return tzinfo

        localtime_path = Path("/etc/localtime")
        tzinfo = _load_zoneinfo("/etc/localtime")
        if tzinfo is not None:
            return tzinfo
        if localtime_path.is_file():
            try:
                with localtime_path.open("rb") as fh:
                    return ZoneInfo.from_file(fh)
            except Exception:
                pass

        return datetime.now().astimezone().tzinfo or timezone.utc

    def _get_display_timezone(self):
        tz_name = self._free_games_display_timezone_name()
        if not tz_name:
            return self._system_tzinfo()
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            self._log_warn(
                "free_games",
                "invalid display timezone, fallback to system",
                tz_name=tz_name,
            )
            return self._system_tzinfo()

    @staticmethod
    def _parse_free_game_datetime(raw: Any) -> int:
        text = str(raw or "").strip()
        if not text or text.lower() in {"n/a", "null", "none"}:
            return 0
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except Exception:
                continue
        try:
            iso_text = text.replace("Z", "+00:00")
            if "T" not in iso_text and " " in iso_text:
                iso_text = iso_text.replace(" ", "T", 1)
            dt = datetime.fromisoformat(iso_text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return 0

    @staticmethod
    def _format_free_game_title(title: str, official_name: str = "") -> str:
        base = str(title or "").strip() or "未知游戏"
        official = str(official_name or "").strip()
        if not official or official.casefold() == base.casefold():
            return base
        return f"{base}（{official}）"

    @staticmethod
    def _extract_free_game_appid(entry: dict[str, Any]) -> str:
        if not isinstance(entry, dict):
            return ""
        for key in ("_store_url", "store_url", "open_giveaway_url", "gamerpower_url", "description", "instructions"):
            text = str(entry.get(key) or "").strip()
            if not text:
                continue
            match = re.search(r"/app/(\d+)", text)
            if match:
                return match.group(1)
        return ""

    def _is_free_game_active(self, entry: dict[str, Any], now_ts: int | None = None) -> bool:
        if not isinstance(entry, dict):
            return False
        status = str(entry.get("status") or "").strip().lower()
        if status and status != "active":
            return False
        end_ts = self._parse_free_game_datetime(entry.get("end_date"))
        if end_ts <= 0:
            return False
        current_ts = int(now_ts if now_ts is not None else time.time())
        return end_ts > current_ts

    def _free_game_entry_to_news(
        self,
        entry: dict[str, Any],
        official_name: str = "",
        appid: str = "",
    ) -> NewsItem:
        title = self._format_free_game_title(entry.get("title", ""), official_name)
        url = str(
            entry.get("_store_url")
            or entry.get("store_url")
            or entry.get("open_giveaway_url")
            or entry.get("gamerpower_url")
            or ""
        ).strip()
        gid = str(entry.get("id") or "").strip() or url
        if not gid:
            raw = f"{title}|{entry.get('published_date') or ''}|{entry.get('end_date') or ''}"
            gid = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]
        published_ts = self._parse_free_game_datetime(entry.get("published_date"))
        end_ts = self._parse_free_game_datetime(entry.get("end_date"))
        appid = str(appid or self._extract_free_game_appid(entry)).strip()
        end_text = self._format_free_game_time(end_ts) if end_ts else (str(entry.get("end_date") or "").strip() or "未知")
        worth = str(entry.get("worth") or "").strip() or "未知"
        lines = [
            f"截止时间: {end_text}",
            f"原价: {worth}",
        ]
        return NewsItem(
            gid=gid,
            title=title,
            url=url,
            contents="\n".join(lines),
            date=published_ts or end_ts,
            appid=appid,
            image_url=str(entry.get("thumbnail") or "").strip(),
        )

    async def _resolve_free_game_store_url(self, url: str) -> str:
        target = str(url or "").strip()
        if not target or not self._client:
            return ""
        if "/app/" in target:
            return target
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            resp = await self._request_with_network_fallback(
                "GET",
                target,
                headers=headers,
                timeout=10,
                follow_redirects=False,
            )
            location = str(resp.headers.get("location") or "").strip()
            if location and "/app/" in location:
                return location
        except Exception as exc:
            self._log_debug("free_games", "store url resolve failed", url=target, error=self._exc_text(exc))
        return ""

    def _split_new_free_game_items(
        self,
        items: list[NewsItem],
        previous_gids: list[str],
    ) -> tuple[list[NewsItem], list[str]]:
        previous = {str(gid).strip() for gid in previous_gids if str(gid).strip()}
        snapshot: list[str] = []
        snapshot_seen: set[str] = set()
        new_items: list[NewsItem] = []
        for item in items or []:
            gid = str(item.gid or "").strip()
            if not gid or gid in snapshot_seen:
                continue
            snapshot_seen.add(gid)
            snapshot.append(gid)
            if gid not in previous:
                new_items.append(item)
        return new_items, snapshot

    @dataclass(frozen=True)
    class PollFreeGameSelection:
        attached_items: list[NewsItem]
        standalone_items: list[NewsItem]

    def _select_poll_free_game_items(
        self,
        has_game_updates: bool,
        active_items: list[NewsItem],
        new_items: list[NewsItem],
    ) -> "SteamUpdatePush.PollFreeGameSelection":
        if has_game_updates:
            return self.PollFreeGameSelection(
                attached_items=list(active_items or []),
                standalone_items=[],
            )
        return self.PollFreeGameSelection(
            attached_items=[],
            standalone_items=list(new_items or []),
        )

    def _merge_game_sections_with_free_games(
        self,
        game_sections: list[AppSection],
        free_game_items: list[NewsItem],
        free_only_when_no_news: bool,
    ) -> list[AppSection]:
        merged = list(game_sections or [])
        if not free_game_items:
            return merged
        free_section = AppSection(
            appid="free_games",
            title="限时免费领取",
            updates=list(free_game_items),
        )
        if merged:
            merged.append(free_section)
            return merged
        return [free_section] if free_only_when_no_news else []

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

        items = self._normalize_news_items(items)

        if only_today:
            filtered = self._normalize_news_items(self._filter_today_items(items))
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

    async def _fetch_free_game_items(self) -> list[NewsItem] | None:
        if not self._client or not self._free_games_enabled():
            return []
        try:
            resp = await self._request_with_network_fallback(
                "GET",
                FREE_GAMES_API,
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            self._log_warn("free_games", "request failed", error=self._exc_text(exc))
            return None
        if isinstance(payload, dict):
            if self._is_no_active_free_games_payload(payload):
                return []
            payload = self._extract_free_games_payload_list(payload)

        if not isinstance(payload, list):
            self._log_warn("free_games", "unexpected payload", payload_type=type(payload).__name__)
            return None

        active_entries = [
            entry for entry in payload
            if isinstance(entry, dict) and self._is_free_game_active(entry)
        ]
        if not active_entries:
            return []

        open_urls: list[str] = []
        for entry in active_entries:
            open_url = str(entry.get("open_giveaway_url") or "").strip()
            if open_url and open_url not in open_urls:
                open_urls.append(open_url)
        store_url_map: dict[str, str] = {}
        if open_urls:
            results = await asyncio.gather(
                *[self._resolve_free_game_store_url(url) for url in open_urls],
                return_exceptions=True,
            )
            for open_url, store_url in zip(open_urls, results):
                if isinstance(store_url, Exception):
                    continue
                clean_url = str(store_url or "").strip()
                if clean_url:
                    store_url_map[open_url] = clean_url

        enriched_entries: list[dict[str, Any]] = []
        for entry in active_entries:
            enriched = dict(entry)
            open_url = str(enriched.get("open_giveaway_url") or "").strip()
            if open_url and open_url in store_url_map:
                enriched["_store_url"] = store_url_map[open_url]
            enriched_entries.append(enriched)

        appids: list[str] = []
        for entry in enriched_entries:
            appid = self._extract_free_game_appid(entry)
            if appid and appid not in appids:
                appids.append(appid)
        official_names: dict[str, str] = {}
        if appids:
            results = await asyncio.gather(
                *[self._get_free_game_official_name(appid) for appid in appids],
                return_exceptions=True,
            )
            for appid, name in zip(appids, results):
                if isinstance(name, Exception):
                    continue
                clean_name = str(name or "").strip()
                if clean_name:
                    official_names[appid] = clean_name

        items = [
            self._free_game_entry_to_news(
                entry,
                official_name=official_names.get(self._extract_free_game_appid(entry), ""),
                appid=self._extract_free_game_appid(entry),
            )
            for entry in enriched_entries
        ]
        items = self._normalize_news_items(items)
        items.sort(key=lambda item: (item.date, item.title), reverse=True)
        return items

    @staticmethod
    def _is_no_active_free_games_payload(payload: dict[str, Any]) -> bool:
        status = str(payload.get("status", "")).strip()
        status_message = str(payload.get("status_message", "")).strip().lower()
        return status == "0" and "no active giveaways" in status_message

    @staticmethod
    def _extract_free_games_payload_list(payload: dict[str, Any]) -> Any:
        for key in ("giveaways", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return payload

    async def _fetch_workshop_details(self, item_ids: list[str]) -> list[dict[str, Any]]:
        if not self._client:
            self._log_warn("workshop", "http client not ready")
            return []
        if not item_ids:
            return []

        async def _request(ids: list[str]) -> tuple[list[dict[str, Any]] | None, Exception | None]:
            url = f"{self._workshop_api_base()}{STEAM_WORKSHOP_DETAILS_API}"
            data: dict[str, Any] = {"itemcount": len(ids)}
            for i, item_id in enumerate(ids):
                data[f"publishedfileids[{i}]"] = item_id
            api_key = str(self._cfg("steam_web_api_key", "")).strip()
            if api_key:
                data["key"] = api_key
            last_exc: Exception | None = None
            attempts = self._workshop_api_retry_attempts()
            for attempt in range(1, attempts + 1):
                try:
                    resp = await self._request_with_network_fallback(
                        "POST",
                        url,
                        data=data,
                        timeout=self._workshop_timeout_sec(),
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    details = payload.get("response", {}).get("publishedfiledetails", []) or []
                    return [item for item in details if isinstance(item, dict)], None
                except Exception as exc:
                    last_exc = exc
                    if attempt < attempts:
                        await asyncio.sleep(0.6 * attempt)
                    continue
            self._log_warn(
                "workshop",
                "details request failed",
                error=self._exc_text(last_exc),
                count=len(ids),
                attempts=attempts,
            )
            return None, last_exc

        batch, batch_err = await _request(item_ids)
        if batch is not None:
            self._log_debug("workshop", "details request ok", count=len(batch), mode="batch")
            return batch

        if len(item_ids) <= 1:
            return []

        # If network itself is unreachable, per-ID fallback usually only adds long delay.
        if isinstance(batch_err, httpx.RequestError | httpx.TimeoutException):
            self._log_warn("workshop", "skip single fallback due network error", count=len(item_ids))
            return []

        # Fallback: query each ID independently to avoid one bad item affecting all.
        merged: list[dict[str, Any]] = []
        for item_id in item_ids:
            single, _ = await _request([item_id])
            if single:
                merged.extend(single)
        self._log_debug("workshop", "details request fallback done", count=len(merged), mode="single")
        return merged

    @staticmethod
    def _extract_meta_content(page_text: str, key: str) -> str:
        if not page_text:
            return ""
        # Match both `property=` and `name=` style meta tags.
        patterns = [
            rf'<meta\s+[^>]*property=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']+)["\'][^>]*>',
            rf'<meta\s+[^>]*name=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']+)["\'][^>]*>',
        ]
        for pattern in patterns:
            match = re.search(pattern, page_text, flags=re.I | re.S)
            if match:
                return html.unescape(match.group(1).strip())
        return ""

    @staticmethod
    def _strip_html_text(raw_text: str) -> str:
        if not raw_text:
            return ""
        text = re.sub(r"<[^>]+>", "", raw_text, flags=re.S)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    def _parse_workshop_time_text(self, text: str) -> int:
        clean = self._strip_html_text(text)
        if not clean:
            return 0
        clean = clean.replace(" at ", " @ ")
        now = datetime.now()
        fmts = [
            "%d %b, %Y @ %I:%M%p",
            "%d %b @ %I:%M%p",
            "%d %b, %Y @ %H:%M",
            "%d %b @ %H:%M",
        ]
        for fmt in fmts:
            try:
                parsed = datetime.strptime(clean, fmt)
                if "%Y" not in fmt:
                    parsed = parsed.replace(year=now.year)
                    if parsed > now + timedelta(days=2):
                        parsed = parsed.replace(year=now.year - 1)
                return int(parsed.timestamp())
            except Exception:
                continue
        return 0

    def _extract_workshop_timestamp(self, page_text: str) -> int:
        if not page_text:
            return 0
        # First prefer raw unix timestamp embedded in script payload.
        ts_patterns = [
            r'"time_updated"\s*:\s*"?(?P<ts>\d{9,13})"?',
            r"time_updated\\\"\s*:\s*(?P<ts>\d{9,13})",
            r"'time_updated'\s*:\s*(?P<ts>\d{9,13})",
        ]
        for pattern in ts_patterns:
            match = re.search(pattern, page_text, flags=re.I)
            if not match:
                continue
            try:
                ts = int(match.group("ts"))
                if ts > 10_000_000_000:
                    ts //= 1000
                if ts > 0:
                    return ts
            except Exception:
                continue

        # Fallback to visible "Updated" row.
        left_labels = re.findall(
            r'<div class="detailsStatLeft">\s*(.*?)\s*</div>',
            page_text,
            flags=re.I | re.S,
        )
        right_values = re.findall(
            r'<div class="detailsStatRight">\s*(.*?)\s*</div>',
            page_text,
            flags=re.I | re.S,
        )
        for idx, left in enumerate(left_labels):
            label = self._strip_html_text(left).lower()
            if "updated" not in label:
                continue
            if idx >= len(right_values):
                continue
            ts = self._parse_workshop_time_text(right_values[idx])
            if ts > 0:
                return ts
        return 0

    @staticmethod
    def _parse_steamcommunity_author_path(path: str) -> str:
        p = str(path or "").strip().lstrip("/")
        if p.startswith("profiles/"):
            return p.split("/", 1)[1].strip()
        if p.startswith("id/"):
            return p.split("/", 1)[1].strip()
        return ""

    def _extract_workshop_author_from_item_page(self, page_text: str) -> str:
        if not page_text:
            return ""
        m = re.search(
            r"Created by[\s\S]{0,800}?href=[\"']https?://steamcommunity\.com/(?P<path>(?:profiles/\d{17}|id/[A-Za-z0-9_\-]+))",
            page_text,
            flags=re.I,
        )
        if m:
            return self._parse_steamcommunity_author_path(m.group("path"))
        m = re.search(
            r"href=[\"']https?://steamcommunity\.com/(?P<path>(?:profiles/\d{17}|id/[A-Za-z0-9_\-]+))",
            page_text,
            flags=re.I,
        )
        if m:
            return self._parse_steamcommunity_author_path(m.group("path"))
        return ""

    def _extract_workshop_changelog_note(self, page_text: str) -> str:
        if not page_text:
            return ""
        lower = page_text.lower()
        idx = lower.find("workshopannouncement")
        if idx < 0:
            return ""
        chunk = page_text[idx : idx + 8000]
        m = re.search(r"<p[^>]*>(?P<note>.*?)</p>", chunk, flags=re.I | re.S)
        if not m:
            return ""
        return self._strip_html_text(m.group("note"))

    def _extract_workshop_author_from_changelog_page(self, page_text: str) -> str:
        if not page_text:
            return ""
        lower = page_text.lower()
        idx = lower.find("workshopannouncement")
        if idx < 0:
            return ""
        chunk = page_text[idx : idx + 4000]
        m = re.search(
            r"changelog author[\s\S]{0,500}?href=[\"']https?://steamcommunity\.com/(?P<path>(?:profiles/\d{17}|id/[A-Za-z0-9_\-]+))",
            chunk,
            flags=re.I,
        )
        if not m:
            return ""
        return self._parse_steamcommunity_author_path(m.group("path"))

    def _build_workshop_content(self, workshop_id: str, author: str, ts: int, change_text: str) -> str:
        lines = [f"ID: {workshop_id}"]
        if author:
            lines.append(f"作者ID: {author}")
        lines.append(f"更新时间: {self._format_time(ts)}")
        lines.append(f"改动: {change_text or '暂无'}")
        return "\n".join(lines)

    def _extract_workshop_author_from_contents(self, contents: str) -> str:
        if not contents:
            return ""
        m = re.search(r"作者ID:\s*(.+)", contents)
        return m.group(1).strip() if m else ""

    async def _fetch_workshop_changelog_meta(self, workshop_id: str) -> tuple[str, str]:
        if not self._client or not workshop_id:
            return "", ""
        url = f"https://steamcommunity.com/sharedfiles/filedetails/changelog/{workshop_id}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        attempts = self._workshop_url_retry_attempts()
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._request_with_network_fallback(
                    "GET",
                    url,
                    headers=headers,
                    timeout=self._workshop_timeout_sec(),
                    follow_redirects=True,
                )
                resp.raise_for_status()
                text = resp.text
                note = self._extract_workshop_changelog_note(text)
                author = self._extract_workshop_author_from_changelog_page(text)
                return note, author
            except Exception as exc:
                last_exc = exc
                if attempt < attempts:
                    await asyncio.sleep(0.4 * attempt)
                continue
        self._log_warn(
            "workshop_changelog",
            "changelog request failed",
            workshop_id=workshop_id,
            error=self._exc_text(last_exc),
        )
        return "", ""

    def _workshop_page_to_news(
        self,
        workshop_id: str,
        page_text: str,
        final_url: str = "",
    ) -> NewsItem | None:
        if not workshop_id or not page_text:
            return None

        # Title
        title = ""
        match = re.search(
            r'<div class="workshopItemTitle">\s*(.*?)\s*</div>',
            page_text,
            flags=re.I | re.S,
        )
        if match:
            title = self._strip_html_text(match.group(1))
        if not title:
            title = self._extract_meta_content(page_text, "og:title")
        if not title:
            title_tag = re.search(r"<title>(.*?)</title>", page_text, flags=re.I | re.S)
            if title_tag:
                title = self._strip_html_text(title_tag.group(1))
        if title.lower().startswith("steam workshop::"):
            title = title.split("::", 1)[-1].strip()
        if not title:
            title = f"创意工坊项目 {workshop_id}"

        # App ID
        appid = ""
        for pattern in [
            r'"consumer_app_id"\s*:\s*"?(?P<id>\d+)"?',
            r'"consumer_appid"\s*:\s*"?(?P<id>\d+)"?',
            r'data-miniprofile-appid="(?P<id>\d+)"',
            r"/app/(?P<id>\d+)",
        ]:
            m = re.search(pattern, page_text, flags=re.I)
            if m:
                appid = str(m.group("id")).strip()
                break

        # URL path: use author id shown in page link.
        author = self._extract_workshop_author_from_item_page(page_text)

        # Updated timestamp
        ts = self._extract_workshop_timestamp(page_text)
        if ts <= 0:
            return None

        # Summary
        summary_raw = self._extract_meta_content(page_text, "og:description")
        if not summary_raw:
            summary_raw = self._extract_meta_content(page_text, "description")
        summary_raw = re.sub(r"^Steam Workshop:\s*", "", summary_raw, flags=re.I).strip()
        summary = self._summarize_text(summary_raw, 180) if summary_raw else ""

        # Cover image
        image_url = self._extract_meta_content(page_text, "og:image")

        item_url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
        if final_url and "filedetails/?id=" in final_url:
            item_url = final_url.split("&", 1)[0]

        content_lines = [f"ID: {workshop_id}"]
        if author:
            content_lines.append(f"作者ID: {author}")
        content_lines.append(f"更新时间: {self._format_time(ts)}")
        content_lines.append(f"摘要: {summary or '暂无'}")

        return NewsItem(
            gid=f"{workshop_id}:{ts}",
            title=title,
            url=item_url,
            contents=self._build_workshop_content(workshop_id, author, ts, ""),
            date=ts,
            appid=appid,
            image_url=image_url,
        )

    async def _fetch_workshop_detail_by_url(self, workshop_id: str) -> NewsItem | None:
        if not self._client or not workshop_id:
            return None
        self._last_workshop_url_network_error = False
        item_url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}&l=english"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        last_exc: Exception | None = None
        attempts = self._workshop_url_retry_attempts()
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._request_with_network_fallback(
                    "GET",
                    item_url,
                    headers=headers,
                    timeout=self._workshop_timeout_sec(),
                    follow_redirects=True,
                )
                resp.raise_for_status()
                news = self._workshop_page_to_news(workshop_id, resp.text, str(resp.url))
                if news:
                    self._log_debug("workshop_url", "url fallback hit", workshop_id=workshop_id, attempt=attempt)
                else:
                    self._log_warn("workshop_url", "url parse failed", workshop_id=workshop_id)
                return news
            except Exception as exc:
                last_exc = exc
                if attempt < attempts:
                    await asyncio.sleep(0.6 * attempt)
                continue
        if isinstance(last_exc, httpx.RequestError | httpx.TimeoutException):
            self._last_workshop_url_network_error = True
        self._log_warn("workshop_url", "url fallback failed", workshop_id=workshop_id, error=self._exc_text(last_exc))
        return None

    def _workshop_item_to_news(self, item: dict[str, Any]) -> NewsItem | None:
        workshop_id = str(item.get("publishedfileid") or "").strip()
        if not workshop_id:
            return None
        title = str(item.get("title") or f"创意工坊项目 {workshop_id}").strip()
        time_updated_raw = item.get("time_updated")
        try:
            ts = int(time_updated_raw or 0)
        except Exception:
            ts = 0
        if ts <= 0:
            return None
        url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
        preview_url = str(item.get("preview_url") or "").strip()
        appid = str(
            item.get("consumer_app_id")
            or item.get("consumer_appid")
            or item.get("creator_app_id")
            or item.get("creator_appid")
            or ""
        ).strip()
        author = str(item.get("creator") or "").strip()
        summary_raw = str(item.get("file_description") or "").strip()
        summary = self._summarize_text(summary_raw, 180) if summary_raw else ""
        content_lines = [f"ID: {workshop_id}"]
        if author:
            content_lines.append(f"作者ID: {author}")
        content_lines.append(f"更新时间: {self._format_time(ts)}")
        content_lines.append(f"摘要: {summary or '暂无'}")
        return NewsItem(
            gid=f"{workshop_id}:{ts}",
            title=title,
            url=url,
            contents=self._build_workshop_content(workshop_id, author, ts, ""),
            date=ts,
            appid=appid,
            image_url=preview_url,
        )

    async def _fetch_workshop_news_items(self) -> list[NewsItem]:
        if not self._workshop_enabled():
            return []
        item_ids = self._normalize_workshop_item_ids(self._cfg("workshop_item_ids", []))
        if not item_ids:
            return []
        details = await self._fetch_workshop_details(item_ids)
        detail_map: dict[str, dict[str, Any]] = {}
        for detail in details:
            workshop_id = str(detail.get("publishedfileid") or "").strip()
            if workshop_id:
                detail_map[workshop_id] = detail

        items: list[NewsItem] = []
        url_network_unreachable = False
        for workshop_id in item_ids:
            news: NewsItem | None = None
            from_api = False

            detail = detail_map.get(workshop_id)
            if detail:
                news = self._workshop_item_to_news(detail)
                if news:
                    from_api = True
                    self._log_debug("workshop", "api hit", workshop_id=workshop_id)
                else:
                    self._log_warn("workshop", "api miss, fallback to url", workshop_id=workshop_id)
            else:
                self._log_warn("workshop", "api miss, fallback to url", workshop_id=workshop_id)

            if not news:
                if url_network_unreachable:
                    self._log_warn("workshop", "skip url fallback due previous network timeout", workshop_id=workshop_id)
                else:
                    news = await self._fetch_workshop_detail_by_url(workshop_id)
                    if news is None and self._last_workshop_url_network_error:
                        url_network_unreachable = True

            if news:
                change_note_raw, url_author = await self._fetch_workshop_changelog_meta(workshop_id)
                change_note = self._summarize_text(change_note_raw, 180) if change_note_raw else "暂无"
                if from_api:
                    # API path: keep official creator field.
                    author = str(detail.get("creator") or "").strip() if isinstance(detail, dict) else ""
                else:
                    # URL path: use the author id shown in URL/page.
                    author = url_author or self._extract_workshop_author_from_contents(news.contents)
                news.contents = self._build_workshop_content(workshop_id, author, news.date, change_note)
                items.append(news)
            else:
                self._log_warn("workshop", "skip item after api+url miss", workshop_id=workshop_id)

        items.sort(key=lambda x: x.date, reverse=True)
        return items

    async def _collect_workshop_updates_for_poll(
        self,
        state: dict[str, str],
    ) -> tuple[list[NewsItem], dict[str, str]]:
        items = await self._fetch_workshop_news_items()
        if not items:
            return [], {}

        updates: list[NewsItem] = []
        state_updates: dict[str, str] = {}
        push_on_first = self._workshop_push_on_first_seen()
        for item in items:
            workshop_id = item.gid.split(":", 1)[0]
            state_key = f"workshop:{workshop_id}"
            prev_raw = str(state.get(state_key, "")).strip()
            try:
                prev_ts = int(prev_raw) if prev_raw else 0
            except Exception:
                prev_ts = 0

            if prev_ts <= 0:
                state_updates[state_key] = str(item.date)
                if push_on_first:
                    updates.append(item)
                continue

            if item.date > prev_ts:
                updates.append(item)
                state_updates[state_key] = str(item.date)
            elif state_key not in state:
                state_updates[state_key] = str(item.date)

        updates.sort(key=lambda x: x.date, reverse=True)
        return updates, state_updates

    async def _build_workshop_sections(self, items: list[NewsItem]) -> list[AppSection]:
        if not items:
            return []
        grouped: dict[str, list[NewsItem]] = {}
        for item in sorted(items, key=lambda x: x.date, reverse=True):
            appid = str(item.appid or "").strip()
            if not appid.isdigit():
                appid = "unknown"
            grouped.setdefault(appid, []).append(item)

        appids = [aid for aid in grouped.keys() if aid.isdigit()]
        app_names = await self._resolve_app_names(appids) if appids else {}

        order = sorted(
            grouped.keys(),
            key=lambda aid: max((it.date for it in grouped.get(aid, []) if it.date), default=0),
            reverse=True,
        )
        sections: list[AppSection] = []
        for aid in order:
            title = app_names.get(aid, "未知游戏") if aid != "unknown" else "未知游戏"
            sections.append(
                AppSection(
                    appid=f"workshop:{aid}",
                    title=title,
                    updates=grouped.get(aid, []),
                )
            )
        return sections

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
            resp = await self._request_with_network_fallback("GET", STEAM_NEWS_API, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self._log_warn("fetch_api", "request failed", appid=appid, error=self._exc_text(exc), has_key=has_key)
            return []

        items = data.get("appnews", {}).get("newsitems", []) or []
        results: list[NewsItem] = []
        for item in items:
            contents = str(item.get("contents", ""))
            image_candidates = self._extract_news_image_candidates(contents)
            results.append(
                NewsItem(
                    gid=str(item.get("gid", "")),
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    contents=contents,
                    date=int(item.get("date", 0)),
                    appid=str(appid),
                    image_url=image_candidates[0] if image_candidates else "",
                    image_candidates=tuple(image_candidates),
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

    def _feed_image_candidates(self, item: ET.Element, description: str) -> tuple[str, ...]:
        candidates: list[str] = []
        markup = html.unescape(description or "")
        for match in re.finditer(
            r"(?is)<img\b[^>]*?\bsrc\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))",
            markup,
        ):
            source = next((value for value in match.groups() if value), "")
            for url in self._extract_news_image_candidates(source):
                if url not in candidates:
                    candidates.append(url)
        for child in item.iter():
            if child.tag.rsplit("}", 1)[-1].lower() != "enclosure":
                continue
            for url in self._extract_news_image_candidates(child.attrib.get("url", "")):
                if url not in candidates:
                    candidates.append(url)
        return tuple(candidates)

    def _first_feed_image_url(self, item: ET.Element, description: str) -> str:
        candidates = self._feed_image_candidates(item, description)
        return candidates[0] if candidates else ""

    async def _fetch_news_feed(self, appid: str, count: int) -> list[NewsItem]:
        feed_url = STEAM_NEWS_FEED_API.format(appid=appid)
        params = {"l": self._steam_lang()}
        timeout = self._get_feed_timeout_sec()
        try:
            resp = await self._request_with_network_fallback("GET", feed_url, params=params, timeout=timeout)
            resp.raise_for_status()
            body = resp.text
        except Exception as exc:
            self._log_warn("fetch_feed", "request failed", appid=appid, error=self._exc_text(exc))
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
            image_candidates = self._feed_image_candidates(item, desc)
            results.append(
                NewsItem(
                    gid=gid,
                    title=title,
                    url=link,
                    contents=self._feed_text_to_plain(desc),
                    date=ts,
                    appid=str(appid),
                    image_url=image_candidates[0] if image_candidates else "",
                    image_candidates=image_candidates,
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

    async def _get_app_name_by_lang(self, appid: str, lang: str) -> str:
        lang = str(lang or "schinese").strip().lower() or "schinese"
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
            resp = await self._request_with_network_fallback(
                "GET",
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

    async def _get_app_name(self, appid: str) -> str:
        lang = str(self._cfg("steam_lang", "schinese")).strip().lower() or "schinese"
        return await self._get_app_name_by_lang(appid, lang)

    async def _get_free_game_official_name(self, appid: str) -> str:
        if not str(appid or "").strip().isdigit():
            return ""
        name = await self._get_app_name_by_lang(str(appid).strip(), "schinese")
        if name.startswith("AppID "):
            return ""
        return name

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
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=0.2,
                ),
                timeout=self._llm_timeout_sec(),
            )
            self._log_debug(
                "llm",
                "generate ok",
                provider_id=provider_id,
                ms=int((time.perf_counter() - t0) * 1000),
            )
        except asyncio.TimeoutError:
            self._log_warn(
                "llm",
                "request timeout, fallback to plugin mode",
                provider_id=provider_id,
                timeout_sec=self._llm_timeout_sec(),
            )
            return None
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
            image_candidates: list[str] = []
            for item in items:
                for url in self._item_image_candidates(item):
                    if url not in image_candidates:
                        image_candidates.append(url)
            image_url = image_candidates[0] if image_candidates else ""
            merged = NewsItem(
                gid=items[0].gid,
                title=items[0].title or "更新内容",
                url=items[0].url,
                contents=llm_text,
                date=latest_ts,
                appid=items[0].appid,
                image_url=image_url,
                image_candidates=tuple(image_candidates),
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
        is_workshop_only = bool(sections) and all(str(sec.appid).startswith("workshop") for sec in sections)
        lines.append("创意工坊更新日志" if is_workshop_only else "更新日志")
        lines.append(f"发布时间：{publish_text}")
        if notice:
            lines.append(notice)
        lines.append("")
        max_chars = int(self._cfg("content_max_chars", 800))
        for sec in sections:
            is_free_games_sec = str(sec.appid).strip().lower() == "free_games"
            lines.append(f"【{sec.title}】")
            if not sec.updates:
                lines.append("暂无更新")
                lines.append("")
                continue
            for item in sec.updates:
                lines.append(f"- {item.title}")
                if is_free_games_sec:
                    summary = self._summarize_free_game_text(item.contents, max_chars)
                else:
                    summary = self._summarize_text(item.contents, max_chars)
                if summary:
                    lines.append(summary)
                if not is_free_games_sec:
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
        card_kind: str = "game",
    ) -> bytes | None:
        t0 = time.perf_counter()
        image_map = await self._prefetch_images(sections)
        t1 = time.perf_counter()
        if self._enable_app_headers():
            header_map = await self._prefetch_app_headers(sections)
        else:
            header_map = {}
        t2 = time.perf_counter()
        loop = asyncio.get_running_loop()
        rendered = await loop.run_in_executor(
            None,
            lambda: self._render_card_sync(
                sections, publish_text, query_text, notice, image_map, header_map, card_kind
            ),
        )
        t3 = time.perf_counter()
        self._log_debug(
            "render",
            "card timing",
            card_kind=card_kind,
            sections=len(sections),
            prefetch_images_ms=int((t1 - t0) * 1000),
            prefetch_headers_ms=int((t2 - t1) * 1000),
            render_ms=int((t3 - t2) * 1000),
            total_ms=int((t3 - t0) * 1000),
        )
        return rendered

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
        left_title: str,
        left_subtitle: str,
        right_title: str = "更新日志",
        right_subtitle: str = "UPDATE LOG",
    ) -> tuple[int, int, int]:
        header_bg = (32, 36, 41)
        divider = (58, 66, 78)
        draw.rectangle([0, 0, width, header_h], fill=header_bg)
        draw.line([(0, header_h), (width, header_h)], fill=divider, width=1)

        icon_y = header_h // 2
        steam_font = self._load_font(30, bold=True)
        left_x = padding
        draw.text((left_x, icon_y - 24), left_title, font=steam_font, fill=title_color)
        draw.text((left_x, icon_y + 16), left_subtitle, font=small_font, fill=muted)

        right_w = draw.textlength(right_title, font=header_font)
        right_sub_w = draw.textlength(right_subtitle, font=small_font)
        draw.text(
            (width - padding - right_w, icon_y - 22),
            right_title,
            font=header_font,
            fill=accent,
        )
        draw.text(
            (width - padding - right_sub_w, icon_y + 10),
            right_subtitle,
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
                    y += block.top_gap
                    image_x = padding
                    if block.align == "center":
                        content_width = width - 2 * padding
                        image_x += (content_width - block.image.width) // 2
                    img.paste(
                        block.image,
                        (image_x, y),
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
        card_kind: str = "game",
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

        top_date_text = query_text or publish_text
        blocks = self._build_card_blocks(
            sections,
            top_date_text,
            card_kind,
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

        if width * total_height > MAX_CARD_RENDER_PIXELS:
            return None
        img = PilImage.new("RGB", (width, total_height), (23, 26, 33))
        draw = ImageDraw.Draw(img)
        self._draw_gradient(draw, width, total_height)

        title_color = (199, 213, 224)
        muted = (143, 152, 160)
        accent = (102, 192, 244)
        is_workshop_card = str(card_kind).strip().lower() == "workshop"
        left_title = "STEAM 创意工坊更新推送" if is_workshop_card else "STEAM 游戏更新推送"
        left_subtitle = "STEAM WORKSHOP UPDATE LOG" if is_workshop_card else "STEAM GAME UPDATE PUSH"
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
            left_title=left_title,
            left_subtitle=left_subtitle,
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
        card_kind: str,
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

        main_title = "创意工坊更新日志" if str(card_kind).strip().lower() == "workshop" else "游戏更新日志"
        blocks.append(RenderBlock("text", main_title, title_font, title_color, 18))

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
        title_color = (199, 213, 224)
        is_workshop_sec = str(sec.appid).strip().lower().startswith("workshop")
        is_free_games_sec = str(sec.appid).strip().lower() == "free_games"
        if not is_workshop_sec:
            game_name = str(sec.title or "").strip().strip("[]").strip("\u3010\u3011")
            blocks.append(RenderBlock("text", game_name, section_title_font, accent, 14))
        if not sec.updates:
            blocks.append(RenderBlock("text", "暂无更新", body_font, muted, 16))
            header_img = header_map.get(sec.appid)
            if header_img:
                header_img = self._scale_image(header_img, image_max_width, max_img_h)
                blocks.append(RenderBlock("image", image=header_img, gap=14))
            return blocks
        for item in sec.updates:
            if is_workshop_sec:
                workshop_title_font = self._load_font(section_title_font.size + 2, bold=True)
                workshop_title = str(item.title or "").strip().strip("[]").strip("\u3010\u3011")
                game_name = str(sec.title or "").strip().strip("[]").strip("\u3010\u3011")
                title_blocks = self._wrap_blocks(workshop_title, workshop_title_font, accent, max_text_width)
                if title_blocks:
                    # Keep workshop title spacing aligned with the main log title rhythm.
                    title_blocks[-1].gap = 18
                    blocks.extend(title_blocks)
                blocks.extend(self._wrap_blocks(f"\u6e38\u620f\uff1a{game_name}", body_font, body_color, max_text_width))
            else:
                section_size = int(getattr(section_title_font, "size", 26))
                body_size = int(getattr(body_font, "size", 18))
                news_title_size = max(body_size + 1, min(section_size - 2, body_size + 6))
                news_title_font = self._load_font(news_title_size, bold=True)
                news_title = str(item.title or "").strip().strip("[]").strip("\u3010\u3011")
                if news_title:
                    title_blocks = self._wrap_blocks(news_title, news_title_font, title_color, max_text_width)
                    if title_blocks:
                        title_blocks[-1].gap = 8
                        blocks.extend(title_blocks)
            if is_free_games_sec:
                summary = self._summarize_free_game_text(item.contents, max_chars)
            else:
                summary = self._summarize_text(item.contents, max_chars)
            if summary:
                blocks.extend(self._wrap_blocks(summary, body_font, body_color, max_text_width))

            image_urls: list[str] = []
            if is_workshop_sec or is_free_games_sec:
                u = str(getattr(item, "image_url", "") or "").strip()
                if u and max_imgs > 0:
                    image_urls = [u]
            else:
                item_image = self._first_prefetched_item_image(item, image_map)
                if item_image is not None and self._news_image_fits_card_budget(
                    item_image, image_max_width
                ):
                    item_image = self._scale_news_image(item_image, image_max_width)
                else:
                    item_image = header_map.get(sec.appid)
                    if item_image:
                        item_image = self._scale_image(item_image, image_max_width, max_img_h)
                if item_image:
                    blocks.append(RenderBlock("image", image=item_image, gap=10, align="center"))
            if is_free_games_sec:
                for url in image_urls:
                    img = image_map.get(url)
                    if img:
                        img = self._scale_image(img, image_max_width, max_img_h)
                        blocks.append(RenderBlock("image", image=img, gap=10))
            if not is_free_games_sec:
                date_text = self._format_time(item.date)
                if date_text:
                    blocks.append(RenderBlock("text", "", small_font, muted, 4))
                    blocks.append(RenderBlock("text", f"发布于：{date_text}", small_font, muted, 10))
                if item.url:
                    blocks.append(RenderBlock("text", f"{item.url}", small_font, muted, 14))
            if is_workshop_sec:
                for url in image_urls:
                    img = image_map.get(url)
                    if img:
                        img = self._scale_image(img, image_max_width, max_img_h)
                        blocks.append(RenderBlock("image", image=img, gap=10))
        if is_workshop_sec or is_free_games_sec:
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
                height += block.top_gap + (block.image.height if block.image else 0) + block.gap
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
            # In llm mode, timeout/failure may fall back to raw upstream content.
            # If markup remains, sanitize with plugin formatter to avoid unreadable card text.
            if re.search(r"\[/?(?:p|list|h[1-6]|b|i|u|url|img|\*)\b[^\]]*\]", clean, flags=re.I) or re.search(
                r"<[^>]+>", clean
            ):
                clean = self._format_news_text(clean)
            if len(clean) > max_chars:
                clean = clean[: max_chars - 3].rstrip() + "..."
            return clean
        clean = self._format_news_text(text)
        clean = re.sub(r"[ 	]+", " ", clean).strip()
        if len(clean) > max_chars:
            clean = clean[: max_chars - 3].rstrip() + "..."
        return clean

    def _summarize_free_game_text(self, text: str, max_chars: int) -> str:
        clean = self._format_news_text(text)
        if not clean:
            return ""

        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        keepers: list[str] = []
        for prefix in ("截止时间:", "原价:"):
            matched = next((line for line in lines if line.startswith(prefix)), "")
            if matched:
                keepers.append(matched)

        if not keepers:
            banned_parts = (
                "领取方式",
                "活动链接",
                "http://",
                "https://",
                "Click the button to visit the giveaway page",
                "Download this game directly via Steam",
                "That's it! Have fun!",
            )
            fallback_lines = [
                line for line in lines
                if not any(part in line for part in banned_parts)
            ]
            keepers = fallback_lines[:2]

        summary = "\n".join(keepers).strip()
        if len(summary) > max_chars:
            summary = summary[: max_chars - 3].rstrip() + "..."
        return summary


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

    def _extract_news_image_candidates(self, text: str) -> list[str]:
        if not text:
            return []
        urls: list[str] = []
        pattern = re.compile(
            r"(?P<localized>\{STEAM_CLAN_LOC_IMAGE\}/(?P<localized_path>[^\s<>\"'\[\]]+))"
            r"|(?P<clan>\{STEAM_CLAN_IMAGE\}/(?P<clan_path>[^\s<>\"'\[\]]+))"
            r"|(?P<url>https?://[^\s<>\"'\[\]]+)",
            flags=re.I,
        )
        base = "https://clan.fastly.steamstatic.com/images/"
        for match in pattern.finditer(text):
            candidates: list[str] = []
            if match.group("localized"):
                path = match.group("localized_path").rstrip(").,;")
                clanid, separator, filename = path.partition("/")
                if separator and filename:
                    stem, suffix = os.path.splitext(filename)
                    candidates.append(f"{base}{clanid}/{stem}/{self._steam_lang()}{suffix}")
                    candidates.append(f"{base}{clanid}/{filename}")
            elif match.group("clan"):
                path = match.group("clan_path").rstrip(").,;")
                candidates.append(f"{base}{path}")
            else:
                candidate = match.group("url").rstrip(").,;")
                lower = candidate.lower()
                path = lower.split("?", 1)[0]
                if path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                    candidates.append(candidate)
                elif "steamusercontent.com/ugc/" in lower:
                    candidates.append(candidate)
            for candidate in candidates:
                if candidate and candidate not in urls:
                    urls.append(candidate)
        return urls

    def _extract_image_urls(self, text: str) -> list[str]:
        return self._extract_news_image_candidates(text)

    def _item_image_candidates(self, item: NewsItem) -> list[str]:
        candidates: list[str] = []
        for url in tuple(getattr(item, "image_candidates", ()) or ()):
            normalized = str(url or "").strip()
            if normalized and normalized not in candidates:
                candidates.append(normalized)
        image_url = str(getattr(item, "image_url", "") or "").strip()
        if image_url and image_url not in candidates:
            candidates.append(image_url)
        for url in self._extract_news_image_candidates(item.contents):
            if url not in candidates:
                candidates.append(url)
        return candidates

    def _first_prefetched_item_image(
        self,
        item: NewsItem,
        image_map: dict[str, PilImage.Image],
    ) -> PilImage.Image | None:
        for url in self._item_image_candidates(item):
            if image_map.get(url):
                return image_map[url]
        return None

    def _news_image_cache_path(self, url: str) -> Path:
        key = hashlib.sha1(url.encode("utf-8", errors="ignore")).hexdigest()
        return self._news_image_dir / f"{key}.bin"

    def _decode_image_bytes(self, data: bytes, url: str = "") -> PilImage.Image | None:
        if not data:
            return None
        if len(data) > MAX_NEWS_IMAGE_BYTES:
            self._debug(f"image too large: {url}")
            return None
        try:
            img = PilImage.open(BytesIO(data))
            img.load()
        except Exception as exc:
            self._debug(f"image decode failed: {url} {exc}")
            return None
        if img.width * img.height > MAX_NEWS_IMAGE_PIXELS:
            self._debug(f"image too large (pixels): {url}")
            return None
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        return img

    def _trim_news_image_cache(self, keep: int = MAX_NEWS_IMAGE_CACHE_FILES) -> None:
        try:
            files = [p for p in self._news_image_dir.iterdir() if p.is_file()]
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

    async def _download_image(self, url: str) -> PilImage.Image | None:
        if not self._client:
            return None
        if not self._is_allowed_image_url(url):
            self._debug(f"skip untrusted image url: {url}")
            return None
        if self._is_in_fail_cooldown(self._image_fail_until, url):
            self._debug(f"skip image by fail cooldown: {url}")
            return None
        cache_path = self._news_image_cache_path(url)
        if cache_path.exists():
            try:
                data = cache_path.read_bytes()
                img = self._decode_image_bytes(data, url)
                if img is not None:
                    try:
                        cache_path.touch()
                    except Exception:
                        pass
                    self._clear_fail_cooldown(self._image_fail_until, url)
                    return img
                cache_path.unlink(missing_ok=True)
            except Exception as exc:
                self._debug(f"read image cache failed: {url} {exc}")
        try:
            resp = await self._client.get(
                url,
                timeout=self._news_image_timeout_sec(),
                follow_redirects=True,
            )
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").lower()
            if content_type and not content_type.startswith("image/"):
                self._debug(f"skip non-image content: {url} {content_type}")
                self._mark_fail_cooldown(self._image_fail_until, url)
                return None
            if resp.headers.get("Content-Length"):
                try:
                    if int(resp.headers["Content-Length"]) > MAX_NEWS_IMAGE_BYTES:
                        self._debug(f"image too large by header: {url}")
                        self._mark_fail_cooldown(self._image_fail_until, url)
                        return None
                except Exception:
                    pass
            data = resp.content
            if not data:
                self._mark_fail_cooldown(self._image_fail_until, url)
                return None
            img = self._decode_image_bytes(data, url)
            if img is None:
                self._mark_fail_cooldown(self._image_fail_until, url)
                return None
            try:
                cache_path.write_bytes(data)
            except Exception as exc:
                self._debug(f"write image cache failed: {url} {exc}")
            self._clear_fail_cooldown(self._image_fail_until, url)
            return img
        except Exception as exc:
            self._debug(f"image download failed: {url} {exc}")
            self._mark_fail_cooldown(self._image_fail_until, url)
            return None

    async def _prefetch_images(self, sections: list[AppSection]) -> dict[str, PilImage.Image]:
        if not self._client:
            return {}
        ordinary_candidates: list[list[str]] = []
        special_urls: list[str] = []
        max_imgs = int(self._cfg("image_max_per_item", 1))
        for sec in sections:
            is_workshop_sec = str(sec.appid).strip().lower().startswith("workshop")
            is_free_games_sec = str(sec.appid).strip().lower() == "free_games"
            for item in sec.updates:
                if is_workshop_sec or is_free_games_sec:
                    u = str(getattr(item, "image_url", "") or "").strip()
                    if u and max_imgs > 0 and u not in special_urls:
                        special_urls.append(u)
                else:
                    candidates = self._item_image_candidates(item)
                    if candidates and max_imgs > 0:
                        ordinary_candidates.append(candidates)

        if not ordinary_candidates and not special_urls:
            return {}

        semaphore = asyncio.Semaphore(self._prefetch_image_concurrency())
        results: dict[str, PilImage.Image] = {}
        download_tasks: dict[str, asyncio.Task[PilImage.Image | None]] = {}

        async def _load_url(url: str) -> PilImage.Image | None:
            async with semaphore:
                return await self._download_image(url)

        async def _fetch_url(url: str) -> PilImage.Image | None:
            task = download_tasks.get(url)
            if task is None:
                task = asyncio.create_task(_load_url(url))
                download_tasks[url] = task
            return await task

        async def _fetch_first(candidates: list[str]):
            for url in candidates:
                img = await _fetch_url(url)
                if img:
                    results[url] = img
                    return

        async def _fetch_special(url: str):
            img = await _fetch_url(url)
            if img:
                results[url] = img

        await asyncio.gather(
            *[_fetch_first(candidates) for candidates in ordinary_candidates],
            *[_fetch_special(url) for url in special_urls],
        )
        if results:
            self._trim_news_image_cache()
        return results

    async def _prefetch_app_headers(self, sections: list[AppSection]) -> dict[str, PilImage.Image]:
        if not self._client:
            return {}
        appids = {sec.appid for sec in sections if str(sec.appid).isdigit()}
        results: dict[str, PilImage.Image] = {}
        semaphore = asyncio.Semaphore(self._prefetch_header_concurrency())

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
                self._clear_fail_cooldown(self._header_fail_until, appid)
                return img
            except Exception:
                try:
                    cache_path.unlink()
                except Exception:
                    pass

        if not self._client:
            return None
        if self._is_in_fail_cooldown(self._header_fail_until, appid):
            self._debug(f"skip header by fail cooldown: appid={appid}")
            return None

        candidates = [
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_hero.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/hero_capsule.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
        ]
        max_bytes = 6_000_000
        for url in candidates:
            try:
                resp = await self._client.get(url, timeout=self._header_download_timeout_sec())
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
                self._clear_fail_cooldown(self._header_fail_until, appid)
                return img
            except Exception as exc:
                self._debug(f"header download failed: {url} {exc}")
                continue
        self._mark_fail_cooldown(self._header_fail_until, appid)
        return None

    def _scale_image(self, img: PilImage.Image, max_w: int, max_h: int) -> PilImage.Image:
        if img.width <= max_w and img.height <= max_h:
            return img
        ratio = min(max_w / img.width, max_h / img.height)
        new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        return img.resize(new_size, PilImage.LANCZOS)

    @staticmethod
    def _news_image_fits_card_budget(img: PilImage.Image, max_w: int) -> bool:
        scaled_height = img.height
        if img.width > max_w:
            scaled_height = max(1, round(img.height * max_w / img.width))
        return 900 * scaled_height <= MAX_CARD_RENDER_PIXELS

    def _scale_news_image(self, img: PilImage.Image, max_w: int) -> PilImage.Image:
        if img.width <= max_w:
            return img
        new_height = max(1, round(img.height * max_w / img.width))
        return img.resize((max_w, new_height), PilImage.LANCZOS)

    def _format_time(self, ts: int) -> str:
        if not ts:
            return ""
        return datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M")

    def _format_free_game_time(self, ts: int) -> str:
        if not ts:
            return ""
        return datetime.fromtimestamp(ts, self._get_display_timezone()).strftime("%Y/%m/%d %H:%M")

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
    async def _push_text(
        self,
        targets: list[NotifyTarget],
        text: str,
    ) -> PushResult:
        chain = MessageChain(chain=[Plain(text)])
        return await self._push_chain(targets, chain)

    async def _push_image(
        self,
        targets: list[NotifyTarget],
        image_bytes: bytes,
    ) -> PushResult:
        path = self._save_temp_image(image_bytes)
        if not path:
            self._log_warn("push", "save temp image failed")
            return PushResult([], list(targets))
        try:
            chain = MessageChain(chain=[Image(file=path)])
        except Exception as exc:
            self._log_warn(
                "push",
                "build image chain failed",
                error_type=type(exc).__name__,
            )
            return PushResult([], list(targets))
        return await self._push_chain(targets, chain)

    async def _push_chain(
        self,
        targets: list[NotifyTarget],
        chain: MessageChain,
    ) -> PushResult:
        self._log_debug(
            "push",
            "start",
            target_count=len(targets),
            chain_size=len(chain.chain),
        )
        succeeded: list[NotifyTarget] = []
        failed: list[NotifyTarget] = []
        for index, target in enumerate(targets, start=1):
            sent = await self._send_to_target(
                target,
                chain,
                target_index=index,
            )
            fields = self._target_log_fields(
                target.umo,
                index,
                legacy=bool(target.legacy_group_id),
            )
            if sent:
                succeeded.append(target)
                self._log_debug("push", "target sent", **fields)
            else:
                failed.append(target)
                self._log_warn("push", "target failed", **fields)
        return PushResult(succeeded, failed)

    async def _send_to_target(
        self,
        target: NotifyTarget,
        chain: MessageChain,
        target_index: int,
    ) -> bool:
        fields = self._target_log_fields(
            target.umo,
            target_index,
            legacy=bool(target.legacy_group_id),
        )
        try:
            sent = await self.context.send_message(
                session=target.umo,
                message_chain=chain,
            )
            if sent is True:
                self._log_debug("send", "via session", **fields)
                return True
            self._log_warn(
                "send",
                "session returned false",
                **fields,
            )
        except Exception as exc:
            self._log_warn(
                "send",
                "session failed",
                error_type=type(exc).__name__,
                **fields,
            )

        if not target.legacy_group_id:
            return False
        if target.platform_id != str(
            self._last_platform_id or ""
        ).strip():
            return False
        if not self._last_bot or not hasattr(
            self._last_bot,
            "send_group_msg",
        ):
            return False
        legacy_group_id = target.legacy_group_id
        if not (
            legacy_group_id.isascii()
            and legacy_group_id.isdigit()
        ):
            return False
        try:
            group_id = int(legacy_group_id)
        except (TypeError, ValueError):
            return False
        try:
            await self._last_bot.send_group_msg(
                group_id=group_id,
                message=chain.chain,
            )
            self._log_debug("send", "via bot", **fields)
            return True
        except Exception as exc:
            self._log_warn(
                "send",
                "bot fallback failed",
                error_type=type(exc).__name__,
                **fields,
            )
            return False

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
            self._log_warn(
                "push",
                "save temp image failed",
                error_type=type(exc).__name__,
            )
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
        game_commands = self._manual_game_query_commands()
        workshop_commands = self._manual_workshop_query_commands()
        text = self._normalize_manual_command_text(self._get_event_text(event))
        if not text:
            return
        is_game_cmd = self._match_command(text, game_commands)
        is_workshop_cmd = self._match_command(text, workshop_commands)
        if not is_game_cmd and not is_workshop_cmd:
            return
        query_kind = "game" if is_game_cmd and not is_workshop_cmd else "workshop"
        if is_game_cmd and is_workshop_cmd:
            query_kind = "game"
            self._log_warn(
                "manual_cmd",
                "command overlaps between game/workshop; default to game",
                command=text,
            )

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
            query_kind=query_kind,
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

        event_umo = str(event.unified_msg_origin or "").strip()
        if not self._manual_query_allowed(event_umo, group_id):
            self._log_warn(
                "manual_cmd",
                "reject: group not allowed",
                group_id=group_id,
                user_id=user_id,
            )
            yield event.plain_result("当前群未启用插件")
            return

        if query_kind == "workshop":
            yield event.plain_result("\u6b63\u5728\u67e5\u8be2\u521b\u610f\u5de5\u574a\u66f4\u65b0\uff0c\u8bf7\u7a0d\u540e...")
        else:
            yield event.plain_result("\u6b63\u5728\u67e5\u8be2\u6e38\u620f\u66f4\u65b0\uff0c\u8bf7\u7a0d\u540e...")
        result, err = await self._manual_query(event.unified_msg_origin, query_kind=query_kind)
        if err:
            self._log_warn("manual_cmd", "query failed", group_id=group_id, user_id=user_id, error=err)
            yield event.plain_result(err)
            return
        payloads: list[bytes | str] = []
        if isinstance(result, list):
            payloads = result
        elif result is not None:
            payloads = [result]

        if not payloads:
            yield event.plain_result("暂无更新")
            return

        for payload in payloads:
            if isinstance(payload, bytes):
                path = self._save_temp_image(payload)
                if path:
                    self._log_debug("manual_cmd", "reply image", group_id=group_id, user_id=user_id, path=path)
                    yield event.image_result(path)
                else:
                    self._log_warn("manual_cmd", "reply image failed", group_id=group_id, user_id=user_id)
                    yield event.plain_result("图片生成失败，请稍后重试")
            else:
                self._log_debug("manual_cmd", "reply text", group_id=group_id, user_id=user_id)
                yield event.plain_result(str(payload) or "暂无更新")




    @filter.command("steam_update_ping")
    async def steam_update_ping(self, event: AstrMessageEvent):
        """捕获平台实例信息用于旧群号推送兼容。"""
        self._last_platform_id = str(
            event.get_platform_id() or ""
        ).strip()
        self._last_bot = (
            event.bot
            if isinstance(event, AiocqhttpMessageEvent)
            else None
        )
        self._log_debug(
            "ping",
            "captured sender context",
            platform_ref=self._target_ref(self._last_platform_id),
            has_bot=bool(self._last_bot),
        )
        yield event.plain_result("Steam 更新推送已就绪")


class Main(SteamUpdatePush):
    pass
