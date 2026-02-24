# 更新日志

本文件用于记录插件的主要变更。

## v1.2.2

### 新增
- 新增 WebUI 代理配置：
  - `proxy_mode`（`off` / `system` / `custom`）
  - `proxy_url`（当 `proxy_mode=custom` 时生效）
- 新增 SOCKS 代理支持依赖：`socksio`。

### 变更
- HTTP 客户端初始化改为根据代理配置动态创建，不再固定直连。
- 插件版本更新为 `v1.2.2`。
- README 增加代理配置与使用说明。

## v1.2.1

### 变更
- 优化轮询、取数、手动查询、发送链路的结构化日志。
- 新增 Steam Feed 回退相关配置：
  - `enable_feed_fallback`
  - `feed_timeout_sec`
