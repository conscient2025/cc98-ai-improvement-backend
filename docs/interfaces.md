# CC98 AI+ 后端接口说明

> 状态：当前实现；最后核对：2026-09-04；API 版本：v1

后端负责产品账号、订阅新帖提醒、通知渠道、通知历史、定时扫描和健康状态。AI 搜索使用用户本机的 CC98 Token 与 LLM API Key，不经过本后端。

## 1. 通用约定

当前业务接口统一位于：

```text
<backend-base>/api/v1
```

请求和响应使用 JSON，时间字段为 ISO 8601 UTC 时间。

### 1.1 鉴权

| 接口 | 鉴权 |
| --- | --- |
| `GET /api/v1/health` | 无 |
| `POST /api/v1/auth/request-code` | 无 |
| `POST /api/v1/auth/verify-code` | 无 |
| 订阅、通知和通知渠道 | `Authorization: Bearer <access_token>` |
| 扫描与管理健康接口 | `X-Admin-Token: <ADMIN_API_TOKEN>`，也接受 Bearer 形式 |

用户身份只取自 access token，不接受请求参数中的 `user_id`。当前 access token 是自定义 HMAC 签名令牌，不是标准 JWT，客户端应将它视为不透明字符串。

### 1.2 错误

一般错误格式：

```json
{ "detail": "错误说明" }
```

| 状态码 | 含义 |
| --- | --- |
| 400 | 业务校验失败、验证码错误、重复订阅或渠道测试失败 |
| 401 | 用户或管理员令牌缺失、无效或过期 |
| 403 | 操作其他用户的资源 |
| 404 | 资源不存在 |
| 410 | 历史接口已移除 |
| 422 | 请求结构或枚举值不合法 |
| 429 | 验证码尝试过多或通知列表读取过频 |
| 503 | 必要配置缺失或验证码无法投递 |

Pydantic 请求校验错误的 `detail` 为数组。

## 2. 健康检查

### `GET /api/v1/health`

公开的进程级健康和前端策略接口。

```json
{
  "status": "ok",
  "components": {
    "database": "ok",
    "scheduler_enabled": true,
    "scan_interval_minutes": 10,
    "subscription_limit": 10,
    "subscription_expression_max_length": 255,
    "notification_read_rate_limit_seconds": 60,
    "cc98_mode": "service_account"
  }
}
```

## 3. 产品账号

### `POST /api/v1/auth/request-code`

请求：

```json
{ "email": "student@zju.edu.cn" }
```

响应：

```json
{
  "status": "ok",
  "email": "student@zju.edu.cn",
  "dev_code": null
}
```

`dev_code` 仅在 `AUTH_DEV_PRINT_CODE=true` 时返回实际验证码，生产环境必须为 `null`。邮箱格式或域名不允许时返回 400；无法投递且不能回退开发模式时返回 503。

### `POST /api/v1/auth/verify-code`

请求：

```json
{ "email": "student@zju.edu.cn", "code": "123456" }
```

响应：

```json
{
  "access_token": "opaque-signed-token",
  "token_type": "bearer",
  "user": {
    "id": "usr_example",
    "email": "student@zju.edu.cn",
    "email_verified_at": "2026-09-04T12:00:00Z",
    "status": "active",
    "created_at": "2026-09-04T12:00:00Z"
  }
}
```

验证码不存在、过期或错误时返回 400；同一验证码记录累计尝试超过 5 次时返回 429。

## 4. 订阅

- `POST /api/v1/subscriptions`
- `GET /api/v1/subscriptions`
- `PATCH /api/v1/subscriptions/{subscription_id}`
- `DELETE /api/v1/subscriptions/{subscription_id}`

创建请求：

```json
{ "expression": "C++ 后端/服务端 实习" }
```

创建或列表中的订阅结构：

```json
{
  "id": 1,
  "expression": "C++ 后端/服务端 实习",
  "status": "enabled",
  "created_at": "2026-09-04T12:00:00Z",
  "updated_at": "2026-09-04T12:00:00Z"
}
```

修改请求可只提交一个字段：

```json
{
  "expression": "C++ 后端/服务端 校招",
  "status": "paused"
}
```

删除响应：

```json
{ "status": "ok", "deleted": 1 }
```

规则：

- 状态只能是 `enabled` 或 `paused`；
- 空白表示 AND，半角 `/` 表示 OR，其他字符按字面量匹配；
- 每个关键词至少 2 个字符，且至少包含字母、数字或中文；
- 规范化后最多 255 个字符，英文匹配不区分大小写；
- 同一用户不能有重复的规范化表达式；
- 暂停订阅也计入数量上限，删除后才释放名额；
- 操作不存在的订阅返回 404，操作其他用户的订阅返回 403；
- 删除订阅不会删除已生成的通知历史。

## 5. 通知渠道

当前 provider 只支持 `dingtalk` 和 `email`，两个渠道共享用户提醒间隔。

### `GET /api/v1/notification-channels`

返回用户已经保存的渠道；未保存的 provider 不返回占位记录。

```json
{
  "id": 1,
  "provider": "dingtalk",
  "enabled": true,
  "config": {
    "webhook": "https://oapi.dingtalk.com/ro...",
    "secret": "***"
  },
  "has_secret": true,
  "notify_interval_minutes": 60,
  "last_attempted_at": null,
  "last_sent_at": null,
  "last_dispatch_status": null,
  "last_dispatch_error": null,
  "created_at": "2026-09-04T12:00:00Z",
  "updated_at": "2026-09-04T12:00:00Z"
}
```

Webhook 只显示前缀；`secret`、`token`、`password` 和 `smtp_password` 返回 `***`。

### `PUT /api/v1/notification-channels`

创建或更新渠道：

```json
{
  "provider": "dingtalk",
  "enabled": true,
  "notify_interval_minutes": 60,
  "config": {
    "webhook": "https://oapi.dingtalk.com/robot/send?access_token=...",
    "secret": "SEC..."
  }
}
```

邮箱配置：

```json
{
  "provider": "email",
  "enabled": true,
  "notify_interval_minutes": 60,
  "config": {
    "to": "student@zju.edu.cn",
    "subject_prefix": "CC98 订阅提醒"
  }
}
```

首次保存 provider 时必须提供 `config`。已有配置按字段合并，省略字段会保留；敏感字段值为 `***` 时不会覆盖原值。最终提醒间隔不会短于扫描间隔。

### `PATCH /api/v1/notification-channels/{provider}`

只修改已有渠道的启用状态：

```json
{ "enabled": false }
```

渠道不存在时返回 404。停用不会删除配置和历史，也不会在重新启用后补发旧消息。

### `POST /api/v1/notification-channels/test`

使用请求中的临时 `provider` 和 `config` 发送测试消息；不保存配置，也不更新正式投递状态。

```json
{
  "provider": "email",
  "config": {
    "to": "student@zju.edu.cn",
    "subject_prefix": "CC98 订阅提醒"
  }
}
```

成功返回 `{"status":"ok"}`，发送失败返回 400。

## 6. 通知历史

### `GET /api/v1/notifications`

返回当前用户最近 100 条匹配历史，按通知 ID 降序：

```json
[
  {
    "id": 10,
    "topic_id": "6609001",
    "topic_title": "帖子标题",
    "topic_url": "https://www.cc98.org/topic/6609001",
    "matched_reason": "命中表达式：C++ + 实习",
    "created_at": "2026-09-04T12:00:00Z"
  }
]
```

响应不包含 `is_read`、命中的订阅 ID 或外部渠道投递状态。默认每个用户 60 秒内最多成功读取一次，超限返回 429，并通过 `Retry-After` 告知剩余秒数。

## 7. 管理接口

管理接口不能由普通前端调用。

### `POST /api/v1/tasks/scan`

立即执行一次 Watch 扫描和到期通知分发。响应字段按用途分组如下：

| 类别 | 字段 |
| --- | --- |
| 扫描量 | `scanned_subscriptions`、`fetched_pages`、`fetched_topic_items`、`unique_topics_before_cursor`、`fetched_topics` |
| 匹配量 | `candidate_pairs`、`matched_user_topics`、`matched_pairs` |
| 通知入库 | `created_notifications`、`queued_notifications` |
| 外部投递 | `processed_notifications`、`sent_notifications`、`dispatch_batches`、`dispatch_attempts`、`dispatch_successes`、`dispatch_failures`、`deduplicated_destination_topics` |
| 游标与结果 | `cursor_found`、`cursor_gap`、`baseline_created`、`source`、`status` |

`status` 通常为 `ok` 或 `cursor_gap`。未捕获异常返回 500，并记录到 worker 状态。

### `GET /api/v1/admin/health`

返回以下管理状态：

- `zju_connect`：当前探测状态和说明；
- `cc98_service_account`：CC98 API 可达性、状态码及最近错误；
- `workers.watch_scan`：最近成功、失败、错误和指标；
- `cursor`：Watch 游标值及更新时间。

当前 CC98 探测只判断 API 是否可达，不能证明公共账号凭据或 refresh token 有效；`zju_connect` 当前可能返回 `unknown`。

## 8. 历史接口

`GET /api/research?query=...` 固定返回 410。旧的非 v1 订阅、通知、通知设置和扫描路径已经删除，通常返回 404。
