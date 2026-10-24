import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from unittest.mock import patch
from zoneinfo import TZPATH


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
MODULE_NAME = "steam_subscription_tests.main"


def _install_stub_modules() -> None:
    if "astrbot" in sys.modules:
        return

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

        def exception(self, *args, **kwargs):
            return None

    def _identity_decorator(*args, **kwargs):
        def _wrap(func):
            return func

        return _wrap

    class _EventMessageType:
        GROUP_MESSAGE = "group"

    filter_obj = types.SimpleNamespace(
        EventMessageType=_EventMessageType,
        command=_identity_decorator,
        event_message_type=_identity_decorator,
    )

    class _Star:
        def __init__(self, context=None):
            self.context = context

    class _StarTools:
        @staticmethod
        def get_data_dir(plugin_name=None):
            return Path("/tmp/astrbot_plugin_steam_updates_tests")

    class _AstrBotConfig(dict):
        pass

    class _Plain:
        def __init__(self, text=""):
            self.text = text

    class _Image:
        def __init__(self, file=""):
            self.file = file

    class _MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

    class _MessageType(Enum):
        GROUP_MESSAGE = "GroupMessage"
        FRIEND_MESSAGE = "FriendMessage"
        OTHER_MESSAGE = "OtherMessage"

    class _MessageSession:
        def __init__(self, platform_name, message_type, session_id):
            self.platform_name = platform_name
            self.platform_id = platform_name
            self.message_type = message_type
            self.session_id = session_id

        def __str__(self):
            return f"{self.platform_id}:{self.message_type.value}:{self.session_id}"

        @staticmethod
        def from_str(session_str):
            platform_id, message_type, session_id = session_str.split(":", 2)
            return _MessageSession(
                platform_id,
                _MessageType(message_type),
                session_id,
            )

    class _AstrMessageEvent:
        pass

    class _AiocqhttpMessageEvent:
        pass

    astrbot_mod = types.ModuleType("astrbot")
    astrbot_api_mod = types.ModuleType("astrbot.api")
    astrbot_api_mod.logger = _Logger()

    astrbot_api_event_mod = types.ModuleType("astrbot.api.event")
    astrbot_api_event_mod.filter = filter_obj

    astrbot_api_star_mod = types.ModuleType("astrbot.api.star")
    astrbot_api_star_mod.Context = object
    astrbot_api_star_mod.Star = _Star
    astrbot_api_star_mod.StarTools = _StarTools

    astrbot_core_config_mod = types.ModuleType("astrbot.core.config.astrbot_config")
    astrbot_core_config_mod.AstrBotConfig = _AstrBotConfig

    astrbot_message_components_mod = types.ModuleType("astrbot.core.message.components")
    astrbot_message_components_mod.Image = _Image
    astrbot_message_components_mod.Plain = _Plain

    astrbot_message_result_mod = types.ModuleType(
        "astrbot.core.message.message_event_result"
    )
    astrbot_message_result_mod.MessageChain = _MessageChain

    astrbot_message_session_mod = types.ModuleType(
        "astrbot.core.platform.message_session"
    )
    astrbot_message_session_mod.MessageSession = _MessageSession

    astrbot_event_mod = types.ModuleType("astrbot.core.platform.astr_message_event")
    astrbot_event_mod.AstrMessageEvent = _AstrMessageEvent

    astrbot_aiocq_mod = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    astrbot_aiocq_mod.AiocqhttpMessageEvent = _AiocqhttpMessageEvent

    pil_mod = types.ModuleType("PIL")
    pil_image_mod = types.ModuleType("PIL.Image")
    pil_draw_mod = types.ModuleType("PIL.ImageDraw")
    pil_font_mod = types.ModuleType("PIL.ImageFont")

    class _PilImage:
        pass

    class _Draw:
        pass

    class _Font:
        pass

    pil_image_mod.Image = _PilImage
    pil_draw_mod.ImageDraw = _Draw
    pil_font_mod.FreeTypeFont = _Font

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = astrbot_api_mod
    sys.modules["astrbot.api.event"] = astrbot_api_event_mod
    sys.modules["astrbot.api.star"] = astrbot_api_star_mod
    sys.modules["astrbot.core.config.astrbot_config"] = astrbot_core_config_mod
    sys.modules["astrbot.core.message.components"] = astrbot_message_components_mod
    sys.modules["astrbot.core.message.message_event_result"] = (
        astrbot_message_result_mod
    )
    sys.modules["astrbot.core.platform.message_session"] = astrbot_message_session_mod
    sys.modules["astrbot.core.platform.astr_message_event"] = astrbot_event_mod
    sys.modules["astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"] = (
        astrbot_aiocq_mod
    )
    sys.modules["PIL"] = pil_mod
    sys.modules["PIL.Image"] = pil_image_mod
    sys.modules["PIL.ImageDraw"] = pil_draw_mod
    sys.modules["PIL.ImageFont"] = pil_font_mod


def _load_module():
    _install_stub_modules()
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    package = types.ModuleType("steam_subscription_tests")
    package.__path__ = [str(PLUGIN_PATH.parent)]
    sys.modules["steam_subscription_tests"] = package
    command = types.ModuleType("astrbot.core.star.filter.command")
    command.GreedyStr = str
    sys.modules["astrbot.core.star.filter.command"] = command
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module
