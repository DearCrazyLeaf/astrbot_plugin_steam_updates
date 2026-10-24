"""Run separately from unit stubs inside an isolated real AstrBot image."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types

from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.filter.command import CommandFilter

root = Path(__file__).resolve().parents[1]
package = types.ModuleType("steam_core_smoke")
package.__path__ = [str(root)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("steam_core_smoke.main", root / "main.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

for name in ("steam_subscriptions", "steam_global_subscriptions"):
    command = CommandFilter("STEAM订阅")
    command.init_handler_md(
        types.SimpleNamespace(handler=getattr(module.SteamUpdatePush, name))
    )
    result = command.validate_and_convert_params(
        ["添加", "游戏", "730", "570"], command.handler_params
    )
    assert result == {"args": "添加 游戏 730 570"}, result
    assert command.validate_and_convert_params([], command.handler_params) == {
        "args": ""
    }

schema = json.loads((root / "_conf_schema.json").read_text())
with tempfile.TemporaryDirectory() as temp:
    path = str(Path(temp) / "config.json")
    config = AstrBotConfig(path, schema=schema)
    assert config["subscription_settings"]["sessions"] == []
    entry = {
        "__template_key": "session",
        "label": "测试会话",
        "umo": "qq:GroupMessage:123",
        "enabled": True,
        "steam_appids": ["730"],
        "workshop_item_ids": [],
        "free_games_mode": "off",
        "message_mode": "card",
    }
    config["subscription_settings"]["sessions"].append(entry)
    config.save_config()
    loaded = AstrBotConfig(path, schema=schema)
    saved = loaded["subscription_settings"]["sessions"][0]
    assert saved["umo"] == entry["umo"] and saved["steam_appids"] == ["730"]
print(
    "real AstrBot import, command argument parsing, template schema and config persistence: PASS"
)
