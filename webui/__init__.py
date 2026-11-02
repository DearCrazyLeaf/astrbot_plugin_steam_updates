from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from astrbot.api.web import error_response, json_response, request


_SECRET_KEYS = {"steam_web_api_key", "proxy_url"}
_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_-]{2,20}$")
_APPID_RE = re.compile(r"^[0-9]{1,12}$")
_PLUGIN_ID = "astrbot_plugin_steam_updates"


class SteamUpdatesWebUI:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"

    def register(self) -> None:
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/dashboard",
            self.dashboard,
            ["GET"],
            "Steam Updates dashboard",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/settings",
            self.settings,
            ["GET"],
            "Steam Updates settings",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/settings",
            self.save_settings,
            ["POST"],
            "Save Steam Updates settings",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/games",
            self.games,
            ["GET"],
            "Steam game name mappings",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/games",
            self.save_game,
            ["POST"],
            "Save Steam game name mapping",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/covers",
            self.covers,
            ["GET"],
            "Cached Steam game covers",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/covers/<appid>",
            self.cover_image,
            ["GET"],
            "Cached Steam game cover",
        )
        self.plugin.context.register_web_api(
            f"/{_PLUGIN_ID}/covers/<appid>/delete",
            self.delete_cover,
            ["POST"],
            "Delete a cached Steam game cover",
        )

    def _schema(self) -> dict[str, Any]:
        try:
            data = json.loads(self.schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to load plugin configuration schema: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Plugin configuration schema must be a JSON object")
        return data

    def _groups(self) -> dict[str, Any]:
        schema = self._schema()
        groups: dict[str, Any] = {}
        for group_name, group_schema in schema.items():
            if not isinstance(group_schema, dict) or not isinstance(
                group_schema.get("items"), dict
            ):
                continue
            group_schema_items = group_schema["items"]
            if group_name == "subscription_settings":
                raw_group = self.plugin.config.get(group_name, {})
                raw_group = raw_group if isinstance(raw_group, dict) else {}
                sessions = raw_group.get("sessions", [])
                if not isinstance(sessions, list):
                    sessions = []
                group_values = {"sessions": deepcopy(sessions)}
            else:
                group_values = {}
                for key, item_schema in group_schema_items.items():
                    if not isinstance(item_schema, dict) or item_schema.get("invisible"):
                        continue
                    group_values[key] = deepcopy(
                        self.plugin._cfg(key, item_schema.get("default"))
                    )
            groups[group_name] = {
                "description": group_schema.get("description", group_name),
                "hint": group_schema.get("hint", ""),
                "items": group_schema_items,
                "values": group_values,
            }
        return groups

    async def dashboard(self):
        sessions = self._session_summaries()
        global_appids = self.plugin._normalize_appids(
            self.plugin._cfg("steam_appids", [])
        )
        global_workshop = self.plugin._normalize_workshop_item_ids(
            self.plugin._cfg("workshop_item_ids", [])
        )
        runtime = deepcopy(self.plugin._webui_poll_state)
        return json_response(
            {
                "data": {
                    "enabled": bool(self.plugin._cfg("enable_push", True)),
                    "global": {
                        "game_count": len(global_appids),
                        "workshop_count": len(global_workshop),
                        "workshop_enabled": self.plugin._workshop_enabled(),
                        "free_games_enabled": self.plugin._free_games_enabled(),
                        "proxy_mode": str(self.plugin._cfg("proxy_mode", "system")),
                    },
                    "sessions": sessions,
                    "session_count": len(sessions),
                    "enabled_session_count": sum(
                        1 for session in sessions if session["enabled"]
                    ),
                    "uses_global": not sessions,
                    "runtime": runtime,
                    "calendar": self.plugin._get_free_games_calendar(),
                    "free_games_enabled": self.plugin._free_games_enabled(),
                }
            }
        )

    def _session_summaries(self) -> list[dict[str, Any]]:
        group = self.plugin.config.get("subscription_settings", {})
        entries = group.get("sessions", []) if isinstance(group, dict) else []
        if not isinstance(entries, list):
            return []
        summaries: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            umo = str(entry.get("umo") or "").strip()
            parts = umo.split(":", 2)
            target = (
                f"{parts[0]}:{parts[1]}:•••"
                if len(parts) == 3
                else "会话标识无效"
            )
            try:
                self.plugin._subscription_entry_values(entry)
                valid = True
            except ValueError:
                valid = False
            summaries.append(
                {
                    "label": str(entry.get("label") or "").strip()[:80],
                    "target": target,
                    "enabled": bool(entry.get("enabled", True)),
                    "game_count": len(
                        self.plugin._normalize_appids(entry.get("steam_appids", []))
                    ),
                    "workshop_count": len(
                        self.plugin._normalize_workshop_item_ids(
                            entry.get("workshop_item_ids", [])
                        )
                    ),
                    "free_games_mode": str(entry.get("free_games_mode", "inherit")),
                    "message_mode": str(entry.get("message_mode", "inherit")),
                    "valid": valid,
                }
            )
        return summaries

    async def settings(self):
        try:
            groups = self._groups()
        except RuntimeError as exc:
            return error_response(str(exc), status_code=500)
        secrets = {
            key: bool(self.plugin._cfg(key, ""))
            for key in _SECRET_KEYS
        }
        for group in groups.values():
            for key in _SECRET_KEYS:
                if key in group["values"]:
                    group["values"][key] = ""
        return json_response({"data": {"groups": groups, "secrets": secrets}})

    async def save_settings(self):
        body = await request.json(default={})
        if not isinstance(body, dict) or not isinstance(body.get("groups"), dict):
            return error_response("请求配置格式无效")
        snapshot = deepcopy(dict(self.plugin.config))
        try:
            schema = self._schema()
            groups = body["groups"]
            unknown_groups = set(groups) - set(schema)
            if unknown_groups:
                return error_response("包含未知配置组")
            snapshot = deepcopy(dict(self.plugin.config))
            for group_name, values in groups.items():
                if group_name == "subscription_settings":
                    self._validate_sessions(values)
                    continue
                group_schema = schema.get(group_name)
                if (
                    not isinstance(values, dict)
                    or not isinstance(group_schema, dict)
                    or not isinstance(group_schema.get("items"), dict)
                ):
                    raise ValueError(f"{group_name} 配置格式无效")
                self._validate_group(group_name, values, group_schema["items"], body)
            self._apply_secret_changes(body)
            self.plugin.config.save_config()
        except (ValueError, TypeError) as exc:
            self._restore_config(snapshot)
            return error_response(str(exc))
        except Exception as exc:
            self._restore_config(snapshot)
            self.plugin._log_warn("webui", "configuration save failed", error=exc)
            return error_response("配置保存失败，原配置已恢复", status_code=500)
        return json_response({"data": {"saved": True}})

    def _restore_config(self, snapshot: dict[str, Any]) -> None:
        try:
            self.plugin.config.clear()
            self.plugin.config.update(snapshot)
        except Exception as exc:
            self.plugin._log_warn("webui", "configuration rollback failed", error=exc)

    def _set_group(self, group_name: str, values: dict[str, Any]) -> None:
        previous = self.plugin.config.get(group_name, {})
        merged = dict(previous) if isinstance(previous, dict) else {}
        merged.update(deepcopy(values))
        self.plugin.config[group_name] = merged

    def _set_config_value(self, group_name: str, key: str, value: Any) -> None:
        self._set_group(group_name, {key: value})
        if key in self.plugin.config:
            self.plugin.config[key] = deepcopy(value)

    def _validate_group(
        self,
        group_name: str,
        values: dict[str, Any],
        item_schemas: dict[str, Any],
        body: dict[str, Any],
    ) -> None:
        unknown = set(values) - {
            key
            for key, item in item_schemas.items()
            if isinstance(item, dict) and not item.get("invisible")
        }
        if unknown:
            raise ValueError(f"{group_name} 包含未知配置项")
        for key, value in values.items():
            item_schema = item_schemas.get(key)
            if not isinstance(item_schema, dict):
                raise ValueError(f"配置项 {key} 无效")
            if key in _SECRET_KEYS and not value and key not in body.get("clear_secrets", []):
                continue
            clean_value = self._validate_value(key, value, item_schema)
            if group_name == "manual_commands":
                continue
            self._set_config_value(group_name, key, clean_value)
        if group_name == "manual_commands":
            all_commands = []
            for command_key in (
                "manual_query_game_command",
                "manual_query_workshop_command",
                "manual_query_free_games_history_command",
            ):
                command_value = values.get(
                    command_key,
                    self.plugin._cfg(command_key, []),
                )
                all_commands.append(
                    self.plugin._normalize_manual_commands(command_value, [])
                )
            normalized = [
                self.plugin._normalize_manual_command_text(command).casefold()
                for commands in all_commands
                for command in commands
            ]
            for index, command in enumerate(normalized):
                for other in normalized[index + 1 :]:
                    if command == other or command.startswith(other + " ") or other.startswith(command + " "):
                        raise ValueError("游戏、工坊与免费游戏记录指令不能重复或存在前缀冲突")
            for key, value in values.items():
                self._set_config_value(group_name, key, self._validate_value(key, value, item_schemas[key]))

    def _validate_value(self, key: str, value: Any, schema: dict[str, Any]) -> Any:
        kind = schema.get("type")
        if kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{schema.get('description', key)} 必须为布尔值")
            return value
        if kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{schema.get('description', key)} 必须为整数")
            limits = {
                "poll_interval_sec": (30, 86400),
                "max_days": (1, 30),
                "feed_timeout_sec": (1, 120),
                "workshop_timeout_sec": (1, 120),
                "game_query_concurrency": (1, 16),
                "appdetails_retry_attempts": (1, 5),
                "llm_timeout_sec": (5, 30),
                "llm_input_max_chars": (1000, 20000),
                "llm_round_timeout_sec": (30, 300),
            }
            if key in limits:
                low, high = limits[key]
                if not low <= value <= high:
                    raise ValueError(f"{schema.get('description', key)} 范围为 {low}-{high}")
            return value
        if kind in {"string", "text"}:
            if not isinstance(value, str):
                raise ValueError(f"{schema.get('description', key)} 必须为文本")
            if len(value) > (10000 if key in {"llm_system_prompt", "llm_user_prompt"} else 2000):
                raise ValueError(f"{schema.get('description', key)} 内容过长")
            options = schema.get("options")
            if options and value not in options:
                raise ValueError(f"{schema.get('description', key)} 选项无效")
            if key == "steam_lang" and not re.fullmatch(r"[A-Za-z0-9_-]{2,20}", value.strip()):
                raise ValueError("Steam 语言格式无效")
            if key == "poll_start_time" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise ValueError("轮询起始时间格式应为 HH:MM")
            if key == "display_timezone" and value:
                try:
                    from zoneinfo import ZoneInfo

                    ZoneInfo(value)
                except (KeyError, ValueError) as exc:
                    raise ValueError("显示时区必须是有效的 IANA 时区名") from exc
            return value
        if kind == "list":
            if not isinstance(value, list):
                raise ValueError(f"{schema.get('description', key)} 必须为列表")
            if len(value) > 100:
                raise ValueError(f"{schema.get('description', key)} 最多允许 100 项")
            item_type = (schema.get("items") or {}).get("type")
            if item_type == "string":
                if not all(isinstance(item, str) for item in value):
                    raise ValueError(f"{schema.get('description', key)} 列表项必须为文本")
                cleaned = list(dict.fromkeys(item.strip() for item in value if item.strip()))
                if key in {"steam_appids", "workshop_item_ids"} and any(
                    not item.isascii() or not item.isdecimal() or int(item) <= 0
                    for item in cleaned
                ):
                    raise ValueError(f"{schema.get('description', key)} 必须全部为正整数")
                if key in {"steam_appids", "workshop_item_ids"} and len(cleaned) > 100:
                    raise ValueError(f"{schema.get('description', key)} 最多允许 100 项")
                if key.endswith("_command") and any(len(item) > 80 for item in cleaned):
                    raise ValueError("查询指令关键字最多 80 个字符")
                return cleaned
            return deepcopy(value)
        if kind == "template_list":
            if not isinstance(value, list):
                raise ValueError(f"{schema.get('description', key)} 必须为列表")
            return deepcopy(value)
        if kind == "object":
            if not isinstance(value, dict):
                raise ValueError(f"{schema.get('description', key)} 必须为对象")
            return deepcopy(value)
        if kind in {"float", "number"}:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{schema.get('description', key)} 必须为数字")
            return value
        return deepcopy(value)

    def _validate_sessions(self, group: Any) -> None:
        if not isinstance(group, dict) or not isinstance(group.get("sessions"), list):
            raise ValueError("分会话订阅必须是列表")
        if len(group["sessions"]) > 500:
            raise ValueError("分会话订阅最多允许 500 项")
        seen: set[str] = set()
        cleaned: list[dict[str, Any]] = []
        template_items = self._schema()["subscription_settings"]["items"]["sessions"]["templates"]["session"]["items"]
        allowed_fields = set(template_items) | {"baseline_token", "__template_key"}
        for entry in group["sessions"]:
            if not isinstance(entry, dict):
                raise ValueError("分会话订阅条目必须为对象")
            if set(entry) - allowed_fields:
                raise ValueError("分会话订阅包含未知配置项")
            self.plugin._subscription_entry_values(entry)
            target = self.plugin._subscription_target(entry.get("umo"))
            if target.umo in seen:
                raise ValueError("分会话订阅中存在重复 UMO")
            seen.add(target.umo)
            mode = entry.get("message_mode", "inherit")
            free_mode = entry.get("free_games_mode", "inherit")
            if mode not in {"inherit", "card", "text"}:
                raise ValueError("会话消息模式无效")
            if free_mode not in {"inherit", "on", "off"}:
                raise ValueError("会话免费领取模式无效")
            if not isinstance(entry.get("enabled", True), bool):
                raise ValueError("会话启用状态必须为布尔值")
            label = str(entry.get("label") or "").strip()
            if len(label) > 80:
                raise ValueError("会话备注最多 80 个字符")
            previous = next(
                (
                    old
                    for old in self.plugin.config.get("subscription_settings", {}).get("sessions", [])
                    if isinstance(old, dict) and old.get("umo") == target.umo
                ),
                {},
            )
            cleaned_entry = deepcopy(entry)
            cleaned_entry.update(
                {
                    "label": label,
                    "umo": target.umo,
                    "enabled": entry.get("enabled", True),
                    "steam_appids": list(self.plugin._normalize_appids(entry.get("steam_appids", []))),
                    "workshop_item_ids": list(self.plugin._normalize_workshop_item_ids(entry.get("workshop_item_ids", []))),
                    "free_games_mode": free_mode,
                    "message_mode": mode,
                    "baseline_token": str(entry.get("baseline_token") or previous.get("baseline_token") or ""),
                }
            )
            cleaned.append(cleaned_entry)
        self._set_group("subscription_settings", {"sessions": cleaned})

    def _apply_secret_changes(self, body: dict[str, Any]) -> None:
        clear_secrets = body.get("clear_secrets", [])
        if not isinstance(clear_secrets, list) or not all(
            isinstance(key, str) for key in clear_secrets
        ):
            raise ValueError("清除凭据参数格式无效")
        if set(clear_secrets) - _SECRET_KEYS:
            raise ValueError("清除凭据包含未知字段")
        for group_name in ("game_updates", "network_proxy"):
            group = body.get("groups", {}).get(group_name, {})
            if not isinstance(group, dict):
                continue
            for key in _SECRET_KEYS:
                if key in group and group[key]:
                    if not isinstance(group[key], str) or len(group[key]) > 2000:
                        raise ValueError(f"{key} 格式无效")
                    self._set_config_value(group_name, key, group[key])
                elif key in clear_secrets:
                    self._set_config_value(group_name, key, "")

    async def games(self):
        manual_map = self._read_manual_appid_map()
        merged_map = self.plugin._load_appid_name_map()
        appids = set(merged_map) | set(manual_map)
        for path in self.plugin._app_header_dir.glob("*.jpg"):
            if _APPID_RE.fullmatch(path.stem):
                appids.add(path.stem)
        language = self.plugin._steam_lang().lower()
        items = []
        for appid in sorted(appids, key=lambda value: (len(value), value)):
            manual_names = manual_map.get(appid, {})
            if isinstance(manual_names, str):
                manual_names = {"default": manual_names}
            mapped = merged_map.get(appid, {})
            items.append(
                {
                    "appid": appid,
                    "name": self.plugin._pick_name_from_map(mapped, language)
                    or self.plugin._app_name_fallback(appid),
                    "manual_names": manual_names if isinstance(manual_names, dict) else {},
                    "has_cover": (self.plugin._app_header_dir / f"{appid}.jpg").is_file(),
                }
            )
        return json_response({"data": {"items": items, "language": language}})

    async def save_game(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("游戏映射请求格式无效")
        appid = str(body.get("appid") or "").strip()
        language = str(body.get("language") or "").strip()
        if not _APPID_RE.fullmatch(appid):
            return error_response("AppID 必须是数字")
        if not _LANGUAGE_RE.fullmatch(language):
            return error_response("语言代码格式无效")
        language = language.lower()
        manual_map = self._read_manual_appid_map()
        names = manual_map.get(appid)
        if not isinstance(names, dict):
            names = {}
        if body.get("delete") is True:
            names.pop(language, None)
            if not names:
                manual_map.pop(appid, None)
            else:
                manual_map[appid] = names
        else:
            name = body.get("name")
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
                return error_response("游戏名称不能为空且最多 120 个字符")
            names[language] = name.strip()
            manual_map[appid] = names
        try:
            self._write_json_atomic(self.plugin._manual_appid_name_map_path, manual_map)
        except OSError as exc:
            self.plugin._log_warn("webui", "manual AppID map save failed", error=exc)
            return error_response("游戏名称保存失败", status_code=500)
        self.plugin._appid_name_map = None
        return json_response({"data": {"saved": True}})

    def _read_manual_appid_map(self) -> dict[str, Any]:
        path = self.plugin._manual_appid_name_map_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.plugin._log_warn("webui", "manual AppID map load failed", error=exc)
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write_json_atomic(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{id(data)}.tmp")
        try:
            temp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)

    async def covers(self):
        items = []
        try:
            paths = sorted(self.plugin._app_header_dir.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            self.plugin._log_warn("webui", "cover cache listing failed", error=exc)
            return error_response("封面缓存读取失败", status_code=500)
        for path in paths:
            if path.is_file() and path.suffix.lower() == ".jpg" and _APPID_RE.fullmatch(path.stem):
                items.append({"appid": path.stem, "size": path.stat().st_size})
        return json_response({"data": {"items": items}})

    async def cover_image(self, appid: str):
        if not _APPID_RE.fullmatch(str(appid)):
            return error_response("AppID 必须是数字", status_code=400)
        directory = self.plugin._app_header_dir.resolve()
        path = (directory / f"{appid}.jpg").resolve()
        if path.parent != directory or not path.is_file():
            return error_response("封面缓存不存在", status_code=404)
        try:
            if path.stat().st_size > 6_000_000:
                return error_response("封面缓存文件过大", status_code=413)
            image_data = path.read_bytes()
        except OSError as exc:
            self.plugin._log_warn("webui", "cover cache read failed", appid=appid, error=exc)
            return error_response("封面缓存读取失败", status_code=500)
        if len(image_data) > 6_000_000:
            return error_response("封面缓存文件过大", status_code=413)
        return json_response(
            {
                "data": {
                    "content_type": "image/jpeg",
                    "base64": base64.b64encode(image_data).decode("ascii"),
                }
            }
        )

    async def delete_cover(self, appid: str):
        if not _APPID_RE.fullmatch(str(appid)):
            return error_response("AppID 必须是数字")
        directory = self.plugin._app_header_dir.resolve()
        path = (directory / f"{appid}.jpg").resolve()
        if path.parent != directory:
            return error_response("封面路径无效")
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self.plugin._log_warn("webui", "cover deletion failed", appid=appid, error=exc)
            return error_response("封面删除失败", status_code=500)
        return json_response({"data": {"deleted": True}})
