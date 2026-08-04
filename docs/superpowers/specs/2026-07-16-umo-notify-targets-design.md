# UMO 推送目标设计

## 背景

插件当前通过 `notify_group_ids` 保存群号，并使用单个 `platform_id`
将群号转换为 `平台实例 ID:GroupMessage:群号`。这一方式无法清晰表达以下目标：

1. 同一 AstrBot 实例中的多个 QQ 适配器。
2. Telegram、飞书等其他消息平台。
3. 私聊或其他消息类型。
4. 会话 ID 相同但平台实例不同的目标。

AstrBot 的统一消息来源 UMO 已经包含平台实例 ID、消息类型和会话 ID，
格式为 `platform_id:message_type:session_id`。主动消息接口
`Context.send_message()` 可以使用完整 UMO 选择目标平台实例和会话。

## 目标

本次修改提供以下能力：

1. 在插件配置界面增加 `notify_umos` 列表。
2. 接受当前 AstrBot 版本支持的全部合法 UMO 消息类型。
3. 支持多个平台和同类型的多个适配器实例。
4. 保留 `notify_group_ids` 与 `platform_id` 的既有行为。
5. 新旧目标合并后保序去重。
6. 单个目标失败时继续处理其他目标。
7. 图片发送失败时只向对应失败目标补发文本。
8. 上游 PR 只包含主线所需修改，线上独有代码保存在个人维护分支。

## 非目标

本次修改不包含以下内容：

1. 私聊和其他消息类型的手动查询命令。
2. 每个推送目标各自维护新闻去重状态。
3. 自动将旧群号写回为 UMO。
4. 自动推断多个同类型适配器中的目标实例。
5. 修改 Steam 新闻、创意工坊或免费领取活动的数据获取逻辑。
6. 将线上运行期 `appid_map.json`、备份文件或字节码文件提交到仓库。

## 上游基线与版本

上游仓库为 `DearCrazyLeaf/astrbot_plugin_steam_updates`，开发基点固定为：

    branch: master
    commit: 2181314208c43d5497f1f5238e1f182a0b220c87
    version: v1.2.9

本功能版本为 `v1.2.10`。

## 配置结构

`basic_settings.items` 增加以下字段：

    "notify_umos": {
      "description": "推送 UMO 列表",
      "type": "list",
      "default": [],
      "hint": "填写 /sid 返回的完整 UMO，可配置多个平台和适配器实例",
      "items": {
        "type": "string"
      }
    }

以下旧字段继续保留：

1. `notify_group_ids`：旧群号列表，也兼容历史上已经填写的完整 UMO。
2. `platform_id`：将旧群号转换为 GroupMessage UMO。

配置读取继续支持仓库现有的分组格式和平铺格式，优先级保持不变。
插件启动时不修改保存的配置。

## 目标数据模型

新增不可变目标对象 `NotifyTarget`：

    @dataclass(frozen=True)
    class NotifyTarget:
        umo: str
        platform_id: str
        legacy_group_id: str = ""

`umo` 是重新序列化后的规范 UMO。只有从旧纯群号转换得到的目标才设置
`legacy_group_id`，该字段仅用于 aiocqhttp 兼容发送。

发送函数返回汇总对象 `PushResult`：

    @dataclass
    class PushResult:
        succeeded: list[NotifyTarget]
        failed: list[NotifyTarget]

该对象用于判断文本补发、日志记录和本轮状态是否可以保存。

## UMO 校验

完整 UMO 使用当前 AstrBot 版本的 `MessageSession.from_str()` 解析。
解析成功后再次调用 `str(session)` 得到规范字符串。

校验规则由 AstrBot 自身提供：

1. 第一段是平台适配器实例 ID。
2. 第二段是当前版本支持的 `MessageType`。
3. 第三段是会话 ID。
4. 解析使用 `split(":", 2)`，会话 ID 内部可以继续包含冒号。

当前公开版本包含 `GroupMessage`、`FriendMessage` 和
`OtherMessage`。插件不建立独立的消息类型白名单，后续 AstrBot 增加合法类型时，
校验行为跟随运行版本。

非法 UMO 逐项跳过。日志只记录配置字段、目标序号和 UMO 摘要值，
不记录完整 UMO。

## 目标解析

新增统一解析函数，签名固定为
`_resolve_notify_targets(self) -> list[NotifyTarget]`。

处理顺序如下：

1. 读取 `notify_umos`。
2. 按列表顺序清理空白并解析完整 UMO。
3. 读取 `notify_group_ids`。
4. 旧字段中包含冒号的值按完整 UMO 解析。
5. 旧字段中的纯群号使用显式 `platform_id` 转换。
6. 显式 `platform_id` 为空时，使用 `steam_update_ping` 最近捕获的平台实例 ID。
7. 缺少平台实例 ID 的旧群号逐项跳过。
8. 以规范 UMO 为键保序去重。

`notify_umos` 先于旧字段处理。因此同一目标同时出现在新旧字段时，
保留新字段产生的目标，不附加旧 aiocqhttp 备用发送信息。

所有目标无效时，`_poll_once()` 在创建网络请求和读取 Steam 数据前返回，
并保留原有状态文件。

## 平台实例捕获

`steam_update_ping` 当前保存 `event.get_platform_name()`。该值表示适配器类型，
无法区分同类型的多个实例。

修改后保存：

    event.get_platform_id()

同时保存 aiocqhttp 事件对应的 bot 对象。捕获结果只服务旧群号兼容发送；
完整 UMO 始终依靠 UMO 第一段选择平台实例。

## 主动发送

现有 `_push_text()`、`_push_image()` 和 `_push_chain()` 的参数从群号列表
调整为 `NotifyTarget` 列表。每个目标单独调用：

    await self.context.send_message(
        session=target.umo,
        message_chain=chain,
    )

结果处理规则如下：

1. 返回 `True` 时目标成功。
2. 返回 `False` 时目标失败。
3. 抛出异常时记录脱敏告警并将目标标记为失败。
4. 任一目标失败都不终止剩余目标。

## aiocqhttp 兼容发送

aiocqhttp 的 `send_group_msg` 备用发送只允许同时满足以下条件的目标：

1. 目标来自旧纯群号。
2. `legacy_group_id` 可以转换为整数。
3. `steam_update_ping` 已经捕获 aiocqhttp bot。
4. 目标 UMO 的平台实例 ID 等于捕获的平台实例 ID。

完整 UMO、实例 ID 不匹配的旧目标以及非群消息目标均不使用该备用方式。
这一限制避免多个 QQ 账号之间使用错误 bot 发送。

## 图片和文本补发

卡片模式为每个目标分别记录发送结果：

1. 图片发送成功的目标结束本次发送。
2. 图片发送失败的目标使用相同内容生成文本消息并补发。
3. 文本补发只包含图片发送失败的目标。
4. 图片成功目标不会收到重复文本。
5. 图片渲染失败时，全部目标使用文本消息。

文本模式只执行一次文本发送。

## 状态保存

保持当前全局新闻状态模型，不增加目标级状态。

一轮轮询的状态保存规则为：

1. 至少一个目标成功时，保存本轮既有的新闻、创意工坊和免费活动状态。
2. 所有发送尝试均失败时，保留轮询开始前的状态，使后续轮询可以再次尝试。
3. 部分成功时保存状态，避免成功目标重复接收。
4. 部分失败的目标可能错过该次内容，日志明确记录失败数量和目标摘要。

该规则在兼容复杂度和重复消息之间选择现有全局状态模型。目标级重试需要独立设计，
不纳入 v1.2.10。

## 手动查询许可

手动查询继续只监听 `GROUP_MESSAGE`。

许可判断抽取为独立函数，并使用以下并集语义：

1. 当前事件完整 UMO 命中合法的完整 UMO 配置时允许。
2. 当前事件群号命中旧 `notify_group_ids` 中的纯群号时允许。
3. 旧字段中保存的完整 UMO 按第一条处理。
4. 非法 UMO 不进入许可集合。

查询结果继续通过当前事件返回。传给 LLM 处理的
`event.unified_msg_origin` 行为保持不变。

## 日志

完整 UMO 可能包含群号、用户 ID 或平台侧会话信息。新增日志采用以下字段：

1. `target_index`：配置处理后的序号。
2. `target_ref`：UMO SHA-256 的前 12 个十六进制字符。
3. `message_type`：解析后的消息类型。
4. `legacy`：是否来自旧纯群号。

日志不输出完整 UMO、完整会话 ID 或原始异常中可能包含的会话信息。

## 测试

新增 `tests/test_notify_targets.py`，沿用 `unittest`。

测试至少覆盖：

1. 三种现有消息类型。
2. 会话 ID 包含冒号。
3. UMO 缺段、消息类型非法和空值。
4. 新旧字段合并和顺序保持。
5. 重复目标消除。
6. 旧字段中的完整 UMO。
7. 纯群号通过显式平台实例 ID 转换。
8. `steam_update_ping` 捕获平台实例 ID。
9. 多个 QQ 实例使用不同平台实例 ID。
10. `Context.send_message` 返回真、返回假和抛出异常。
11. aiocqhttp 备用发送的允许和拒绝条件。
12. 图片失败目标的文本补发。
13. 全部失败与部分成功时的状态保存。
14. 完整 UMO和旧群号的手动查询许可。
15. 所有目标无效时不发起 Steam 请求。
16. 配置、README、CHANGELOG 和版本元数据一致。

验证命令包括：

    python3 -m unittest discover -s tests -v
    python3 -m py_compile main.py
    python3 -m json.tool _conf_schema.json
    git diff --check

## 文档和版本

README 增加以下内容：

1. UMO 的格式与用途。
2. 使用 `/sid` 获取完整 UMO。
3. 多平台和多适配器实例示例。
4. 新旧字段同时使用时的合并和去重规则。
5. 主动消息受平台能力限制。
6. `steam_update_ping` 只服务旧群号兼容。

CHANGELOG 增加 `v1.2.10` 条目。metadata 版本同步为 `v1.2.10`。

## 分支安排

### 上游 PR 分支

    branch: feat/umo-notify-targets
    base: DearCrazyLeaf/master@2181314
    version: v1.2.10

该分支只包含本设计要求的 UMO 功能、测试、配置和文档。

### 个人维护分支

    branch: local/steam-updates-v1.2.10
    base: DearCrazyLeaf/master@2181314
    version: v1.2.10

该分支按顺序保存：

1. PR 合并后的 GamerPower 空活动响应修正。
2. 线上遗留轮询锁文件描述符清理。
3. 与上游 PR 分支内容一致的 UMO 功能提交。

现有 `master`、`dev/free-games-giveaway` 和其他历史分支保持不变。

## 线上部署

线上插件只使用个人维护分支内容。部署步骤必须满足：

1. 保存当前插件目录和配置文件副本。
2. 保留当前 `notify_group_ids` 和 `platform_id` 值。
3. 新增 `notify_umos` 默认空列表。
4. 只重载 `astrbot_plugin_steam_updates`。
5. 不重启 AstrBot 容器。
6. 不重载其他插件。
7. 部署后核对插件版本、配置结构和旧目标发送行为。

## 验收条件

满足以下条件时，功能可以进入上游 PR：

1. 新字段接受 AstrBot 当前版本支持的全部合法 UMO。
2. 两个相同类型的 QQ 实例可以通过不同平台实例 ID 分别发送。
3. 旧群号配置无需修改即可继续运行。
4. 新旧字段中的重复会话只发送一次。
5. 非法目标不影响合法目标。
6. 所有目标无效时不会获取或消费更新状态。
7. 图片发送失败只对失败目标补发文本。
8. 全部发送失败时保留状态。
9. 手动群聊查询同时支持完整 UMO和旧群号许可。
10. 上游 PR 不包含线上独有修改和运行期文件。
11. 个人维护分支包含线上独有修改与 UMO 功能。
12. 完整测试、语法检查、配置检查和差异检查通过。
