# 前端交接文档

## 结论

前端只调用 `/api/v1/*`。用户登录后，所有用户接口携带 `Authorization: Bearer <access_token>`；不要发送或依赖 `user_id`。完整接口示例见根目录 `interfaces.md`。

## 登录

1. `POST /api/v1/auth/request-code`，请求体 `{ "email": "...@zju.edu.cn" }`。
2. `POST /api/v1/auth/verify-code`，请求体 `{ "email": "...", "code": "123456" }`。
3. 保存响应中的 `access_token`。用户接口返回 401 时清除本地登录信息并引导重新登录。

## 订阅

订阅不再有名称、说明或板块字段，创建请求只有：

```json
{ "expression": "C++ 后端/服务端 实习" }
```

表达式语法：

- 空白表示 AND；半角 `/` 表示 OR。
- 其他连续字符全部是关键词的一部分。
- 每个关键词至少 2 个字符，并至少包含一个字母、数字或中文字符。
- `/` 两侧不能为空；全角 `／` 非法。
- 规范化后最长 255 个字符。

前端应实时解析并只保留两种状态：合法时展示解析预览并允许提交；非法时展示错误并禁用提交。后端仍会执行相同的最终校验，非法请求返回 400。

每个用户最多存在 10 条订阅，暂停也计数。列表展示 `订阅 n / 10`；达到上限后只禁用新增，暂停、恢复和删除仍可使用。

相关接口：

- `POST /api/v1/subscriptions`
- `GET /api/v1/subscriptions`
- `PATCH /api/v1/subscriptions/{id}`
- `DELETE /api/v1/subscriptions/{id}`

## 通知列表

`GET /api/v1/notifications` 返回当前用户最近 100 条匹配历史。响应不包含 `is_read`、逐通知投递状态或内部队列状态。

同一用户 60 秒内最多成功读取一次；429 响应带 `Retry-After`。前端应缓存最近成功结果、记录最后尝试与成功时间，并只在用户主动打开或刷新时请求。不要调用扫描接口，也不要做后台轮询。

本机未读徽章按“后端地址 + 用户 ID”保存 `lastSeenNotificationId`。第一次读取建立基线；用户实际打开通知页后更新位置。超过最近 100 条的可见范围时显示 `99+`。

## 通知渠道

支持 `dingtalk` 和 `email`：

- `GET /api/v1/notification-channels`
- `PUT /api/v1/notification-channels`
- `POST /api/v1/notification-channels/test`

页面只需要一个通知间隔选择器。测试接口使用当前表单里的临时配置发送消息，不会保存；保存必须调用 PUT。

渠道正式投递状态由以下字段提供：

```text
last_attempted_at
last_sent_at
last_dispatch_status
last_dispatch_error
```

发现 `last_dispatch_status=failed` 时，在通知页或设置页展示渠道级错误。失败批次不会自动补发，帖子仍可在通知历史查看。

## 健康检查

`GET /api/v1/health` 无需登录，可读取：

```text
scan_interval_minutes
subscription_limit
subscription_expression_max_length
notification_read_rate_limit_seconds
```

前端可用这些值覆盖本地兜底常量。

## 不要使用的旧接口

旧的 `/api/subscribe`、`/api/subscriptions`、`/api/notifications`、`/api/notification-settings*` 和 `/api/tasks/scan` 已删除，不再兼容旧插件。
