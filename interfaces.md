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
- 关键词匹配规则：空格分隔表示 AND，逗号/顿号/分号/换行分隔表示 OR。例如 `计算机学院 保研，软院 推免` 表示“同时命中计算机学院和保研”或“同时命中软院和推免”。

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
- 通知频率由通知渠道的 `notify_interval_minutes` 控制。
- 后端会自动保证 `notify_interval_minutes >= SCAN_INTERVAL_MINUTES`。例如扫描每 10 分钟一次，用户传 1 分钟，实际返回和保存都是 10 分钟；用户传 60 分钟，则每小时聚合推送一次。
- 未到通知时间的匹配帖子会先以 `delivery_status=pending` 保存在通知历史里，到时间后聚合发送。

旧接口兼容：

- `GET /api/notification-settings`
- `PUT /api/notification-settings`
- `POST /api/notification-settings/test`

## 通知历史

- `GET /api/v1/notifications?user_id=demo_user`
- `GET /api/notifications?user_id=demo_user`

通知去重规则是 `(user_id, subscription_id, topic_id)`，所以重复扫描不会给同一个用户、同一个订阅、同一个帖子重复生成提醒。

## Watch 新帖扫描

- `POST /api/v1/tasks/scan`
- `POST /api/tasks/scan`

扫描流程：

1. 读取所有启用中的订阅。
2. 使用 CC98 服务账号拉取“查看新帖”里的全站最新帖子；如果没有服务账号或开启 mock，则使用 mock 帖子。
3. 保存 CC98 帖子快照。
4. 判断帖子是否匹配订阅。
5. 按关键词组合命中规则生成唯一通知。
6. 按用户和通知渠道判断是否到达用户选择的通知时间；未到时间则保持 `pending`。
7. 到通知时间后，把待发送通知聚合成一条消息，再通过已启用的通知渠道发送。
8. 更新 worker 健康状态和扫描游标。

重要环境变量：

- `CC98_SERVICE_USERNAME`
- `CC98_SERVICE_PASSWORD`
- `CC98_SERVICE_REFRESH_TOKEN`
- `WATCH_FORCE_MOCK_TOPICS`
- `MATCHER_FORCE_RULES`
- `ENABLE_SCHEDULER`
