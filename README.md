# Steam Update Push (AstrBot Plugin)

### Steam 更新日志推送（Steam News API）
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-v4.12%2B-brightgreen)
![License](https://img.shields.io/badge/License-GPL--3.0-orange)

![views](https://count.getloli.com/get/@astrbotchuanhuatong?theme=booru-jaypee)

---

## ✅ 简介 | Introduction

这是一个为 **AstrBot** 编写的 Steam 更新推送插件，支持多 AppID 订阅、群推送、卡片/文本两种输出

> [!IMPORTANT]
> 本插件使用 **Steam News API** 获取更新日志，如需更高频或更稳定请求，可配置 Steam Web API Key

---

## ✅ 功能列表 | Features

- **被动推送**：自动轮询 Steam 更新日志
- **多游戏**：支持多个 AppID 统一推送
- **手动查询**：群内指令触发即时查询
- **LLM整理**：可选用大模型对更新内容进行翻译/总结/排版
- **卡片/文本**：两种输出模式可选
- **无更新静默**：当天无更新不推送

<img width="900" height="1184" alt="preview" src="https://github.com/user-attachments/assets/59a296a5-23f8-428f-9a32-72e38f64289c" />

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
| steam_appids | AppID 列表（如 `730`） |
| steam_lang | 语言（如 `schinese` / `english`） |
| poll_interval_sec | 轮询间隔（秒） |
| notify_group_ids | 推送群号列表 |
| platform_id | 平台 ID（可选，如 chatbot2） |
| message_mode | `card` 或 `text` |
| manual_query_command | 手动查询指令（可配置多个） |
| content_process_mode | 内容处理方式：`plugin` / `llm` |
| llm_provider_id | LLM 提供商ID（可选） |
| llm_prompt | LLM 提示词（仅 llm 模式生效） |
| max_items_per_app | 每个游戏最多展示最近 N 天更新 |
| content_max_chars | 单游戏正文最大字符数 |
| image_max_per_item | 每条更新最多渲染图片数 |
| image_max_height | 图片最大高度 |
| debug_log | 调试日志 |

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

部分地区访问 Steam API 可能不稳定，推荐使用 Clash **混合模式**并仅代理 Steam 相关域名：

```yaml
mixin:
  mode: rule

  proxy-groups:
    - name: STEAM-API
      type: select
      proxies:
        - HK   # ← 改成你的节点
        - DIRECT

  rules:
    # Steam API / News API
    - DOMAIN,api.steampowered.com,STEAM-API

    # 资源直连
    - DOMAIN-SUFFIX,steamstatic.com,DIRECT
    - DOMAIN-SUFFIX,akamaihd.net,DIRECT
    - DOMAIN-SUFFIX,steampowered.com,DIRECT

    # 其他全部直连
    - MATCH,DIRECT
```

---

## 📌 使用方法 | Usage

### 📣 自动推送
开启 `enable_push` 即启用插件，插件会自动轮询 Steam 更新日志并推送到配置的群

### 💬 手动查询
群内发送任一配置指令即可触发，例如：
```
STEAM更新
steam更新
cs2更新
```

### 🛰️ 平台捕获
```
steam_update_ping
```
用于捕获平台信息并测试推送通路

---

## 🖼️ 输出模式 | Render Modes

- `card`：图片卡片（推荐）
- `text`：纯文本输出

---

## 📜 License

GPL-3.0

---

> Made for AstrBot ❤️
