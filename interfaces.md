# CC98 AI 优化项目后端接口说明

后端职责：产品账号、Watch 订阅、通知渠道、通知历史、CC98 服务账号新帖扫描、匹配 worker 和健康状态。

后端现在只保留“订阅新帖提醒”。历史搜索/历史查阅功能已砍掉，`/api/research` 会返回 410。AI 搜索如果后续要做，建议放在浏览器插件里完成，使用用户自己的 CC98 Token 和 LLM API Key。

## 健康检查

- `GET /api/health`
- `GET /api/v1/health`
- `GET /api/v1/admin/health`

`/api/v1/admin/health` 会返回 worker 状态、CC98 服务账号状态和 Watch 扫描游标。

## 产品账号登录

产品账号和 CC98 账号是分开的。MVP 阶段使用浙大邮箱验证码登录。

- `POST /api/v1/auth/request-code`

```json
{ "email": "student@zju.edu.cn" }
```

- `POST /api/v1/auth/verify-code`

```json
{ "email": "student@zju.edu.cn", "code": "123456" }
```

开发环境下如果 `AUTH_DEV_PRINT_CODE=true`，接口会返回 `dev_code`，方便本地测试。当前网易邮箱只用于订阅帖子推送，不用于登录验证码。

## 订阅管理

- `POST /api/v1/subscriptions`
- `GET /api/v1/subscriptions?user_id=demo_user`
- `PATCH /api/v1/subscriptions/{id}`
- `DELETE /api/v1/subscriptions/{id}`

创建订阅：

```json
{
  "user_id": "demo_user",
  "name": "backend internship",
  "description": "posts about backend internship and hiring",
  "board_id": null
}
```

规则：

- `status` 只有 `enabled` 和 `paused`。
- 默认每个用户最多启用 10 个订阅。
- 为了兼容旧前端，旧字段 `topic` 也可以传，后端会映射成 `name`。
- 关键词匹配规则：空格分隔表示 AND，斜杠 `/` 表示同义词 OR。例如 `微积分/微甲/vjf 历年卷 资料` 表示必须命中 `历年卷` 和 `资料`，同时还要命中 `微积分`、`微甲`、`vjf` 之一。

旧接口兼容：

- `POST /api/subscribe`
- `GET /api/subscriptions`
- `GET /api/admin/subscriptions`
- `DELETE /api/subscribe/{id}`

## 通知渠道

- `GET /api/v1/notification-channels?user_id=demo_user`
- `PUT /api/v1/notification-channels`
- `POST /api/v1/notification-channels/test`

DingTalk 配置示例：

```json
{
  "user_id": "demo_user",
  "provider": "dingtalk",
  "enabled": true,
  "notify_interval_minutes": 60,
  "config": {
    "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
    "secret": "SECxxx"
  }
}
```

邮箱通知配置示例：

```json
{
  "user_id": "demo_user",
  "provider": "email",
  "enabled": true,
  "notify_interval_minutes": 60,
  "config": {
    "to": "student@zju.edu.cn",
    "subject_prefix": "CC98 订阅提醒"
  }
}
```

默认复用全局 SMTP 配置。网易邮箱配置示例：

```text
SMTP_HOST=smtp.163.com
SMTP_PORT=465
SMTP_USE_SSL=true
SMTP_USERNAME=你的网易邮箱
SMTP_PASSWORD=网易邮箱授权码
SMTP_FROM=你的网易邮箱
```

如果想给某个通知渠道单独指定 SMTP，也可以在 `config` 里补 `smtp_host`、`smtp_port`、`smtp_username`、`smtp_password`、`from`。

通知频率说明：

- 扫描频率由后端 `.env` 的 `SCAN_INTERVAL_MINUTES` 固定控制。
- `notify_interval_minutes` 是用户级统一频率；从任一渠道保存时更新，所有启用渠道在同一轮尝试。
- 后端会自动保证 `notify_interval_minutes >= SCAN_INTERVAL_MINUTES`。例如扫描每 10 分钟一次，用户传 1 分钟，实际返回和保存都是 10 分钟；用户传 60 分钟，则每小时聚合推送一次。
- 有启用渠道时，新通知以 `dispatch_pending=true` 进入一次性提醒队列；没有渠道时只进入前端历史，不会在以后启用渠道时补发。
- 到提醒时间后先提交 `dispatch_pending=false`，再对邮箱和钉钉各尝试一次。失败只写入渠道最近错误，不会跨轮自动重试。

旧接口兼容：

- `GET /api/notification-settings`
- `PUT /api/notification-settings`
- `POST /api/notification-settings/test`

## 通知历史

- `GET /api/v1/notifications?user_id=demo_user`
- `GET /api/notifications?user_id=demo_user`

通知去重规则是 `(user_id, topic_id)`；同一用户多条订阅匹配同一帖子时，只保留订阅 ID 最小的第一条匹配原因。接口先按当前认证用户筛选，再返回最近 100 条。同一用户 60 秒内重复成功读取会收到 `429` 和 `Retry-After`。

## Watch 新帖扫描

- `POST /api/v1/tasks/scan`
- `POST /api/tasks/scan`

扫描流程：

1. 读取旧高水位游标，并按每页 20 条分页拉取 `/topic/new`，直到遇见旧游标。
2. 页间按 `topic_id` 去重，对 403 做有限退避；达到最大页数仍找不到游标时标记 `cursor_gap` 且不推进游标。
3. 保存 CC98 帖子快照，按订阅 ID 升序匹配，同一 `(user_id, topic_id)` 第一条命中胜出。
4. 新通知按用户当前是否有启用渠道决定是否进入 `dispatch_pending` 队列。
5. 数据和通知可靠提交后，把第一页第一条帖子设为新游标。
6. 到用户统一提醒时间后先提交出队，再按每批最多 20 帖对所有启用渠道逐一尝试。
7. 单轮对相同 provider 和目的地去重；失败不重新入队，只更新渠道状态和日志。

重要环境变量：

- `CC98_SERVICE_USERNAME`
- `CC98_SERVICE_PASSWORD`
- `CC98_SERVICE_REFRESH_TOKEN`
- `WATCH_FORCE_MOCK_TOPICS`
- `MATCHER_FORCE_RULES`
- `ENABLE_SCHEDULER`
