# Steam Update Push (AstrBot Plugin)

### Steam 游戏/创意工坊更新推送（Steam News API）
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-v4.12%2B-brightgreen)
![License](https://img.shields.io/badge/License-GPL--3.0-orange)

![views](https://count.getloli.com/get/@astrbotchuanhuatong?theme=booru-jaypee)

---

## ✅ 简介 | Introduction

这是一个为 **AstrBot** 编写的插件：轮询游戏/创意工坊更新并推送（支持多 AppID、创意工坊ID、卡片或文本）

[![Release](https://img.shields.io/github/v/release/DearCrazyLeaf/astrbot_plugin_steam_updates?include_prereleases&color=blueviolet&label=最新版本)](https://github.com/DearCrazyLeaf/astrbot_plugin_steam_updates/releases/latest)
[![License](https://img.shields.io/badge/许可证-GPL%203.0-orange)](https://www.gnu.org/licenses/gpl-3.0.txt)
[![Issues](https://img.shields.io/github/issues/DearCrazyLeaf/astrbot_plugin_steam_updates?color=darkgreen&label=反馈)](https://github.com/DearCrazyLeaf/astrbot_plugin_steam_updates/issues)
[![Pull Requests](https://img.shields.io/github/issues-pr/DearCrazyLeaf/astrbot_plugin_steam_updates?color=blue&label=请求)](https://github.com/DearCrazyLeaf/astrbot_plugin_steam_updates/pulls)
[![GitHub Stars](https://img.shields.io/github/stars/DearCrazyLeaf/astrbot_plugin_steam_updates?color=yellow&label=标星)](https://github.com/DearCrazyLeaf/astrbot_plugin_steam_updates/stargazers)

> [!IMPORTANT]
> 本插件优先使用 **Steam News API** 获取游戏更新日志；当 API 失败或返回为空时，可启用 Steam Feed 自动回退

---

## ✅ 功能列表 | Features

- **被动推送**：自动轮询 Steam 更新日志
- **多游戏**：支持多个 AppID 统一推送
- **多平台推送**：支持使用 UMO 配置多个平台、多个同类型适配器实例、群聊、私聊和其他会话
- **手动查询**：群内指令触发即时查询
- **LLM整理**：可选用大模型同时翻译公告标题并整理正文，不增加额外模型请求
- **逐条公告配图**：优先显示每条公告正文中的首张有效图片，失败时自动尝试后续候选并回退游戏头图
- **卡片/文本**：两种输出模式可选
- **无更新静默**：当天无更新不推送
- **限时免费领取活动**：支持将当前仍可领取的 Steam 游戏作为独立分区“限时免费领取”并入游戏更新推送
- **创意工坊订阅监控**：支持轮询 Workshop PublishedFileID，发现更新时间变化后推送，支持查询非公开创意工坊内容

<img width="600" alt="free_games_preview" src="docs/images/free_games_preview.png" />

<img width="900" height="1184" alt="preview" src="https://github.com/user-attachments/assets/59a296a5-23f8-428f-9a32-72e38f64289c" />

<img width="900" height="847" alt="workshop_public_3240880604_v4_spacing" src="https://github.com/user-attachments/assets/74d16dc0-f15e-41e8-9d59-50b25dca6231" />

---

## 📦 安装 | Installation

将插件目录放入：
```
AstrBot/data/plugins/astrbot_plugin_steam_updates
```

重启 AstrBot 后即可在 WebUI 中看到插件

---

## ⚙️ 配置 | Configuration

插件使用 `_conf_schema.json` 定义配置，入口：
```
AstrBot WebUI -> 插件 -> 插件配置
```

### 🔧 核心配置

| 配置项 | 说明 |
|--------|------|
| enable_push | 是否启用插件 |
| steam_web_api_key | Steam Web API Key（可选） |
| enable_feed_fallback | 是否启用 Steam Feed 回退 |
| feed_timeout_sec | Feed 回退超时秒数 |
| free_games_enable | 是否启用限时免费领取活动 |
| free_games_manual_only_when_no_news | 手动查询在没有普通游戏更新时，是否仅返回“限时免费领取”分区 |
| display_timezone | 免费领取截止时间显示时区；留空跟随容器系统时区，填写 IANA 时区名时按 UTC 活动源时间转换显示 |
| proxy_mode | 代理模式：`off` / `system` / `custom` |
| proxy_url | 自定义代理地址（仅 `custom` 生效） |
| steam_appids | AppID 列表（如 `730`） |
| workshop_enable | 是否启用创意工坊订阅监控 |
| workshop_item_ids | 创意工坊订阅ID列表（PublishedFileID） |
| workshop_api_base | 创意工坊API基础地址 |
| workshop_timeout_sec | 创意工坊请求超时秒数 |
| workshop_push_on_first_seen | 首次发现时是否立即推送（默认仅记录基线） |
| steam_lang | 语言（如 `schinese` / `english`） |
| poll_interval_sec | 轮询间隔（秒，从起始时间开始计时） |
| poll_start_time | 轮询起始时间（HH:MM） |
| notify_umos | 完整 UMO 列表；推荐字段，支持多个平台实例与全部 AstrBot 合法消息类型 |
| notify_group_ids | 推送群号列表 |
| platform_id | 平台 ID（可选，如 chatbot2） |
| message_mode | `card` 或 `text` |
| manual_query_game_command | 游戏更新手动查询指令（可配置多个） |
| manual_query_workshop_command | 创意工坊手动查询指令（可配置多个） |
| content_process_mode | 内容处理方式：`plugin` / `llm` |
| llm_provider_id | LLM 提供商ID（可选，WebUI下拉选择） |
| llm_timeout_sec | LLM 请求超时秒数（默认 `20`） |
| llm_prompt | LLM 提示词（仅 llm 模式生效） |
| max_days | 每个游戏最多展示最近 N 天更新 |
| content_max_chars | 单游戏正文最大字符数 |
| image_max_per_item | 每条更新最多渲染图片数（默认 1） |
| image_max_height | 图片最大高度 |
| enable_app_headers | 是否启用游戏头图渲染 |
| image_download_timeout_sec | 内容图片下载超时秒数 |
| header_download_timeout_sec | 游戏头图下载超时秒数 |
| prefetch_image_concurrency | 内容图片预取并发数 |
| prefetch_header_concurrency | 头图预取并发数 |
| failed_download_cooldown_sec | 下载失败冷却时间（秒） |
| debug_log | 调试日志（仅控制台输出） |

### 🧪 调试日志 | Debug Logs

- 打开 `debug_log` 后，插件会输出结构化日志到 AstrBot 控制台
- 主要阶段：`poll`、`fetch`、`fetch_api`、`fetch_feed`、`manual`、`manual_cmd`、`send`、`push`、`ping`
- 不会生成额外日志文件，便于在线排查

### 🌐 代理说明 | Proxy

- `proxy_mode=system`：读取 AstrBot 进程环境变量（如 `HTTP_PROXY` / `HTTPS_PROXY`）
- `proxy_mode=custom`：使用 `proxy_url` 强制指定代理（推荐，最可控）
- `proxy_mode=off`：不使用代理，直连请求

建议：
- 若创意工坊查询出现 `ConnectTimeout/ReadTimeout`，优先改为 `custom` 并设置 `proxy_url`（例如 `http://127.0.0.1:7890`）
- 创意工坊不仅依赖 `api.steampowered.com`，还依赖 `steamcommunity.com`
- 仅代理 API 而不代理社区域名时，常见现象是：游戏更新可用、创意工坊失败

---

## 🗂️ AppID 名称映射 | AppID Name Map

插件根目录内的 `appid_map.json` 用于缓存和手动维护 AppID 对应名称（多语言）：

- 优先读取该文件中的名称
- 若该 AppID 语言缺失，会自动通过 Steam API 获取并**写回同一条记录**
- 不会覆盖已有语言条目（除非你手动修改）

示例：
```json
{
  "730": {
    "schinese": "反恐精英2",
    "english": "Counter-Strike 2",
    "japanese": "カウンターストライク 2"
  }
}
```

---

## 🧭 网络代理建议 | Network Proxy (Clash)

部分地区访问 Steam 相关服务可能不稳定，推荐使用 Clash **混合模式**并代理下列域名：

```yaml
mixin:
  mode: rule

  proxy-groups:
    - name: STEAM-UPDATES
      type: select
      proxies:
        - HK   # ← 改成你的节点
        - DIRECT

  rules:
    # Steam 更新插件核心域名（建议代理）
    - DOMAIN,api.steampowered.com,STEAM-UPDATES
    - DOMAIN,store.steampowered.com,STEAM-UPDATES
    - DOMAIN-SUFFIX,steamcommunity.com,STEAM-UPDATES
    - DOMAIN-SUFFIX,steampowered.com,STEAM-UPDATES

    # 资源域名（建议同样走代理，减少超时）
    - DOMAIN-SUFFIX,steamstatic.com,STEAM-UPDATES
    - DOMAIN-SUFFIX,steamusercontent.com,STEAM-UPDATES
    - DOMAIN-SUFFIX,akamaihd.net,STEAM-UPDATES

    # 其余流量直连
    - MATCH,DIRECT
```

---

## 📌 使用方法 | Usage

### 📣 自动推送
开启 enable_push 后，插件会自动轮询并向 notify_umos 与旧 notify_group_ids 的合并目标发送更新。新字段先处理，旧字段随后处理；规范 UMO 相同的目标只发送一次。
若同时开启 `workshop_enable` 并配置 `workshop_item_ids`，会在同一轮询中检测创意工坊条目更新时间并合并推送
游戏公告会按正文顺序提取图片候选并显示首张可用图片；图片保持原始比例，不裁剪、不拉伸，窄图保持原始尺寸并水平居中，全部候选失败时回退游戏头图
使用 `llm` 内容处理模式时，同一次模型请求会同时返回中文公告标题和整理后的正文；解析器兼容空行、Markdown 围栏、同行标记及全角/半角冒号，并阻止协议标记进入最终卡片或文本
若开启 `free_games_enable`，新的免费领取活动会在首次发现时主动推送一次；活动仍在领取期内时，只要同一轮存在普通游戏更新推送，就会作为独立分区“限时免费领取”附在游戏更新后面
免费领取活动正文现在仅保留“截止时间”和“原价”
免费领取活动在文本模式和卡片模式下均不会额外显示发布时间与链接
即使某一轮免费领取活动正文仍混入旧版“领取方式”或“活动链接”，渲染阶段也会自动裁剪，只保留“截止时间”和“原价”
`display_timezone` 留空时跟随容器系统时区；填写 IANA 时区名时，活动源时间会先按 UTC 解释，再转换为目标时区显示截止时间
免费领取活动数据来自 GamerPower 公开接口：`https://www.gamerpower.com/api/giveaways?platform=steam&type=game`
该功能已经经过线上轮询与手动查询验证，可通过 `free_games_enable` 独立开启或关闭

### 🌐 UMO 推送目标

UMO 格式为 platform_id:message_type:session_id。可在目标会话发送 /sid 获取完整值。

当前 AstrBot 常见消息类型包括 GroupMessage、FriendMessage 和 OtherMessage。插件使用 AstrBot 的 MessageSession 校验 UMO，因此接受当前运行版本认可的全部合法 UMO，后续版本增加的合法消息类型也会随运行版本生效。

多个同类型适配器必须填写各自的平台实例 ID，例如：

    qq-account-1:GroupMessage:123456
    qq-account-2:GroupMessage:123456
    telegram-main:FriendMessage:987654
    lark-main:OtherMessage:chat:thread:42

notify_umos 与 notify_group_ids 可以同时使用。notify_umos 中的目标优先；新旧目标规范化为 UMO 后保序去重。旧字段中的纯群号使用 platform_id 转换为 GroupMessage UMO，旧字段中已有的完整 UMO 继续按完整 UMO 解析。

主动消息能否发送取决于对应平台适配器的能力。AstrBot 的 QQ 官方 API 适配器不支持该主动发送接口。单个目标失败时，其余目标仍会继续发送。卡片图片失败时，文本只补发给该图片失败目标。全部目标均发送失败时，插件保留本轮轮询状态，等待后续再次发送。

手动查询仍限群聊。当前事件的完整 UMO 命中配置，或者当前群号命中旧 notify_group_ids 中的纯群号时，群内命令可以执行。

### 💬 手动查询
群内发送任一配置指令即可触发，例如：
```
STEAM更新
steam更新
cs2更新
```
当没有普通游戏更新但存在仍可领取的免费游戏时，`free_games_manual_only_when_no_news` 默认为开启，此时仅返回“限时免费领取”分区
如果将 `free_games_manual_only_when_no_news` 设为关闭，当天没有普通游戏更新时，命令会回退展示较早的游戏更新，并在该回退结果后附上“限时免费领取”分区
如果未配置游戏 AppID 但仍启用 `free_games_enable`，手动查询会继续返回当前有效的“限时免费领取”分区
免费领取活动标题优先显示接口原始名称；如果可解析到 Steam 官方中文名，则显示为 `原始标题（官方中文名）`

### 🛰️ 平台捕获
```
steam_update_ping
```
steam_update_ping 只为旧纯群号兼容模式捕获平台实例 ID 和 aiocqhttp bot。完整 UMO 使用自身的平台实例 ID，无需依赖该命令。

---

## 🖼️ 输出模式 | Render Modes

- `card`：图片卡片（推荐）
- `text`：纯文本输出

---

## 📜 License

GPL-3.0

---

> Made for AstrBot ❤️
