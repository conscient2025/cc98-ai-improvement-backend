# 测试交接文档

这份文档给测试同学用，目标是快速验证后端 MVP 是否可演示、接口是否稳定。

## 测试范围

本后端负责：

- 产品账号邮箱验证码登录。
- Watch 订阅创建、查看、暂停、恢复、删除。
- CC98 新帖扫描，默认可用 mock 数据。
- 订阅和帖子匹配。
- 通知记录落库。
- DingTalk 通知渠道配置和测试。
- 邮箱通知渠道配置和测试。
- Worker 健康状态和扫描结果统计。

本后端暂不负责：

- 浏览器插件里的正式 AI 搜索。
- 历史搜索/历史查阅功能。
- 用户自己的 CC98 Token 管理。
- 用户自己的 LLM API Key 管理。

## 本地准备

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --port 8000 --reload
```

建议 `.env`：

```text
WATCH_FORCE_MOCK_TOPICS=true
MATCHER_FORCE_RULES=true
ENABLE_SCHEDULER=false
AUTH_DEV_PRINT_CODE=true
AUTH_EMAIL_DELIVERY=false
ADMIN_API_TOKEN=本地测试管理员密钥
SCAN_INTERVAL_MINUTES=10
```

如果测试真实 CC98 新帖扫描，需要配置公共 CC98 服务账号。扫描对象是 CC98 的“查看新帖”列表，即全站多个版块汇总出来的新帖，不需要配置版块 ID。

```text
CC98_SERVICE_USERNAME=公共 CC98 账号
CC98_SERVICE_PASSWORD=公共 CC98 密码
WATCH_FORCE_MOCK_TOPICS=false
```

如果要测试真实网易邮箱订阅推送，把 `.env` 改成：

```text
SMTP_HOST=smtp.163.com
SMTP_PORT=465
SMTP_USE_SSL=true
SMTP_USERNAME=你的网易邮箱
SMTP_PASSWORD=网易邮箱授权码
SMTP_FROM=你的网易邮箱
```

接口文档页面：

```text
http://127.0.0.1:8000/docs
```

## 自动化测试

```powershell
python -m compileall app tests
python -m pytest -q -p no:cacheprovider --basetemp .test-tmp
```

当前预期：

```text
12 passed
```

如果出现 FastAPI `on_event` deprecation warning，可以忽略，不影响功能。

## 手工测试用例

### 1. 健康检查

```http
GET /api/v1/health
```

预期：

- HTTP 200。
- `status=ok`。
- `components.database=ok`。

### 2. 邮箱验证码登录

请求验证码：

```http
POST /api/v1/auth/request-code
Content-Type: application/json
```

```json
{ "email": "student@zju.edu.cn" }
```

预期：

- HTTP 200。
- 开发环境返回 `dev_code`。
- 如果要测试真实邮件，把 `AUTH_EMAIL_DELIVERY=true` 并配置 SMTP；生产环境应设置 `AUTH_DEV_PRINT_CODE=false`。

验证：

```http
POST /api/v1/auth/verify-code
Content-Type: application/json
```

```json
{ "email": "student@zju.edu.cn", "code": "上一步返回的 dev_code" }
```

预期：

- HTTP 200。
- 返回 `access_token`。
- 返回用户信息。
- 后续用户接口都要带 `Authorization: Bearer <access_token>`。

反例：

- 非浙大邮箱应返回 400。
- 错误验证码应返回 400。

### 3. 创建订阅

```http
POST /api/v1/subscriptions
Content-Type: application/json
Authorization: Bearer <access_token>
```

```json
{
  "user_id": "demo_user",
  "name": "CC98 AI",
  "description": "search and watch notification",
  "board_id": null
}
```

预期：

- HTTP 200。
- `status=enabled`。
- `active=true`。
- 返回里的 `user_id` 应是 token 对应的用户 ID，不是请求体里的 `demo_user`。

关键词规则：

- 空格分隔表示 AND：`计算机学院 保研` 需要两个词都命中。
- 斜杠 `/` 表示同义词 OR：`微积分/微甲/vjf 历年卷 资料` 表示必须命中 `历年卷` 和 `资料`，同时还要命中 `微积分`、`微甲`、`vjf` 之一。

### 4. 订阅列表

```http
GET /api/v1/subscriptions
Authorization: Bearer <access_token>
```

预期：

- 返回数组。
- 包含刚才创建的订阅。

### 5. 暂停和恢复订阅

暂停：

```http
PATCH /api/v1/subscriptions/{id}
Content-Type: application/json
Authorization: Bearer <access_token>
```

```json
{ "status": "paused" }
```

预期：`active=false`。

恢复：

```json
{ "status": "enabled" }
```

预期：`active=true`。

### 6. 订阅数量限制

`.env` 默认：

```text
SUBSCRIPTION_LIMIT=10
```

同一个 `user_id` 创建超过 10 个启用订阅时，预期返回 400。

注意：现在实际按 token 对应的用户统计，不按请求体里的 `user_id` 统计。

### 7. 手动扫描新帖

```http
POST /api/v1/tasks/scan
X-Admin-Token: <ADMIN_API_TOKEN>
```

mock 模式预期：

- HTTP 200。
- `scanned_subscriptions >= 1`。
- `fetched_topics >= 1`。
- 首次扫描 `created_notifications >= 1`。
- 第二次重复扫描 `created_notifications=0`，因为通知有唯一约束。
- 如果本次有多个新通知，DingTalk/邮箱应收到一条聚合消息，而不是多条刷屏消息。

### 8. 通知历史

```http
GET /api/v1/notifications
Authorization: Bearer <access_token>
```

预期：

- 返回通知数组。
- 每条通知有 `topic_title`、`topic_url`、`matched_reason`。
- 未配置通知渠道时 `delivery_status=skipped`。
- `matched_reason` 应显示命中的搜索表达式，例如 `命中搜索表达式：微甲 + 历年卷 + 资料`。

### 9. DingTalk 通知渠道

保存配置：

```http
PUT /api/v1/notification-channels
Content-Type: application/json
Authorization: Bearer <access_token>
```

```json
{
  "user_id": "demo_user",
  "provider": "dingtalk",
  "enabled": true,
  "notify_interval_minutes": 60,
  "config": {
    "webhook": "真实 DingTalk webhook",
    "secret": "真实 secret，可为空"
  }
}
```

测试：

```http
POST /api/v1/notification-channels/test
Authorization: Bearer <access_token>
```

预期：

- 配置正确：HTTP 200。
- 配置错误：HTTP 400，并返回错误原因。
- 获取配置时 secret 被隐藏为 `***`。
- 返回的 `notify_interval_minutes` 是实际生效值，不会小于 `SCAN_INTERVAL_MINUTES`。

### 10. 邮箱通知渠道

保存配置：

```http
PUT /api/v1/notification-channels
Content-Type: application/json
Authorization: Bearer <access_token>
```

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

测试：

```http
POST /api/v1/notification-channels/test
Authorization: Bearer <access_token>
```

预期：

- SMTP 配置正确：HTTP 200，收件邮箱收到测试邮件。
- SMTP 配置错误：HTTP 400，并返回错误原因。
- 扫描有多个新帖子时，只收到一封聚合邮件。

### 11. 通知频率

如果 `.env` 里：

```text
SCAN_INTERVAL_MINUTES=10
```

保存通知渠道时传：

```json
{
  "user_id": "demo_user",
  "provider": "dingtalk",
  "enabled": true,
  "notify_interval_minutes": 1,
  "config": {
    "webhook": "真实 DingTalk webhook",
    "secret": ""
  }
}
```

预期：

- 接口返回 `notify_interval_minutes=10`，因为通知不能比扫描更频繁。
- 如果用户设置 `notify_interval_minutes=60`，后端会先生成通知记录，但不到 60 分钟不会推送；到时间后把期间积累的匹配帖子聚合成一条消息。

### 12. 管理健康状态

```http
GET /api/v1/admin/health
X-Admin-Token: <ADMIN_API_TOKEN>
```

预期包含：

- `cc98_service_account`
- `workers`
- `cursor`
- `GET /api/v1/health` 的 `components.scan_interval_minutes` 可用于确认扫描间隔。

如果没有真实 CC98 服务账号，`cc98_service_account` 可能不是 ok，这是符合预期的。

## 旧接口回归

为了兼容旧前端，也请抽测：

- `POST /api/subscribe`
- `GET /api/subscriptions`
- `DELETE /api/subscribe/{id}`
- `GET /api/notifications`
- `GET /api/notification-settings`
- `PUT /api/notification-settings`
- `POST /api/tasks/scan`

旧接口能用即可，新功能优先按 `/api/v1/*` 验证。

旧接口也已经接入鉴权：用户相关旧接口要带 `Authorization`，`POST /api/tasks/scan` 要带 `X-Admin-Token`。

## 风险点

- 真实 CC98 新帖抓取依赖 `CC98_SERVICE_USERNAME` / `CC98_SERVICE_PASSWORD` 或 refresh token；扫描的是 CC98 全站新帖列表，不要求订阅带 `board_id`。
- 真实通知依赖 DingTalk webhook 是否可用。
- 现在匹配默认是规则匹配，`MATCHER_FORCE_RULES=false` 后才会尝试 LLM。
- 生产环境不要开启 `WATCH_FORCE_MOCK_TOPICS`，真实 CC98 抓取失败时应直接暴露失败，不能回退 mock。
- 产品登录验证码现在可以通过 SMTP 发送；本地开发可关闭 `AUTH_EMAIL_DELIVERY` 并用 `dev_code` 测试。
