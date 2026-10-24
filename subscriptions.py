"""Session subscription policy and commands; configuration is the single source of truth."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import shlex
import uuid
from typing import Any


@dataclass(frozen=True)
class Subscription:
    target: Any
    games: tuple[str, ...]
    workshop: tuple[str, ...]
    free_games: bool
    mode: str
    enabled: bool = True
    explicit: bool = False
    label: str = ""
    legacy: bool = False
    baseline_token: str = ""


def ids(values):
    if not isinstance(values, list):
        raise ValueError("订阅 ID 必须为列表")
    result = []
    for value in values:
        value = str(value).strip()
        if not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise ValueError("订阅 ID 必须为正整数")
        if value not in result:
            result.append(value)
    if len(result) > 100:
        raise ValueError("单个会话每类最多订阅 100 个来源")
    return tuple(result)


class SubscriptionMixin:
    def _subscription_entries(self):
        group = self.config.get("subscription_settings", {})
        entries = group.get("sessions", []) if isinstance(group, dict) else None
        if not isinstance(entries, list):
            raise ValueError("分会话订阅配置必须为列表")
        return entries

    def _subscription_target(self, raw):
        value = str(raw or "").strip()
        # Keep the whole session id, including further colons.
        parts = value.split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("请填写 /sid 返回的完整 UMO")
        target = self._parse_notify_target(value, target_index=0)
        if target is None:
            raise ValueError("UMO 格式无效")
        return target

    def _resolve_subscriptions(self):
        legacy = {target.umo: target for target in self._resolve_notify_targets()}
        result = {
            umo: Subscription(
                target,
                tuple(self._normalize_appids(self._cfg("steam_appids", []))),
                tuple(
                    self._normalize_workshop_item_ids(
                        self._cfg("workshop_item_ids", [])
                    )
                )
                if self._workshop_enabled()
                else (),
                self._free_games_enabled(),
                str(self._cfg("message_mode", "card")),
                legacy=True,
            )
            for umo, target in legacy.items()
        }
        seen, blocked = set(), set()
        for entry in self._subscription_entries():
            target = None
            try:
                if not isinstance(entry, dict):
                    raise ValueError("订阅条目必须为对象")
                target = self._subscription_target(entry.get("umo"))
                if target.umo in seen:
                    blocked.add(target.umo)
                    raise ValueError("同一 UMO 只能配置一个订阅条目")
                seen.add(target.umo)
                mode = entry.get("message_mode", "inherit")
                free = entry.get("free_games_mode", "inherit")
                enabled = entry.get("enabled", True)
                if mode not in ("inherit", "card", "text") or free not in (
                    "inherit",
                    "on",
                    "off",
                ):
                    raise ValueError("订阅模式无效")
                if not isinstance(enabled, bool):
                    raise ValueError("启用状态必须为布尔值")
                games, workshop = (
                    ids(entry.get("steam_appids", [])),
                    ids(entry.get("workshop_item_ids", [])),
                )
                result[target.umo] = Subscription(
                    legacy.get(target.umo, target),
                    games,
                    workshop if self._workshop_enabled() else (),
                    self._free_games_enabled() and free != "off",
                    str(self._cfg("message_mode", "card"))
                    if mode == "inherit"
                    else mode,
                    enabled,
                    True,
                    str(entry.get("label", "")),
                    target.umo in legacy,
                    str(entry.get("baseline_token", "")),
                )
            except ValueError as exc:
                if target is not None:
                    blocked.add(target.umo)
                self._log_warn(
                    "subscription", "invalid subscription entry", error=str(exc)
                )
        for umo in blocked:
            result.pop(umo, None)
        return result

    async def _subscription_can_edit(self, event, target_umo, global_scope=False):
        if event.is_admin():
            return True
        if global_scope or str(event.unified_msg_origin).strip() != target_umo:
            return False
        group_id = str(event.get_group_id() or "")
        if not group_id or ":GroupMessage:" not in target_umo:
            return False
        if target_umo.split(":", 2)[2] != group_id:
            return False
        try:
            group = await asyncio.wait_for(event.get_group(group_id), timeout=5)
            if group is None or str(group.group_id) != group_id:
                return False
            sender = str(event.get_sender_id())
            return sender == str(group.group_owner) or sender in {
                str(member) for member in (group.group_admins or [])
            }
        except Exception:
            return False

    @staticmethod
    def _subscription_summary(profile):
        if profile is None:
            return "当前会话尚未订阅"
        return "\n".join(
            [
                "STEAM订阅（当前会话）",
                "状态：" + ("启用" if profile.enabled else "暂停"),
                "来源：" + ("独立配置" if profile.explicit else "继承全局配置"),
                "游戏：" + ("、".join(profile.games) or "无"),
                "创意工坊：" + ("、".join(profile.workshop) or "无"),
                "免费领取：" + ("开启" if profile.free_games else "关闭"),
                "消息模式：" + ("图片" if profile.mode == "card" else "文字"),
            ]
        )

    async def _subscription_command(self, event, raw, global_scope=False):
        if global_scope and not event.is_admin():
            return "权限不足：跨会话管理仅限 AstrBot 管理员"
        try:
            args = shlex.split(str(raw).replace("「", '"').replace("」", '"'))
            if global_scope and args == ["列表"]:
                profiles = self._resolve_subscriptions()
                return (
                    "\n\n".join(
                        p.target.umo + "\n" + self._subscription_summary(p)
                        for p in profiles.values()
                    )
                    or "暂无订阅"
                )
            target = self._subscription_target(
                args.pop(0) if global_scope and args else event.unified_msg_origin
            )
            if not args or args == ["列表"]:
                return self._subscription_summary(
                    self._resolve_subscriptions().get(target.umo)
                )
            if not await self._subscription_can_edit(event, target.umo, global_scope):
                return "权限不足：仅 AstrBot 管理员或可验证的本群群主、管理员可修改订阅"
            if not hasattr(self, "_subscription_edit_lock"):
                self._subscription_edit_lock = asyncio.Lock()
            async with self._subscription_edit_lock:
                if self._stop_event.is_set() or not self._is_current_poll_instance():
                    return "插件正在重载，请稍后重试"
                entries = deepcopy(self._subscription_entries())
                matches = [
                    e
                    for e in entries
                    if isinstance(e, dict)
                    and str(e.get("umo", "")).strip() == target.umo
                ]
                if len(matches) > 1:
                    return "当前 UMO 存在重复条目，请在配置界面合并后再操作"
                profile = self._resolve_subscriptions().get(target.umo)
                if matches and profile is None:
                    return "当前订阅条目无效，请先在配置界面修正"
                entry = (
                    matches[0]
                    if matches
                    else {
                        "__template_key": "session",
                        "label": "",
                        "umo": target.umo,
                        "enabled": True,
                        "steam_appids": list(profile.games) if profile else [],
                        "workshop_item_ids": list(profile.workshop) if profile else [],
                        "free_games_mode": "on"
                        if profile and profile.free_games
                        else "off",
                        "message_mode": profile.mode if profile else "inherit",
                    }
                )
                feedback = "订阅设置已保存"
                if args[0] in ("添加", "删除") and len(args) >= 3:
                    field = {
                        "游戏": "steam_appids",
                        "创意工坊": "workshop_item_ids",
                    }.get(args[1])
                    if field is None:
                        raise ValueError("来源类型须为 游戏 或 创意工坊")
                    values = ids(args[2:])
                    previous = list(ids(entry.get(field, [])))
                    added = args[0] == "添加"
                    changed = [v for v in values if (v not in previous) == added]
                    entry[field] = (
                        list(dict.fromkeys(previous + list(values)))
                        if added
                        else [v for v in previous if v not in values]
                    )
                    ids(entry[field])
                    feedback = ("已添加：" if added else "已删除：") + (
                        "、".join(changed) or "无变更"
                    )
                elif args in (["暂停"], ["恢复"]):
                    was_enabled = entry.get("enabled", True)
                    entry["enabled"] = args[0] == "恢复"
                    if args[0] == "恢复" and not was_enabled:
                        entry["baseline_token"] = uuid.uuid4().hex
                elif (
                    len(args) == 2
                    and args[0] == "免费领取"
                    and args[1] in ("开启", "关闭", "继承")
                ):
                    entry["free_games_mode"] = {
                        "开启": "on",
                        "关闭": "off",
                        "继承": "inherit",
                    }[args[1]]
                    if args[1] == "开启" and not self._free_games_enabled():
                        feedback += "；全局免费领取总开关关闭，暂不会推送"
                elif (
                    len(args) == 2
                    and args[0] == "模式"
                    and args[1] in ("图片", "文字", "继承")
                ):
                    entry["message_mode"] = {
                        "图片": "card",
                        "文字": "text",
                        "继承": "inherit",
                    }[args[1]]
                elif args == ["恢复全局", "确认"]:
                    entries = [e for e in entries if e not in matches]
                    entry = None
                    feedback = "已移除独立规则；是否推送由原全局目标列表决定"
                else:
                    return "用法：/STEAM订阅 列表 | 添加/删除 游戏/创意工坊 ID... | 免费领取 开启/关闭/继承 | 模式 图片/文字/继承 | 暂停 | 恢复 | 恢复全局 确认"
                if entry is not None and not matches:
                    entries.append(entry)
                old = deepcopy(self.config.get("subscription_settings", {}))
                existed = "subscription_settings" in self.config
                self.config["subscription_settings"] = {**old, "sessions": entries}
                try:
                    self.config.save_config()
                except Exception:
                    if existed:
                        self.config["subscription_settings"] = old
                    else:
                        self.config.pop("subscription_settings", None)
                    return "保存失败，订阅设置未变更，请稍后重试"
                return feedback
        except ValueError as exc:
            return "参数错误：" + str(exc)
