# CC98 AI 优化项目后端接口说明

后端只负责产品账号、订阅新帖提醒、通知渠道、通知历史、定时扫描和健康状态。AI 搜索使用用户本机的 CC98 Token 与 LLM API Key，不经过本后端。

除健康检查外，用户接口都需要 `Authorization: Bearer <access_token>`；用户身份只取自 JWT，不接受请求参数中的 `user_id`。管理接口需要 `X-Admin-Token`。

## 健康检查

- `GET /api/v1/health`
- `GET /api/v1/admin/health`

普通健康检查会返回扫描间隔、订阅数量上限、表达式长度上限和通知列表限频。管理健康检查还会返回 worker、CC98 服务账号和扫描游标状态。

## 产品账号登录

- `POST /api/v1/auth/request-code`

```json
{ "email": "student@zju.edu.cn" }
```

- `POST /api/v1/auth/verify-code`

```json
{ "email": "student@zju.edu.cn", "code": "123456" }
```

开发环境设置 `AUTH_DEV_PRINT_CODE=true` 时，请求验证码会返回 `dev_code`。

## 订阅管理

- `POST /api/v1/subscriptions`
- `GET /api/v1/subscriptions`
- `PATCH /api/v1/subscriptions/{id}`
- `DELETE /api/v1/subscriptions/{id}`

创建订阅：

```json
{ "expression": "C++ 后端/服务端 实习" }
```

修改表达式或状态：

```json
{ "expression": "C++ 后端/服务端 校招", "status": "paused" }
```

表达式规则：

- 空白字符表示 AND，半角 `/` 表示同义词 OR；只有这两类字符具有语法含义。
- 其他连续字符按字面量整体匹配，因此 `C++`、`C#`、`.NET`、`Node.js` 都是合法关键词。
- 每个关键词至少 2 个字符，并且至少包含一个字母、数字或中文字符。
- `/` 两侧必须有关键词，不接受全角 `／`。
- 规范化后的表达式最多 255 个字符，英文匹配不区分大小写。
- 每个用户最多存在 10 条订阅；启用和暂停都会计数，删除后才释放名额。
- 完全相同的规范化表达式不能重复创建。

## 通知渠道

- `GET /api/v1/notification-channels`
- `PUT /api/v1/notification-channels`
- `PATCH /api/v1/notification-channels/{provider}`
- `POST /api/v1/notification-channels/test`

保存 DingTalk：

```json
{
  "provider": "dingtalk",
  "enabled": true,
  "notify_interval_minutes": 60,
  "config": {
    "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
    "secret": "SECxxx"
  }
}
```

保存邮箱：

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

首次保存某个渠道时必须提供完整 `config`。之后使用 PUT 修改配置时，`config` 按字段合并；未提交的字段会保留，因此前端不要把响应中的 `***` 当作真实密钥回传。只修改启用状态应调用独立的 PATCH 接口并立即生效：

```json
{ "enabled": false }
```

测试接口只使用请求中的临时 `provider` 和 `config` 发送测试消息，不保存配置，也不覆盖正式投递状态。

渠道响应中的正式投递状态字段为：

```text
last_attempted_at
last_sent_at
last_dispatch_status
last_dispatch_error
```

外部提醒采用至多一次语义：到达提醒时间后先出队，再对所有启用渠道各尝试一次；失败不自动补发。通知频率不会快于扫描频率。

## 通知历史

- `GET /api/v1/notifications`

接口按 JWT 用户筛选并返回最近 100 条。每个用户 60 秒内最多成功读取一次，超限返回 `429` 和 `Retry-After`。

响应只包含帖子和匹配历史：

```json
{
  "id": 1,
  "topic_id": "123",
  "topic_title": "帖子标题",
  "topic_url": "https://www.cc98.org/topic/123",
  "matched_reason": "命中表达式：C++ + 实习",
  "created_at": "2026-09-03T12:00:00Z"
}
```

后端不保存 `is_read`。未读徽章和上次查看位置由前端在本机维护。

## Watch 新帖扫描

- `POST /api/v1/tasks/scan`

该接口需要管理员密钥。扫描会分页读取全站新帖、按 `topic_id` 去重、按订阅 ID 升序匹配，并以 `(user_id, topic_id)` 保证通知唯一。达到用户提醒时间后，待处理通知会按批次交给所有启用渠道各尝试一次。

重要环境变量：

- `CC98_SERVICE_USERNAME`
- `CC98_SERVICE_PASSWORD`
- `CC98_SERVICE_REFRESH_TOKEN`
- `WATCH_FORCE_MOCK_TOPICS`
- `MATCHER_FORCE_RULES`
- `ENABLE_SCHEDULER`
- `SUBSCRIPTION_LIMIT`
- `NOTIFICATION_READ_RATE_LIMIT_SECONDS`
